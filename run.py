"""
Daily Navi capacity run.

Fetches live pool state, builds the capacity matrix at Navi's current defaults
(top-20 candidate paths per pair), and writes to data/navi/YYYY-MM-DD/ (UTC):

    matrix.csv      every pair: collateral, debt, capacity_usd, path, and
                    coverage ("complete", or "incomplete" when a failed read
                    could have changed the pair) and the failed pools with TVL
    summary.json    per collateral: Q* (row minimum) and the binding debt asset,
                    the minimum over nonzero cells and its debt asset, and the
                    debt assets with zero capacity
    adequacy.csv    every pair: liquidation demand now and at caps, bounded
                    by the debt asset's borrowing, against the pair's capacity
    stress.csv      every (scenario, shock, collateral, debt) with liquidations:
                    debt of positions pushed below HF 1, the collateral it puts
                    on the market, and the pair's capacity
    config.json     the run's parameters, treasury factors, the debt set and the
                    excluded debts with reasons, and the Q_cap formula
    coverage.json   pools needed and read, each failed pool with its reason,
                    Navi reserves the matrix does not track, and the positions
                    read with the health-factor check against Navi's own

summary.json also carries each collateral's adequacy status and the live Navi
parameters, read from the Storage object on chain, with their differences from
lrg/protocols/navi.py. The matrix is built with the live parameters.

Prices are Navi's on-chain oracle prices (lrg/protocols/navi_oracle.py), each
decoded with its own decimal, keyed by normalized coin type. Coins with no
oracle price, such as route intermediates Navi does not list, keep the price
derived from pool spot rates; coverage.json records each coin's price_source
("oracle" or "derived"). Symbols are display labels only.

Status, per pair and per collateral (worst pair):
    red    Q_now > Q*            current positions already exceed the routes
    amber  Q_now <= Q* < Q_cap   fine now, not at the caps
    green  Q_cap <= Q*           fine even at the caps

Q_cap, one definition for adequacy and signal 1 (pair_demand):
    Q_cap(B, A) = beta·CF·(1+b_B)·min(LTV_B·S_B·P_B, max(borrowed_A, cap_A·S_A)·P_A)
with b_B = (1 - treasury_factor_B)·LB_B. Q_now uses current supply and current
user debt in A.

Signal 1 (summary.json "signal_1"), per collateral: fails if any pair's Q_cap
exceeds its Q*.

Debt set: Q* (row minima), adequacy and signal 1 range over borrowable assets
plus any non-borrowable asset that still carries user debt. Assets that can't
be borrowed and carry no debt keep their cells in matrix.csv but are left out;
config.json lists both sets with reasons. Every Navi asset is in the matrix:
one with no pool has capacity 0 in every pair.

Stress (summary.json "breaking_shock"): every Navi borrower with at least $1 of
debt is read from chain (positions.py). Prices drop by s = 5%, 10%, ..., 50%,
one price group at a time: the SUI family, the BTC family, each other volatile
asset alone; stables never drop. s = 0 is the baseline. Separately, each of
vSUI, haSUI and stSUI depegs by 2/5/10% against a flat SUI (the "depeg" block).
A position whose HF falls below 1 is liquidated (position_flows): its debt is
split across its collateral by borrowing power c·P·LT (E-Mode LT where it
applies); a liquidator repays at most min(that share, CF x the collateral's
value) per pair, as Navi caps a call; the collateral seized is
beta·(1+(1-tf)·LB)·base, with the E-Mode bonus for E-Mode positions, whose
tranche is also checked against Q* at the E-Mode budget. Protected positions
stay in (only a designated liquidator may liquidate them, and each has one).
A position with an unpriced reserve is counted as unpriced, and one with no
collateral value as bad debt (summary.json "stress_scenarios"); neither is
silently dropped. The breaking shock is the smallest s at which a pair with
complete coverage in B's row fails. Q* is measured before the shock, so
breaking shocks are upper bounds.

Signal 1 also checks E-Mode-eligible pairs at the E-Mode LTV and bonus.

Exit codes: 0 success, 1 the computation raised (including a health factor
that disagrees with Navi's own), 2 it finished but the result is unusable (a
pool failed with a network error, no pools read, no prices, no cells, or every
cell zero).

    python run.py
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import os
import random
import shutil
import subprocess
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)

from lrg import matrix                      # noqa: E402
from lrg.book import PoolBook, pool_key     # noqa: E402
from lrg.clmm import gql, Q_FIELDS, Q_OBJECT  # noqa: E402
from lrg.coins import norm_type            # noqa: E402
from lrg.curves import is_saturated         # noqa: E402
from lrg.protocols import navi              # noqa: E402
from lrg.protocols import navi_oracle       # noqa: E402
import positions as navi_positions          # noqa: E402

POOLS_CSV = os.path.join(ROOT, "data", "navi_pools.csv")
OUT_ROOT = os.path.join(ROOT, "data", "navi")

# Navi main market. The reserves table is read from this object on each run.
NAVI_STORAGE = "0xbb4e2f4b6205c2e2a2db47aeb4f830796ec7c005f88537ee775986639bc442fe"
RAY = 10 ** 27
NAVI_DECIMALS = 9               # Navi stores every balance and cap at 9 decimals

# Stress grid and price groups. Reserves not in navi.py are classed by their
# oracle price: within BTC_BAND of wBTC is BTC, within STABLE_BAND of $1 stable.
SHOCKS = tuple(round(0.05 * i, 2) for i in range(1, 11))     # 5% steps to 50%
LSTS = ("vSUI", "haSUI", "stSUI")
DEPEGS = (0.02, 0.05, 0.10)      # one LST down against SUI, SUI flat
SUI_FAMILY = {"SUI", "vSUI", "haSUI", "stSUI"}
BTC_FAMILY = {"wBTC", "xBTC", "LBTC", "MBTC", "WBTC.lz", "enzoBTC"}
STABLES = {"USDC", "suiUSDT", "suiUSDe", "USDSUI", "USDY", "FDUSD", "BUCK", "wUSDC"}
BTC_BAND, STABLE_BAND = 0.10, 0.02
MIN_DEBT_USD = 1.0              # smaller positions are dust: counted, not stressed
HF_SAMPLE, HF_TOL = 5, 1e-6     # positions checked against Navi's own HF
HF_SAMPLE_MIN_USD = 1_000.0     # below this, Navi's rounding to native decimals
                                # alone moves HF by more than HF_TOL


class Unusable(Exception):
    """The build finished but its output cannot be trusted."""


class _RecordingBook(PoolBook):
    """PoolBook that keeps a reference to itself, so the failure reasons that
    build() discards can be read back after the run."""
    instances: list = []

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        _RecordingBook.instances.append(self)


NETWORK_MARKERS = ("NameResolutionError", "Max retries exceeded", "ConnectionError",
                   "Read timed out", "ConnectTimeout", "Connection aborted")


def failure_kind(reason: str) -> str:
    if any(m in reason for m in NETWORK_MARKERS):
        return "network"
    if reason.startswith("truncated"):
        return "truncated"
    if "GraphQL error" in reason:
        return "graphql_error"
    if "no tick table found" in reason or "not this pool's tick table" in reason:
        return "tick_table_not_found"
    if ("no ranges built" in reason or "none parsed as a tick" in reason
            or "did not read" in reason or "tick table is empty but liquidity" in reason):
        return "parse_error"
    return "other"


def git_state() -> dict:
    def git(*args):
        try:
            return subprocess.run(["git", *args], cwd=ROOT, capture_output=True,
                                  text=True, check=True).stdout.strip()
        except Exception:
            return None
    status = git("status", "--porcelain", "lrg")
    return {"commit": git("rev-parse", "HEAD"),
            "lrg_dirty": bool(status) if status is not None else None}


def fetch_navi_live() -> dict:
    """
    Live risk parameters and balances for every Navi reserve.

    Returns {"assets": {symbol: {...}}, "untracked": [...]} where symbols are
    those of navi.COIN_TYPES. Reserves whose coin type is not there are listed
    under "untracked". Raises if a tracked asset has no reserve.
    """
    st = gql(Q_OBJECT, {"a": NAVI_STORAGE})["object"]["asMoveObject"]["contents"]
    if not st["type"]["repr"].endswith("::storage::Storage"):
        raise RuntimeError(f"{NAVI_STORAGE} is a {st['type']['repr']}, not Navi Storage")
    table = st["json"]["reserves"]["id"]

    nodes, after = [], None
    while True:
        d = gql(Q_FIELDS, {"a": table, "n": 50, "after": after})["address"]["dynamicFields"]
        nodes += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            break
        after = d["pageInfo"]["endCursor"]

    scale = RAY * 10 ** NAVI_DECIMALS
    sym_of = {norm_type(t): s for s, t in navi.COIN_TYPES.items()}
    assets, untracked = {}, []
    for n in nodes:
        v = n["value"]["json"]
        lf = v["liquidation_factors"]
        rec = {
            "reserve_id": int(v["id"]),
            "ltv": int(v["ltv"]) / RAY,
            "lt": int(lf["threshold"]) / RAY,
            "lb": int(lf["bonus"]) / RAY,
            "close_factor": int(lf["ratio"]) / RAY,
            "treasury_factor": int(v["treasury_factor"]) / RAY,
            "supply_cap": int(v["supply_cap_ceiling"]) / scale,
            "borrow_cap_ratio": int(v["borrow_cap_ceiling"]) / RAY,
            "is_isolated": bool(v["is_isolated"]),
            # new borrowing allowed: a positive borrow cap and not isolated
            "borrowable": int(v["borrow_cap_ceiling"]) > 0 and not v["is_isolated"],
            "supplied": (int(v["supply_balance"]["total_supply"])
                         * int(v["current_supply_index"]) / scale),
            "borrowed": (int(v["borrow_balance"]["total_supply"])
                         * int(v["current_borrow_index"]) / scale),
        }
        sym = sym_of.get(norm_type(v["coin_type"]))
        if sym is None:
            untracked.append({"reserve_id": rec["reserve_id"],
                              "coin_type": norm_type(v["coin_type"]),
                              "ltv": rec["ltv"],
                              "borrow_cap_ratio": rec["borrow_cap_ratio"]})
        else:
            assets[sym] = rec

    missing = sorted(set(navi.COIN_TYPES) - set(assets))
    if missing:
        raise RuntimeError(f"no Navi reserve for {', '.join(missing)}")
    cfs = {round(a["close_factor"], 9) for a in assets.values()}
    if len(cfs) != 1:
        raise RuntimeError(f"close factor differs across assets ({sorted(cfs)}); "
                           "Params takes a single value")
    return {"assets": assets, "untracked": sorted(untracked, key=lambda r: r["reserve_id"])}


def live_asset_table(live: dict) -> dict:
    """symbol -> (ltv, lt, lb, supply_cap, emode_ltv, emode_lt), the shape
    matrix.build() takes. LTV 0 on chain means the asset is not collateral.
    E-Mode values are not on the reserve and stay as in navi.py."""
    table = {}
    for s, a in live["assets"].items():
        if s not in navi.ASSETS:
            continue
        emode = navi.ASSETS[s][4:6]
        table[s] = (a["ltv"] or None, a["lt"], a["lb"], a["supply_cap"], *emode)
    return table


def param_diffs(live: dict) -> list:
    """Where the chain disagrees with lrg/protocols/navi.py."""
    out = []
    names = ("ltv", "lt", "lb", "supply_cap")
    for s, row in navi.ASSETS.items():
        a = live["assets"][s]
        for i, k in enumerate(names):
            static = row[i] if row[i] is not None else 0.0
            if abs(static - a[k]) > 1e-9 * max(1.0, abs(static)):
                out.append({"asset": s, "field": k, "navi_py": row[i], "chain": a[k]})
    for s, tf in navi.TREASURY_FACTORS.items():
        if s in live["assets"] and abs(tf - live["assets"][s]["treasury_factor"]) > 1e-9:
            out.append({"asset": s, "field": "treasury_factor", "navi_py": tf,
                        "chain": live["assets"][s]["treasury_factor"]})
    if abs(navi.CLOSE_FACTOR - next(iter(live["assets"].values()))["close_factor"]) > 1e-9:
        out.append({"asset": "*", "field": "close_factor", "navi_py": navi.CLOSE_FACTOR,
                    "chain": next(iter(live["assets"].values()))["close_factor"]})
    return out


def oracle_prices() -> tuple:
    """
    Navi's oracle prices for every reserve, keyed by normalized coin type.
    Returns (prices {coin type: USD}, info {coin type: {symbol, reserve, age_s}}).
    """
    q = navi_oracle.quotes()
    prices = {ct: v["price"] for ct, v in q.items()}
    info = {ct: {"symbol": v["symbol"], "reserve": v["reserve"], "age_s": v["age_s"]}
            for ct, v in q.items()}
    return prices, info


def price_labels(M, book) -> dict:
    """coin type -> display label: navi.py's symbol where there is one, else
    the pool's on-chain symbol. Labels can repeat; keys never do."""
    out = {}
    for p in book.pools.values():
        for ct, sym in ((p.coin_a, p.sym_a), (p.coin_b, p.sym_b)):
            out.setdefault(norm_type(ct), sym)
    out.update({t: s for s, t in M.tokens.items()})
    return out


