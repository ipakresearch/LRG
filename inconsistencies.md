# Inconsistencies

Things noticed while reading the capacity-matrix code (`lrg/matrix.py`, `lrg/curves.py`, `lrg/graph.py`) and the README.

- **`q_star` reuses the original path at other bonus levels.** When `Matrix.q_star(B, lb)` is evaluated at a different bonus, it reuses the path chosen at the asset's original LB. At another budget a different candidate path might have done better, so the LB→Q\* curves (and `s_max` / `feasible`, which build on them) are a lower bound.
- **Path ranking and leg pricing look at different pools.** The bottleneck ranking used to pick the top 20 candidate paths only looks at the deepest pool on each leg. The split cost curves then use up to 4 pools per leg (`max_pools_per_leg`).
- **README references missing files.** The README mentions `examples/run_navi.py` and `docs/`, but neither is in the repo.

---

# Results (2026-10-09)

## Code changes (uncommitted)

- **`lrg/matrix.py`**
  - `build()` now keeps the cost curves of every priced candidate path in a new `Matrix.candidates` field.
  - A shared `_best_route(cands, tau)` picks the best route; `build()` and `q_star` both use it.
  - `q_star(B, lb)` now takes the best path for each pair at the new budget, and counts unreachable pairs as 0, as `row_min` does.
  - `curves` and `paths` still hold the route chosen at the asset's own LB.
- **`lrg/analysis.py`**: `LoadedMatrix.demand` now uses `seized_factor(LB)`, matching `Matrix.demand`, so Q for a reloaded matrix excludes the protocol's share of the bonus. Previously it used `(1 + LB)`.

## Inconsistency 1: `q_star` at other bonus levels — fixed

- **Check:** for all 22 collaterals, `q_star(B)` at the asset's own LB equals `row_min(B)` exactly.
- **Before/after:** the bonus curves (LB 2–20%) are identical on this snapshot. The winning route never changes as the bonus varies, and no pair is unreachable. The fix matters on other pool states, but changes no numbers today.

## Inconsistency 2: top-20 cutoff — tested

Without the cutoff there are about 21,500 paths in total (median 41 per pair, max 135); the cutoff drops paths in 389 of 506 pairs. Both runs used the same live pool state from a shared cache (117 pools needed, 96 readable).

Removing the cutoff raised capacity for **17 of 506 pairs**, lowered none, and changed no Q\* value.

| collateral | debt | capacity, top 20 | capacity, all paths | change | path, top 20 | path, all paths | winner's rank / paths |
|---|---|---|---|---|---|---|---|
| LBTC | stSUI | $0 | $8.5k | new | LBTC→SUI→stSUI | LBTC→WBTC.lz→xBTC→USDC→stSUI | #38 / 57 |
| WBTC.lz | stSUI | $0 | $8.5k | new | WBTC.lz→SUI→stSUI | WBTC.lz→wBTC→xBTC→USDC→stSUI | #41 / 63 |
| xBTC | stSUI | $0 | $8.4k | new | xBTC→SUI→stSUI | xBTC→wBTC→WBTC.lz→USDC→stSUI | #28 / 54 |
| USDC | stSUI | $0 | $4.4k | new | USDC→SUI→stSUI | USDC→stSUI | #27 / 41 |
| SUI | SOL | $993 | $5.9k | +498% | SUI→USDSUI→USDC→SOL | SUI→SOL | #29 / 41 |
| SOL | BUCK | $1.1k | $6.4k | +483% | SOL→USDC→BUCK | SOL→SUI→USDSUI→USDC→BUCK | #28 / 47 |
| SOL | WBTC.lz | $1.1k | $6.3k | +479% | SOL→USDC→xBTC→wBTC→WBTC.lz | SOL→SUI→USDSUI→USDC→WBTC.lz | #29 / 63 |
| SOL | SUI | $1.1k | $5.8k | +434% | SOL→USDC→USDSUI→SUI | SOL→SUI | #29 / 41 |
| xBTC | SOL | $1.7k | $8.8k | +428% | xBTC→wBTC→WBTC.lz→USDC→SOL | xBTC→USDC→USDSUI→SUI→SOL | #26 / 54 |
| WBTC.lz | SOL | $1.7k | $8.8k | +426% | WBTC.lz→wBTC→xBTC→USDC→SOL | WBTC.lz→xBTC→USDC→SUI→SOL | #27 / 63 |
| wBTC | SOL | $1.7k | $8.8k | +424% | wBTC→WBTC.lz→xBTC→USDC→SOL | wBTC→xBTC→USDC→SUI→SOL | #34 / 50 |
| SOL | LBTC | $1.2k | $6.3k | +421% | SOL→USDC→xBTC→WBTC.lz→LBTC | SOL→SUI→USDC→WBTC.lz→LBTC | #25 / 57 |
| SOL | xBTC | $1.2k | $6.3k | +421% | SOL→USDC→WBTC.lz→wBTC→xBTC | SOL→SUI→USDC→WBTC.lz→xBTC | #25 / 54 |
| LBTC | SOL | $1.7k | $8.7k | +418% | LBTC→WBTC.lz→xBTC→USDC→SOL | LBTC→WBTC.lz→USDC→SUI→SOL | #29 / 57 |
| SOL | wBTC | $1.2k | $6.2k | +411% | SOL→USDC→WBTC.lz→xBTC→wBTC | SOL→SUI→USDC→WBTC.lz→wBTC | #33 / 50 |
| BUCK | SOL | $987 | $4.9k | +400% | BUCK→SUI→USDSUI→USDC→SOL | BUCK→SUI→SOL | #21 / 47 |
| wBTC | stSUI | $8.4k | $8.5k | +1% | wBTC→xBTC→USDC→stSUI | wBTC→WBTC.lz→xBTC→USDC→stSUI | #24 / 50 |

**Q\* per collateral did not change.** The four rows that touch stSUI (USDC, xBTC, LBTC, WBTC.lz) still have a minimum of $0. Only the debt asset named as setting that minimum changed, from stSUI to MBTC, because both are at $0.

### What this says

- **The ranking is wrong, but the error is small in dollars.** Better routes sat at rank 21–41, so the cutoff is a real flaw. Most of the 16 big jumps add a SOL→SUI or stSUI hop.
- **Direct one-hop pools ranked low.** SUI→SOL ranked #29 and USDC→stSUI #27, yet each was the best route for its pair. A likely (unverified) cause: the ranking uses the CSV's TVL, while capacity depends on liquidity near the current price.
- **Cost of dropping the cutoff:** the uncapped run took 738s against 86s. A cheaper alternative, not yet tested, is to rank by the cost curve at a reference size and keep 20 paths.

### Caveats

- 21 of the 117 pools failed to read in both runs (tick tables not found or not parsed). This affects absolute numbers but not the comparison.
- Live pool state as of 2026-10-09, `CF=0.35`, `β=1.0`, platform share 10%, `pick="split"`.

---

# Failed pool reads (2026-10-09)

21 of the 117 pools that the matrix runs needed failed to read. 19 failed because the tick table wasn't found and 2 failed while parsing. A failed pool is just left out of its leg, and the route is priced with the pools that remain, so the failure is silent and capacity is understated. None of the 21 pools contains stSUI, MBTC or SOL itself; they matter as intermediate legs.

## Failure types (as quoted in the run log)

- **A. Tick table not found: no ticks in the first 100 dynamic fields** (10 pools, all Turbos). `no ticks in the first 100 dynamic fields under 0x… — this venue stores its liquidity elsewhere`
- **B. Tick table not found: unexpected struct layout** (7 pools: Cetus and Bluefin). `no ticks parsed — tick table not where expected for this venue`. The Cetus pools have a `tick_manager` field and the Bluefin pools a `ticks_manager` field, both different from the layout the adapters expect.
- **C. Parse error** (2 pools, both Turbos). `ticks parsed but no ranges built — check sign convention`

