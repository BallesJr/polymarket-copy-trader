# POLYMARKET SMART MONEY COPY-TRADER

Polymarket's leaderboard is full of wallets with impressive PnL, but a leaderboard tells you nothing about whether that edge is *copyable*: by the time a copier sees a trade, the price has moved, the book has thinned, and the exit may already be underway. This project answers that question empirically. First, a research pipeline separates skill from luck across trader cohorts and profiles the survivors for copyability; then a paper bot mirrors the two wallets that passed, recording for every position the leader's entry price, our fill, and the copy delay. The whole point of the experiment is measuring what a copier actually gets.

---

## WHAT I WORKED ON

- **Cohort harvest**: Downloaded full activity histories (trades, redeems, splits/merges, rebates) for the top-30 wallets by 30-day leaderboard PnL plus 30 random active wallets as a control group, via Polymarket's public data API.
- **Skill-vs-luck screen**: Computed per-wallet metrics designed to kill survivorship stories: realized PnL windowed to the same 30 days as the leaderboard, complete round-trips only (markets touched by SPLIT/MERGE dropped as unverifiable), daily-PnL t-statistic, PnL concentration (top-1/top-5 share), first-half vs second-half consistency, and truncation flags for wallets that hit the harvest cap.
- **Deep verification**: For the shortlist, re-harvested 180 days of history (chunking by time windows to bypass the API's offset cap) and recomputed monthly PnL matrices and t-stats *excluding June 2026* — if a wallet's edge only exists during the World Cup, copying it afterwards buys nothing.
- **Copyability profile**: For each candidate, measured the PnL-weighted median hours between their first entry in a market and their last cash flow (the reaction window a copier gets), edge per share bought, and how their PnL survives 1/2/3 cents of entry+exit slippage. Two wallets passed every filter and became the leaders.
- **Copy bot**: A paper trader that aggregates each leader's micro-orders into net cost per token and mirrors positions once conviction crosses a threshold, filling against the live CLOB order book — walking real ask depth with a price limit, not assuming the leader's price. Since 2026-07-22, entries whose fill would land at/after the scheduled game start are skipped: an in-play fill prices in what the leader knew (see results). Exits when the leader unwinds, waiting for bid depth to absorb us; resolutions settle at $1/$0 via the Gamma API with a CLOB winner-flag fallback (Gamma silently omits resolved markets), and canceled matches that resolve 50/50 with no winner flag settle at $0.50.
- **Skip log**: Entries rejected for chasing (ask beyond leader VWAP + limit), thin books, or landing in-play are recorded with the prices and timestamps involved — the cost of *not* getting fills is part of what a copier pays, and the in-play skips double as an unfiltered shadow series to test the filter against.
- **Automation**: Each cycle runs on GitHub Actions, triggered every 5 minutes (10 before 2026-07-22) by an external cron via `workflow_dispatch` — GitHub's own scheduler proved unreliable for this (a 30-minute cron fired every 1–4 hours in practice; the cron remains as fallback). State is committed back to the repo only when something beyond the timestamp changed.

---

## PROJECT STRUCTURE

- `harvest.py`: Downloads leaderboard + control cohorts and per-wallet activity/positions.
- `analyze.py`: Skill-vs-luck metrics over the harvested cohorts; writes `data/metrics.json`.
- `harvest_deep.py` / `harvest_windowed.py`: 180-day deep harvest for shortlisted wallets; the windowed variant splits dense periods recursively to get complete history past the API offset cap.
- `analyze_deep.py`: Long-window verification (monthly PnL, World-Cup-excluded t-stats) and the copyability profile.
- `copy_trader.py`: The paper copy bot; persists state to `data/copy_state.json`.
- `status.py`: Read-only portfolio status — open positions marked to live books, recent closes.
- `.github/workflows/copy-trader.yml`: Runs one bot cycle; dispatched externally every 5 minutes.

---

## CONFIGURATION

| Parameter | Value |
|---|---|
| Conviction threshold | $500 leader net cost in a token |
| Stake | $100 flat per position |
| Exit trigger | leader below 50% of their max shares |
| Max chase | leader VWAP + $0.15, walking real ask depth |
| Price band | [0.03, 0.97] |
| In-play filter | skip entries at/after scheduled game start (since 2026-07-22) |
| Initial bankroll | $10,000 paper |

---

## PAPER TRADING RESULTS

Live state (bankroll, open/closed positions, skips, per-position leader VWAP and copy delay) is in `data/copy_state.json`, updated after every cycle. The numbers below are a snapshot, **last updated 2026-08-12** — they go stale the moment the bot runs again, so if that date is old, trust the state file over this section.

### Snapshot as of 2026-08-12 (bot live since 2026-07-03)

| Period (by open date) | Trades | Realized PnL | Win rate | Avg / trade |
|---|---|---|---|---|
| Before in-play filter (07-03 → 07-22) | 191 | −$1,937 | 38.7% | −$10.14 |
| After filter + 5-min trigger (07-22 → 08-12) | 81 | **+$2,108** | 46.9% | +$26.03 (t = 1.53) |

Cumulative realized PnL is back above water (+$171 on the $10,000 paper bankroll). The honest caveat: a counterfactual replay of the 141 entries the in-play filter skipped (entry at the market price at skip time, same $100 stake, 134 resolved) would have made **+$1,485 too** — the copied trades beat the skipped ones by only ~$15/trade (Welch t = 0.69, not significant). So far the turnaround says more about the leader's regime than about the filter; the filter stays because the skipped set still underperforms the copied set and skipping costs nothing, but its value is unproven. (The counterfactual flatters the skips slightly: last-trade prices ignore the in-play spread a marketable order would actually cross.)

**Regime notes** — analyses should split on these boundaries:

- before 2026-07-05 ~12:30 UTC: GitHub's unreliable scheduler, copy delays of 26–130 minutes;
- 2026-07-05 → 2026-07-22: external 10-minute trigger, single-digit delays (median 7.6 min, p90 13.9);
- since 2026-07-22 23:30 UTC: in-play filter live and the trigger halved to 5 minutes (median delay 5.6 min, p90 9.6).

---

## REQUIREMENTS

`pip install requests`

---

## EXECUTION

```bash
python copy_trader.py --once      # single cycle (what the scheduler runs)
python copy_trader.py --loop 600  # poll every 600s
python status.py                  # portfolio status, marked to live books
```

---

## LIMITATIONS

**Paper fills**: Entries and exits walk a snapshot of the real order book, but there is no queue position, no market impact, and no adverse selection — a real order alongside the leader's flow would likely do worse. Results are a *lower bound* on copying costs, not an estimate of live returns.

**Leader inventory is reconstructed from public trades only**: Positions built before tracking started are invisible; a heuristic resets the running position when shares go non-positive on a new buy episode. Sells of pre-tracking inventory can still masquerade as exits.

**Two leaders, small sample**: The screen was strict, so the experiment rides on two wallets. A leader going cold (or having been lucky all along) dominates any conclusion until the trade count grows.

**Exit latency**: Exits wait until bid depth can absorb the full position, and resolution checks run once per cycle — both can hold a position past the price the leader got.

**No fee model**: Polymarket currently charges no trading fees on these markets; if that changes, the accounting here doesn't capture it.

---

## BACKGROUND

The cohort screen grew out of two earlier projects: the [Polymarket Edge Model](https://github.com/BallesJr/polymarket-edge-model), which studied systematic biases in resolved-market data, and the [Weather Edge bot](https://github.com/BallesJr/polymarket-weather-edge), which trades one of those biases live. This project asks the complementary question: when the edge belongs to *someone else*, how much of it survives the copy?
