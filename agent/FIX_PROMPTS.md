# Claude Code prompts: fixes from the 2026-10-15 review and inconsistencies.md

Run in `~/LRG`, one prompt at a time. Each asks for a plan before edits. After a step passes its check: commit, update `agent/OPEN_ITEMS.md`, then `/clear`.

Order matters. Steps 1–2 change what every number is measured against, 3–6 change which pools exist and how they read, 7–9 change how capacity is computed, 10–12 change the stress test. Step 13 is the rerun.

Background for every step: `agent/REVIEW_2026-10-15.md` (findings with file:line) and `inconsistencies.md`.

---

## 0. Setup

```
Commit the current uncommitted work (oracle prices, positions retries) on a
new branch fixes/review-2026-10-15 with message "Oracle prices and position
read retries". Then add these items to agent/OPEN_ITEMS.md as a checklist,
keeping the existing ones: liquidation bonus split, coin-type keying, swap
direction, pool universe (DeepBook, Aftermath, discovery), Turbos reader,
empty pools, read robustness, curve interpolation, path ranking, matrix
completeness and borrowable debts, coverage flag, stress corrections, LST
mint exit, rerun.
```

## 1. Liquidation bonus split

```
inconsistencies.md (2026-10-15, finding 1) shows from 20 real liquidations
that the liquidator receives the full bonus and the treasury amount is paid
on top. Our model deducts a 10% platform share from the bonus.
- Plan the change: the liquidator's bonus b = LB (not 0.9*LB), so
  seized_factor = 1 + LB and budget = 1 - (1+phi)/(1+LB). The treasury's
  amount is taken from the borrower but not sold, so it is not part of the
  collateral that hits the routes.
- Replace Params.platform_share with an explicit liquidator_bonus_share,
  default 1.0 for Navi, set in lrg/protocols/navi.py with a comment citing
  the 20-liquidation check. Keep the parameter so other protocols can set it.
- Update every use: matrix.Params, analysis.LoadedMatrix.demand, run.py
  (adequacy, stress, s_max_debt_bounded), README formula section.
- Verify: budget at LB 5/8/10/15% (expect 4.68/7.32/9.01/12.97% with
  phi=0.0009), and re-derive the implied bonus for the same 20 liquidations
  from the new Params to show it matches. Also read treasury_factor per
  reserve from chain into config so the split is recorded, not assumed.
Plan first.
```

## 2. Coin-type keying and swap direction

```
Two identity bugs, fixed together because both are about which coin is which.
(a) Prices are keyed by on-chain metadata symbol (inconsistencies.md,
"Prices keyed by on-chain symbol"): USDC/wUSDC, USDT/suiUSDT and
WBTC.lz/Wormhole WBTC collide. Key every price by normalized coin type in
lrg/ (graph.derive_prices, curves._out_usd, matrix.build/demand/s_max,
analysis). Symbols become display labels only. Remove run.py's
alias_prices and the dual keying in oracle_prices once lrg/ no longer needs
them.
(b) The swap side for a leg comes from the CSV's coin_type_a
(matrix.py:396 leg_ids, graph.py:119), but _out_usd applies it to the
pool's on-chain coin_a. Where GeckoTerminal's order differs from the
pool's, the leg is priced backwards. Derive the side from the fetched
pool's coin_a vs the leg's input coin type.
Verify: (1) count CSV rows whose order differs from the on-chain order
(agent/scratch/diagnostics.py section D1 does this) and show them with TVL;
(2) for 3 reversed pools, show out(q) for a fixed size before and after;
(3) a run where every Navi asset has a price under its coin type and no
collisions are reported.
Plan first.
```

## 3. Pool universe: DeepBook, Aftermath, discovery

```
data/navi_pools.csv has no DeepBook or Aftermath rows, though navi.VENUES
lists both, so their adapters never run (review A1). Also discovery.py
raises NameError (VENUE_TYPE / TYPE_BY_ADAPTER undefined) and fetches only
2 pages per token, which truncates SUI and USDC (review C7).
- Fix discovery.py so it runs end to end, pages until exhausted (with a
  stated cap and a warning when hit), and records the date of discovery.
- Append DeepBook pools (discovery_extra / venues.deepbook_pools) and
  Aftermath pools above a TVL floor to the CSV, with coin types normalized.
- Before these venues enter routing, fix the two things that break once
  they do: (1) curves.LegCurve pick="best" and graph.derive_prices rank pools
  by .liquidity, whose units differ by venue; rank by USD out at a reference
  size (e.g. $10k) instead. (2) book.py caches by pool_id only, but
  Aftermath multi-coin pools share one id across pairs; key the cache by
  (pool_id, coin_in, coin_out) and match coin order exactly. (3) DeepBook
  _parse_book: sort bids descending and asks ascending before walking.
- Verify: rebuilt CSV row counts by venue vs the old CSV; DeepBook SUI/USDC
  appears on the SUI->USDC leg; for SUI->USDC show each pool's allocation at
  $1M with and without DeepBook.
Plan first, show the discovery diff before overwriting the CSV.
```