def compute():
    live = fetch_navi_live()
    cf = next(iter(live["assets"].values()))["close_factor"]
    oracle, oracle_info = oracle_prices()
    params = matrix.Params(close_factor=cf, beta=1.0, prices=oracle,
                           treasury_factors={s: a["treasury_factor"]
                                             for s, a in live["assets"].items()})
    _RecordingBook.instances.clear()
    original, matrix.PoolBook = matrix.PoolBook, _RecordingBook
    try:
        # cache=None: always read live state, never a stale on-disk cache
        M = matrix.build(POOLS_CSV, navi.COIN_TYPES, live_asset_table(live),
                         navi.VENUES, cache=None, params=params)
    finally:
        matrix.PoolBook = original

    if len(_RecordingBook.instances) != 1:
        raise RuntimeError(f"expected build() to create one PoolBook, saw "
                           f"{len(_RecordingBook.instances)}; coverage cannot be read")
    book = _RecordingBook.instances[0]
    if len(book.pools) != M.pools_read or len(book.failed) != M.pools_failed:
        raise RuntimeError("PoolBook counts disagree with the matrix; "
                           "coverage cannot be read")

    net = [key for key, why in book.failed.items() if failure_kind(why) == "network"]
    if net:
        # a transient outage, not missing liquidity: publishing would understate Q*
        raise Unusable(f"{len(net)} pools failed with network errors; rerun")
    trunc = [key for key, why in book.failed.items() if failure_kind(why) == "truncated"]
    if trunc:
        print(f"WARNING: {len(trunc)} pools truncated (counted as failed): "
              f"{', '.join(k[0][:12] for k in trunc)}", file=sys.stderr)
    if not M.pools_read:
        raise Unusable("no pools read")
    if not M.prices:
        raise Unusable("no prices derived")
    if not M.cells:
        raise Unusable("no cells computed")
    if not any(v for v in M.cells.values()):
        raise Unusable("every cell has zero capacity")

    for sym, a in M.assets.items():
        a.supplied = live["assets"][sym]["supplied"]

    state = navi_positions.read_state()
    pos = navi_positions.read_positions(state, min_debt_usd=MIN_DEBT_USD)
    navi_positions.refresh_prices(state)      # stress at prices from after the read
    pos["hf_check"] = check_health_factors(state, pos["positions"])
    pos["state"] = state
    live["oracle"] = {"info": oracle_info}

    # The debt set: borrowable assets, plus any that still carry user debt
    # (a liquidation of that debt must buy it). The rest leave row minima
    # and Q*; their cells stay in matrix.csv.
    sym = reserve_symbols(state)
    pos["borrowed"] = {sym[rid]: t for rid, t in pos["debt_tokens_all"].items() if rid in sym}
    live["debt_set"] = debt_set(live, pos["borrowed"])
    params.excluded_debts = tuple(sorted(live["debt_set"]["excluded"]))
    return params, M, book, live, pos


