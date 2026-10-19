#!/usr/bin/env python3
"""PolyBot v2 — autonomous Polymarket PAPER trader.

Trades a fake $1,000 bankroll against live Polymarket order books.
No keys, no wallet, no real money. Strategies:
  1. negrisk_arb — buy every YES in a winner-take-all event when the asks
                   sum to < $1 after fees (locked profit if filled). On a
                   30-min cron this almost never wins the race vs pro bots;
                   kept as a free option, expected fills ~zero.
  2. favorite    — favorite-longshot bias, BOTH sides: buy YES on heavy
                   favorites and NO on overpriced longshots (96-99c all-in),
                   with an EV floor (min daily yield), no in-play sports,
                   per-event exposure caps, and early take-profit to recycle
                   capital. High win rate by construction; P&L is the verdict.

Fills are conservative: real ask book, max 25% of displayed depth, taker fees
modeled per market, positions valued at best bid.

Usage: polybot.py run | status | dashboard
State: state.json   Log: logs/run.log   Dashboard: dashboard.html
"""
import json, re, sys, time, html, fcntl
import urllib.request, urllib.parse
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATE_FILE = ROOT / "state.json"
DASH_FILE = ROOT / "dashboard.html"
LOCK_FILE = ROOT / ".lock"

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"

START_BANKROLL = 1000.00
ARB_MIN_EDGE = 0.015          # net sum must be <= 1 - this
ARB_MAX_EQUITY_FRAC = 0.15
FAV_MIN_ASK, FAV_MAX_ASK = 0.96, 0.99
FAV_MAX_DAYS = 21
FAV_POS_FRAC = 0.05           # max 5% of equity per position
FAV_MAX_NEW_PER_RUN = 6
FAV_MIN_LIQUIDITY = 20000     # $ liquidity filter
FAV_MIN_DAILY_YIELD = 0.0010  # >= 0.10%/day at entry or skip (EV floor)
EVENT_CAP_FRAC = 0.10         # max 10% of equity in any one event
TAKE_PROFIT_BID = 0.995       # sell early at >= this bid...
TP_MIN_DAYS = 5               # ...if more than this many days still locked
DEPTH_FRAC = 0.25             # take at most 25% of displayed best-ask depth
MIN_STAKE = 10.0
EVENTS_SCANNED = 120

# --- v3 risk patch (2026-10-18): kill the rare big hits ---
THEME_CAP_FRAC = 0.15         # max 15% of equity per correlated theme
OTHER_THEME_CAP_FRAC = 0.30   # the uncorrelated grab-bag gets a looser cap
CASH_FLOOR_FRAC = 0.15        # always keep >= 15% of equity in cash
CUT_LOSS_BID = 0.80           # a 96-99c favorite whose bid sinks here is cut...
CUT_LOSS_MIN_BID = 0.05       # ...unless the book is already near zero
TRIM_MIN_BID = 0.97           # rebalance only sells winners this close to done
V3_EPOCH = "2026-10-19T00:00:00Z"   # the clean post-risk-patch record starts here
START_DATE = "2026-06-11"            # first paper dollar at risk (for annualizing)

# markets a single person controls (tweet counts, "will X say Y"): the subject
# can see the odds and act on them — adverse selection, excluded like in-play
# sports (the July Musk tweet-count loss was this class)
MANIPULABLE = re.compile(
    r"\btweets?\b|\bretweets?\b|truth social|\bposts\b|\bsays?\b|\bmentions?\b"
    r"|\bwears?\b|\bfollowers?\b", re.I)

THEMES = [
    ("person", MANIPULABLE),
    ("crypto", re.compile(r"bitcoin|\bbtc\b|ethereum|\beth\b|solana|\bsol\b|crypto"
                          r"|\bxrp\b|doge|cardano|litecoin", re.I)),
    ("geopolitics", re.compile(r"iran|israel|gaza|hezbollah|houthi|russia|ukraine"
                               r"|putin|zelensky|taiwan|nuclear|ceasefire|\bwar\b", re.I)),
    ("us-politics", re.compile(r"trump|biden|vance|congress|senate|\bhouse\b|election"
                               r"|president|white house|supreme court|shutdown|\bfed\b"
                               r"|powell|tariff", re.I)),
    ("sports", re.compile(r"\bnfl\b|\bnba\b|\bmlb\b|\bnhl\b|\bufc\b|\bf1\b|premier league"
                          r"|champions league|world series|super bowl|\bvs\.?\b", re.I)),
]


