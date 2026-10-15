# LRG — Liquidation Route Graph

What a liquidation actually costs to execute on-chain, and what that implies for lending-market risk parameters.

A lending protocol's liquidation bonus has to cover the cost of converting seized collateral back into the debt asset. That conversion is a path through the chain's trading venues, and its cost depends on which pools exist, how deep they are at the current price, how many hops the path takes, and how much is being sold. LRG builds that path graph from live pool state and inverts it: for each collateral and debt asset, how much can clear the routes within the bonus available to a liquidator.

The reference instance is [Navi Protocol](https://naviprotocol.io) on Sui — 24 assets, 506 ordered liquidation pairs, six venues. The engine itself is protocol- and chain-agnostic; everything specific to Navi lives in one file.

---

## The condition

A liquidator repays debt `D` and receives collateral worth `(1 + b)·D`, where `b = (1 − tf)·LB` is the bonus net of the protocol's cut. On Navi, `tf` is the collateral reserve's `treasury_factor` (0.55–0.85): the protocol takes `tf` of the bonus out of the seized collateral and keeps it in the pool, so that part is neither earned nor sold by the liquidator. They sell that collateral at a total cost `c` — swap fees plus price impact, referenced to the oracle rate — and must end with at least `(1 + φ)·D` to cover the repayment and the flash-loan fee:

    (1 + b) * (1 - c) >= 1 + phi      which is      c <= 1 - (1 + phi) / (1 + b)

The right-hand side is the **cost budget**. The familiar first-order form `b − φ` overstates it by roughly `b` squared, because it measures the loss against the pre-bonus base while the quantity being sold is the post-bonus amount. For SUI collateral on Navi (LB 10%, treasury factor 0.65, so `b` = 3.5%) the exact budget is **3.29%**, not 3.41%; the gap grows with `b`, to 1.6 percentage points at `b` = 15%. LRG uses the exact form.

The quantity that has to clear is the collateral a full liquidation would put on the market:

    Q = beta * CF * (1 + b) * LTV * S * P

with `CF` the close factor, `S` the supply cap, `P` the oracle price, and `beta` the share of the book liquidatable at once. `beta = 1` is adversarial and is the default.

**Capacity** `Q*` is the inverse: the largest `Q` whose route cost stays inside the budget. Comparing `Q` against `Q*` is the question the tool exists to answer.

---

## Install

```bash
git clone https://github.com/ipakresearch/ipak-lrg
cd ipak-lrg
pip install -r requirements.txt
```

Python 3.9+. Two dependencies, both stdlib-adjacent: `requests` and `pandas`. No RPC keys — everything comes from public endpoints.

---

## Usage

```python
from lrg import graph, matrix, analysis
from lrg.protocols import navi

# the route graph and an interactive network view
data = graph.build("data/navi_pools.csv", navi.COIN_TYPES, navi.VENUES,
                   out="liq_network.html", template="templates/network.html")

# the capacity matrix
p = matrix.Params(close_factor=navi.CLOSE_FACTOR, beta=1.0,
                  treasury_factors=navi.TREASURY_FACTORS)
M = matrix.build("data/navi_pools.csv", navi.COIN_TYPES, navi.ASSETS,
                 navi.VENUES, params=p)

analysis.summary(M)
```

Or end to end:

```bash
python examples/run_navi.py                    # from the committed pool snapshot
python examples/run_navi.py --discover         # rebuild the pool universe first
python examples/run_navi.py --beta 0.4 --pick best
```

### Topology alone

Routes without costs, straight from the pool list:

```python
from lrg.topology import load
from lrg.protocols import navi

r = load("data/navi_pools.csv", navi.COIN_TYPES)
r.find("vSUI", "SUI")
```

```
1. vSUI → SUI   (1 hop)
     vSUI → SUI
       ferra-dlmm       DLMM      $     111,530  fee ?        0x0d6b...
       cetus            CLMM      $     107,199  fee 0.05%    0xa528...
       momentum         CLMM      $      94,825  fee 0.05%    0x4e1a...
       bluefin          CLMM      $      69,029  fee 0.20%    0x8f0c...

2. vSUI → BUCK → SUI   (2 hops)
     ...
```

### Reading the matrix

```python
df = analysis.to_frame(M)              # one row per pair: capacity, path, hops
analysis.to_wide(df)                   # collateral x debt grid
analysis.collateral_view(M)            # Q vs Q*, ratio, implied cap, flags
analysis.row_stats(M)                  # min / p10 / median / p90 / max per row
analysis.concentration(M)              # one thin route, or thin throughout?
analysis.debt_view(M)                  # column minima: borrow-cap ceilings
analysis.hub_load(M)                   # which intermediaries carry the routes
```

The row minimum is the adversarial capacity — the worst debt asset a liquidation could be forced into. It is frequently set by one near-empty debt market, which says something quite different from a row that is thin throughout, so `row_stats` reports the whole distribution and `collateral_view` takes `exclude_debts=[...]` to read the picture without a handful of dead markets dominating it.

A saved run can be reopened without refetching:

```python
M = analysis.load("navi_matrix.json", asset_table=navi.ASSETS)   # params, treasury factors included, come from the file
```

---

## Layout

| path | what it does |
|---|---|
| `lrg/pool.py` | the adapter interface, as a `typing.Protocol` |
| `lrg/clmm.py` | concentrated liquidity: Cetus, Bluefin, Momentum, Turbos |
| `lrg/venues.py` | weighted AMM (Aftermath) and order book (DeepBook) |
| `lrg/discovery.py` | pool universe: GeckoTerminal plus DeepBook and Aftermath, TVL checked on chain |
| `lrg/book.py` | pool state, fetched once and cached, dispatched by venue |
| `lrg/topology.py` | which routes exist between two assets |
| `lrg/curves.py` | split execution, leg cost curves, route cost and capacity |
| `lrg/graph.py` | the route graph and its network view |
| `lrg/matrix.py` | capacity matrix, cost budget, parameter curves |
| `lrg/analysis.py` | frames and views over the matrix |
| `lrg/protocols/navi.py` | Navi's coin types, risk parameters, venue set |

Nothing in `lrg/` imports `lrg.protocols`. Asset sets and parameters are passed in, so adding a protocol means adding a module under `protocols/` and nothing else.

Adding a venue means writing a class that satisfies `lrg.pool.Pool` — five methods and nine attributes. Route enumeration, cost composition and capacity inversion are untouched by it.

---

## Modelling choices

| choice | what LRG does | why |
|---|---|---|
| route selection | every simple path up to 4 hops, top 20 by bottleneck depth, best by capacity | a thin direct pool loses to a deeper two-hop; shortest-path gets this wrong |
| per leg | all pools between the two tokens, split by marginal value | what an aggregator does; `pick="best"` gives the single-pool floor |
| multi-hop cost | composed leg by leg, output of one is input to the next | `min` over legs overstates a two-hop by roughly 2x |
| reference price | oracle, not pool spot | the liquidator was credited at the oracle rate |
| no route | capacity zero, with a reason | absence of an exit is the worst case, not a missing datum |
| zero-volume pools | flagged, not filtered | standing liquidity that does not trade is not a demonstrated exit |
| debt set | adversarial: the worst permitted debt asset | a cap cannot rely on which pair a liquidation resolves into |

### What the numbers are not

**A conservative lower bound, not a forecast.** LRG routes a trade through one best path with splitting inside each leg. A production aggregator also splits *across* paths, so real execution is cheaper. On Sui — six venues, four pool models, an order book — the gap is measurable; on chains where two or three Uniswap-shaped venues carry everything it is small. Erring low is the right direction for setting a cap.

**A single-block snapshot.** Capacity is measured at current pool state. A liquidation happens after the price has already moved by at least the LT−LTV buffer, in the direction that drains the side being sold, and possibly after liquidity providers have withdrawn. Measured capacity is an upper bound on capacity under stress.

**Not additive.** Pair capacities are computed independently, but routes share pools — on Sui almost everything passes through SUI or USDC. Summing capacities across assets overstates the total. `hub_load` shows where the overlap is.

---

## Sui encoding notes

Reading pool state directly took some spelunking, recorded here because it is not documented in one place.

- JSON-RPC on Sui Foundation fullnodes was retired on 28 July 2026. This uses GraphQL at `https://graphql.mainnet.sui.io/graphql`.
- Square-root prices and tick prices are X64 fixed point: divide by 2^64.
- Fee rates use a denominator of 10⁶ across every CLMM venue seen so far (`500` → 0.05%).
- Tick indices are two's-complement `u32`; `liquidity_net` is two's-complement `u128`.
- The tick index lives in a different place per venue: in the value for Cetus (wrapped in a `skip_list::Node`) and Bluefin (a plain `Table` entry), and in the **dynamic-field key** for Momentum. Cetus keys by a `u64` score equal to `tick + 443636`.
- Balances appear as `coin_a`/`coin_b` on Cetus and Bluefin, `reserve_x`/`reserve_y` on Momentum.
- Turbos keeps ticks as dynamic fields on the pool object itself rather than in a separate table.
- DeepBook's order books are at `deepbook-indexer.mainnet.mystenlabs.com/orderbook/{PAIR}?level=2&depth=N`; its pool metadata is at `/get_pools`. GeckoTerminal does not index it.
- Aftermath is a Balancer-style weighted invariant with a `flatness` parameter interpolating toward constant sum. Pools with `flatness > 0` are priced as pure weighted here, which understates them — conservative.

`clmm.inspect(pool_id)` and `clmm.inspect_fields(uid)` dump a pool struct and its dynamic fields, which is how a new venue's layout gets read off.

---

## Known limitations

- **No multi-path splitting.** One best path per pair, split within legs only.
- **No position data.** Demand is computed from the supply cap, so the output describes what the parameters permit rather than what the book currently holds. `beta` is the placeholder for this and defaults to the adversarial 1.
- **No debt-side bound.** A pair's demand should be capped by the debt asset's own borrowable amount as well as the collateral's supply cap. Not yet applied, so demand against thinly-supplied debt assets is overstated.
- **Derived prices.** USD prices are propagated from USDC through pool spot rates, deepest pools first. Supply oracle prices via `Params(prices=...)` where accuracy matters.
- **E-Mode not modelled.** Boosted LTV and LT are published; the E-Mode liquidation bonus is not, so rows use normal-mode parameters. `Asset` carries the E-Mode fields for when it is resolved.
- **Close factor unresolved.** Navi's docs say "up to 35%", the newer hub says 50%. It scales demand linearly. `Params(close_factor=...)`.
- **Venues not covered.** Magma, fullsail, flowx, kriya and ferra-dlmm are discovered but unpriced — roughly $2-3M of Sui TVL. DLMM is a different model and would need its own adapter.
- **Single protocol.** Liquidations from other lending markets on the same chain compete for the same pools. Not modelled.

---

## Methodology

`docs/` holds the written derivation in two parts: the route graph — construction, structure, and how edge weights are formed from fees and per-pool-type slippage — and the parameter system — the constraint set, the capacity matrix, and the curves along which parameters can be traded against one another.

---

## License

Code is licensed under Apache-2.0 (see `LICENSE`).

The methodology documents in `docs/` are licensed under **CC BY 4.0** — reuse and adaptation permitted with attribution to Ipak Systems.

---

**Ipak Systems** — independent DeFi risk research. [ipak.systems](https://www.ipak.systems)

Corrections and venue adapters welcome. If a number looks wrong, it may well be: `clmm.sanity(pool, oracle_price=...)` is the first check, and the open items above are the known suspects.