## Pools

The last two columns use two different tests:

- **Within 4 hops:** the pool is one of the up-to-4 pools used on some leg, on some path of up to 4 hops, for any pair with stSUI, MBTC or SOL at either end. Nearly every pool passes, so it tells you little.
- **On a winning route:** the pool's token pair is a leg on a winning route for a pair involving those tokens. Identical in the top-20 and uncapped runs.

Short pool IDs are the first 10 and last 4 characters of the full ID in `data/navi_pools.csv`.

| pool | venue | pair | TVL (CSV) | failure | within 4 hops | on a winning route |
|---|---|---|---|---|---|---|
| `0x022db7c3…a626` | turbos-finance | haSUI/SUI | $26,158 | A | all three | **MBTC, SOL, stSUI** |
| `0x0df4f02d…8443` | turbos-finance | USDC/SUI | $1,203,394 | A | all three | **MBTC, SOL, stSUI** |
| `0x102d0804…6709` | turbos-finance | BUCK/USDC | $11,119 | C | all three | **MBTC, SOL, stSUI** |
| `0x198af6ff…1f5a` | turbos-finance | DEEP/USDC | $51,357 | A | all three | **MBTC** |
| `0x2ce76410…a5ca` | turbos-finance | USDT/wUSDC | $15,878 | A | SOL, stSUI | — |
| `0x2de1a48b…7825` | bluefin | WBTC.lz/USDSUI | $5,640 | B | all three | — |
| `0x40ceeed0…df94` | cetus | BUCK/USDY | $35,053 | B | all three | — |
| `0x5eb2dfcd…ca78` | turbos-finance | SUI/wUSDC | $173,005 | A | all three | **MBTC** |
| `0x62d39858…663e` | cetus | FDUSD/USDC | $87,197 | B | all three | **MBTC, SOL, stSUI** |
| `0x74915fb0…ea9c` | turbos-finance | vSUI/SUI | $51,618 | A | all three | **MBTC, SOL, stSUI** |
| `0x7db7a727…c0bd` | turbos-finance | FDUSD/USDC | $6,074 | C | all three | **MBTC, SOL, stSUI** |
| `0x835effa9…8056` | turbos-finance | USDSUI/SUI | $33,464 | A | all three | **SOL, stSUI** |
| `0x8989fbbd…73d3` | cetus | BUCK/USDT | $16,698 | B | SOL, stSUI | — |
| `0x9490a133…3081` | turbos-finance | WAL/USDC | $5,948 | A | all three | **MBTC** |
| `0x97bde23c…c850` | cetus | BUCK/WAL | $7,877 | B | all three | — |
| `0xaad89a13…9b60` | turbos-finance | LBTC/wBTC | $306,795 | A | all three | **MBTC, SOL** |
| `0xacf163d7…d650` | bluefin | xBTC/USDSUI | $5,310 | B | all three | — |
| `0xb9ddcf85…878b` | bluefin | XAUm/WBTC.lz | $6,267 | B | all three | — |
| `0xd2a3915f…90b6` | turbos-finance | LBTC/wBTC | $5,495 | A | all three | **MBTC, SOL** |
| `0xd97d8b73…b31a` | bluefin | XAUm/LBTC | $6,187 | B | all three | — |
| `0xe9750931…04d6` | turbos-finance | USDSUI/USDC | $445,591 | A | all three | **MBTC, SOL, stSUI** |

## What stands out

- **13 of the 21 pools sit on winning routes** for pairs involving stSUI, MBTC or SOL. Each of those legs is priced without the failed pool, so its capacity is understated. Those pairs are where the 4 zero-capacity rows and the small SOL capacities come from.
- **The three largest failures are all Turbos:** USDC/SUI at $1.2M, USDSUI/USDC at $446k, and LBTC/wBTC at $307k. USDC/SUI and USDSUI/SUI both appear in the routes that changed in the cutoff test, so the "after" capacities in that table are also understated.
- **All 12 failed Turbos pools are type A or C.** Not yet checked whether any Turbos pool was read successfully. Type B looks like Cetus and Bluefin pools whose struct layout the adapters don't recognise.
- Nothing has been fixed yet.

---

# Prices keyed by on-chain symbol (2026-10-11)

`lrg` keys USD prices by the coin's on-chain metadata symbol, not by coin type. `graph.derive_prices` (`lrg/graph.py:73`) stores prices under each pool's `sym_a` / `sym_b`, which `clmm.fetch` takes from coin metadata (`lrg/clmm.py:461`). `curves._out_usd` (`lrg/curves.py:22`) reads them back the same way. This causes two problems.

## 1. Two tokens can share a symbol, and so a price

In the pools the Navi matrix reads:

| symbol | tokens sharing it |
|---|---|
| `USDC` | native USDC (`0xdba3…::usdc::USDC`) and wUSDC (`0x5d4b…::coin::COIN`) |
| `USDT` | suiUSDT (`0x375f…::usdt::USDT`) and another USDT (`0xc060…::coin::COIN`) |

Whichever pool is priced first sets the price for both tokens. That price is then used to convert USD in and out on every leg touching either token. All four are stablecoins near $1, so today's error is small. But nothing prevents a collision between tokens with different prices (e.g. two wrapped BTCs, or any of the many `coin::COIN` types).

## 2. `Matrix.demand` looks prices up by Navi symbol

`Matrix.demand` and `s_max` use `self.prices.get(B)` with Navi's symbol. For five Navi assets the on-chain symbol differs, so `demand` returns `None` for them:

| Navi symbol | price key in `M.prices` |
|---|---|
| suiUSDT | `USDT` |
| suiETH | `ETH` |
| WBTC.lz | `WBTC` |
| wUSDC | `USDC` (shared, see above) |
| XAUm | `XAUM` |

## Current workaround

`run.py` (`alias_prices`) maps each Navi symbol to its price key by coin type, using the pools `build()` read, and adds the Navi names to `M.prices` without overwriting existing keys. This fixes problem 2 for `run.py` only. Problem 1 is not fixed: the fix belongs in `lrg/`, keying prices (and `_out_usd` lookups) by normalized coin type.

# Oracle prices (2026-10-15)

`run.py` now prices tokens with Navi's on-chain oracle (`lrg/protocols/navi_oracle.py`, passed through `Params(prices=...)`). Run: `data/navi/2026-10-15/`, exit 0.

## Code changes (uncommitted)

- `lrg/protocols/navi_oracle.py` (new): reads the PriceOracle price table (`0x1568865e…fbef`, 38 entries `{value, decimal, timestamp}`) and maps each reserve to its entry through `oracle_id`. Price = value / 10**decimal, with the decimal taken from each entry (6, 8 and 9 all occur).
- `lrg/matrix.py`: `build()` merges supplied prices over derived ones and records `price_source` (`supplied`/`derived`) and `derived_prices` on `Matrix`.
- `run.py`: `oracle_prices()` keys oracle prices by both on-chain and Navi symbol. Where two coins share a symbol, the coin with the larger pool TVL wins. `coverage.json` gets a `prices` block with per token: `price_source` (oracle/derived), price, derived price, oracle age, coin type, plus collisions.
- `positions.py`: uses `navi_oracle.entries`. Page reads now retry on partial GraphQL errors (`_post`, `PageError`).

## Verification

