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