def debt_set(live: dict, borrowed: dict) -> dict:
    """Which assets count as debt for row minima and Q_cap, and why."""
    included, excluded = {}, {}
    for s, a in sorted(live["assets"].items()):
        owed = borrowed.get(s, 0.0)
        if a["borrowable"]:
            included[s] = "borrowable"
        elif owed > 0:
            included[s] = f"not borrowable, {owed:,.6g} tokens of user debt outstanding"
        else:
            excluded[s] = ("isolated" if a["is_isolated"] else "borrow cap 0") + ", no user debt"
    return {"included": included, "excluded": excluded}


def check_health_factors(state: dict, positions: list) -> dict:
    """
    Compare the off-chain HF with Navi's own for HF_SAMPLE random positions
    with at least HF_SAMPLE_MIN_USD of debt, at least one of them in e-mode
    when any is. Raises if any differs by more than HF_TOL, relative: the
    stress check rests on the off-chain HF.
    """
    rng = random.Random()
    px = state["prices"]
    big = [p for p in positions
           if sum(t * px.get(rid, 0.0) for rid, t in p["debt"].items()) >= HF_SAMPLE_MIN_USD]
    emode = [p for p in big if p["emode"] is not None]
    sample = rng.sample(emode, 1) if emode else []
    rest = [p for p in big if p not in sample]
    sample += rng.sample(rest, min(HF_SAMPLE - len(sample), len(rest)))
    # Re-read the sample's balances just before the simulation, and price them
    # with the oracle prices read in the same request as Navi's HF: both move
    # during the long position read.
    # Reserve indices too: re-read Storage, since they update during the read.
    now = navi_positions.read_state()
    users = [p["user"] for p in sample]
    fresh = navi_positions.read_user_positions(now, users)
    navi_hf, sim_prices = navi_positions.navi_health_factors(now, users)
    at_sim = dict(now, prices=sim_prices)
    rows = []
    for p, hn in zip(fresh, navi_hf):
        hm = navi_positions.health_factor(at_sim, p)
        rel = abs(hm - hn) / hn if hn else (0.0 if hm == hn else float("inf"))
        rows.append({"user": p["user"], "emode": p["emode"], "navi_hf": hn,
                     "computed_hf": hm, "rel_diff": rel})
    bad = [r for r in rows if not r["rel_diff"] <= HF_TOL]      # NaN counts as bad
    if bad:
        raise RuntimeError(f"health factor disagrees with Navi's for {len(bad)} of "
                           f"{len(rows)} positions: {json.dumps(bad)}")
    return {"tolerance": HF_TOL, "min_debt_usd": HF_SAMPLE_MIN_USD, "positions": rows}


