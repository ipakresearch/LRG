# Goal
Decide, for each Navi collateral market, (1) whether its risk parameters are
safe given today's liquidation routes, and (2) whether the current book can be
cleared under stress. State how confident you are in each conclusion.
Write the result to data/navi/<today>/report.md.

# Tools
- `python run.py`: full pipeline. Writes to data/navi/<today>/:
  matrix.csv (capacity per pair), adequacy.csv (signal 1),
  stress.csv (signal 2), coverage.json (pool reads), summary.json.
- The lrg package in Python (see README.md), e.g. clmm.inspect(pool_id),
  clmm.sanity(pool, oracle_price=...), topology.load(...).find(A, B).
- Earlier runs in data/navi/<date>/.
- You may write and run your own scripts in agent/scratch/.

# The two signals
Signal 1, parameters: Q_cap = CF*(1+b)*LTV*S*P vs Q*. Fails if the market,
filled to its supply cap at max LTV, could not be cleared by the routes.
Signal 2, current book: breaking shock, the smallest price drop at which the
liquidatable debt of any (collateral, debt) pair exceeds that pair's Q*.

# How to work
1. Run the pipeline. If it fails, diagnose and retry once. If it still fails,
   report the error and stop.
2. Investigate before concluding, for every market where either signal fails,
   and every market whose status or breaking shock changed since the last run:
   - Does the binding route use a pool that failed to read? Can you read it
     another way?
   - Is either asset's derived price far from its level in earlier runs?
   - Which leg binds, and did that pool's depth change since the last run?
   - Did a Navi parameter change?
   - For signal 2: is the liquidatable debt concentrated in a few positions?
3. Classify each finding as CONFIRMED, ARTIFACT (state the cause) or
   UNRESOLVED (state what you would need to decide it).
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