def theme_of(question):
    for name, rx in THEMES:
        if rx.search(question or ""):
            return name
    return "other"


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_dt(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def get_json(url, tries=3):
    for i in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "polybot-paper/2.0"})
            with urllib.request.urlopen(req, timeout=20) as r:
                return json.loads(r.read().decode())
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(1 + i)


def log(msg):
    line = f"[{now_iso()}] {msg}"
    print(line)
    with open(ROOT / "logs" / "run.log", "a") as f:
        f.write(line + "\n")


# ---------------- state ----------------

def migrate(s):
    """Build running aggregates once, so the resolved/trades lists can be
    capped (state.json hit 860KB) without losing the lifetime record."""
    if "agg" not in s:
        res = s.get("resolved", [])
        agg = {"n": len(res),
               "wins": sum(1 for r in res if r["won"]),
               "pnl": round(sum(r["pnl"] for r in res), 2),
               "gw": round(sum(max(r["pnl"], 0) for r in res), 2),
               "gl": round(sum(min(r["pnl"], 0) for r in res), 2),
               "worst": round(min((r["pnl"] for r in res), default=0), 2),
               "strat": {}}
        for r in res:
            agg["strat"][r["strategy"]] = round(
                agg["strat"].get(r["strategy"], 0) + r["pnl"], 2)
        s["agg"] = agg
    if "v3" not in s:
        res = [r for r in s.get("resolved", []) if r.get("t", "") >= V3_EPOCH]
        s["v3"] = {"n": len(res),
                   "wins": sum(1 for r in res if r["won"]),
                   "pnl": round(sum(r["pnl"] for r in res), 2),
                   "worst": round(min((r["pnl"] for r in res), default=0), 2)}
    return s


def load_state():
    if STATE_FILE.exists():
        return migrate(json.loads(STATE_FILE.read_text()))
    return migrate({"cash": START_BANKROLL, "positions": [], "trades": [],
                    "equity_curve": [], "resolved": [], "last_run": None})


def save_state(s):
    s["trades"] = s["trades"][-400:]
    s["resolved"] = s["resolved"][-500:]
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(s, indent=1))
    tmp.replace(STATE_FILE)


def record_result(state, question, strategy, won, pnl, cost):
    """Every closed result flows through here: display list + lifetime
    aggregates + the clean since-v3 record."""
    state["resolved"].append({"t": now_iso(), "question": question,
                              "strategy": strategy, "won": won,
                              "pnl": round(pnl, 2), "cost": cost})
    a = state["agg"]
    a["n"] += 1
    a["wins"] += 1 if won else 0
    a["pnl"] = round(a["pnl"] + pnl, 2)
    a["gw"] = round(a["gw"] + max(pnl, 0), 2)
    a["gl"] = round(a["gl"] + min(pnl, 0), 2)
    a["worst"] = round(min(a["worst"], pnl), 2)
    a["strat"][strategy] = round(a["strat"].get(strategy, 0) + pnl, 2)
    v = state["v3"]
    v["n"] += 1
    v["wins"] += 1 if won else 0
    v["pnl"] = round(v["pnl"] + pnl, 2)
    v["worst"] = round(min(v["worst"], pnl), 2)


# ---------------- market data ----------------

def parse_market(m):
    """Normalize a gamma market dict."""
    try:
        outcomes = json.loads(m.get("outcomes", "[]"))
        prices = [float(x) for x in json.loads(m.get("outcomePrices", "[]"))]
        tokens = json.loads(m.get("clobTokenIds", "[]"))
    except Exception:
        return None
    if not (outcomes and tokens) or len(outcomes) != len(tokens):
        return None
    fee_rate, fee_exp = 0.0, 1
    sports = False
    if m.get("feesEnabled"):
        fs = m.get("feeSchedule") or {}
        fee_rate = float(fs.get("rate", 0.02))
        fee_exp = float(fs.get("exponent", 1))
        sports = "sports" in str(fs.get("takerOnly", "")) or "sports" in str(m.get("feeType", ""))
    return {
        "id": str(m.get("id")), "question": m.get("question", "?"),
        "outcomes": outcomes, "prices": prices, "tokens": tokens,
        "end": m.get("endDate"), "closed": m.get("closed", False),
        "liquidity": float(m.get("liquidity") or 0),
        "best_ask": float(m.get("bestAsk") or 1), "best_bid": float(m.get("bestBid") or 0),
        "fee_rate": fee_rate, "fee_exp": fee_exp, "sports": sports,
        "game_start": m.get("gameStartTime"),
    }