# incomplete outranks green: "green" is never claimed over a pair a failed read
# could have changed, but a confirmed red or amber still shows
STATUS_RANK = {"red": 4, "amber": 3, "incomplete": 2, "green": 1, "unknown": 0}


def pair_demand(k: float, q_now_B, q_cap_B, pA, la: dict, borrowed_A: float) -> tuple:
    """
    (Q_now, Q_cap) for one (collateral B, debt A) pair: the one definition used
    by adequacy and signal 1.

        Q(B, A) = beta·CF·(1+b_B) · min(LTV_B·S_B·P_B,  debt_A·P_A)

    k = beta·CF·(1+b_B); q_now_B / q_cap_B are k·LTV_B·S_B·P_B at current
    supply and at the supply cap. debt_A is the user debt outstanding in A
    for Q_now, and for Q_cap the most A can be borrowed:
    max(borrowed_A, borrow_cap_ratio_A·S_A), since existing debt can exceed a
    lowered cap. With no price for A the debt side is left unbounded
    (conservative).
    """
    if q_now_B is None or q_cap_B is None:
        return None, None
    if pA is None:
        return q_now_B, q_cap_B
    maxdebt = max(borrowed_A, la["borrow_cap_ratio"] * la["supply_cap"])
    return min(q_now_B, k * borrowed_A * pA), min(q_cap_B, k * maxdebt * pA)


def emode_terms(state: dict, sym: dict) -> dict:
    """(collateral, debt) symbol pairs that an active E-Mode pair allows ->
    [(ltv, lb)] of the collateral in each such pair."""
    out = {}
    for pair in state["emode_pairs"].values():
        coll = [r for r, a in pair.items() if a["collateral"]]
        debt = [r for r, a in pair.items() if a["debt"]]
        for b in coll:
            for a in debt:
                if b in sym and a in sym:
                    out.setdefault((sym[b], sym[a]), []).append(
                        (pair[b]["ltv"], pair[b]["lb"]))
    return out


def adequacy(params, M, live: dict, coverage_of, borrowed: dict, state: dict) -> tuple:
    """
    Per pair: demand now and at caps against the pair's capacity.

    Demand is Matrix.demand for the collateral, bounded by the debt asset's
    borrowing: a liquidation repays at most CF of the debt outstanding in A,
    and seizes (1 + b) times that in collateral.

        Q_now = min(demand(B, current), beta·CF·(1+b)·borrowed_A·P_A)
        Q_cap = min(demand(B, cap),     beta·CF·(1+b)·maxdebt_A·P_A)

    maxdebt_A is the most A can be borrowed at its caps: the borrow-cap ratio
    times A's supply cap, and never less than what is already borrowed.

    borrowed_A is the sum of every user's debt in A (tokens), not the reserve's
    recorded total: for Navi's oldest reserves the two differ widely, and only
    debt held by a user can be liquidated.
    """
    a_live = live["assets"]
    excluded = set(params.excluded_debts or ())
    emode = emode_terms(state, reserve_symbols(state))
    pairs, per_coll = [], {}
    for B in sorted({b for b, _ in M.cells}):
        a = M.assets[B]
        q_now_B, q_cap_B = M.demand(B, "current"), M.demand(B, "cap")
        k = params.beta * params.close_factor * params.seized_factor(a.lb, params.tf(B))
        rows = []
        for (b, A), qs in sorted(M.cells.items()):
            if b != B:
                continue
            if A in excluded:
                continue
            pA = M.price(A)
            q_now, q_cap = pair_demand(k, q_now_B, q_cap_B, pA, a_live[A],
                                       borrowed.get(A, 0.0))
            # E-Mode: users may enter it, so an eligible pair is also checked
            # at the E-Mode LTV and bonus, against Q* at the E-Mode budget
            q_cap_e = q_star_e = None
            pB = M.price(B)
            for ltv_e, lb_e in emode.get((B, A), []):
                if pB is None or not ltv_e:
                    continue
                k_e = params.beta * params.close_factor * params.seized_factor(lb_e, params.tf(B))
                _, qc = pair_demand(k_e, 0.0, k_e * ltv_e * a.supply_cap * pB, pA,
                                    a_live[A], borrowed.get(A, 0.0))
                qs_e = M.pair_capacity(B, A, lb_e)
                if q_cap_e is None or (qs_e is not None and qc - qs_e > q_cap_e - (q_star_e or 0)):
                    q_cap_e, q_star_e = qc, qs_e
            if q_now is None:
                status = "unknown"
            elif q_now > qs:
                status = "red"
            elif q_cap > qs:
                status = "amber"
            else:
                status = "green"
            cov = coverage_of((B, A))
            status_if_complete = status
            if cov["status"] == "incomplete" and status != "unknown":
                status = "incomplete"          # never red or amber on an incomplete read
            path = M.paths.get((B, A))
            rows.append({"collateral": B, "debt": A, "Q_now": q_now, "Q_cap": q_cap,
                         "Q_star": qs, "Q_star_saturated": is_saturated(qs),
                         "Q_cap_emode": q_cap_e, "Q_star_emode": q_star_e,
                         "status": status, "status_if_complete": status_if_complete,
                         "coverage": cov["status"], "failed_pools": cov["failed_pools"],
                         "debt_price_missing": pA is None,
                         "path": " → ".join(path) if path else ""})
        pairs += rows

        def pressure(r):
            """How far demand sits above capacity: Q_now/Q* for red, else Q_cap/Q*."""
            q = r["Q_now"] if r["status"] == "red" else r["Q_cap"]
            if q is None:
                return -1.0
            return float("inf") if not r["Q_star"] else q / r["Q_star"]

        worst = max(rows, key=lambda r: (STATUS_RANK[r["status"]], pressure(r)))
        per_coll[B] = {
            "status": worst["status"],
            "binding_debt": worst["debt"],
            "binding_Q_now": worst["Q_now"], "binding_Q_cap": worst["Q_cap"],
            "binding_Q_star": worst["Q_star"],
            "coverage": worst["coverage"],
            "pairs": {s: sum(r["status"] == s for r in rows)
                      for s in ("red", "amber", "incomplete", "green", "unknown")},
            "incomplete_pairs": sorted(r["debt"] for r in rows if r["coverage"] == "incomplete"),
            "s_max": M.s_max(B, a.ltv, a.lb),
            "s_max_debt_bounded": s_max_debt_bounded(params, M, borrowed, B),
        }
    return pairs, per_coll


