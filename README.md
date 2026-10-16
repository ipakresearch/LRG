# LRG — Liquidation Route Graph

What a liquidation actually costs to execute on-chain, and what that implies for lending-market risk parameters.

A lending protocol's liquidation bonus has to cover the cost of converting seized collateral back into the debt asset. That conversion is a path through the chain's trading venues, and its cost depends on which pools exist, how deep they are at the current price, how many hops the path takes, and how much is being sold. LRG builds that path graph from live pool state and inverts it: for each collateral and debt asset, how much can clear the routes within the bonus available to a liquidator.

The reference instance is [Navi Protocol](https://naviprotocol.io) on Sui — 24 assets, 506 ordered liquidation pairs, six trading venues plus LST minting. The engine itself is protocol- and chain-agnostic; everything specific to Navi lives under `lrg/protocols/`.

---

## The condition

A liquidator repays debt `D` and receives collateral worth `(1 + b)·D`, where `b = (1 − tf)·LB` is the bonus net of the protocol's cut. On Navi, `tf` is the collateral reserve's `treasury_factor` (0.55–0.85): the protocol takes `tf` of the bonus out of the seized collateral and keeps it in the pool, so that part is neither earned nor sold by the liquidator. They sell that collateral at a total cost `c` — swap fees plus price impact, referenced to the oracle rate — and must end with at least `(1 + φ)·D` to cover the repayment and the flash-loan fee:

    (1 + b) * (1 - c) >= 1 + phi      which is      c <= 1 - (1 + phi) / (1 + b)

The right-hand side is the **cost budget**. The familiar first-order form `b − φ` overstates it by roughly `b` squared, because it measures the loss against the pre-bonus base while the quantity being sold is the post-bonus amount. For SUI collateral on Navi (LB 10%, treasury factor 0.65, so `b` = 3.5%) the exact budget is **3.29%**, not 3.41%; the gap grows with `b`, to 1.6 percentage points at `b` = 15%. LRG uses the exact form.

The quantity that has to clear is the collateral a full liquidation would put on the market:

    Q = beta * CF * (1 + b) * LTV * S * P

with `CF` the close factor, `S` the supply cap, `P` the oracle price, and `beta` the share of the book liquidatable at once. `beta = 1` is adversarial and is the default.

A liquidation also cannot repay more debt than exists, so the daily run bounds demand per pair by the debt asset `A`:

    Q_cap(B, A) = beta * CF * (1 + b_B) * min(LTV_B * S_B * P_B,  max(borrowed_A, borrow_cap_ratio_A * S_A) * P_A)

with `borrowed_A` the users' outstanding debt in `A` (existing debt can exceed a lowered borrow cap). Row minima and this bound range over the debt set: assets that can be borrowed, plus any that still carry debt. An asset with no pool stays in the matrix with capacity 0.

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

Prices are keyed by coin type, never by symbol: two coins can share a symbol (native and Wormhole USDC are both "USDC" on chain). `Params(prices=...)` takes `{coin type: USD}`; `M.price(symbol)` looks a Navi asset up through `M.tokens`.

### The daily run

```bash
python run.py
```

reads Navi's live parameters and oracle prices, every pool on the routes and every borrower's position, and writes `data/navi/YYYY-MM-DD/` (UTC):

| file | contents |
|---|---|
| `matrix.csv` | every pair: capacity, path, saturated (capacity at the $50M grid top, i.e. "at least"), coverage and the failed pools |
| `adequacy.csv` | every pair: demand now and at caps (`Q_cap`, debt-bounded), the E-Mode check, status |
| `stress.csv` | every (scenario, shock, collateral, debt) with liquidations, and whether its Q is material (≥ `min_sold_usd`) |
| `stress_breaks.csv` | per (collateral, debt, scenario): the first shock at which the pair fails materially, with D, Q, Q*, positions and coverage there, or `holds`; `non_monotone` flags a pair that fails at one shock but not at a larger one |
| `summary.json` | per collateral: Q* (over material debts) and `Q_star_all`, binding debt, signal 1, adequacy status, breaking shock or `holds`, LST depeg; dust positions; per-scenario unpriced positions and bad debt; live parameters |
| `matrix.json`, `positions.json.gz` | the matrix (with E-Mode capacities) and the positions stress ran on, for `--from-run` |
| `coverage.json` | pools read / failed / empty, coverage share, prices and their source, positions and the health-factor check |
| `config.json` | parameters, treasury factors, debt set and excluded debts, the `Q_cap` formula |

It exits 2 when the result is unusable (a network failure while reading pools, nothing read). `python -m unittest discover tests` runs the tests.

`python run.py --from-run YYYY-MM-DD` redoes only stress for a saved run, from its `matrix.json` and `positions.json.gz`, without reading the chain. It rewrites `stress.csv`, `stress_breaks.csv`, the stress and Q* fields of `summary.json` and the params in `config.json`. `--min-position-usd` and `--min-sold-usd` override the floors in either mode.

To rebuild the pool universe (GeckoTerminal, DeepBook, Aftermath and the LST mint rows, TVL checked on chain):

```python
from lrg import discovery
from lrg.protocols import navi, navi_oracle
prices = {ct: v["price"] for ct, v in navi_oracle.quotes().items()}
rows, dropped = discovery.discover_all(navi.COIN_TYPES, navi.VENUES, prices)
discovery.write_rows(rows, "data/navi_pools.new.csv")     # diff before replacing navi_pools.csv
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
| `lrg/coins.py` | coin identity: normalized coin types |
| `lrg/clmm.py` | concentrated liquidity: Cetus, Bluefin, Momentum, Turbos |
| `lrg/venues.py` | weighted AMM (Aftermath), order book (DeepBook), LST minting (SUI → vSUI/haSUI/stSUI, one way) |
| `lrg/discovery.py` | pool universe: GeckoTerminal plus DeepBook and Aftermath, TVL checked on chain |
| `lrg/book.py` | pool state, fetched once and cached, dispatched by venue |
| `lrg/topology.py` | which routes exist between two assets |
| `lrg/curves.py` | split execution, leg cost curves, route cost and capacity |
| `lrg/graph.py` | the route graph and its network view |
| `lrg/matrix.py` | capacity matrix, cost budget, parameter curves |
| `lrg/analysis.py` | frames and views over the matrix |
| `lrg/protocols/navi.py` | Navi's coin types, risk parameters, treasury factors, venue set |
| `lrg/protocols/navi_oracle.py` | Navi's on-chain oracle prices, decoded per asset |
| `run.py` | the daily run: live parameters, matrix, adequacy, positions, stress |
| `positions.py` | every Navi borrower's position from chain, health factors checked against Navi's own |
| `tests/` | `unittest` suites: reads, curves, coverage, stress, LST mint |

Nothing in `lrg/` imports `lrg.protocols`. Asset sets and parameters are passed in, so adding a protocol means adding a module under `protocols/` and nothing else.

Adding a venue means writing a class that satisfies `lrg.pool.Pool` — five methods and nine attributes. Route enumeration, cost composition and capacity inversion are untouched by it.

---

## Modelling choices

| choice | what LRG does | why |
|---|---|---|
| route selection | every simple path up to 4 hops, ranked by route cost at $1k, $10k and $250k; the cheapest 6 at each go to the capacity search | listed TVL is stale and in no common unit; a thin direct pool loses to a deeper two-hop, and thin routes can be best only at small sizes |
| per leg | every pool between the two tokens is read; the 4 giving the most out at $10k are kept and the trade is split across them by marginal value | what an aggregator does; failed and empty pools never take a slot; `pick="best"` gives the single-pool floor |
| swap side | from the pool's own on-chain coin order | listings often reverse the pair |
| capacity | cost interpolated in log size on a grid, then refined on the exact composition | the reported capacity's exact cost is within the budget |
| multi-hop cost | composed leg by leg, output of one is input to the next | `min` over legs overstates a two-hop by roughly 2x |
| reference price | Navi's on-chain oracle, each asset decoded with its own decimal; coins it does not price get a price derived from pools | the liquidator is credited at the oracle rate |
| no route | capacity zero, with a reason | absence of an exit is the worst case, not a missing datum |
| zero-volume pools | flagged, not filtered | standing liquidity that does not trade is not a demonstrated exit |
| debt set | adversarial: the worst debt asset that can be borrowed or still carries debt | a cap cannot rely on which pair a liquidation resolves into |
| coverage | a pair is `incomplete` when a failed read could have changed it; never reported red or amber | a missing pool is not evidence of a thin route |
| stress | each position below HF 1 is liquidated: debt split by borrowing power, repayment capped at CF x collateral value per Navi, E-Mode bonus where it applies | follows Navi's `calculate_liquidation` |
| materiality | positions under $100 of debt are dust, left out of stress and counted; a pair fails only if Q > Q* and Q ≥ $1,000; Q* ranges over debts where some scenario reaches $1,000 (`Q_star_all` does not) (`Params.min_position_usd`, `min_sold_usd`) | a sub-dollar liquidation against a zero cell is not a market failure |
| breaking shock | the first shock at which a complete pair fails materially; the baseline is shock 0 of every scenario; `holds` if none up to 50%; pairs that recover at a larger shock are flagged, not hidden | Q can fall with the shock, as collateral value drops |

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
- Turbos keeps ticks as dynamic fields on the pool object itself, among every LP position (20,000+ on deep pools), so paging finds them too late. Its `tick_map` (`Table<I32, u256>`) marks initialized ticks, (word·256 + bit)·spacing, and each tick is fetched by key with `multiGetDynamicFields`.
- Cetus, Bluefin and Momentum tick tables record their `size`; a read that returns fewer fields is treated as truncated and the pool is re-read (pages are served at the latest checkpoint, so a busy table can change mid-read).
- DeepBook's order books are at `deepbook-indexer.mainnet.mystenlabs.com/orderbook/{PAIR}?level=2&depth=N`; its pool metadata is at `/get_pools`. GeckoTerminal does not index it. The indexer runs out of gas at `depth=200` on deep books; the reader steps down to 100 and 50.
- Navi's `LiquidationEvent.collateral_amount` is gross: liquidator plus treasury. The liquidator's bonus is `LB·(1 − treasury_factor)`.
- vSUI, haSUI and stSUI mint atomically at their protocol rate (Volo `stake_pool::stake`, Haedal `staking::request_stake_coin`, AlphaFi `liquid_staking::mint`); the rate is read by simulating a mint.
- Aftermath is a Balancer-style weighted invariant with a `flatness` parameter interpolating toward constant sum. Pools with `flatness > 0` are priced as pure weighted here, which understates them — conservative.

`clmm.inspect(pool_id)` and `clmm.inspect_fields(uid)` dump a pool struct and its dynamic fields, which is how a new venue's layout gets read off.

---

## Known limitations

- **No multi-path splitting.** One best path per pair, split within legs only.
- **Close factor on debt in `Q_cap`.** Navi caps a liquidation at CF × the borrower's *collateral* value; `Q = β·CF·(1+b)·LTV·S·P` applies CF to the debt, which understates demand by about 1/LTV at the threshold. Stress applies Navi's form; `Q_cap` does not yet.
- **LST oracle.** Navi prices vSUI, haSUI and stSUI near the SUI price, 3.6–8.5% below their own redemption rate. Debt in these assets cannot be liquidated at a profit at Navi's bonuses, and LST collateral sells for more than Navi values it. LST redemption (LST → SUI) is atomic on chain but deliberately not modelled as an exit.
- **Pool universe.** GeckoTerminal's free tier stops at 10 pages per token, so the deepest-traded tokens' lists are truncated (a warning names them). Listed TVL is replaced by on-chain reserves; pools under $5k are dropped.
- **Coverage heuristic.** A failed pool counts against a leg by its on-chain TVL, since a pool that did not read cannot be ranked by output.
- **Venues not covered.** Magma, fullsail, flowx, kriya and ferra-dlmm are discovered but not routed. DLMM is a different model and would need its own adapter.
- **Single protocol.** Liquidations from other lending markets on the same chain compete for the same pools. Not modelled.

---

## License

Code is licensed under Apache-2.0 (see `LICENSE`).

---

**Ipak Systems** — independent DeFi risk research. [ipak.systems](https://www.ipak.systems)

Corrections and venue adapters welcome. If a number looks wrong, it may well be: `clmm.sanity(pool, oracle_price=...)` is the first check, and the open items above are the known suspects.