def fetch_events():
    url = (f"{GAMMA}/events?closed=false&limit={EVENTS_SCANNED}"
           "&order=volume24hr&ascending=false")
    return get_json(url)


def fetch_book(token_id):
    b = get_json(f"{CLOB}/book?token_id={urllib.parse.quote(token_id)}")
    # best price level is LAST in each array
    bids = [(float(x["price"]), float(x["size"])) for x in b.get("bids", [])]
    asks = [(float(x["price"]), float(x["size"])) for x in b.get("asks", [])]
    best_bid = bids[-1] if bids else (0.0, 0.0)
    best_ask = asks[-1] if asks else (1.0, 0.0)
    return best_bid, best_ask


def fetch_market_by_id(mid):
    # path-style endpoint keeps serving closed/settled markets; the
    # list-style ?id= query returns [] once a market closes, which left
    # resolved positions stuck open forever (cash never freed)
    try:
        res = get_json(f"{GAMMA}/markets/{mid}")
        if isinstance(res, dict) and res.get("id"):
            return parse_market(res)
    except Exception:
        pass
    res = get_json(f"{GAMMA}/markets?id={mid}")
    if isinstance(res, list) and res:
        return parse_market(res[0])
    return None


def eff_cost(price, fee_rate, fee_exp=1):
    """All-in cost per share. Polymarket taker fee = rate * min(p, 1-p)^exp."""
    return price + fee_rate * (min(price, 1 - price) ** fee_exp)


def exit_proceeds(bid, m):
    """Per-share proceeds selling at bid, net of taker fee."""
    return bid - m["fee_rate"] * (min(bid, 1 - bid) ** m["fee_exp"])


def side_quote(m, idx):
    """Approx (bid, ask) for outcome idx from gamma's YES-side quote."""
    if idx == 0:
        return m["best_bid"], m["best_ask"]
    return 1 - m["best_ask"], 1 - m["best_bid"]


# ---------------- strategies ----------------

def scan_negrisk_arb(events, state, equity):
    held_events = {p.get("group") for p in state["positions"] if p.get("group")}
    fills = []
    for ev in events:
        if not ev.get("negRisk") or not ev.get("enableOrderBook"):
            continue
        ev_id = f"ev{ev.get('id')}"
        if ev_id in held_events:
            continue
        markets = [parse_market(m) for m in ev.get("markets", [])]
        markets = [m for m in markets if m and not m["closed"]]
        if len(markets) < 3:
            continue
        rough = sum(eff_cost(m["best_ask"], m["fee_rate"], m["fee_exp"]) for m in markets)
        if rough > 1 - ARB_MIN_EDGE * 0.5:
            continue
        legs = []
        try:
            for m in markets:
                _, (ask_p, ask_sz) = fetch_book(m["tokens"][0])
                legs.append((m, ask_p, ask_sz))
        except Exception as e:
            log(f"arb book fetch failed for event {ev_id}: {e}")
            continue
        net_sum = sum(eff_cost(p, m["fee_rate"], m["fee_exp"]) for m, p, _ in legs)
        if net_sum > 1 - ARB_MIN_EDGE:
            continue
        shares = min(sz * DEPTH_FRAC for _, _, sz in legs)
        max_cost = equity * ARB_MAX_EQUITY_FRAC
        if shares * net_sum > max_cost:
            shares = max_cost / net_sum
        cost = shares * net_sum
        if cost < 20:
            continue
        thesis = (f"NegRisk arb: {len(legs)} YES legs sum to {net_sum:.3f} "
                  f"net of fees -> locked {(1-net_sum)*100:.1f}% on resolution")
        for m, p, _ in legs:
            fills.append({"market": m, "idx": 0, "price": p, "shares": shares,
                          "strategy": "negrisk_arb", "group": ev_id,
                          "event": ev_id, "thesis": thesis})
        log(f"ARB FOUND {ev.get('title','?')}: sum={net_sum:.4f} shares={shares:.1f}")
    return fills