def s_max_debt_bounded(params, M, borrowed: dict, B: str):
    """
    Largest supply cap of B, in tokens, at which no pair is red given current
    borrowing in each debt asset. A pair whose debt-side limit is already
    within its capacity puts no bound on B's cap. None means no pair binds.
    """
    a, pB = M.assets[B], M.price(B)
    if not a.ltv or not pB:
        return None
    k = params.beta * params.close_factor * params.seized_factor(a.lb, params.tf(B))
    bounds = []
    excluded = set(params.excluded_debts or ())
    for (b, A), qs in M.cells.items():
        if b != B or A in excluded:
            continue
        pA = M.price(A)
        debt_lim = k * borrowed.get(A, 0.0) * pA if pA else float("inf")
        if debt_lim > qs:
            bounds.append(qs / (k * a.ltv * pB))
    return min(bounds) if bounds else None


def signal_1(pairs: list) -> dict:
    """
    Per collateral: each pair's debt-bounded Q_cap (pair_demand, as in
    adequacy) against its Q*, over the debt set. Fails if a pair with
    complete coverage has Q* below its Q_cap; "unresolved" if only pairs with
    incomplete coverage do. The binding pair is the one with the smallest Q*.
    """
    out = {}
    for B in sorted({r["collateral"] for r in pairs}):
        rows = [r for r in pairs if r["collateral"] == B]
        if any(r["Q_cap"] is None for r in rows):
            out[B] = {"status": "unknown"}
            continue
        def fails(r):
            emode = (r.get("Q_cap_emode") is not None and r.get("Q_star_emode") is not None
                     and r["Q_cap_emode"] > r["Q_star_emode"])
            return r["Q_cap"] > r["Q_star"] or emode
        failing = sorted(r["debt"] for r in rows if fails(r) and r["coverage"] == "complete")
        failing_inc = sorted(r["debt"] for r in rows if fails(r) and r["coverage"] == "incomplete")
        bind = min(rows, key=lambda r: r["Q_star"])
        status = "fail" if failing else ("unresolved" if failing_inc else "pass")
        out[B] = {"status": status,
                  "binding_debt": bind["debt"], "binding_Q_star": bind["Q_star"],
                  "binding_Q_cap": bind["Q_cap"],
                  "failing_debts": failing, "failing_debts_incomplete": failing_inc}
    return out


def reserve_symbols(state: dict) -> dict:
    """reserve id -> symbol: Navi's symbol where the coin type is in navi.py,
    else the coin type's last segment, or r<id> where that is just COIN."""
    sym_of = {navi_positions.norm_type(t): s for s, t in navi.COIN_TYPES.items()}
    out = {}
    for rid, r in state["reserves"].items():
        s = sym_of.get(r["coin_type"])
        if s is None:
            tail = r["coin_type"].rsplit("::", 1)[-1]
            s = f"r{rid}" if tail == "COIN" else tail
        out[rid] = s
    return out


def price_groups(state: dict, sym: dict) -> tuple:
    """(groups {name: set of reserve ids}, how each reserve was classed)."""
    px = state["prices"]
    btc_px = next((px[r] for r, s in sym.items() if s == "wBTC" and r in px), None)
    groups, how = {}, {}
    for rid, s in sorted(sym.items()):
        p = px.get(rid)
        if s in SUI_FAMILY:
            g, why = "SUI family", "listed"
        elif s in BTC_FAMILY:
            g, why = "BTC family", "listed"
        elif s in STABLES:
            g, why = None, "listed stable"
        elif s in navi.COIN_TYPES:
            g, why = s, "listed, alone"
        elif p is not None and btc_px and abs(p / btc_px - 1) <= BTC_BAND:
            g, why = "BTC family", f"price {p:,.2f} within {BTC_BAND:.0%} of wBTC"
        elif p is not None and abs(p - 1) <= STABLE_BAND:
            g, why = None, f"price {p:.4f} within {STABLE_BAND:.0%} of $1: stable"
        else:
            g, why = s, "untracked, alone"
        how[s] = {"reserve": rid, "group": g or "stable (unshocked)", "why": why}
        if g:
            groups.setdefault(g, set()).add(rid)
    return groups, how


