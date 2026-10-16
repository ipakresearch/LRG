# Goal
Decide, for each Navi collateral market, (1) whether its risk parameters are
safe given today's liquidation routes, and (2) whether the current book can be
cleared under stress. State how confident you are in each conclusion.
Write the result to data/navi/<today>/report.md.

# Tools
- `python run.py`: full pipeline. Writes to data/navi/<today>/:
  matrix.csv (capacity per pair, with its coverage), adequacy.csv (signal 1),
  stress.csv (signal 2, every shock), stress_breaks.csv (signal 2, first
  failing shock per pair and scenario), coverage.json (pool reads, coverage
  share), config.json (parameters, debt set, floors), summary.json, and
  matrix.json + positions.json.gz (what stress ran on).
- `python run.py --from-run <date>`: redo stress for a saved run without
  reading the chain.
- The lrg package in Python (see README.md), e.g. clmm.inspect(pool_id),
  clmm.sanity(pool, oracle_price=...), topology.load(...).find(A, B).
- Earlier runs in data/navi/<date>/.
- You may write and run your own scripts in agent/scratch/.

# The two signals
Signal 1, parameters: per (collateral B, debt A) pair,
Q_cap = beta*CF*(1+b_B)*min(LTV_B*S_B*P_B, max(borrowed_A, cap_A*S_A)*P_A)
vs that pair's Q*, with b_B = (1 - treasury_factor_B)*LB_B. E-Mode-eligible
pairs are also checked at the E-Mode LTV and bonus. Fails if the market,
filled to its caps, could not be cleared by the routes. Only the debt set in
config.json counts.
Signal 2, current book: breaking shock, the smallest price drop (5% steps to
50%; the baseline is 0%) at which the liquidations of any (collateral, debt)
pair exceed that pair's Q* and are material: Q >= min_sold_usd ($1,000).
Positions under min_position_usd ($100) of debt are dust and left out
(summary.json "stress" counts them). stress_status "holds" means no complete
pair fails materially up to 50%. stress_breaks.csv has the first failing
shock per pair and scenario; non_monotone rows fail at one shock but not at
a larger one: report them, never smooth them over. summary.json also reports
LST depegs (vSUI, haSUI, stSUI down 2/5/10% against SUI), unpriced positions
and bad debt per scenario.
Q* in summary.json ranges over the debts where some stress scenario reaches
$1,000 (material_debts); Q_star_all is the row minimum over the whole debt
set. Signal 1 and adequacy are per pair and use every pair's own capacity.

# Coverage
Every pair has a coverage status in matrix.csv: "complete", or "incomplete"
when a pool that failed to read could have changed its capacity (the failed
pools are listed with their TVL). An incomplete pair is never red or amber:
adequacy.csv gives it status "incomplete" (status_if_complete holds what it
would be), signal 1 is "unresolved" when only incomplete pairs fail, and
stress reports breaking_shock_incomplete apart. A finding that rests on an
incomplete pair is UNRESOLVED, never CONFIRMED, unless you read the failed
pool another way and show it could not change the result.

# How to work
1. Run the pipeline. If it fails, diagnose and retry once. If it still fails,
   report the error and stop.
2. Investigate before concluding, for every market where either signal fails,
   and every market whose status or breaking shock changed since the last run:
   - Is the binding pair's coverage incomplete? Which pools failed, and can
     you read them another way?
   - Is either asset's price far from its level in earlier runs? (Prices are
     Navi's oracle; coverage.json says which coins fall back to derived.)
   - Which leg binds, and did that pool's depth change since the last run?
   - Did a Navi parameter change?
   - For signal 2: is the liquidatable debt concentrated in a few positions?
3. Classify each finding as CONFIRMED, ARTIFACT (state the cause) or
   UNRESOLVED (state what you would need to decide it). Incomplete coverage
   is always UNRESOLVED (see Coverage). A capacity flagged saturated means
   "at least $50M", not a measured value.
4. For CONFIRMED signal 1 failures, report s_max at current LTV and LB.

# Report
Start with one table: market, signal 1 status, breaking shock, binding pair,
classification. Then CONFIRMED findings with evidence, then UNRESOLVED,
then ARTIFACT, then changes since the last run.
Note that Q* is measured before the shock, so breaking shocks are upper bounds.

# Rules
- Every number comes from a tool output. Never estimate or recompute by hand.
- Never modify lrg/ or run.py. Write only to data/ and agent/scratch/.
- If something happens that these instructions don't cover, describe it in
  the report instead of guessing.