## 4. Turbos reader

```
11 Turbos pools fail with "no ticks in the first 100 dynamic fields".
Review finding A2: clmm.Q_FIELDS asks for dynamic-field values only as
"... on MoveValue { json }". If Turbos ticks are dynamic object fields, the
value comes back empty and nothing parses, whatever we select by.
- Diagnose on 0x0df4f02d0e210169cb6d5aabd03c3058328c06f2c4dbb0804faa041159c78443
  (USDC/SUI): run agent/scratch/diagnostics.py section D2, and show the
  value __typename and type for the pool's dynamic fields.
- If ticks are MoveObject: add "... on MoveObject { contents { json } }" to
  Q_FIELDS and read json from either branch. Then filter Turbos fields by the
  tick key type so we don't parse unrelated objects, and replace the
  "two empty pages" exit with one that only applies once the tick type is
  known to be absent.
- Then recheck the 2 "ticks parsed but no ranges built" pools (BUCK/USDC
  0x102d0804..., FDUSD/USDC 0x7db7a727...): empty pool, or sign bug?
- Verify: every Turbos pool in the CSV reads; clmm.sanity passes against
  the oracle price; in-range liquidity summed from ticks equals the pool's
  liquidity field; show the read count before and after.
Diagnose first, plan, then fix.
```

## 5. Empty pools

```
8 Cetus/Bluefin pools fail with "no ticks parsed — tick table not where
expected". Review C1: the tick UID lookup works on 50+ other pools of the
same venues, and all 8 have zero 24h volume, so they are probably empty.
- Run diagnostics.py section D3: liquidity field and tick table size for each.
- Where liquidity == 0 and the tick table is empty: record the pool as
  status "empty" with zero depth, not as failed. It must not mark routes
  unverified.
- Where liquidity > 0 and nothing parsed: it's a read bug; find and fix it.
- Change the error message so "tick table not where expected" is only used
  when the UID is actually missing.
Show the table before changing anything.
```

## 6. Read robustness

```
Make failed reads loud instead of silent (review C2, C3, C4, B3):
- clmm.gql: raise on a GraphQL "errors" field for pool, oracle and Storage
  reads, as positions._post already does, with retries. A timeout must
  never become "no ticks".
- clmm.fetch_ticks: the 80-page cap truncates silently and fields arrive in
  hash order. Raise (or mark the pool "truncated", treated as failed) when
  the cap is hit; log the largest tick count seen in the run.
- ClmmPool.out: cap output at the pool's reserve of the output coin.
- matrix.leg_ids: drop failed and empty pools before taking the top
  max_pools_per_leg, so a failed pool is replaced by the next one.
Verify with unit tests on mocked responses for each case, and one live run
showing pools read, failed (by reason), empty and truncated.
Plan first.
```

## 7. Curve interpolation

```
curves.LegCurve.out interpolates USD out linearly in log(size). Output is
convex in log size, so cost is understated by up to ~0.5 points per leg and
capacity is overstated: +2.7% on a single 0.25%-fee pool at an 8% budget,
+20% at a 2% budget, more on multi-hop routes (review A3).
- Interpolate cost (1 - out/in) in log size instead of out, and in
  route_capacity refine the final answer with exact allocate() calls on the
  legs (a few bisection steps on the exact composition), so the reported
  capacity's exact cost is within the budget.
- Flag capacity at GRID_HI as saturated ("at least $50M") everywhere it is
  reported, and stop linear extrapolation above GRID_HI.
- Verify: tests comparing grid vs exact capacity on (a) one constant-product
  pool at budgets 1/2/5/8.17/10%, (b) a two-hop route; all within 0.5%.
  Then the before/after change in Q* for the 10 largest pairs.
Plan first.
```

## 8. Path ranking

```
inconsistencies.md (2026-10-09) shows the top-20 cutoff drops the best path
in 17 pairs because candidates are ranked by CSV TVL of the deepest pool per
leg, while capacity depends on depth near the current price.
- Rank candidate paths by exact route output at a reference size (e.g.
  $10k, or 1% of the pair's demand) using the leg curves, then keep the top
  20 for the capacity search.
- Verify against the uncapped run: the 17 pairs listed in inconsistencies.md
  should now find their best path within the top 20, with runtime close to
  the capped run. Report any pair where uncapped still beats ranked.
Plan first.
```

## 9. Matrix completeness and borrowable debts