**(b) Decimals: 20 recent liquidations.** The implied bonus is `collateral × P_c / (debt × P_d) − 1`, with each price decoded using its own decimal. All 20 fall within 0.2 pp of the configured bonus: 19 exactly at 10% or 8%, and one at 14.95% against 15%. Per-asset decimals are correct.

**(a) Oracle vs derived vs CoinGecko.**

| asset | oracle | derived | CoinGecko | oracle vs derived | oracle vs CoinGecko |
|---|---|---|---|---|---|
| SUI | 0.9896 | 0.9803 | 0.9833 | +0.9% | +0.6% |
| vSUI | 0.9885 | 1.0484 | 1.050 | −5.7% | −5.9% |
| haSUI | 0.9878 | 1.0613 | 1.065 | −6.9% | −7.2% |
| stSUI | 0.9937 | 1.0230 | 1.022 | −2.9% | −2.8% |
| SOL | 102.41 | 101.44 | 102.37 | +1.0% | +0.0% |
| suiETH | 2,368.07 | 2,352.76 | 2,365.09 | +0.7% | +0.1% |
| xBTC / wBTC / WBTC.lz | 81,109.99 | 80,696–80,807 | 80,899–81,068 | +0.4–0.5% | +0.1–0.3% |
| LBTC | 81,208.29 | 80,958.20 | 81,167 | +0.3% | +0.1% |
| MBTC | 81,183.65 | 79,902.26 | 52,978 | +1.6% | +53% (CoinGecko quote looks stale) |
| enzoBTC | 81,183.65 | 80,803.86 | 83,503 | +0.5% | −2.8% |
| XAUm | 4,178.56 | 4,121.97 | 4,122.23 | +1.4% | +1.4% |
| WAL | 0.05228 | 0.05149 | 0.05183 | +1.5% | +0.9% |
| DEEP | 0.02229 | 0.02182 | 0.02220 | +2.1% | +0.4% |
| NAVX | 0.01049 | 0.01040 | 0.01049 | +0.9% | 0.0% |
| stables (USDC, suiUSDT, suiUSDe, USDSUI, FDUSD, BUCK, wUSDC) | 0.997–1.000 | 0.998–1.000 | 0.998–1.000 | within 0.1% | within 0.2% |
| USDY | 1.1491 | 1.1482 | 1.150 | +0.1% | −0.1% |

Oracle prices were 3–46 minutes old at read time. Apart from the liquid-staking tokens and MBTC's CoinGecko quote, the oracle is within about 1.4% of CoinGecko.

**Fallbacks.** MMT, MUSIC and USDB have no oracle entry, so they use the derived price (`price_source="derived"`). Symbol collisions: USDC (native vs Wormhole), USDT (suiUSDT vs Wormhole) and WBTC (WBTC.lz vs Wormhole). In each pair the oracle prices differ by under 0.1%.

**Health factors.** 5 sampled positions, one of them e-mode, match Navi's own health factor to within 5e-7.

## Effect on Q* (vs 2026-10-13)

`Q_star` is 0 for every collateral in both runs, mostly because MBTC debt has no capacity. The comparison below uses `Q_star_ex_zero`.

- Liquid-staking collateral rose: stSUI +137% (732 → 1,737), haSUI +66% (6,030 → 10,016), vSUI +57% (5,931 → 9,339). The oracle values the seized collateral below its sale price, so liquidating it looks cheaper.
- haSUI as debt dropped to zero for DEEP, NAVX, USDC, USDSUI, USDY and suiUSDT, and NAVX→vSUI dropped to zero as well. These pairs had up to $1.46M of capacity before. A liquidator pays about 7% more for haSUI than Navi credits, which uses up most of the 10% bonus.
- Other changes: SOL −46%, FDUSD −31%, DEEP −17%. Most other collaterals moved within ±7%.
- Per pair (455 pairs with capacity > 0 in both runs): median −16.9%, 10th percentile −56%, 90th +34%. Not decomposed yet. A likely contributor is that the oracle prices most tokens 0.5–2% above the derived prices.
- Adequacy status: red for every collateral in both runs.

## Findings (not changed)

1. **`platform_share` overstates the liquidator's bonus. Corrected 2026-10-15; see "Liquidation bonus split" below.** This entry originally said the liquidator gets the full bonus with the treasury paid on top. That was wrong: the event's `collateral_amount` is gross, and the treasury's cut comes out of the bonus.
2. **The oracle prices vSUI and haSUI at the SUI price** (stSUI is about 3% under market). The one vSUI liquidation among the 20 was priced at the oracle value, so this is how Navi actually liquidates. It now drives the zero haSUI-debt cells and the higher liquid-staking collateral capacity.
3. **Inconsistent symbol naming persists** (see "Prices keyed by on-chain symbol" above). `run.py` works around it by keying oracle prices under both names.

# Liquidation bonus split (2026-10-15)

Finding 1 above was wrong, and the error went the other way. Navi's bytecode (package `0x1e4a13a0…59cb`) settles it:

- `logic::calculate_liquidation`:
  - base = min(amount offered, close factor × the borrower's collateral value in that asset, loan value)
  - bonus = base × LB (the e-mode bonus for e-mode users)
  - treasury = bonus × `treasury_factor` of the **collateral** reserve
  - the liquidator receives base + bonus − treasury
- `lending::base_liquidation_call` deposits the treasury amount into the pool's treasury, so it is never sold. It emits `collateral_amount = to_liquidator + treasury`, which is the **gross** amount.

The old 20-event check read `collateral_amount` as the liquidator's take. The same events show gross bonus = LB and treasury ÷ bonus = the collateral's `treasury_factor` exactly.

**Treasury factors (chain, 2026-10-15):**

| collateral | LB | treasury_factor | liquidator's bonus | budget (was, 10% share) | budget (now) |
|---|---|---|---|---|---|
| SUI, vSUI, haSUI | 10% | 0.65 | 3.50% | 8.17% | 3.29% |
| stSUI, SOL, MBTC, DEEP, WAL, enzoBTC, FDUSD, BUCK, suiUSDe | 10% | 0.55 | 4.50% | 8.17% | 4.22% |
| USDC, suiUSDT, USDSUI, USDY | 8% | 0.55 | 3.60% | 6.54% | 3.39% |
| WBTC.lz, xBTC | 15% | 0.80 | 3.00% | 11.98% | 2.83% |
| LBTC, wBTC, suiETH | 15% | 0.85 | 2.25% | 11.98% | 2.11% |
| NAVX | 5% | 0.55 | 2.25% | 3.59% | 2.11% |

## Code changes (uncommitted)

- `lrg/matrix.py`:
  - `Params.platform_share` is replaced by `treasury_factors` (per collateral symbol), with an optional `treasury_share` fallback.
  - `Params.tf(symbol)` raises when neither is set.
  - `budget(lb, tf)` and `seized_factor(lb, tf)` take the factor explicitly, with b = (1 − tf)·LB.
  - All call sites are updated: `q_star`, `demand`, `s_max`, `feasible`, the printed tables and `build`.
  - The factors are saved with the params, so `analysis.load` picks them up.
- `lrg/analysis.py`: the same calls, updated.
- `lrg/protocols/navi.py`: `PLATFORM_SHARE` is replaced by `TREASURY_FACTORS` (the static fallback above, with sources).
- `run.py`:
  - `fetch_navi_live` reads `treasury_factor` per reserve, and `summary.json` records it under `navi_live`.
  - `param_diffs` flags drift against `navi.py`.
  - Adequacy, `s_max_debt_bounded` and stress Q use the collateral's factor.
- `positions.py`: `read_state` reads `treasury_factor`.
- `README.md`: formula section and `Params` example.

## Verification

- **`budget()` against hand values:** (5%, 0.55) → 2.11%; (8%, 0.55) → 3.39%; (10%, 0.65) → 3.29%; (10%, 0.55) → 4.22%; (15%, 0.80) → 2.83%; (15%, 0.85) → 2.11%. All match.
- **Latest 20 liquidations (a newer window than the first check):** 20 of 20 have gross bonus = LB and net bonus `(collateral_amount − treasury)·P_c / (debt·P_d) − 1` = LB·(1 − tf), within 0.2 pp.
- **Live factors vs `navi.TREASURY_FACTORS`:** no differences.
- **Reloaded matrix:** a saved matrix reloads with its factors, and `demand(SUI)` = 0.35 × 1.035 × 0.75 × 55M, as expected.

Not rerun: budgets roughly halve, so Q* will fall across the matrix. Step 13 measures it.

## Close factor applies to collateral value (not changed)

Navi caps the base at close factor × the borrower's **collateral value** in that asset (then at the loan value). `Q = CF·(1+b)·LTV·S·P` applies it to debt instead. At the liquidation threshold this understates Q by about 1/LTV (about 1.3× at LTV 0.75). Repeated calls can also liquidate further while the health factor stays below 1. The stress Q (`CF·seized·D`) has the same issue. To be handled with steps 9 and 11.

# Coin-type keying and swap direction (2026-10-15)

## Changes (uncommitted)

- **`lrg/coins.py` (new):** `norm_type` and the native `USDC` anchor. `navi_oracle` re-exports `norm_type`, and `run.py` imports it instead of keeping its own copy.
- **Prices keyed by normalized coin type throughout:**
  - `graph.derive_prices` (anchored on native USDC)
  - `curves._out_usd`
  - `matrix.build` (`Params.prices`, `M.prices`, `derived_prices`, `price_source`)
  - `graph.build` (TVL; the HTML gets `labels` for display)
- **Symbol lookups go through the coin type:**
  - `Matrix` gains `tokens` (symbol → type) and `price(symbol)`, which `demand`, `s_max` and `feasible` use.
  - `save()` writes `tokens`.
  - `LoadedMatrix.price()` falls back to symbol keys for files saved before this change.
- **Swap side from the pool, not the CSV:**
  - `leg_ids` returns pool ids only.
  - `graph.leg_pools` takes each side from the fetched pool's own `coin_a`/`coin_b` (`curves.side_of`).
  - A pool holding neither coin is skipped and listed in `Matrix.skipped_pools`.
  - CSV coin types are normalized on load.
  - `book.py` matches DeepBook/Aftermath pairs on normalized types.
- **`run.py`:**
  - `alias_prices` and the dual-keyed, collision-resolving `oracle_prices` are removed. `oracle_prices()` returns `{coin type: price}` for all 35 reserves.
  - `coverage.json` prices are keyed by coin type with a display `label`, plus `navi_assets` (symbol → type) and `skipped_pools`. The `collisions` field is gone.
- **Not changed:** `venues.aftermath_pools`/`to_csv_rows` still use symbol-keyed prices for discovery-time TVL. Step 3 rewrites that path.

## Verification

1. **Coin order (D1)**, CSV vs chain, across the routed venues (Cetus, Bluefin, Momentum, Turbos): 18 rows reversed, about $5.3M of CSV TVL. Another 8 reversed rows sit on unrouted venues, plus 5 "other" (Steamm, FlowX), whose types match neither. The largest routed ones:
   - Turbos USDC/SUI $1.20M
   - Cetus USDSUI/USDC $1.20M
   - Cetus HAEDAL/USDC $599k
   - Momentum MBTC/wBTC $593k
   - Cetus suiUSDe/USDC $501k
   - Cetus suiUSDT/USDC $371k
   - Cetus SUI/wUSDC $205k
   - Cetus WBTC.lz/USDC $181k
2. **Output with the CSV side (old) vs the chain side (new):**
   - Cetus USDSUI→USDC: at $1M the cost is 45.01% with the CSV side vs 46.16% with the chain side, and the reverse leg mirrors it. Before the fix, each direction used the other direction's depth.
   - MBTC/wBTC and suiUSDe/USDC are near-empty on chain: reserves of about $12 and $3.2k, against $593k and $501k of CSV TVL. Their output is about $10 and $1.7k either way.
3. **Full run** (`data/navi/2026-10-15`, exit 0):
   - All 24 Navi assets are priced under their coin type, all from the oracle.
   - USDC 0.999888 vs wUSDC 0.999883; suiUSDT 0.998823 vs Wormhole USDT 0.998768; WBTC.lz 80,101.61 vs wBTC 80,165.88. Each coin now carries its own price.
   - Derived prices: MMT, MUSIC, USDB. No pools skipped.

## Q* vs the previous 2026-10-15 run

The previous run predates step 1, so these changes combine the bonus-split fix (budgets roughly halved) with this step. `Q_star_ex_zero` fell for every collateral:
- −47% for SUI
- −69% to −75% for the BTC assets, suiETH and stSUI
- −84% to −87% for the stables and enzoBTC (binding now stSUI)
- −22% to −29% for DEEP, WAL, haSUI and vSUI

New zero-capacity debts: vSUI for 17 collaterals, haSUI for 12, stSUI for 5. The oracle values the LSTs at about the SUI price while the market charges 3–7% more, and that premium now exceeds the 3.3–4.2% budgets. Step 12 (the mint exit) addresses this.

## New finding: CSV TVL is stale for some pools

GeckoTerminal TVL in `data/navi_pools.csv` can be far from on-chain reserves: Momentum MBTC/wBTC lists $593k and holds about $12; Cetus suiUSDe/USDC lists $501k and holds about $3.2k. Pool selection (top 4 per leg) and path ranking both use CSV TVL, so an empty pool can take a slot from a live one. Relevant to steps 3, 6 and 8: rank by on-chain depth or by output at a reference size.

# Pool universe: DeepBook, Aftermath, discovery (2026-10-15)

## Changes (uncommitted)

- **`lrg/discovery.py`** runs end to end:
  - `VENUE_TYPE` is defined, and `has_adapter` comes from the routed venues.
  - Each token is paged up to GeckoTerminal's free-tier cap of 10 pages, with a warning naming any token that hits it.
  - An exhausted rate-limit retry now raises, where it used to silently end a token's list.
  - Rows carry `discovered_at`, coin types are normalized, and `discover_all()` adds DeepBook and Aftermath.
- **On-chain TVL** (`verify_tvl`/`onchain_tvl`):
  - `tvl_usd` is now each pool's on-chain reserves at oracle prices; when one side is unpriced, the priced side is doubled.
  - GeckoTerminal's figure is kept as `tvl_gecko`, and `tvl_source` says which applies (onchain, venue or geckoterminal).
  - Routed pools under $5k, or that can't be valued, are dropped.
- **`lrg/venues.py`:**
  - `discovery_rows()` returns DeepBook pools (both coins Navi assets, readable book) and Aftermath pairs above $25k. TVL uses coin-type prices.
  - `_parse_book` sorts bids descending and asks ascending.
  - The order-book fetch steps down from 200 to 100 to 50 levels, because the indexer runs out of gas on SUI_USDC at 200. Levels fetched are recorded as `levels`.
- **Pool identity:**
  - `book.pool_key` = (object id, sorted normalized coin pair).
  - It's used by the `PoolBook` cache and failures, `matrix.build`/`graph.build` (`leg_ids`, `vol_of`, `pools`) and `run.py` (failure metadata).
  - An Aftermath pair is matched exactly; a missing pair fails, where the code used to fall back to another pair of the same pool.
- **Venue-neutral ranking:**
  - `derive_prices(seed=...)` starts from the supplied oracle prices and orders pools by USD TVL under known prices.
  - `LegCurve(pick="best")` picks the pool with the most USD out at $10k (`REF_USD`).
  - `.liquidity` is no longer compared across venues.
- **Housekeeping:** `lrg/discovery_extra.py` (endpoint guessing, superseded) is removed, and the README is updated.
- **`data/navi_pools.csv` regenerated:** 179 rows became 272; the old file is in git.

## Diff, routed venues

| venue | old rows | old TVL (Gecko) | new rows | new TVL (on chain) |
|---|---|---|---|---|
| cetus | 62 | $22.9M | 101 | $24.6M |
| bluefin | 42 | $16.7M | 51 | $12.4M |
| momentum | 27 | $3.9M | 27 | $4.1M |
| deepbook | 0 | — | 14 | $3.1M |
| turbos-finance | 20 | $2.6M | 35 | $2.7M |
| aftermath | 0 | — | 2 | $0.2M |

**Added (112).** The largest are:
- Bluefin SVBTC/wBTC ($1.6M) and sUSN/USDC ($978k)
- DeepBook USDSUI/USDC ($722k), SUI/USDC ($579k), WBTC.lz/USDC ($462k), suiUSDe/USDC ($329k) and xBTC/USDC ($310k)
- Momentum xSUI/SUI ($633k)
- Aftermath LBTC/wBTC ($122k)

**Dropped (33)**, all with on-chain reserves under $5k:
- Bluefin BTCvc/USDC: $7.0M listed, $220 held
- Momentum MBTC/wBTC: $593k listed, $13 held
- Cetus suiUSDe/USDC: $501k listed, $3.2k held
- Bluefin NAVX/USDC, Turbos stBTC/LBTC and Cetus FDUSD/USDC: $0 held
- Several dead BUCK pools

**Fake listings removed:** GeckoTerminal lists 25 pools above $1M that hold almost nothing. Examples: xSKHY/USDC ($9.4B listed, $70 held), USDD/wUSDC ($327M listed, $0 held), UNIVERSE/wBTC ($47M listed, $56 held). Paging 10 deep would have put them at the top of the wUSDC, USDC, wBTC and DEEP legs.

**Truncated by the page cap:** SUI, USDC, WAL, DEEP and wUSDC.

## SUI → USDC leg after the change

The leg has 9 pools. The two Turbos pools fail (step 4); DeepBook and Aftermath read.

| pools used | cost at $100k | cost at $1M | DeepBook share at $1M |
|---|---|---|---|
| all readable, with DeepBook | 0.39% | 4.45% | 19% |
| all readable, without DeepBook | 0.54% | 6.60% | — |
| matrix's top 4 by TVL | 0.61% | 7.84% | not included |

The matrix still misses DeepBook. It takes the top 4 pools by TVL, DeepBook ranks 6th at $579k, and the 4th slot goes to Turbos USDC/SUI, which fails to read. Steps 6 (drop failed pools before taking the top N) and 8 (rank by output) fix this.

Also seen: the Momentum package is missing from `clmm.PACKAGES`, so those pools are labelled by package prefix (`0x70285592`). This is cosmetic.

## Full run with the new pool universe (2026-10-15)

**Pools:** 129 needed, 115 read, 14 failed. All 14 failures are Turbos `tick_table_not_found` (step 4). DeepBook and Aftermath now read.

**`Q_star_ex_zero` vs the step 2 run:**
- Stables and enzoBTC: about ×4 (782 → 3,188 for USDC)
- SUI: +173%
- SOL: ×4
- stSUI: +141%
- BTC assets: +50% to +82%
- WAL: −80% (the binding debt is now stSUI)
- Others: within ±8%

vSUI and haSUI are no longer zero-capacity debts for any collateral; stSUI still is for most.

**MBTC silently dropped out of the matrix.** Its only pool, Momentum MBTC/wBTC, has $13 of reserves and fell below the floor. `build()` then treats MBTC as "absent from the pool set" and leaves it out of every row and column: 462 cells instead of 506. MBTC is missing from `summary.json` and `adequacy.csv`, and appears in `stress.csv` only through positions. Before, MBTC had zero capacity everywhere and failed. Now it doesn't appear at all, which reads better than it is. Step 9 is required before these numbers are quoted: an asset with no pool must get capacity 0, not be absent.

# Turbos reader (2026-10-15)

## Diagnosis

Review finding A2 guessed that Turbos ticks are dynamic **object** fields, which `Q_FIELDS` doesn't select. That's wrong. The ticks are ordinary dynamic fields (`MoveValue`, type `pool::Tick`, key `i32::I32`). The pool's UID also carries every LP `Position` as a dynamic object field. On USDC/SUI (`0x0df4f02d…`), the first 20,000 fields (400 pages, 6.5 minutes) were all positions, with no ticks among them. The reader stopped after 2 pages, and even a full walk would be impractical.

Turbos indexes its ticks with `tick_map: Table<I32, u256>`: bit b of word w marks tick (w·256 + b)·spacing. On USDC/SUI that's 182 words and 1,098 ticks, from −443,630 to 443,630. Each tick can be fetched by key with `multiGetDynamicFields`.

## Changes (uncommitted)

- **`clmm.fetch_ticks_bitmap`:**
  - pages `tick_map`, decodes the words, and fetches ticks by key in batches of 25
  - takes the I32 key type from the words' names, with no hard-coded package
  - raises if any tick marked in the bitmap doesn't read
- **`clmm.gql_strict`:** any GraphQL error fails the call after retries, even with partial data. Used for the tick reads.
- **`clmm.fetch`:**
  - takes the bitmap path when the pool has a `tick_map` table
  - no longer pages the pool's own fields as a fallback
  - reports an empty pool as "empty pool: liquidity 0 and no initialized ticks"
  - the paging reader's error now says the id isn't the pool's tick table
- **`clmm.PACKAGES`:** labels Turbos and Momentum, which were shown by package prefix.

## Verification

- **Turbos pools in the CSV:** 35 of 35 read; all of them failed before.
- **Liquidity check:** the summed `liquidity_net` for ticks at or below the pool's `tick_current_index` equals its `liquidity` field.
  - DEEP/USDC first showed a mismatch from my float comparison against `sqrtP`. Against the pool's tick index the two match exactly (17,065,455,667,241).
- **Sanity check against the oracle:** passes for every priced pool except vSUI/SUI and haSUI/SUI. That is the known oracle issue (vSUI and haSUI priced at about the SUI price, while these pools trade them about 6% higher), not a read error.
- **The two "no ranges built" pools** (BUCK/USDC `0x102d0804…`, FDUSD/USDC `0x7db7a727…`) have `liquidity` 0 and an empty bitmap. They're empty, not a sign bug, and the step 3 floor already dropped them from the CSV.
- **SUI → USDC leg, same snapshot** (costs differ from the step 3 table because the market moved):

| pools used | cost at $1M |
|---|---|
| all readable, with DeepBook | 1.90%, of which Turbos USDC/SUI takes 9% |
| all readable, without DeepBook | 3.25% |
| matrix's top 4 by TVL | 4.87%, now with no unreadable pool |

# Empty pools (2026-10-15)

## Diagnosis (D3)

The 8 Cetus and Bluefin pools that failed with "no ticks parsed — tick table not where expected" are all empty:

| venue | pair | pool | liquidity | tick table size | reserves |
|---|---|---|---|---|---|
| cetus | BUCK/USDT | `0x8989fbbd` | 0 | 0 | $0.00 |
| cetus | BUCK/USDY | `0x40ceeed0` | 0 | 0 | $4.61 |
| cetus | BUCK/WAL | `0x97bde23c` | 0 | 0 | $2.19 |
| cetus | FDUSD/USDC | `0x62d39858` | 0 | 0 | $0.11 |
| bluefin | WBTC.lz/USDSUI | `0x2de1a48b` | 0 | 0 | $10.57 |
| bluefin | XAUm/LBTC | `0xd97d8b73` | 0 | 0 | $757.26 |
| bluefin | XAUm/WBTC.lz | `0xb9ddcf85` | 0 | 0 | $172.49 |
| bluefin | xBTC/USDSUI | `0xacf163d7` | 0 | 0 | $0.16 |

- The tick table was found in every case; it just had no entries, so the error message was wrong.
- None of these pools is in the current CSV, because the step 3 floor dropped them.
- None of the 179 Cetus, Bluefin and Momentum pools in the current CSV fails to read, so there is no read bug with liquidity above 0.

## Changes (uncommitted)

- **`clmm.EmptyPool`:** raised when a pool has liquidity 0 and an empty tick table, on both the paging and the bitmap paths.
- **Distinct errors:**
  - "no tick table found" when no tick table id is located
  - a populated table that parses to nothing raises a reader-bug error
  - an empty table with liquidity above 0 raises its own error
- **`PoolBook.empty`:** records empty pools apart from `failed`. They're zero depth: legs go without them, and they never mark a route unverified.
- **Counts:** `Matrix.pools_empty` and `save()` include them, and `build()` prints them. `coverage.json` gets `pools_empty` and an `empty` list, and `pools_needed` includes them.

## Verification

- **The 8 pools via `PoolBook`:** 8 empty, 0 failed.
- **Two-pool matrix:** a temp CSV puts empty Cetus FDUSD/USDC (`0x62d39858`, forced to the top TVL rank) and live Cetus FDUSD/USDC (`0x43d4c9ad`) on the same leg. Result: read 1, failed 0, empty 1. FDUSD→USDC = $1,301 and USDC→FDUSD = $31,372, both priced from the live pool.

# Read robustness (2026-10-15)

## Changes (uncommitted)

- **`clmm.gql` is strict:**
  - any GraphQL `errors` entry fails the call even with partial data (4 attempts, with backoff)
  - the final error keeps the original message, so a timeout is still classed as network
  - `gql_strict` is now an alias
  - this covers every pool, oracle, Storage and discovery read; `positions._post` already did this
- **`fetch_ticks`:**
  - a missing page raises instead of reading as "no ticks"
  - the page cap rises from 80 to 200, and hitting it with pages left raises `TruncatedRead`
  - the fields read must equal the table's own `size` (Cetus, Bluefin, Momentum), else `TruncatedRead`
  - `clmm.max_ticks_seen` is logged by `build()`
- **`clmm.fetch` re-reads the whole pool** up to 3 times on `TruncatedRead`. Pages are served at the latest checkpoint, so a busy pool's table can change mid-read.
- **`ClmmPool.out`** returns at most the pool's balance of the output coin, when that balance is known.
- **`graph.read_legs`**, used by `matrix.build` and `graph.build`, reads each leg down its TVL ranking until it has `max_pools_per_leg` readable pools. Failed and empty pools no longer take a slot.
- **`run.py`:**
  - `failure_kind` gains `truncated` and `graphql_error`
  - the tick-table and parse kinds match the new messages
  - truncated pools are warned about and counted as failed
- **`tests/test_read_robustness.py`:** 13 `unittest` cases on mocked responses, covering:
  - partial error, retry then success
  - timeout classed as network, not "no ticks"
  - size mismatch, size match, page cap, missing page
  - re-read succeeds, re-read keeps failing
  - output capped at reserves, unknown balance not capped
  - failed top pool replaced, empty pool takes no slot
  - all pass

## Live run (`data/navi/2026-10-15`, exit 0, before the re-read was added)

- **Pools:** 131 legs; 130 pools needed, 129 read, 1 failed, 0 empty, 0 network errors.
- **Largest tick table read:** 3,534 ticks (Bluefin xBTC/SUI).
- **The one failure:** Cetus USDC/SUI `0x51e883ba…` ($862k), "truncated: read 700 of 702 fields". Read again later, the table had 705 entries and read 705 of 705 three times. The table was changing during the read, and the size check caught an inconsistent snapshot. The pool is now re-read up to 3 times. The leg-slot change put the next-ranked pool in its place, so SUI→USDC still had 4 readable pools.
- **`Q_star_ex_zero` vs the step 3 run:** BTC assets up 83–172%; stables, suiETH and suiUSDe down 20–33%; LSTs down 11–14%; the rest within ±5%. This mixes a market move of several hours with the changes here, and isn't attributed further.
- **DeepBook is still not on the matrix's SUI → USDC leg.** It ranks 6th by TVL and every pool above it now reads. Step 8 (rank by output) is what brings it in.

# Curve interpolation (2026-10-16)

## Changes (uncommitted)

- **`curves.LegCurve.out`:** interpolates cost (1 − out/in) linearly in log size instead of USD out. Below the grid it uses the first grid point's cost. Above `GRID_HI` it computes exactly with `allocate()` and never extrapolates.
- **`curves.route_capacity`:** takes the grid answer (`grid_capacity`, now bisecting in log size), then brackets and bisects 20 steps on `exact_route_cost`, where each leg's `allocate()` feeds the next. The returned size's exact cost is within the budget. "Exact" means exact for our 32-chunk greedy split.
- **`matrix._best_route`:** ranks candidate paths on the grid and refines every path within 10% of the grid best, so a near tie can't pick the wrong path.
- **Saturation:**
  - `curves.is_saturated` marks a capacity at the top of the grid; `Matrix.saturated` lists those pairs and is saved
  - `run.py`: new `saturated` column in `matrix.csv`; `Q_star_saturated` in `stress.csv` and `summary.json`, alongside `adequacy.csv`
  - `_fmt` prints "≥50M"
- **`ClmmPool.out`** walks its already-sorted ranges instead of re-sorting on every call, which keeps the extra exact evaluations cheap. `clmm.load` sorts ranges on load.
- **`tests/test_curves.py`:** a closed-form constant-product pool, a two-hop route against exact bisection, saturation, and no extrapolation. All 4 pass, and so do the 13 read-robustness tests.

## Tests: capacity, old method vs refined (constant-product pools)

| route | budget | exact | old (USD-out interpolation) | refined | old error | refined error |
|---|---|---|---|---|---|---|
| single, 0.25% fee | 1.00% | 75,947 | 115,947 | 75,947 | +52.7% | 0.000% |
| | 2.00% | 179,019 | 213,637 | 179,019 | +19.3% | 0.000% |
| | 5.00% | 501,253 | 545,709 | 501,253 | +8.9% | 0.000% |
| | 8.17% | 864,625 | 885,119 | 864,625 | +2.4% | 0.000% |
| | 10.00% | 1,086,048 | 1,096,977 | 1,086,048 | +1.0% | 0.000% |
| two-hop | 1.00% | 15,474 | 24,259 | 15,474 | +56.8% | 0.000% |
| | 2.00% | 37,959 | 52,865 | 37,959 | +39.3% | 0.000% |
| | 5.00% | 108,255 | 119,750 | 108,255 | +10.6% | 0.000% |
| | 8.17% | 187,526 | 207,129 | 187,526 | +10.5% | 0.000% |
| | 10.00% | 235,830 | 254,741 | 235,830 | +8.0% | 0.000% |

The old method's overstatement grows as the budget shrinks. After step 1, Navi budgets are 2.1–4.2%, which is the range where the old error was largest.

## Live, same snapshot (one matrix build, old method emulated on the same curves)

- **Build time:** 811 s, matrix only. No pair is saturated.
- **414 pairs positive both ways:** median 0.0%, 10th percentile −29%, 90th percentile +5%, range −90% to +10%.
- **8 pairs went from positive to 0**, all with stSUI as debt: WBTC.lz, suiUSDT, SOL, USDSUI, USDC, xBTC, SUI and USDY. The old method gave up to $13k.
- **Largest drops:** WAL→stSUI $13,530 → $1,341; BUCK→stSUI $770 → $106; NAVX→enzoBTC −60%; NAVX→DEEP −59%; wBTC→SOL −55%.
- **10 largest pairs move little,** −3.9% to +6.9%. For example haSUI→SUI is +0.9%, USDSUI→USDC +1.3%, USDSUI→BUCK +6.9% and USDSUI→SUI −3.9%. Deep single-hop legs were already near-linear between grid points.
- **Row minimum excluding zeros:** most collaterals fall 20–58%. Examples: USDC 2,641 → 2,114, LBTC 2,115 → 1,099, wBTC 2,088 → 943, WAL 3,227 → 1,341, BUCK 770 → 106.
  - SUI, SOL, WBTC.lz and xBTC appear to rise (236 → 2,294, for example). That's because their old minimum was a small, overstated stSUI cell that is now exactly 0, and zeros are excluded from this measure.

# Path ranking (2026-10-16)

## Changes (uncommitted)

- **Pools per leg:**
  - `graph.read_legs` reads every pool on every leg that a path uses.
  - `graph.choose_pools` keeps the `max_pools_per_leg` (4) that give the most USD out at $10k for that direction, rather than the top 4 by listed TVL.
  - Failed and empty pools never take a slot.
- **Paths:**
  - `matrix.build` and `graph.build` enumerate every path up to 4 hops (22,853 here) and rank them only after pools are read.
  - `graph.rank_paths` scores each path by grid route cost at $1k, $10k and $250k (`RANK_SIZES`) and keeps the union of the cheapest 6 at each, at most 18 (`max_candidates // 3`).
  - The capacity search (grid plus exact refinement, step 7) runs only on those.
  - `Params.max_candidates=None` keeps every path. `Matrix.candidate_paths` records the kept paths.
- **Removed:** the bottleneck-TVL pre-ranking.
- **Tests:** the read-robustness leg test now covers read-all plus choose-by-output. 16 tests pass.

## Verification (one snapshot: 146 pools read, 0 failed, 0 empty)

- **Uncapped build:** 1,005–1,119 s. Ranked selection plus capacity on the same curves: 38 s (three sizes) or 42–53 s (two sizes). Pool reads went from about 130 to 146 now that every pool on a used leg is read.
- **Pairs where the uncapped search still beats the ranked set by more than 0.5%:**

| ranking | misses | pair | ranked | uncapped | rank of winner at $10k |
|---|---|---|---|---|---|
| $10k + $250k, top 10 each | 1 | stSUI→vSUI | $749 | $1,305 | 24 / 29 |
| $1k + $10k + $250k, top 6 each (adopted) | 1 | vSUI→wBTC | $41,840 | $46,199 | 8 / 51 |

  - A first two-size run also missed stSUI→haSUI ($0 vs $1,205).
  - stSUI→vSUI sets stSUI's row minimum, so the two-size ranking would understate stSUI's Q\*. The adopted three-size ranking keeps it.
  - Its remaining miss, vSUI→wBTC at −9%, is far above vSUI's row minimum (about $6.5k), so no row minimum is affected.
  - Other differences were under 0.1%, which is refinement noise.
- **The pairs from 2026-10-09 that improved without the cap:** all match the uncapped result, apart from refinement noise. These include SUI→SOL ($2,229, direct path), SOL→SUI ($2,669) and the SOL↔BTC routes. The stSUI-debt pairs are $0 under both, and MBTC is no longer in the pool set.
- **SUI → USDC leg in the matrix:** DeepBook `0xe05dafb5` is now first, then Bluefin `0x15dbcac8`, then Cetus `0xb8d7d9e6` and `0x51e883ba`.

# Matrix completeness, borrowable debts, one Q_cap (2026-10-16)

## Chain (reserves)

`borrow_cap_ceiling` is 0 for:
- **wUSDC:** 2.14M of user debt outstanding, about $2.1M
- **XAUm:** 0.28 tokens, about $1.2k
- **enzoBTC:** no debt
- **MBTC:** no debt

No reserve is isolated. `reserve_field_a/b/c` are 0 and unlabeled, and no other borrowing flag was found.

## Changes (committed in `4717ad9`)

- **Every asset is in the matrix.**
  - `build()` includes every asset with a coin type. One with no pool has no path, so capacity 0 in every pair. These are listed in `Matrix.no_pool`.
  - MBTC is back as a row and a column of zeros.
- **Debt set**, from `run.debt_set`:
  - It holds the borrowable assets (`borrow_cap > 0` and not isolated), plus any non-borrowable asset that still carries user debt, since a liquidation of that debt must buy it.
  - Excluded today: MBTC and enzoBTC. Kept, unlike the prompt: wUSDC and XAUm, because of their outstanding debt.
  - Excluded debts keep their cells in `matrix.csv` but leave row minima and `Q_star` (`Params.excluded_debts`, `Matrix.debt_row`), adequacy, signal 1 and `s_max_debt_bounded`.
  - A new `config.json` lists both sets with reasons, along with params, treasury factors and the Q_cap formula.
- **One Q_cap** (`run.pair_demand`), used by both adequacy and signal 1:
  - `Q_cap(B, A) = β·CF·(1+b_B)·min(LTV_B·S_B·P_B, max(borrowed_A, cap_A·S_A)·P_A)`
  - Signal 1 is now per pair and debt-bounded; it was previously unbounded.
  - Documented in the README and the `run.py` docstring. `agent/TASK.md` is still to be updated (step 14).

## Run (`data/navi/2026-10-15`, exit 0)

- **Row minimum (Q\*):** still 0 for every collateral except haSUI ($6,890 → $7,110), vSUI ($6,090 → $6,387) and stSUI ($904 → $1,285). The binding debt is haSUI, as before.
- **MBTC:** now present, with Q\* 0 against all 22 debts in the set; adequacy red, signal 1 fail.
- **MBTC and enzoBTC bind no row.**
- **Row minimum excluding zeros, before → after:**
  - BUCK 768 → 2,834 and WAL 766 → 2,706: stSUI moved to zero, so it no longer counts.
  - SOL 804 → 55 (stSUI)
  - NAVX 1,434 → 809 (DEEP)
  - Most others within ±30%. The runs are about a day apart, so this mixes in market movement.
- **Status:** every collateral is red on adequacy and fails signal 1.

# Coverage flag (2026-10-16)

## Changes

- **`matrix._coverage` / `Matrix.coverage`:** each pair is `complete` or `incomplete`, with the failed pool keys. Empty pools never count.
  - **When a failed pool counts on a leg:** the leg has fewer than 4 readable pools, or the failed pool's on-chain TVL is at least that of the weakest chosen pool.
  - **Incomplete when** any of these holds:
    1. a counting failed pool sits on the winning path
    2. it sits on another kept path that could overtake the winner: capacity scales at most by (1 + T/L) for a failed pool of TVL T on a leg holding L
    3. an enumerated path went unpriced on a leg with a failed pool
    4. capacity is 0 and a failed pool holds either coin
- **`run.py`:**
  - `matrix.csv` replaces `unverified` with `coverage` and `failed_pools`, each pool listed with its TVL.
  - In `adequacy.csv`, an incomplete pair gets status `incomplete`, never red or amber, with `status_if_complete` kept.
  - Per-collateral rank, worst first: red > amber > incomplete > green.
  - Signal 1 fails only on complete pairs, and is `unresolved` when it fails only on incomplete ones.
  - Stress: incomplete pairs don't set `breaking_shock`; they're reported as `breaking_shock_incomplete`.
  - `coverage.json` and `summary.json` carry `coverage_share` (on-chain TVL read ÷ needed) and `pairs_incomplete`.
- **`tests/test_coverage.py`:** 8 cases (failed pool in a top slot or below the slots, lost route, zero capacity, none failed, overtaking yes and no, signal 1). All 24 tests pass.

## Forced failure (Turbos USDC/SUI, $778k, made to fail; matrix-only build)

- **Without the overtaking test:** 455 of 506 pairs were incomplete. Almost every pair keeps some candidate path through SUI→USDC. One failed pool on a leg holding about $6.7M flagged pairs whose winning path never uses that leg. Too broad to mean anything.
- **With the overtaking test:** 255 of 506 pairs are incomplete.
  - 197 have a winning path that crosses the SUI–USDC leg, e.g. WBTC.lz→USDC→SUI→DEEP.
  - 58 have capacity 0 with the failed pool holding either coin, and SUI is in almost every pair.
  - Losing paths no longer flag pairs on their own.
  - That's what the definition asks for. A failed pool on the most-used leg makes about half the matrix unresolved.

# Stress corrections (2026-10-16)

## Changes

- **`run.position_flows`:**
  - each liquidatable position's debt is split across its collateral by borrowing power c·P·LT, using the E-Mode LT where it applies
  - repayment per (B, A) is `min(D, CF·value_B·d_A/Σd)`, Navi's cap of CF × collateral value per call (open since step 1; now applied in stress)
  - the bonus is the E-Mode bonus for an E-Mode position's E-Mode collateral
  - returns `unpriced` when any reserve has no price, and `bad_debt` when collateral is worth 0
- **`run.stress`:**
  - Q = β·Σ(1+(1−tf_B)·lb)·base
  - the E-Mode tranche is also checked against `Matrix.pair_capacity(B, A, LB_e)` (new: one pair's capacity at another bonus), and the pair fails if either check fails
  - shocks run from 5% to 50% in 5% steps, plus a depeg of vSUI, haSUI and stSUI by 2/5/10% against a flat SUI, reported per collateral under `depeg`
  - unpriced positions and bad debt are counted per scenario (`summary.json` `stress_scenarios`)
  - `stress.csv` gains `kind`, `base_usd`, `Q_emode`, `Q_star_emode` and `fails_emode`
- **Protected users.** `storage::is_liquidatable` lets anyone liquidate an unprotected user. A protected user can be liquidated only by a liquidator designated for them in `DESIGNATED_LIQUIDATORS_KEY`. All 6 protected users have one, so they stay in D, reported as `D_protected_usd`. `positions.read_state` reads the designated table, and `coverage.json` gets `protected_with_designated_liquidator`.
- **Signal 1 / adequacy.** E-Mode-eligible pairs (B collateral and A debt in an active E-Mode pair) are also checked at the E-Mode LTV and bonus, against Q\* at the E-Mode budget (`Q_cap_emode`, `Q_star_emode`). `positions.read_state` now reads the E-Mode `ltv`.
- **`tests/test_stress.py`:** a synthetic state with single-collateral, two-collateral/two-debt, E-Mode, protected, unpriced and zero-collateral positions, checked against hand calculations. All 29 tests pass.

## Live run

Pending: breaking shock per collateral before and after.

# LST mint venue (2026-10-16)

## Research (on chain, by simulating a mint of 1,000 SUI then redeeming it in one transaction)

| LST | mint call | SUI per LST | mint fee | instant redeem | Navi oracle (LST÷SUI) | rate vs oracle |
|---|---|---|---|---|---|---|
| vSUI | Volo `stake_pool::stake` | 1.0689 | 0 | `unstake`, 3 bp | 0.9989 | +7.0% |
| haSUI | Haedal `staking::request_stake_coin` | 1.0829 | 0 | `request_unstake_instant_coin`, 2 bp | 0.9982 | +8.5% |
| stSUI | AlphaFi `liquid_staking::mint` | 1.0398 | 0 | `redeem`, 1 bp | 1.0041 | +3.6% |

- **Minting is atomic and permissionless.**
  - Each mint call is public and returns the coin in the same transaction, and the simulation ran from an arbitrary address.
  - Nothing is paused: Volo's top-level `manage.paused` is false (the `paused: true` seen earlier is inside `validator_pool`), Haedal's `pause_stake` is false and stSUI's `is_paused` is false.
  - No mint cap was found.
- **The mint rate equals the market rate:** CoinGecko had vSUI at 1.068, haSUI at 1.083 and stSUI at 1.040 SUI.
- **Redemption is also atomic for all three.** By decision it is not modelled as an exit.
- **Protocol risk.** Navi's oracle prices all three LSTs at about the SUI price, below their own redemption rate.
  - Repaying vSUI or haSUI debt costs 7.0–8.5% against the oracle, by DEX or by mint, above every budget (2.1–4.2%). Those debts can't be liquidated at a profit.
  - LST collateral is valued 3.6–8.5% below what it redeems for.

## Changes

- **`venues.MintPool`:** SUI→LST at the live simulated rate, with unlimited depth and `one_way = "a"`.
- **`venues.MINTERS`:** the call configuration for each LST.
- **`venues.mint_pools` / `mint_rows`:** the pools, and their CSV rows (venue `lst-mint`, pool type `MINT`).
- **`curves.side_of`** raises `OneWay` for the reverse direction, and `graph.leg_pools` skips it silently.
- **Wiring:** `PoolBook` loads the venue, `load_pool_rows` keeps `MINT` rows despite their 0 TVL, `navi.VENUES` includes `lst-mint`, and discovery appends the rows. The three rows were appended to `data/navi_pools.csv`.
- **`tests/test_mint.py`:** fixed rate at any size, one-way, leg selection, and cost equal to the premium at every size. All 33 tests pass.

## Before and after (vSUI/haSUI/stSUI debt cells; one snapshot, CLMM pools from a shared cache)

| debt | cells | zero without mint | zero with mint | increased |
|---|---|---|---|---|
| vSUI | 21 | 19 | 19 | 2 |
| haSUI | 21 | 19 | 19 | 0 |
| stSUI | 21 | 16 | 16 | 5 |

- **Increased:**
  - haSUI→vSUI $14,386 → $2,190,553, and haSUI→stSUI $13,530 → $2,249,386, both via haSUI→SUI→mint. The oracle undervalues the haSUI collateral (+8.5% on sale), which outweighs the 7.0% and 3.6% mint premiums.
  - vSUI→stSUI $13,530 → $90,045.
  - BUCK→stSUI, suiUSDe→stSUI and FDUSD→stSUI rise 8–12% ($4.0k → $4.5k, $2.8k → $3.0k, $1.2k → $1.3k).
  - stSUI→vSUI $1,378 → $1,466.
- **Unchanged:**
  - haSUI debt stays 0 everywhere, as expected from its 8.5% premium.
  - The 4.22%-budget collaterals (SOL, DEEP, WAL, enzoBTC and others) still have 0 against stSUI: their legs to SUI cost more than the 0.6 points left after the 3.6% premium.
- **Conclusion:** the zero LST-debt cells are mostly real. They come from Navi's oracle pricing, not from missing routes.
