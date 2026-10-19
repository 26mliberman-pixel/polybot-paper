# PolyBot — autonomous Polymarket paper trader

**PAPER ONLY.** Fake $1,000 bankroll traded against live Polymarket order books.
No wallet, no keys, no real money. Going live is a separate, explicit decision
gated on this paper record being positive after ~4 weeks.

## How it runs
- `launchd` job `com.mikha.polybot` runs `python3 polybot.py run` every 30 min.
- Each cycle: resolve/mark existing positions → scan top-120 events → trade.
- Dashboard: `dashboard.html` (auto-refreshes; open it in any browser).
- Logs: `logs/run.log`. State: `state.json` (delete it to reset the experiment).
- `python3 polybot.py status` → one-line summary.

## Strategies (v1)
1. **negrisk_arb** — in winner-take-all events, if all YES asks sum < $0.985
   net of fees, buy every leg: exactly one pays $1, profit locked at entry.
   Rare (pro bots compete) but mathematically riskless when filled.
2. **favorite** — favorite-longshot bias: buy YES at 96-99¢ resolving within
   21 days, max 5% of equity per position. Expect a HIGH win rate and the
   occasional total loss on a leg. The P&L line is the only verdict.

## Honesty rules baked in
- Fills walk the real ask book, capped at 25% of displayed depth.
- Taker fees modeled per market (`rate × min(p,1-p)^exp`).
- Positions marked at best BID (what you could actually sell at).
- If this can't make paper money, real money would do worse.

## v2 ideas (not built)
- Cross-market consistency arb (conditional-probability violations).
- LLM forecaster divergence: Claude estimates probabilities w/ news retrieval,
  trade only when |model − market| > threshold AND calibration is proven.
  Research to date says expect ≈ market parity — measure before believing.
- Kalshi cross-platform arb (needs Kalshi API + account).

## Going live (the gate)
Requires ALL of: ≥4 weeks paper history, positive P&L net of realistic fills,
Mikha's explicit go, his own Polymarket account + funding (his steps), and
small initial size. Until then this is a measurement instrument, not income.