def position_flows(state: dict, p: dict, factor: dict, cf: float) -> tuple:
    """
    What one liquidatable position puts on the market, at prices x factor.

    Returns ("unpriced", None) if any of its reserves has no oracle price,
    ("bad_debt", {debt rid: USD}) if its collateral is worth nothing, else
    ("ok", [(b, a, D, base, lb, emode)]) with, per (collateral b, debt a):
      D      the debt in a assigned to b, split across collateral by borrowing
             power c·P·LT (the E-Mode LT for an E-Mode position)
      base   what a liquidator can repay on (b, a): Navi caps one call at
             CF x the user's collateral value in b (logic::calculate_liquidation)
             and at the debt, so base = min(D, CF·value_b·d_a/sum d), the
             collateral cap shared across debts in proportion to them
      lb     the bonus paid in b: the E-Mode bonus when b is collateral in the
             position's E-Mode pair, else the reserve's
    """
    px = state["prices"]
    if any(r not in px for r in list(p["collateral"]) + list(p["debt"])):
        return "unpriced", None
    cval = {b: t * px[b] * factor.get(b, 1.0) for b, t in p["collateral"].items()}
    dval = {a: t * px[a] * factor.get(a, 1.0) for a, t in p["debt"].items()}
    total_c, total_d = sum(cval.values()), sum(dval.values())
    if total_c <= 0:
        return "bad_debt", dval
    power = {b: v * navi_positions.collateral_lt(state, p, b) for b, v in cval.items()}
    total_p = sum(power.values()) or total_c
    pair = state["emode_pairs"].get(p["emode"]) if p["emode"] is not None else None
    flows = []
    for a, d in dval.items():
        for b, cv in cval.items():
            if cv <= 0:
                continue
            D = d * (power[b] if sum(power.values()) else cv) / total_p
            base = min(D, cf * cv * d / total_d) if total_d else 0.0
            emode = bool(pair and b in pair and pair[b]["collateral"])
            lb = pair[b]["lb"] if emode else state["reserves"][b]["lb"]
            flows.append((b, a, D, base, lb, emode))
    return "ok", flows


def stress(params, M, pos: dict, coverage_of) -> tuple:
    """
    stress.csv rows, per-collateral breaking shock and LST depeg results, and
    per-scenario counts of unpriced positions and bad debt.

    Every position with HF < 1 under a scenario is liquidated through
    position_flows. Per (B, A): Q = beta·sum (1 + (1-tf_B)·lb)·base over its
    positions, against Q*[B, A]; the E-Mode tranche, at its own bonus, is also
    checked against the pair's capacity at the E-Mode budget, and the pair
    fails if either check fails. Protected positions stay in: they can be
    liquidated, but only by their designated liquidator
    (storage::is_liquidatable); their share is reported as D_protected_usd.
    """
    state = pos["state"]
    sym = reserve_symbols(state)
    rid_of = {s_: r for r, s_ in sym.items()}
    groups, how = price_groups(state, sym)
    tf = {rid: state["reserves"][rid]["treasury_factor"] for rid in sym}
    cf, beta = params.close_factor, params.beta

    scenarios = [("baseline", 0.0, {}, "grid")]
    for g, members in sorted(groups.items()):
        for s in SHOCKS:
            scenarios.append((g, s, {rid: 1 - s for rid in members}, "grid"))
    for lst in LSTS:
        if lst in rid_of:
            for s in DEPEGS:
                scenarios.append((f"depeg {lst}", s, {rid_of[lst]: 1 - s}, "depeg"))

    rows, scen_stats = [], []
    for name, s, factor, kind in scenarios:
        acc, n_liq, unpriced, bad = {}, 0, [], {}
        for p in pos["positions"]:
            if navi_positions.health_factor(state, p, factor) >= 1:
                continue
            status, flows = position_flows(state, p, factor, cf)
            if status == "unpriced":
                unpriced.append(p["user"])
                continue
            if status == "bad_debt":
                for a, v in flows.items():
                    bad[sym.get(a, str(a))] = bad.get(sym.get(a, str(a)), 0.0) + v
                continue
            n_liq += 1
            for b, a, D, base, lb, emode in flows:
                x = acc.setdefault((b, a), {"D": 0.0, "base": 0.0, "Q": 0.0, "n": 0,
                                            "D_protected": 0.0, "D_emode": 0.0, "Q_emode": {}})
                q = beta * (1 + (1 - tf[b]) * lb) * base
                x["D"] += D
                x["base"] += base
                x["Q"] += q
                x["n"] += 1
                x["D_protected"] += D if p["protected"] else 0.0
                if emode:
                    x["D_emode"] += D
                    x["Q_emode"][lb] = x["Q_emode"].get(lb, 0.0) + q
        scen_stats.append({"scenario": name, "shock": s, "kind": kind,
                           "liquidatable": n_liq, "unpriced": len(unpriced),
                           "unpriced_users": unpriced[:50],
                           "bad_debt_usd": sum(bad.values()), "bad_debt_by_debt": bad})
        for (b_rid, a_rid), x in acc.items():
            B, A = sym[b_rid], sym[a_rid]
            qs = M.cells.get((B, A))
            fails = (x["Q"] > qs) if qs is not None else None
            q_e = sum(x["Q_emode"].values())
            qs_e, fails_e = None, None
            for lb_e, qe in x["Q_emode"].items():
                cap_e = M.pair_capacity(B, A, lb_e)
                if cap_e is None:
                    continue
                qs_e = cap_e if qs_e is None else min(qs_e, cap_e)
                fails_e = bool(fails_e) or qe > cap_e
            path = M.paths.get((B, A))
            rows.append({"scenario": name, "kind": kind, "shock": s, "collateral": B, "debt": A,
                         "D_usd": x["D"], "base_usd": x["base"], "Q": x["Q"], "Q_star": qs,
                         "Q_star_saturated": is_saturated(qs),
                         "Q_emode": q_e, "Q_star_emode": qs_e, "fails_emode": fails_e,
                         "fails": (bool(fails) or bool(fails_e)) if qs is not None else None,
                         "no_capacity_data": qs is None,
                         "n_positions": x["n"], "D_protected_usd": x["D_protected"],
                         "D_emode_usd": x["D_emode"],
                         "coverage": coverage_of((B, A))["status"] if qs is not None else None,
                         "path": " → ".join(path) if path else ""})

    per_coll = {}
    for B in sorted({b for b, _ in M.cells}):
        mine = [r for r in rows if r["collateral"] == B and r["fails"]]
        # an incomplete pair never sets the breaking shock; its own lowest
        # failing shock is reported apart
        fail_inc = [r for r in mine if r["kind"] == "grid" and r["coverage"] == "incomplete"]
        extra = {"breaking_shock_incomplete": min((r["shock"] for r in fail_inc), default=None),
                 "incomplete_debts": sorted({r["debt"] for r in fail_inc}),
                 "depeg": {lst: min((r["shock"] for r in mine if r["scenario"] == f"depeg {lst}"
                                     and r["coverage"] != "incomplete"), default=None)
                           for lst in LSTS}}
        failing = [r for r in mine if r["kind"] == "grid" and r["coverage"] != "incomplete"]
        if not failing:
            per_coll[B] = {"breaking_shock": None, "scenario": None, "binding_debt": None,
                           "note": f"no complete pair fails up to {max(SHOCKS):.0%}", **extra}
            continue
        s_min = min(r["shock"] for r in failing)
        worst = max((r for r in failing if r["shock"] == s_min),
                    key=lambda r: r["Q"] / r["Q_star"] if r["Q_star"] else float("inf"))
        per_coll[B] = {"breaking_shock": s_min, "scenario": worst["scenario"],
                       "binding_debt": worst["debt"], "Q": worst["Q"],
                       "Q_star": worst["Q_star"], "D_usd": worst["D_usd"],
                       "base_usd": worst["base_usd"], "n_positions": worst["n_positions"],
                       "D_emode_usd": worst["D_emode_usd"],
                       "failed_on_emode": bool(worst["fails_emode"]), **extra}
    return rows, per_coll, how, scen_stats