def cluster_key(ev):
    """Group related events (same game split into spreads/totals/exact-score)."""
    slug = ev.get("slug") or f"ev{ev.get('id')}"
    m = re.match(r"^(.*?\d{4}-\d{2}-\d{2})", slug)
    return m.group(1) if m else slug


def scan_favorites(events, state, equity, skip_themes=None):
    skip_themes = skip_themes or set()  # themes the rebalancer just trimmed:
    # re-buying them same cycle would churn spread+fees for nothing
    held = {(p["market_id"], p.get("outcome_idx", 0)) for p in state["positions"]}
    ev_exposure = {}
    for p in state["positions"]:
        if p.get("event"):
            ev_exposure[p["event"]] = ev_exposure.get(p["event"], 0) + p["cost"]
    theme_exposure = {}
    for p in state["positions"]:
        th = theme_of(p.get("question", ""))
        theme_exposure[th] = theme_exposure.get(th, 0) + p["cost"]
    now = datetime.now(timezone.utc)
    cands = []
    for ev in events:
        ev_id = cluster_key(ev)
        for raw in ev.get("markets", []):
            m = parse_market(raw)
            if not m or m["closed"] or m["liquidity"] < FAV_MIN_LIQUIDITY or not m["end"]:
                continue
            if MANIPULABLE.search(m["question"] or ""):
                continue  # v3: person-controlled market
            if theme_of(m["question"]) in skip_themes:
                continue
            # never trade in-play sports: live watchers know more than a cron job
            gs = parse_dt(m["game_start"] or "")
            if gs and gs <= now:
                continue
            if m["sports"] and not gs:
                continue  # sports market w/ unknown game time: assume unsafe
            end = parse_dt(m["end"])
            if not end:
                continue
            days = (end - now).total_seconds() / 86400
            if not (0.05 < days <= FAV_MAX_DAYS):
                continue
            if len(m["outcomes"]) != 2:
                continue
            for idx in (0, 1):  # YES side and NO side (longshot fade)
                if (m["id"], idx) in held:
                    continue
                _, approx_ask = side_quote(m, idx)
                if FAV_MIN_ASK <= approx_ask <= FAV_MAX_ASK:
                    cands.append((m, idx, days, ev_id))
    cands.sort(key=lambda x: x[2])  # soonest resolution first
    fills = []
    for m, idx, days, ev_id in cands:
        if len(fills) >= FAV_MAX_NEW_PER_RUN:
            break
        try:
            _, (ask_p, ask_sz) = fetch_book(m["tokens"][idx])
        except Exception:
            continue
        cost_ps = eff_cost(ask_p, m["fee_rate"], m["fee_exp"])
        if not (FAV_MIN_ASK <= ask_p <= FAV_MAX_ASK) or cost_ps >= 0.995:
            continue
        daily_yield = (1 / cost_ps - 1) / max(days, 0.05)
        if daily_yield < FAV_MIN_DAILY_YIELD:
            continue  # EV floor: dead capital is a cost
        room = equity * EVENT_CAP_FRAC - ev_exposure.get(ev_id, 0)
        th = theme_of(m["question"])
        th_cap = OTHER_THEME_CAP_FRAC if th == "other" else THEME_CAP_FRAC
        th_room = equity * th_cap - theme_exposure.get(th, 0)
        stake = min(equity * FAV_POS_FRAC, ask_p * ask_sz * DEPTH_FRAC,
                    max(room, 0), max(th_room, 0))
        if stake < MIN_STAKE:
            continue
        shares = stake / cost_ps
        side = m["outcomes"][idx]
        thesis = (f"Favorite-longshot bias: {side} @ {ask_p:.3f} (all-in {cost_ps:.3f}), "
                  f"{days:.1f}d to resolution, {daily_yield*100:.2f}%/day if fair")
        fills.append({"market": m, "idx": idx, "price": ask_p, "shares": shares,
                      "strategy": "favorite", "group": None, "event": ev_id,
                      "thesis": thesis})
        ev_exposure[ev_id] = ev_exposure.get(ev_id, 0) + shares * cost_ps
        theme_exposure[th] = theme_exposure.get(th, 0) + shares * cost_ps
    return fills


# ---------------- engine ----------------