```
Three related gaps in which pairs exist (review B4, B7, and the
inconsistencies.md note that Q_star is 0 for every collateral, mostly
because of MBTC debt):
- Every Navi asset must be in the matrix. An asset with no pool gets
  capacity 0 for all its pairs (no route), never "absent", and those pairs
  must be able to fail in adequacy and stress.
- Read from chain whether each reserve can be borrowed (borrowing enabled,
  borrow cap > 0, not isolated or restricted). Exclude non-borrowable
  assets from the debt set used for row minima and for Q_cap; list them in
  config.json. Check MBTC, enzoBTC, XAUm and wUSDC specifically.
- One Q_cap definition: signal_1 and adequacy currently differ (signal_1
  has no debt-side bound). Use the debt-bounded per-pair form everywhere:
  Q_cap(B,A) = beta*CF*(1+b_B)*min(LTV_B*S_B*P_B, borrow_cap_ratio_A*
  S_A*P_A); document the debt bound in the README.
Verify: row minima before/after, naming the binding debt per collateral.
Plan first.
```

## 10. Coverage flag

```
The unverified flag (run.py write) only checks the winning path's token
pairs. A pair whose only route died because of a failed read gets capacity
0 and unverified=false, which is how the MBTC zeros looked trustworthy; a
better path removed by a failed read isn't flagged either; and matching by
token pair flags a failed 5th pool that was never used (review B2).
Replace it with a coverage status per pair:
- "complete": no failed or truncated pool among the top pools of any leg of
  any candidate path considered for the pair;
- "incomplete": otherwise, with the failed pool ids and their CSV TVL;
- zero-capacity pairs are "incomplete" whenever any failed pool touches
  either asset.
Empty pools (step 5) don't count as failures. Unverified/incomplete pairs
are listed separately in adequacy and stress and never reported as red or
amber. Add a run-level coverage share: TVL read / TVL needed.
Plan first.
```

## 11. Stress corrections

```
Bring run.py stress() in line with the methodology (review B1, B5, B6, D):
- Split each liquidatable position's debt across collaterals by borrowing
  power, c*P*LT (E-Mode LT for E-Mode positions), not by value. Fix the
  docstring.
- E-Mode positions: use the E-Mode liquidation bonus (already read in
  positions.py:203) for Q and for the budget behind Q*; signal 1 uses E-Mode
  LTV for E-Mode-eligible pairs.
- Protected positions: find out from Navi's code what "protected" means.
  If they cannot be liquidated, exclude them from D and report them as a
  separate column; otherwise document why they stay.
- An asset with no oracle price must not get price 0 (that hides
  liquidations): mark the position "unpriced" and report it.
- Apply beta to stress Q as adequacy does. Report positions liquidatable
  with zero collateral (already bad debt) instead of skipping them.
- Replace the 4-step shock grid with 5% steps to 50%, and add LST depeg
  scenarios: vSUI, haSUI, stSUI down 2/5/10% against SUI while SUI is flat.
Verify on a synthetic state with one single-collateral, one two-collateral
two-debt, one E-Mode and one protected position, against hand calculations,
then show the breaking shock per collateral before/after on the live run.
Plan first.
```

## 12. LST mint exit

```
inconsistencies.md finding 2: the oracle prices vSUI and haSUI at the SUI
price, while the market pays ~6-7% more. A liquidator repaying vSUI or haSUI
debt must buy it, so the DEX route costs ~7% before any slippage. But
anyone can stake SUI and mint vSUI/haSUI/stSUI atomically at the
protocol's exchange rate.
- Research first: for Volo (vSUI), Haedal (haSUI) and stSUI, is minting
  atomic and permissionless, what is the exchange rate source on chain, any
  fee or cap, and is redemption (LST->SUI) epoch-bound?
- If minting is atomic: add a one-directional "mint" venue SUI->LST at the
  on-chain exchange rate, with any cap as its depth, so routes into LST debt
  can use it. Never add LST->SUI redemption as an atomic exit.
- Verify: haSUI and vSUI debt cells that dropped to 0 in the 2026-10-15
  run, before/after.
Research and plan first. No code until I approve.
```

## 13. Rerun and recheck

```
Run run.py and the stress view (lrg_stress_view.py) with all fixes. Compare
with 2026-10-13 and 2026-10-15:
- pools read / failed / empty / truncated by venue, and coverage share;
- Q* per collateral and the 20 largest pair changes, attributing each to a
  step where possible (bonus split, venues, Turbos, interpolation, ranking);
- the stress pairs that failed before (SUI->USDC, MBTC->SUI,
  MBTC->WBTC.lz, MBTC->wBTC, xBTC->vSUI): new breaking shock, capacity,
  coverage status, and for MBTC the diagnostics.py D4 table.
Write the results to inconsistencies.md under a dated heading. No code
changes in this step.
```

## 14. Housekeeping

```
Update README.md to match the code (bonus split, coin-type keying, venues,
coverage status, stress method) and remove references to
examples/run_navi.py and docs/ or add them. Update agent/TASK.md so the
agent treats "incomplete" coverage as UNRESOLVED, never CONFIRMED. Tick
off agent/OPEN_ITEMS.md. Merge the branch after I review the diff.
```