def write(params, M, book, live, pos, out_dir: str):
    # keyed like PoolBook: object id plus coin pair (Aftermath ids are shared)
    meta = {pool_key(r["pool_id"], r["coin_type_a"], r["coin_type_b"]): r
            for r in csv.DictReader(open(POOLS_CSV))}

    failed = []
    for key, reason in sorted(book.failed.items()):
        r = meta.get(key, {})
        failed.append({"pool_id": key[0], "venue": r.get("venue"),
                       "pair": (f"{r['symbol_a']}/{r['symbol_b']}" if r else None),
                       "kind": failure_kind(reason), "reason": reason})

    def coverage_of(pair) -> dict:
        """Matrix.coverage for a pair, with failed pools named and sized."""
        c = M.coverage.get(pair, {"status": "complete", "failed": []})
        return {"status": c["status"],
                "failed_pools": [{"pool_id": k[0], "venue": meta.get(k, {}).get("venue"),
                                  "tvl_usd": float(meta.get(k, {}).get("tvl_usd") or 0)}
                                 for k in c["failed"]]}

    # share of the needed pool TVL (on-chain, from the CSV) that was read;
    # empty pools are zero depth and left out of both sides
    tvl = lambda k: float(meta.get(k, {}).get("tvl_usd") or 0)
    read_tvl = sum(tvl(k) for k in book.pools)
    need_tvl = read_tvl + sum(tvl(k) for k in book.failed)
    coverage_share = read_tvl / need_tvl if need_tvl else None
    n_incomplete = sum(1 for c in M.coverage.values() if c["status"] == "incomplete")

    with open(os.path.join(out_dir, "matrix.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["collateral", "debt", "capacity_usd", "saturated", "path",
                    "coverage", "failed_pools"])
        for (B, D), cap in sorted(M.cells.items()):
            path = M.paths.get((B, D))
            cov = coverage_of((B, D))
            w.writerow([B, D, cap, str(is_saturated(cap)).lower(),
                        " → ".join(path) if path else "", cov["status"],
                        ";".join(f'{f["pool_id"]}({f["tvl_usd"]:.0f})'
                                 for f in cov["failed_pools"])])

    borrowed = pos["borrowed"]
    pairs, per_coll = adequacy(params, M, live, coverage_of, borrowed, pos["state"])
    sig1 = signal_1(pairs)
    s_rows, s_coll, groups_how, scen_stats = stress(params, M, pos, coverage_of)
    n_scen = len(scen_stats)
    s_cols = ["scenario", "kind", "shock", "collateral", "debt", "D_usd", "base_usd", "Q",
              "Q_star", "Q_star_saturated", "Q_emode", "Q_star_emode", "fails_emode", "fails",
              "no_capacity_data", "n_positions", "D_protected_usd", "D_emode_usd",
              "coverage", "path"]
    with open(os.path.join(out_dir, "stress.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=s_cols)
        w.writeheader()
        for r in sorted(s_rows, key=lambda r: (r["scenario"] != "baseline", r["scenario"],
                                               r["shock"], r["collateral"], r["debt"])):
            w.writerow({k: (str(v).lower() if isinstance(v, bool) else
                            "" if v is None else v) for k, v in r.items()})
    cols = ["collateral", "debt", "Q_now", "Q_cap", "Q_star", "Q_star_saturated",
            "Q_cap_emode", "Q_star_emode", "status", "status_if_complete", "coverage", "failed_pools",
            "debt_price_missing", "path"]
    with open(os.path.join(out_dir, "adequacy.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in pairs:
            row = dict(r, failed_pools=";".join(f'{f["pool_id"]}({f["tvl_usd"]:.0f})'
                                                for f in r["failed_pools"]))
            w.writerow({k: (str(v).lower() if isinstance(v, bool) else
                            "" if v is None else v) for k, v in row.items()})

    collaterals = {}
    for B in sorted({b for b, _ in M.cells}):
        q, binding = M.row_min(B)
        row = M.debt_row(B)          # excluded (never-borrowed) debts left out
        positive = {D: v for D, v in row.items() if v}
        D_ex = min(positive, key=positive.get) if positive else None
        collaterals[B] = {"Q_star": q, "binding_debt": binding,
                          "Q_star_saturated": is_saturated(q),
                          "Q_star_ex_zero": positive.get(D_ex),
                          "binding_debt_ex_zero": D_ex,
                          "zero_debts": sorted(D for D, v in row.items() if v == 0),
                          "signal_1": sig1[B],
                          "adequacy_status": per_coll[B]["status"],
                          "breaking_shock": s_coll[B]["breaking_shock"],
                          "stress": s_coll[B],
                          "adequacy": per_coll[B]}
    run = {"timestamp_utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           **git_state(),
           "params": {k: v for k, v in vars(params).items()
                      if k not in ("prices", "venues")},
           "venues": sorted(navi.VENUES)}
    anomalies = [{"asset": s, "issue": "supplied exceeds supply cap",
                  "supplied": a["supplied"], "supply_cap": a["supply_cap"]}
                 for s, a in live["assets"].items() if a["supplied"] > a["supply_cap"]]
    for s_, a in live["assets"].items():
        a["borrowed_in_positions"] = borrowed.get(s_, 0.0)
    navi_live = {"storage": NAVI_STORAGE, "assets": live["assets"],
                 "diffs_from_navi_py": param_diffs(live),
                 "unpriced": sorted(s for s in navi.COIN_TYPES if M.price(s) is None),
                 "debt_set": live["debt_set"],
                 "no_pool": M.no_pool,
                 "anomalies": anomalies}
    run["coverage_share"] = coverage_share
    run["pairs_incomplete"] = n_incomplete
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump({"run": run, "navi_live": navi_live, "collaterals": collaterals,
                   "stress_scenarios": scen_stats}, fh, indent=1)

    config = {"params": run["params"], "venues": run["venues"],
              "treasury_factors": params.treasury_factors,
              "debt_set": sorted(live["debt_set"]["included"]),
              "debt_set_reasons": live["debt_set"]["included"],
              "excluded_debts": live["debt_set"]["excluded"],
              "no_pool": M.no_pool,
              "q_cap": "beta*CF*(1+b_B)*min(LTV_B*S_B*P_B, max(borrowed_A, "
                       "borrow_cap_ratio_A*S_A)*P_A); b_B = (1-treasury_factor_B)*LB_B"}
    with open(os.path.join(out_dir, "config.json"), "w") as fh:
        json.dump(config, fh, indent=1)

    labels = price_labels(M, book)
    empty = [{"pool_id": key[0], "venue": meta.get(key, {}).get("venue"),
              "pair": (f"{meta[key]['symbol_a']}/{meta[key]['symbol_b']}" if key in meta else None),
              "reason": reason}
             for key, reason in sorted(book.empty.items())]
    coverage = {"pools_needed": len(book.pools) + len(book.failed) + len(book.empty),
                "pools_read": len(book.pools), "pools_failed": len(book.failed),
                "pools_empty": len(book.empty),
                "coverage_share": coverage_share,
                "pairs_incomplete": n_incomplete,
                "failed": failed,
                "empty": empty,     # zero depth; never makes a pair incomplete
                "untracked_navi_reserves": live["untracked"],
                "prices": {
                    # keyed by normalized coin type; label is for display only
                    "tokens": {ct: {"label": labels.get(ct, ct.rsplit("::", 1)[-1]),
                                    "price_source": "oracle" if M.price_source.get(ct) == "supplied"
                                    else "derived",
                                    "price": M.prices[ct],
                                    **({"oracle_age_s": live["oracle"]["info"][ct]["age_s"],
                                        "navi_reserve": live["oracle"]["info"][ct]["reserve"]}
                                       if ct in live["oracle"]["info"] else {})}
                               for ct in sorted(M.prices)},
                    "navi_assets": {s: M.tokens.get(s) for s in sorted(navi.COIN_TYPES)},
                    "derived": sorted(labels.get(ct, ct) for ct in M.prices
                                      if M.price_source.get(ct) != "supplied"),
                    "skipped_pools": M.skipped_pools},
                "positions": {**pos["stats"], "hf_check": pos["hf_check"],
                              "market": pos["state"]["market"],
                              "emode_users": len(pos["state"]["user_emode"]),
                              "protected_users": len(pos["state"]["protected"]),
                              "protected_with_designated_liquidator": sum(
                                  1 for u in pos["state"]["protected"]
                                  if pos["state"]["designated_liquidators"].get(u)),
                              "stress_scenarios": n_scen,
                              "price_groups": groups_how}}
    with open(os.path.join(out_dir, "coverage.json"), "w") as fh:
        json.dump(coverage, fh, indent=1)


def main() -> int:
    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
    target = os.path.join(OUT_ROOT, day)
    try:
        params, M, book, live, pos = compute()
        os.makedirs(OUT_ROOT, exist_ok=True)
        tmp = tempfile.mkdtemp(prefix=f".{day}-", dir=OUT_ROOT)
        os.chmod(tmp, 0o755)                # mkdtemp creates it owner-only
        try:
            write(params, M, book, live, pos, tmp)
            if os.path.exists(target):
                shutil.rmtree(target)
            os.rename(tmp, target)
        except BaseException:
            shutil.rmtree(tmp, ignore_errors=True)
            raise
    except Unusable as e:
        print(f"run unusable: {e}", file=sys.stderr)
        return 2
    except Exception:
        traceback.print_exc()
        return 1
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