def execute_fills(fills, state):
    reserve = equity_of(state) * CASH_FLOOR_FRAC
    for f in fills:
        m = f["market"]
        idx = f["idx"]
        cost = f["shares"] * eff_cost(f["price"], m["fee_rate"], m["fee_exp"])
        if cost > state["cash"]:
            log(f"skip (cash): {m['question'][:60]}")
            continue
        if state["cash"] - cost < reserve:
            log(f"skip (cash floor): {m['question'][:60]}")
            continue
        state["cash"] -= cost
        pos = {"market_id": m["id"], "token": m["tokens"][idx],
               "outcome_idx": idx, "side": m["outcomes"][idx],
               "question": m["question"], "shares": round(f["shares"], 2),
               "avg_cost": round(cost / f["shares"], 4), "cost": round(cost, 2),
               "strategy": f["strategy"], "group": f["group"], "event": f.get("event"),
               "opened": now_iso(), "end": m["end"], "last_bid": f["price"]}
        state["positions"].append(pos)
        state["trades"].append({"t": now_iso(), "action": "BUY",
                                "question": f"{m['question']} [{pos['side']}]",
                                "price": f["price"], "shares": round(f["shares"], 2),
                                "cost": round(cost, 2), "strategy": f["strategy"],
                                "thesis": f["thesis"]})
        log(f"BUY {f['shares']:.1f} {pos['side']} '{m['question'][:55]}' @ {f['price']:.3f} (${cost:.2f}) [{f['strategy']}]")


def mark_and_resolve(state):
    now = datetime.now(timezone.utc)
    keep = []
    for p in state["positions"]:
        try:
            m = fetch_market_by_id(p["market_id"])
        except Exception:
            m = None
        if m is None:
            keep.append(p)
            continue
        idx = p.get("outcome_idx", 0)
        side_price = m["prices"][idx] if idx < len(m["prices"]) else p["last_bid"]
        bid, _ = side_quote(m, idx)
        if m["closed"]:
            won = side_price >= 0.95
            payout = p["shares"] * (1.0 if won else 0.0)
            state["cash"] += payout
            pnl = payout - p["cost"]
            record_result(state, p["question"], p["strategy"], won, pnl, p["cost"])
            state["trades"].append({"t": now_iso(), "action": "RESOLVE",
                                    "question": f"{p['question']} [{p.get('side','Yes')}]",
                                    "price": 1.0 if won else 0.0,
                                    "shares": p["shares"], "cost": -payout,
                                    "strategy": p["strategy"],
                                    "thesis": f"Resolved {'WON' if won else 'LOST'}, P&L ${pnl:+.2f}"})
            log(f"RESOLVED {'WON' if won else 'LOST'} '{p['question'][:55]}' P&L ${pnl:+.2f}")
            continue
        # v3 cut-loss: a 96-99c favorite whose bid broke down is no longer a
        # favorite — salvage the stake instead of riding it to zero (skip
        # in-play sports: a mid-game dip is noise, not news)
        gs = parse_dt(m.get("game_start") or "")
        if (p["strategy"] == "favorite" and not (gs and gs <= now)
                and CUT_LOSS_MIN_BID <= bid <= CUT_LOSS_BID):
            proceeds = p["shares"] * exit_proceeds(bid, m)
            state["cash"] += proceeds
            pnl = proceeds - p["cost"]
            record_result(state, p["question"], p["strategy"], False, pnl, p["cost"])
            state["trades"].append({"t": now_iso(), "action": "SELL",
                                    "question": f"{p['question']} [{p.get('side','Yes')}]",
                                    "price": bid, "shares": p["shares"],
                                    "cost": -round(proceeds, 2), "strategy": p["strategy"],
                                    "thesis": f"Cut loss @ {bid:.3f}: favorite broke down, "
                                              f"salvaging stake, P&L ${pnl:+.2f}"})
            log(f"CUT-LOSS '{p['question'][:55]}' @ {bid:.3f} P&L ${pnl:+.2f}")
            continue
        # early take-profit: recycle capital instead of waiting weeks for 100c
        end = parse_dt(p.get("end") or "")
        days_left = (end - now).total_seconds() / 86400 if end else 0
        if (p["strategy"] == "favorite" and bid >= TAKE_PROFIT_BID
                and days_left > TP_MIN_DAYS):
            proceeds = p["shares"] * exit_proceeds(bid, m)
            state["cash"] += proceeds
            pnl = proceeds - p["cost"]
            record_result(state, p["question"], p["strategy"], True, pnl, p["cost"])
            state["trades"].append({"t": now_iso(), "action": "SELL",
                                    "question": f"{p['question']} [{p.get('side','Yes')}]",
                                    "price": bid, "shares": p["shares"],
                                    "cost": -round(proceeds, 2), "strategy": p["strategy"],
                                    "thesis": f"Take-profit @ {bid:.3f} with {days_left:.0f}d left; "
                                              f"recycling capital, P&L ${pnl:+.2f}"})
            log(f"TAKE-PROFIT '{p['question'][:55]}' @ {bid:.3f} P&L ${pnl:+.2f}")
            continue
        p["last_bid"] = bid
        keep.append(p)
    state["positions"] = keep


