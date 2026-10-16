# Progress log: Navi liquidation-capacity pipeline

This log covers the work on `ipakresearch/LRG` since it was forked, from the first agent spec (2026-10-11) to the merge of `fixes/review-2026-10-15` into `main` (2026-10-16). For each step it gives what was changed and why, what the data showed, what went wrong and how it was fixed.

Detailed tables live in `inconsistencies.md` (one dated section per step). The original review is `agent/REVIEW_2026-10-15.md`, the step prompts are `agent/FIX_PROMPTS.md`, and the checklist is `agent/OPEN_ITEMS.md`.

---

## Contents

- [Where the repo started](#where-the-repo-started)
- [Phase 1: the daily pipeline (2026-10-09 to 2026-10-13)](#phase-1-the-daily-pipeline-2026-10-09-to-2026-10-13)
- [Phase 2: oracle prices (2026-10-15)](#phase-2-oracle-prices-2026-10-15)
- [Phase 3: the code review (2026-10-15)](#phase-3-the-code-review-2026-10-15)
- [Phase 4: the fixes, step by step](#phase-4-the-fixes-step-by-step)
  - [Step 0: setup](#step-0-setup)
  - [Step 1: liquidation bonus split](#step-1-liquidation-bonus-split)
  - [Step 2: coin-type keying and swap direction](#step-2-coin-type-keying-and-swap-direction)
  - [Step 3: pool universe (DeepBook, Aftermath, discovery)](#step-3-pool-universe-deepbook-aftermath-discovery)
  - [Step 4: Turbos reader](#step-4-turbos-reader)
  - [Step 5: empty pools](#step-5-empty-pools)
  - [Step 6: read robustness](#step-6-read-robustness)
  - [Step 7: curve interpolation](#step-7-curve-interpolation)
  - [Step 8: path ranking](#step-8-path-ranking)
  - [Step 9: matrix completeness, borrowable debts, one Q_cap](#step-9-matrix-completeness-borrowable-debts-one-q_cap)
  - [Step 10: coverage status](#step-10-coverage-status)
  - [Step 11: stress corrections](#step-11-stress-corrections)
  - [Step 12: LST mint exit](#step-12-lst-mint-exit)
  - [Step 13: rerun with all fixes](#step-13-rerun-with-all-fixes)
  - [Step 14: housekeeping and merge](#step-14-housekeeping-and-merge)
- [Main findings](#main-findings)
- [Data-fetch lessons](#data-fetch-lessons)
- [All problems hit and how each was solved](#all-problems-hit-and-how-each-was-solved)
- [Still open](#still-open)
- [Commit map](#commit-map)

---

## Where the repo started

When forked (commit `f98050b`, 2026-09-15), `lrg/` could already:
- read CLMM pools (Cetus, Bluefin, Momentum, Turbos) from Sui GraphQL;
- build per-leg cost curves and route capacity (`curves.py`);
- build a collateral × debt capacity matrix for Navi (`matrix.py`).

There was no daily run, no position reader, no stress test and no report spec. The README referred to `examples/run_navi.py` and `docs/`, which did not exist.

**Core idea.** For each pair (collateral B, debt A), the capacity is the largest size a liquidator can sell B into A along on-chain routes while the total cost (slippage + fees + flash-loan fee) stays inside the liquidation bonus. Q\* for a collateral is the minimum over its debt row. That is compared with the liquidations Navi could need: now, at the caps, and under price shocks.

---

## Phase 1: the daily pipeline (2026-10-09 to 2026-10-13)

Commits `bd988f7` (agent spec) and `7284f69` (WIP).

### What was built

- **`run.py`**, the daily run. It writes `data/navi/<date>/` containing:
  - `matrix.csv`: capacity per pair, with path
  - `summary.json`: Q\* per collateral and the binding debt
  - `adequacy.csv`: Q_now and Q_cap against capacity, with status red/amber/green
  - `stress.csv`: liquidations under price shocks
  - `coverage.json`: pools needed, read and failed
  - It also reads Navi's live parameters from the Storage object and flags drift from `lrg/protocols/navi.py`.
- **`positions.py`**, the borrower reader:
  - finds every borrower by paging Navi's 35 per-reserve borrow tables;
  - reads collateral from `user_info` and the supply tables. Amounts are `stored × index / 1e27` at 9 decimals.
  - Health factors are computed off-chain, because the stress test needs them at shocked prices.
  - They are checked against Navi's own view function, run through a simulated transaction (`logic_getter_unchecked::user_health_factor_batch`).
- **`agent/TASK.md`**, a spec for a future agent to write `report.md` from a run, using the two signals:
  - Signal 1: parameters vs routes, at the caps.
  - Signal 2: the current book under shocks.

### First findings (`inconsistencies.md`, 2026-10-09 to 2026-10-11)

- **`q_star` reused the original path at other bonus levels.**
  - Fixed: `_best_route` now picks the best candidate at each budget.
  - It changed no numbers on that snapshot.
- **The top-20 path cutoff dropped better paths.**
  - Without the cap, capacity rose for 17 of 506 pairs; the winners had sat at rank 21–41.
  - Cause: paths were ranked by the listed TVL of the deepest pool per leg, not by depth near the current price.
  - Running uncapped took 738 s against 86 s, so it was not a fix. Step 8 is the real one.
- **21 of 117 pools failed to read, silently:**
  - 12 Turbos pools had "no ticks in the first 100 dynamic fields" or "no ranges built";
  - 9 Cetus/Bluefin pools had "tick table not where expected".
  - A failed pool was just left off its leg, so capacity was understated without any flag. 13 of the 21 sat on winning routes.
- **Prices were keyed by on-chain symbol:**
  - USDC/wUSDC, USDT/suiUSDT and WBTC.lz/Wormhole WBTC shared prices.
  - Five Navi assets had no price under their Navi name.
  - `run.py` papered over this with `alias_prices`; step 2 is the real fix.

### Challenges in the positions reader

- **GraphQL limits:** pages of 50, a 5,000-byte payload cap that includes variables (about 25 keys per `multiGetDynamicFields`), and about 0.67 s per page. Reads were spread over 4 parallel streams in batches of 25.
- **The checked health-factor function aborts on stale prices** (abort 1502), so the unchecked getter is used.
- **The simulated transaction's argument shape** had to be found by trial: in gRPC-JSON, results are `{"kind":"RESULT","result":i,"subresult":j}`, not `NESTED_RESULT`; `checksEnabled: false`.
- **E-Mode:** an $11.8M vSUI/SUI position uses LT 0.93, so the off-chain HF must apply the E-Mode LT. With it, sampled health factors match Navi's own to 1e-9.

---

## Phase 2: oracle prices (2026-10-15)

### Why

Capacity in USD and the demand Q were priced off pool spot rates. Navi itself liquidates at its oracle price, so the gap between the oracle and the market is part of a liquidator's cost.

### What changed

- **`lrg/protocols/navi_oracle.py`** (new):
  - reads the PriceOracle table (38 entries);
  - maps each reserve to its entry via `oracle_id`;
  - decodes the price as `value / 10**decimal`, using **each entry's own decimal** (6, 8 and 9 all occur).
- **`matrix.build`** merges supplied prices over derived ones and records `price_source`.
- **`positions.py`** retries pages on partial GraphQL errors (`_post`, `PageError`).

### Checks

- **Decimals.** On 20 real liquidations, the implied bonus matched the configured LB to within 0.2 pp, so per-entry decimals are right.
- **Oracle vs pool-derived vs CoinGecko.** Within about 1.4% for everything except the LSTs:

  | LST | oracle vs market |
  |---|---|
  | vSUI | −5.9% |
  | haSUI | −7.2% |
  | stSUI | −2.8% |

  In other words, the oracle prices the LSTs at roughly the SUI price. MBTC's CoinGecko quote was stale (+53%).
- **Fallbacks.** MMT, MUSIC and USDB have no oracle entry and keep derived prices.
- **Effect.** Q\* was still 0 for every collateral (mostly from MBTC debt). haSUI as a debt dropped to 0 capacity for 6 collaterals: buying haSUI costs about 7% more than Navi credits.

### A wrong finding, later corrected

From the 20 events, I first concluded that the liquidator keeps the full bonus and the treasury is paid on top. That was backwards. The event's `collateral_amount` is **gross** (liquidator + treasury). See step 1.

---

## Phase 3: the code review (2026-10-15)

`agent/REVIEW_2026-10-15.md` reviewed the code against the methodology.

Verdict: the maths was sound, verified by tests:
- CLMM tick walk, Aftermath invariant, DeepBook walk;
- route composition, split allocation, budget formula;
- health factor, shocked prices.

Five problems changed headline numbers:

| # | Problem | Effect on capacity |
|---|---|---|
| A1 | No DeepBook/Aftermath rows in the pool CSV | understated |
| A2 | Turbos reads failing | understated |
| A3 | USD-out interpolation | overstated |
| A4 | Swap side taken from the CSV's coin order | direction unknown |
| A5 | Hardcoded 10% platform share | changes every budget |

It also listed:
- method gaps B1–B7: stress split, the unverified flag, leg slots, missing assets, E-Mode, protected users, two Q_caps;
- read issues C1–C9;
- small items D.

`agent/FIX_PROMPTS.md` turned it into 15 ordered steps:
- 1–2 change what every number is measured against;
- 3–6 change which pools exist and how they read;
- 7–9 change how capacity is computed;
- 10–12 change the stress test and the routes into LSTs;
- 13 is the rerun.

**How each step was run:**
1. Plan first, wherever the prompt asked. Make no edits until approved.
2. Implement, then verify against the step's own check.
3. Write the results into `inconsistencies.md`.
4. Commit, then tick `agent/OPEN_ITEMS.md`.

---

## Phase 4: the fixes, step by step

### Step 0: setup

- Created branch `fixes/review-2026-10-15`.
- Committed the oracle and position-retry work (`09152df`).
- Added the 14 review items to `agent/OPEN_ITEMS.md`.

### Step 1: liquidation bonus split

**Prompt premise.** Finding 1 said the liquidator keeps the full bonus, so the budget should be `1−(1+φ)/(1+LB)`.

**What I did instead, and why.** Before changing the formula, I read Navi's bytecode (package `0x1e4a13a0…59cb`).

`logic::calculate_liquidation` works like this:
- base = min(amount offered, CF × the borrower's collateral value in B, loan value)
- bonus = base × LB (the E-Mode LB for E-Mode users)
- treasury = bonus × `treasury_factor` **of the collateral reserve**
- the liquidator gets base + bonus − treasury

`lending::base_liquidation_call` deposits the treasury cut into the pool, so it is never sold. It then emits `collateral_amount = to_liquidator + treasury`.

So finding 1 was reversed. The real treasury cut is not 10% but 55–85% of the bonus, varying by reserve. The liquidator's bonus is `b = (1 − tf)·LB`.

**Changes:**
- `Params.platform_share` → `treasury_factors` per collateral, with an optional `treasury_share` fallback.
- `budget(lb, tf)` and `seized_factor(lb, tf)`.
- `navi.TREASURY_FACTORS` is a static fallback. The live value is read per reserve in `run.fetch_navi_live` and `positions.read_state`, and drift is flagged.

**Verification:**
- On a new window of 20 liquidations, all 20 have gross bonus = LB and net bonus = LB·(1−tf), to within 0.2 pp.
- Budgets match hand values.

**Effect.** Budgets fell to about half (example: SUI 8.17% → 3.29%):

| collateral | LB | tf | budget now |
|---|---|---|---|
| SUI, vSUI, haSUI | 10% | 0.65 | 3.29% |
| stSUI, SOL, DEEP, WAL, … | 10% | 0.55 | 4.22% |
| USDC, suiUSDT, USDSUI, USDY | 8% | 0.55 | 3.39% |
| WBTC.lz, xBTC | 15% | 0.80 | 2.83% |
| LBTC, wBTC, suiETH | 15% | 0.85 | 2.11% |

**Also found:** Navi caps the repayable base at CF × **collateral** value, not CF × debt. The model applied it to debt. This was recorded here and applied in step 11.

**Corrections made:** `inconsistencies.md` finding 1 and the memory note were both rewritten.

### Step 2: coin-type keying and swap direction

**Why.** There were two identity bugs, both about which coin is which:
- (a) prices keyed by symbol collided;
- (b) the swap side came from GeckoTerminal's coin order in the CSV but was applied to the pool's on-chain `coin_a`.

**Changes:**
- `lrg/coins.py` (new): `norm_type` and the native-USDC anchor.
- Every price is keyed by normalized coin type: `derive_prices`, `_out_usd`, `matrix.build`, `graph.build`.
- `Matrix.tokens` maps symbol → type, and `Matrix.price(symbol)` does the lookup. Symbols are display labels only.
- The side comes from the fetched pool (`curves.side_of`). A pool holding neither coin is skipped and listed.
- `run.py`'s `alias_prices` and dual-keyed `oracle_prices` were removed.

**Findings:**
- 18 routed rows had reversed coin order, about $5.3M of TVL. Examples: Turbos USDC/SUI, Cetus USDSUI/USDC, Momentum MBTC/wBTC.
- On Cetus USDSUI→USDC at $1M, each direction had been priced with the other direction's depth (45.0% vs 46.2% cost).
- **New finding: CSV TVL is stale.** Momentum MBTC/wBTC was listed at $593k and holds about $12. Cetus suiUSDe/USDC was listed at $501k and holds $3.2k. Since pool selection used the CSV's TVL, empty pools could take slots from live ones. This was fed into steps 3, 6 and 8.

**Effect.** With step 1's halved budgets, Q\* (excluding zeros) fell 22–87%. vSUI, haSUI and stSUI became zero-capacity debts for many collaterals.

### Step 3: pool universe (DeepBook, Aftermath, discovery)

**Why.**
- `navi_pools.csv` had no DeepBook or Aftermath rows, so those adapters never ran.
- `discovery.py` raised `NameError` and fetched only 2 pages per token.

**Changes:**
- **`discovery.py` runs end to end:**
  - pages up to 10 (GeckoTerminal's cap), warning per token when the cap is hit;
  - an exhausted rate-limit retry now raises, where it used to quietly end the list;
  - records `discovered_at`.
- **On-chain TVL (added at your request):**
  - every routed pool's TVL is recomputed from on-chain reserves at oracle prices;
  - GeckoTerminal's figure is kept as `tvl_gecko`, with `tvl_source` saying which applies;
  - pools under $5k are dropped.
- **DeepBook and Aftermath rows** come from `venues.discovery_rows()`.
- **Venue-neutral ranking:** pools are ranked by USD out at $10k, not `.liquidity`, whose units differ per venue.
- **Pool identity:** the cache key is `pool_key = (id, sorted coin pair)`, because Aftermath multi-coin pools share one id across pairs.
- **DeepBook book:** sorted before walking.
- `discovery_extra.py` deleted.

**Data findings:**
- The CSV went from 179 to 272 rows; 112 were added and 33 dropped.
- GeckoTerminal lists 25 "pools" above $1M that hold almost nothing. For example, xSKHY/USDC is listed at $9.4B and holds $70. Paging deeper would have put them at the top of legs; the on-chain floor removes them.
- DeepBook SUI/USDC cut the SUI→USDC cost at $1M from 6.60% to 4.45%. The matrix still missed it, because it ranked 6th by TVL. Steps 6 and 8 fixed that.
- **MBTC dropped out of the matrix entirely.** Its only pool fell below the floor, and `build()` treated an asset with no pool as absent, which looked better than "zero". Flagged for step 9.

**Problems hit:**
- **DeepBook out of gas.** The indexer ran out of gas on the SUI_USDC book at 200 levels. The reader now steps down 200 → 100 → 50 and records the levels it got.
- **Silent truncation.** Rate limits could truncate discovery without warning; `get` now raises.
- **My own check script** passed positional arguments to `book.get`; fixed to keywords.
- **MBTC** was skipped at your direction ("skip the MBTC").

### Step 4: Turbos reader

**The review's guess was wrong.** The prompt assumed Turbos ticks were dynamic *object* fields. Diagnosis on USDC/SUI showed otherwise:
- The ticks are ordinary `MoveValue` fields (`pool::Tick`, key `i32::I32`).
- But the pool's UID also holds every LP `Position`.
- The first 20,000 fields (400 pages, 6.5 minutes) were all positions. A full walk is impractical.

**Fix.** Turbos keeps a bitmap, `tick_map: Table<I32, u256>`. Bit b of word w marks tick (w·256 + b)·spacing. `clmm.fetch_ticks_bitmap`:
1. pages the bitmap;
2. decodes the tick indices;
3. fetches each tick by key with `multiGetDynamicFields`, in batches of 25;
4. raises if any marked tick fails to read.

The I32 key type is taken from the data, not hardcoded. `PACKAGES` now labels Turbos and Momentum.

**Verification:**
- 35 of 35 Turbos pools read; all had failed before.
- Summed `liquidity_net` at or below the current tick equals the pool's `liquidity` field exactly.
  - My first DEEP/USDC check showed a mismatch. It was float error in my comparison against `sqrtP`; against the tick index it matches exactly.
- The two "no ranges built" pools were empty, not a sign bug.
- The oracle sanity check fails only on vSUI/SUI and haSUI/SUI, which is the known LST oracle gap.

### Step 5: empty pools

**Diagnosis.** The 8 Cetus/Bluefin "tick table not where expected" pools all had liquidity 0 and an empty tick table. The table *was* found, so the error message was wrong.

**Changes:**
- New `clmm.EmptyPool`, recorded in `PoolBook.empty` with zero depth. Empty pools never count as failures and never mark a route unverified.
- Error messages now separate three cases: no tick table found; a populated table that parses to nothing (a reader bug); and an empty table with liquidity > 0.

**Verification.** On a two-pool test leg (one empty, one live FDUSD/USDC), the pair is priced from the live pool, with read 1, failed 0, empty 1.

### Step 6: read robustness

**Why.** Failed reads were silent:
- a GraphQL timeout became "no ticks";
- tick paging stopped quietly at 80 pages;
- a mis-parsed table could output more than the pool holds;
- a failed pool kept its slot on the leg.

**Changes:**
- **Strict `clmm.gql`:** any `errors` entry fails the call after 4 attempts with backoff, even with partial data. It covers every pool, oracle, Storage and discovery read.
- **`fetch_ticks`:**
  - a missing page raises;
  - hitting the 200-page cap raises `TruncatedRead`;
  - the field count must equal the table's own `size`.
- **`clmm.fetch`** re-reads up to 3 times on `TruncatedRead`.
- **`ClmmPool.out`** is capped at the output-coin reserve.
- **`graph.read_legs`** reads down each leg's ranking until 4 pools are readable, so failed and empty pools take no slot.
- **`tests/test_read_robustness.py`:** 13 mocked cases, all passing.

**Problem found by the new checks.** In the live run, Cetus USDC/SUI read "700 of 702 fields". Read again, it had 705 entries and read cleanly. The table changed during the read, because pages are served at the latest checkpoint. The size check caught an inconsistent snapshot, which is why the re-read was added.

**Result.** 129 of 130 pools read; the one truncated pool was replaced on its leg. DeepBook was still not on the SUI→USDC leg (6th by TVL), which led into step 8.

### Step 7: curve interpolation

**Why.** Interpolating USD out linearly in log size understates cost, because output is convex in log size. That overstates capacity, and the error is worst at small budgets, which is where step 1 put Navi.

**Changes:**
- `LegCurve.out` interpolates **cost** in log size, and computes exactly above the grid instead of extrapolating.
- `route_capacity` takes the grid answer, then bisects 20 steps on the exact composition, where each leg's `allocate()` feeds the next.
- `_best_route` refines every path within 10% of the grid best.
- Capacity at `GRID_HI` ($50M) is flagged "saturated" (≥$50M) in `matrix.csv`, `stress.csv` and `summary.json`.
- `ClmmPool.out` stops re-sorting ranges on every call, which keeps the exact refinement cheap.

**Tests** on constant-product pools compare the old and refined methods against exact capacity:

| case | old error | refined error |
|---|---|---|
| one pool, 1% budget | +52.7% | 0.000% |
| one pool, 8.17% budget | +2.4% | 0.000% |
| two hops, 1% budget | +56.8% | 0.000% |
| two hops, 10% budget | +8.0% | 0.000% |

**Live effect:**
- The median pair is unchanged, the 10th percentile is −29%, and the range is −90% to +10%.
- 8 stSUI-debt pairs went from positive (up to $13k) to exactly 0.
- The 10 largest pairs moved −4% to +7%.

**Problem hit.** The emulated old method returned $50 instead of 0 for unreachable pairs (the bottom of its grid), so I treated ≤$50 as 0 in the comparison.

### Step 8: path ranking

**Why.** Paths were pre-ranked by listed TVL, which missed real winners (2026-10-09), and the 4 pools per leg were also chosen by listed TVL.

**Changes:**
- **Pools per leg:** `read_legs` reads every pool on every used leg, and `choose_pools` keeps the 4 with the most USD out at $10k in that direction.
- **Paths:** all paths up to 4 hops are enumerated (22,853). `rank_paths` scores each by grid route cost at $1k, $10k and $250k, and keeps the union of the cheapest 6 at each size (at most 18).
- The bottleneck-TVL pre-ranking is removed.

**Verification against the uncapped search** (1,005–1,119 s vs 38 s ranked):
- The two-size ranking missed stSUI→vSUI, which sets stSUI's Q\*. The three-size ranking keeps it, so it was adopted.
- The one remaining miss is vSUI→wBTC at −9%, far above vSUI's row minimum. No Q\* is affected.
- All 17 pairs from 2026-10-09 now find their best path.
- DeepBook is now first on the SUI→USDC leg.

### Step 9: matrix completeness, borrowable debts, one Q_cap

**Why:**
- An asset with no pool vanished from the matrix (MBTC after step 3).
- Non-borrowable assets set row minima.
- Signal 1 and adequacy used two different Q_caps.

**Chain facts.** `borrow_cap_ceiling` = 0 for wUSDC, XAUm, enzoBTC and MBTC. No reserve is isolated.
- wUSDC still carries about $2.1M of user debt, and XAUm about $1.2k.
- I kept those two in the debt set, unlike the prompt, because a liquidation of existing debt still has to buy the asset.
- MBTC and enzoBTC (no debt) are excluded.

**Changes:**
- Every Navi asset is in the matrix; one with no pool gets 0 everywhere (`Matrix.no_pool`).
- `Params.excluded_debts` and `Matrix.debt_row` drop excluded debts from row minima, Q\*, adequacy and signal 1. Their cells stay in `matrix.csv`.
- New `config.json` lists both sets with reasons, plus params and the formula.
- One Q_cap, in `run.pair_demand`:
  `Q_cap(B,A) = β·CF·(1+b_B)·min(LTV_B·S_B·P_B, max(borrowed_A, cap_A·S_A)·P_A)`

**Result:**
- Q\* is still 0 for every collateral except the three LSTs. The binding debt is now haSUI, not MBTC.
- MBTC is back as a row and column of zeros, red, failing signal 1.

### Step 10: coverage status

**Why.** The old `unverified` flag checked only the winning path's token pairs:
- A pair whose only route died from a failed read got capacity 0 and looked trustworthy. This is how the MBTC zeros had looked.
- A failed pool that was never used could still set the flag.

**Changes:**
- **Coverage per pair:** `matrix._coverage` marks each pair `complete` or `incomplete`, listing the failed pools with TVL.
- **When a failed pool counts on a leg:** the leg has fewer than 4 readable pools, or the failed pool is at least as large as the weakest chosen pool.
- **A pair is incomplete when** any of these holds:
  - a counting failure is on the winning path;
  - it is on another kept path that could overtake the winner;
  - an enumerated path went unpriced on a leg with a failure;
  - capacity is 0 and a failed pool holds either coin.
- **Outputs:**
  - `adequacy.csv` gives incomplete pairs status `incomplete`, never red or amber, with `status_if_complete` kept.
  - Signal 1 is `unresolved` when it fails only on incomplete pairs.
  - Stress reports `breaking_shock_incomplete` separately.
  - Run-level `coverage_share` = on-chain TVL read ÷ TVL needed.
- **Tests:** 8 cases, all passing.

**Problem hit, and the fix.** I forced Turbos USDC/SUI to fail as a test, and 455 of 506 pairs became incomplete. Almost every pair keeps *some* candidate path through SUI→USDC, so the flag meant nothing. I added the **overtaking test**: a failed pool of TVL T on a leg holding L can raise a path's capacity by at most (1 + T/L). A losing path only flags the pair if that bound would let it beat the winner. With the test, 255 pairs were flagged, 197 of them because their winning path really crosses that leg.

**Also hit:** the test file first used non-normalized coin types and failed; fixed by normalizing.

### Step 11: stress corrections

**Changes (`run.position_flows`, `run.stress`):**
- **Debt split:** each liquidatable position's debt is split across its collateral by **borrowing power** c·P·LT (E-Mode LT where it applies), not by value.
- **Repayment cap:** repayment per pair is `min(D, CF·value_B·d_A/Σd)`. This is Navi's real cap on collateral value, from step 1.
- **E-Mode:** E-Mode positions use the E-Mode bonus. Their tranche is checked against `Matrix.pair_capacity(B, A, LB_e)`, a new method giving one pair's capacity at another bonus. Signal 1 also checks E-Mode-eligible pairs at the E-Mode LTV and bonus.
- **β** is applied to stress Q.
- **Unpriced positions** are reported, not given price 0, which would hide liquidations.
- **Bad debt** (liquidatable with zero collateral) is reported.
- **Shock grid:** 5% steps to 50%, plus LST depegs (vSUI, haSUI and stSUI down 2/5/10% against a flat SUI).
- **Protected users.** From the bytecode, `storage::is_liquidatable` lets only a liquidator designated for that user (`DESIGNATED_LIQUIDATORS_KEY`) liquidate a protected user. All 6 protected users have one, so they can be liquidated and stay in D, reported as `D_protected_usd`.
- **Tests:** a synthetic state checked against hand calculations, covering single-collateral, two-collateral/two-debt, E-Mode, protected, unpriced and zero-collateral positions. 29 tests pass.

**Problems hit:**
- A mock's `one_way` attribute broke `side_of`, which now checks `isinstance(str)`.
- A float equality assert failed; it became a relative check.

**Live result:**
- Breaking shock is 0% for 18 collaterals, as counted then. That run wasn't kept; the step-13 run has 16 (see step 13).
- **838 positions are already below HF 1**, and their vSUI/haSUI debt meets pairs with zero capacity.
- Bad debt at baseline is $68.

### Step 12: LST mint exit

**Research (done before any code, as asked).** I simulated a mint of 1,000 SUI and an immediate redeem in one transaction, from an arbitrary address:

| LST | mint call | SUI per LST | fee | instant redeem | vs Navi oracle |
|---|---|---|---|---|---|
| vSUI | Volo `stake_pool::stake` | 1.0689 | 0 | `unstake`, 3 bp | +7.0% |
| haSUI | Haedal `staking::request_stake_coin` | 1.0829 | 0 | `request_unstake_instant_coin`, 2 bp | +8.5% |
| stSUI | AlphaFi `liquid_staking::mint` | 1.0398 | 0 | `redeem`, 1 bp | +3.6% |

- Minting is atomic and permissionless, with no cap found, and nothing is paused.
  - Volo's `paused: true` was inside `validator_pool`; the top-level flag is false.
- The mint rate equals the market rate.
- Redemption is also atomic. **By your decision it is not modelled as an exit.**

**Changes (approved: "approve the mint venue, keep the no-redemption rule"):**
- `venues.MintPool`: SUI→LST at the live simulated rate, with unlimited depth, one way only.
- `curves.side_of` raises `OneWay` for the reverse direction, and `leg_pools` skips it.
- Three `lst-mint` rows were added to the CSV.
- `tests/test_mint.py`: 33 tests pass.

**Result:**
- haSUI→vSUI and haSUI→stSUI went from about $13k to about $2.2M, via haSUI→SUI→mint. The oracle also undervalues the haSUI collateral, which offsets the mint premium.
- haSUI debt stays 0 everywhere: its 8.5% premium is above every budget (2.1–4.2%).
- **Conclusion:** the zero LST-debt cells are real. They come from Navi's oracle pricing LSTs below their own mint/redeem rate, not from missing routes.

**Problem hit:** the simulation needed the RESULT/subresult argument shape (see Phase 1).

### Step 13: rerun with all fixes

Run `data/navi/2026-10-16`, with no code changes. `lrg_stress_view.py` named in the prompt does not exist in the repo, so only `run.py` was run.

| run | needed | read | failed | coverage |
|---|---|---|---|---|
| 2026-10-13 | 117 | 96 | 21 | not measured |
| 2026-10-16 | 149 | 149 | 0 | 100%, 0 incomplete pairs |

**Q\*:**
- 0 for 19 of 22 collaterals, all bound by haSUI debt.
- haSUI 7,105, vSUI 6,470, stSUI 1,118.
- Every collateral is red and fails signal 1.

**Largest changes and their causes:**
- SUI→USDC rose from $636k to $2.2M, through DeepBook (step 3), the Turbos reads (step 4) and pool choice by output (step 8). That outweighed the budget falling from 8.17% to 3.29% (step 1).
- haSUI→stSUI and haSUI→vSUI rose about ×170, from the mint venue (step 12).
- SUI→haSUI went from $1.37M to 0, because the budget is now below haSUI's oracle premium.
- DEEP routes fell about 70%, from the budget (step 1) and exact curves (step 7).

**Artifacts.** The $76–$88 row minima (BUCK, FDUSD, suiUSDe → vSUI; LBTC, suiETH → stSUI) run through a small WAL cross-pool price gap. In substance they are zero.

**Pairs that had failed stress before:**
- SUI→USDC no longer breaks up to a 50% shock.
- MBTC pairs break at 40%.
- xBTC→vSUI breaks at 0%.

**MBTC (D4).** Its only pool is Momentum wBTC/MBTC, with liquidity 0 and about $12 of balances. The price is within 1.2% of the oracle. So the zero is real: MBTC is collateral only (200 MBTC, about $16M) with no on-chain market to sell into.

### Step 14: housekeeping and merge

- **README.md** now matches the code: bonus split, coin-type keying, venues including `lst-mint`, coverage status, stress method. The dead references to `examples/run_navi.py` and `docs/` are removed.
  - Problem hit: my scripted edit failed once because the slice had already removed the Methodology section; I reran it without that step.
- **`agent/TASK.md`** now says incomplete coverage is always UNRESOLVED, and describes the new signals and outputs.
- **`agent/OPEN_ITEMS.md`** is ticked; items 6 and 7 stay open.
- **`lrg/__init__.py`** docstring updated.
- After your review, merged into `main` as `0116397`.
- **Push to origin failed:** this machine has no GitHub credentials ("could not read Username"), and `gh` is not installed. `main` is 19 commits ahead of `origin/main`.

---

## Main findings

1. **Navi keeps most of the liquidation bonus.** The treasury takes 55–85% of it, by collateral. Liquidators net 2.25–4.5%, so cost budgets are 2.1–4.2%, not 8–12%.
2. **Navi's oracle prices vSUI, haSUI and stSUI at about the SUI price,** 7.0%, 8.5% and 3.6% below what they mint and redeem for.
   - LST *debt* can't be liquidated at a profit at any size: buying the LST costs more than the bonus, whether by DEX or by mint.
   - LST *collateral* is undervalued, which makes selling it look cheap.
   - This one issue sets Q\* = 0 for 19 of 22 collaterals.
3. **838 positions are already below HF 1,** mostly with LST debt that no liquidator can profitably clear. That is why breaking shocks are 0%.
4. **MBTC has no market.** About $16M of collateral, and its only pool holds about $12. If liquidated, there is nowhere to sell.
5. **SUI→USDC is deep** (about $2.2M at a 3.29% budget) once DeepBook and Turbos are read and pools are chosen by output.
6. **The listed TVL can't be trusted.** GeckoTerminal lists pools at up to $9.4B that hold $70, and stale figures (MBTC/wBTC listed at $593k, holding $12). Only on-chain reserves are used now.

---

## Data-fetch lessons

| Issue | What happened | Fix |
|---|---|---|
| Partial GraphQL errors | data came back with an `errors` entry and was used anyway; a timeout became "no ticks" | strict `gql`: any error raises after retries |
| Page caps | tick paging stopped quietly at 80 pages; discovery at 2 pages | raise on cap; `TruncatedRead`; discovery to 10 pages with a warning |
| Live tables change mid-read | read 700 of 702 fields | compare with the table's `size`; re-read up to 3 times |
| Turbos tick storage | ticks buried among 20k+ position objects | read via the `tick_map` bitmap, then fetch ticks by key |
| Empty pools | read as layout failures | `EmptyPool`: zero depth, not failed |
| DeepBook gas | the indexer ran out of gas at 200 book levels | step down 200 → 100 → 50 |
| Rate limits (GeckoTerminal, CoinGecko) | could silently end a list | raise when retries run out; space requests |
| Coin order | GeckoTerminal's order differs from on-chain on 18 routed pools | take the side from the fetched pool |
| Symbols | collide and differ from Navi's names | key everything by normalized coin type |
| Oracle decimals | 6, 8 and 9 all occur | decode each entry with its own decimal |
| Listed TVL | fake and stale listings | on-chain reserves at oracle prices, with a $5k floor |
| Payload limit | 5,000 bytes including variables | batches of 25 keys |

---

## All problems hit and how each was solved

| Problem | Fix |
|---|---|
| Finding 1 (treasury paid on top of the bonus) was backwards | read the bytecode; corrected in `inconsistencies.md` and memory |
| Review A2 guessed Turbos ticks were object fields | diagnosed live: plain fields among positions; bitmap reader |
| DeepBook SUI_USDC out of gas | stepped book depth |
| Discovery silently truncated by rate limits | `get` raises |
| Check script passed positional args to `book.get` | keyword args |
| DEEP/USDC liquidity "mismatch" | float error in my check; exact against the tick index |
| Table truncated 700/702 during a read | size check plus re-read |
| Tests broke when `read_legs` changed signature | tests updated |
| Coverage test used raw coin types | normalized |
| Coverage flagged 455/506 pairs | overtaking test: 255 |
| Old-method emulator returned $50 for no route | treat ≤$50 as 0 |
| Simulated transaction rejected | RESULT with subresult, `checksEnabled: false` |
| Mock `one_way` broke `side_of` | `isinstance(str)` check |
| Float assert failed | relative tolerance |
| README edit script failed | reran without the already-done step |
| `lrg_stress_view.py` missing | ran `run.py` only, noted in step 13 |
| `git push` failed (no credentials, no `gh`) | not solved; you need to push |

---

## Still open

- **Item 6, coverage gate.** Coverage status and share exist, but no threshold withholds a run.
- **Item 7, validation against realized liquidations.** The bonus split is checked on 40 events; capacity itself is not yet validated.
- **LST→SUI redemption** is atomic (1–3 bp) but deliberately not modelled.
- **Q\* is measured before the shock,** so breaking shocks are upper bounds.
- **Push `main` to origin:** `! git push origin main`, with a personal access token or SSH.

---

## Commit map

| commit | step |
|---|---|
| `bd988f7` | agent task spec |
| `7284f69` | WIP: `run.py`, `positions.py`, runs 10-09 to 10-13 |
| `09152df` | 0: oracle prices, position retries |
| `b79f950` | 1: bonus split |
| `18a8766` | 2: coin-type keying, swap side |
| `08da99c` | 3: pool universe |
| `4a10a5c` | 4: Turbos bitmap |
| `5a965ce` | 5: empty pools |
| `42eecec` | 6: read robustness |
| `1d6cff2` | 7: curves |
| `ec190ae` | 8: path ranking |
| `4717ad9` | 9: matrix completeness, debt set, Q_cap |
| `0212dd5` | 10: coverage status |
| `6271b25` | 11: stress |
| `5d2c9c4`, `e0c1092` | 12: LST mint venue |
| `be1a8a7` | 13: rerun |
| `e1e0de7` | 14: docs |
| `0116397` | merge into `main` |