def equity_of(state):
    return state["cash"] + sum(p["shares"] * p["last_bid"] for p in state["positions"])


def rebalance(state):
    """v3.1: trim the EXISTING book back inside the theme caps. The caps in
    scan_favorites only gate NEW buys, so legacy concentration (July: 3 themes
    were 91% of equity) would otherwise persist for weeks. Sell the positions
    closest to resolution first (bid >= TRIM_MIN_BID): they have the least EV
    left to sacrifice but still carry full tail risk. Returns the themes it
    trimmed so the scanner won't re-buy them this cycle."""
    trimmed = set()
    eq = equity_of(state)
    expo = {}
    for p in state["positions"]:
        th = theme_of(p.get("question", ""))
        expo[th] = expo.get(th, 0) + p["shares"] * p["last_bid"]
    for th, val in sorted(expo.items(), key=lambda x: -x[1]):
        cap = (OTHER_THEME_CAP_FRAC if th == "other" else THEME_CAP_FRAC) * eq
        if val <= cap:
            continue
        cands = sorted((p for p in state["positions"]
                        if theme_of(p.get("question", "")) == th
                        and p["last_bid"] >= TRIM_MIN_BID),
                       key=lambda p: -p["last_bid"])
        for p in cands:
            if val <= cap:
                break
            try:
                m = fetch_market_by_id(p["market_id"])
            except Exception:
                continue
            if m is None or m["closed"]:
                continue
            bid, _ = side_quote(m, p.get("outcome_idx", 0))
            if bid < TRIM_MIN_BID:
                continue
            proceeds = p["shares"] * exit_proceeds(bid, m)
            state["cash"] += proceeds
            pnl = proceeds - p["cost"]
            state["positions"].remove(p)
            val -= p["shares"] * p["last_bid"]
            record_result(state, p["question"], p["strategy"], pnl >= 0, pnl, p["cost"])
            state["trades"].append({"t": now_iso(), "action": "SELL",
                                    "question": f"{p['question']} [{p.get('side','Yes')}]",
                                    "price": bid, "shares": p["shares"],
                                    "cost": -round(proceeds, 2), "strategy": p["strategy"],
                                    "thesis": f"Rebalance: {th} over the theme cap; sold "
                                              f"near-resolved winner @ {bid:.3f}, P&L ${pnl:+.2f}"})
            log(f"REBALANCE {th} '{p['question'][:50]}' @ {bid:.3f} P&L ${pnl:+.2f}")
            trimmed.add(th)
    return trimmed


def run_cycle():
    with open(LOCK_FILE, "w") as lk:
        try:
            fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            log("another run in progress; exiting")
            return
        state = load_state()
        log("=== cycle start (v3) ===")
        mark_and_resolve(state)
        trimmed = rebalance(state)
        equity = equity_of(state)
        try:
            events = fetch_events()
        except Exception as e:
            log(f"event fetch failed: {e}")
            events = []
        fills = []
        if events:
            try:
                fills += scan_negrisk_arb(events, state, equity)
            except Exception as e:
                log(f"negrisk scan error: {e}")
            try:
                fills += scan_favorites(events, state, equity, trimmed)
            except Exception as e:
                log(f"favorites scan error: {e}")
        execute_fills(fills, state)
        equity = equity_of(state)
        state["equity_curve"].append({"t": now_iso(), "equity": round(equity, 2)})
        state["equity_curve"] = state["equity_curve"][-2000:]
        state["last_run"] = now_iso()
        save_state(state)
        render_dashboard(state)
        log(f"=== cycle end: equity ${equity:.2f} cash ${state['cash']:.2f} "
            f"positions {len(state['positions'])} ===")


# ---------------- dashboard ----------------

def render_dashboard(state):
    eq = equity_of(state)
    pnl = eq - START_BANKROLL
    agg, v3 = state["agg"], state["v3"]
    wr = (f"{agg['wins']}/{agg['n']} ({100*agg['wins']/agg['n']:.0f}%)"
          if agg["n"] else "—")
    days = max((datetime.now(timezone.utc)
                - datetime.fromisoformat(START_DATE + "T00:00:00+00:00")).days, 1)
    annual = (eq / START_BANKROLL) ** (365 / days) - 1
    pf = f"{agg['gw']/abs(agg['gl']):.2f}" if agg["gl"] else "∞"
    curve = [p["equity"] for p in state["equity_curve"]] or [START_BANKROLL]
    peak, mdd = curve[0], 0.0
    for v in curve:
        peak = max(peak, v)
        mdd = min(mdd, v / peak - 1)
    strat_pnl = dict(agg["strat"])
    for p in state["positions"]:
        strat_pnl[p["strategy"]] = (strat_pnl.get(p["strategy"], 0)
                                    + p["shares"] * p["last_bid"] - p["cost"])
    strat_str = " · ".join(f"{k} ${v:+.2f}" for k, v in strat_pnl.items()) or "—"
    theme_val = {}
    for p in state["positions"]:
        th = theme_of(p.get("question", ""))
        theme_val[th] = theme_val.get(th, 0) + p["shares"] * p["last_bid"]
    theme_str = " · ".join(f"{t} {v/eq*100:.0f}%"
                           for t, v in sorted(theme_val.items(), key=lambda x: -x[1])) or "—"
    lo, hi = min(curve), max(curve)
    span = (hi - lo) or 1
    pts = " ".join(f"{i/(max(len(curve)-1,1))*560:.1f},{120-((v-lo)/span)*110:.1f}"
                   for i, v in enumerate(curve))
    color = "#16a34a" if pnl >= 0 else "#dc2626"

    def rows(items, cols):
        return "".join("<tr>" + "".join(f"<td>{html.escape(str(c))}</td>" for c in cols(x)) + "</tr>"
                       for x in items)

    pos_rows = rows(sorted(state["positions"], key=lambda p: p["opened"], reverse=True)[:40],
                    lambda p: (p["opened"][5:16].replace("T", " "), p["strategy"],
                               p.get("side", "Yes"), p["question"][:64], f"{p['shares']:.1f}",
                               f"{p['avg_cost']:.3f}", f"{p['last_bid']:.3f}",
                               f"${p['shares']*p['last_bid']-p['cost']:+.2f}"))
    trade_rows = rows(list(reversed(state["trades"][-30:])),
                      lambda t: (t["t"][5:16].replace("T", " "), t["action"], t["strategy"],
                                 t["question"][:60], f"{t['price']:.3f}",
                                 t["shares"], html.escape(t["thesis"][:100])))
    DASH_FILE.write_text(f"""<!doctype html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="300"><title>PolyBot v3 — paper trading</title>
<style>body{{font-family:-apple-system,Helvetica,sans-serif;background:#0b0f14;color:#e6edf3;margin:24px}}
h1{{font-size:20px}} .big{{font-size:34px;font-weight:700}} .pnl{{color:{color}}}
.cards{{display:flex;gap:16px;flex-wrap:wrap;margin:14px 0}}
.card{{background:#151b23;border:1px solid #2a3340;border-radius:10px;padding:14px 20px;min-width:130px}}
.lbl{{font-size:11px;text-transform:uppercase;color:#8b98a5;letter-spacing:.06em}}
table{{border-collapse:collapse;width:100%;font-size:12px;margin:8px 0 24px}}
td,th{{padding:5px 8px;border-bottom:1px solid #1f2730;text-align:left}}
th{{color:#8b98a5;font-size:11px;text-transform:uppercase}}
.note{{color:#8b98a5;font-size:12px;max-width:760px}}</style></head><body>
<h1>🤖 PolyBot v3 <span style="color:#8b98a5">— PAPER trading, fake $1,000. Not real money.</span></h1>
<div class="cards">
<div class="card"><div class="lbl">Equity</div><div class="big">${eq:,.2f}</div></div>
<div class="card"><div class="lbl">P&amp;L</div><div class="big pnl">${pnl:+,.2f}</div></div>
<div class="card"><div class="lbl">Cash</div><div class="big">${state['cash']:,.2f}</div></div>
<div class="card"><div class="lbl">Open positions</div><div class="big">{len(state['positions'])}</div></div>
<div class="card"><div class="lbl">Resolved win rate</div><div class="big">{wr}</div></div>
<div class="card"><div class="lbl">Max drawdown</div><div class="big">{mdd*100:.1f}%</div></div>
<div class="card"><div class="lbl">Since v3 risk patch</div><div class="big" style="color:{'#16a34a' if v3['pnl'] >= 0 else '#dc2626'}">${v3['pnl']:+,.2f}</div><div class="lbl">{v3['wins']}/{v3['n']} wins · worst ${v3['worst']:+.2f}</div></div>
<div class="card"><div class="lbl">Worst hit ever</div><div class="big">${agg['worst']:+,.2f}</div></div>
</div>
<svg width="560" height="124" style="background:#151b23;border:1px solid #2a3340;border-radius:10px">
<polyline fill="none" stroke="{color}" stroke-width="2" points="{pts}"/></svg>
<p class="note">Annualized return {annual*100:+.1f}% over {days}d · profit factor {pf} (gross wins
${agg['gw']:,.0f} / gross losses ${abs(agg['gl']):,.0f}).<br>
Theme exposure: {theme_str} (caps: {THEME_CAP_FRAC*100:.0f}% per theme, other
{OTHER_THEME_CAP_FRAC*100:.0f}% — the rebalancer trims back by selling near-resolved winners first).<br>
Per-strategy P&amp;L: {strat_str}. Fills walk the real ask book (max {int(DEPTH_FRAC*100)}%
of displayed depth), taker fees added where charged, positions valued at best bid. v2: trades both
sides of the favorite-longshot bias, skips in-play sports, requires ≥{FAV_MIN_DAILY_YIELD*100:.2f}%/day
yield at entry, caps {EVENT_CAP_FRAC*100:.0f}% equity per event, takes profit early at
{TAKE_PROFIT_BID:.3f} bid to recycle capital. v3 (big-hit protection): excludes person-controlled
markets (tweet counts etc.), caps {THEME_CAP_FRAC*100:.0f}% of equity per correlated theme, keeps a
{CASH_FLOOR_FRAC*100:.0f}% cash floor, and cuts a broken favorite at ≤{CUT_LOSS_BID:.2f} bid instead of
riding it to zero. Win rate is decoration — the P&amp;L line is the verdict.
Last run {state['last_run'] or '—'} (auto-refreshes).</p>
<h2 style="font-size:15px">Open positions</h2>
<table><tr><th>opened</th><th>strategy</th><th>side</th><th>market</th><th>shares</th><th>cost</th><th>bid</th><th>unrl P&amp;L</th></tr>
{pos_rows or '<tr><td colspan=8>none yet</td></tr>'}</table>
<h2 style="font-size:15px">Recent activity</h2>
<table><tr><th>time</th><th>action</th><th>strategy</th><th>market</th><th>px</th><th>shares</th><th>thesis</th></tr>
{trade_rows or '<tr><td colspan=7>none yet</td></tr>'}</table>
<script>
const last = Date.parse("{state['last_run'] or ''}");
if (!isNaN(last) && Date.now() - last > 2 * 3600e3) {{
  const b = document.createElement('div');
  b.style.cssText = 'background:#7f1d1d;color:#fecaca;padding:10px 14px;border-radius:8px;margin:12px 0;font-weight:600';
  b.textContent = '⚠️ No bot run for ' + Math.round((Date.now() - last) / 3600e3) +
    'h — the bot may be dark (blind is not quiet). Check the cloud job.';
  document.body.prepend(b);
}}
</script>
</body></html>""")


def status():
    s = load_state()
    eq = equity_of(s)
    a, v = s["agg"], s["v3"]
    print(f"equity ${eq:.2f} (P&L ${eq-START_BANKROLL:+.2f}) | cash ${s['cash']:.2f} | "
          f"open {len(s['positions'])} | resolved {a['wins']}/{a['n']} | "
          f"since-v3 ${v['pnl']:+.2f} ({v['wins']}/{v['n']}, worst {v['worst']:+.2f}) | "
          f"last run {s['last_run']}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "run":
        run_cycle()
    elif cmd == "status":
        status()
    elif cmd == "dashboard":
        render_dashboard(load_state())
        print(DASH_FILE)
    else:
        print(__doc__)
