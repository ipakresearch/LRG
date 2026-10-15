"""
Daily Navi capacity run.

Fetches live pool state, builds the capacity matrix at Navi's current defaults
(top-20 candidate paths per pair), and writes to data/navi/YYYY-MM-DD/ (UTC):

    matrix.csv      every pair: collateral, debt, capacity_usd, path, and
                    unverified (a leg of the path has a pool that failed to read)
    summary.json    per collateral: Q* (row minimum) and the binding debt asset,
                    the minimum over nonzero cells and its debt asset, and the
                    debt assets with zero capacity
    adequacy.csv    every pair: liquidation demand now and at caps, bounded
                    by the debt asset's borrowing, against the pair's capacity
    stress.csv      every (scenario, shock, collateral, debt) with liquidations:
                    debt of positions pushed below HF 1, the collateral it puts
                    on the market, and the pair's capacity
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

Signal 1 (summary.json "signal_1"), per collateral: fails if the market filled
to its supply cap at max LTV, Q_cap = beta·CF·(1+b)·LTV·S·P, exceeds Q* for any
debt asset. No debt-side bound; that is what the adequacy status adds.

Stress (summary.json "breaking_shock"): every Navi borrower with at least $1 of
debt is read from chain (positions.py). Prices drop by s in {0.10, ..., 0.40},
one price group at a time: the SUI family, the BTC family, each other volatile
asset alone; stables never drop. s = 0 is the baseline. A position whose HF
falls below 1 puts CF·(1+b_B) times its debt in A, split across its collateral
by value, on the market as D[B,A]. The breaking shock is the smallest s, over
all scenarios, at which some pair in B's row has Q[B,A] > Q*[B,A]. Q* is
measured before the shock, so breaking shocks are upper bounds.

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
from lrg.book import PoolBook               # noqa: E402
from lrg.clmm import gql, Q_FIELDS, Q_OBJECT  # noqa: E402
from lrg.coins import norm_type            # noqa: E402
from lrg.curves import GRID_HI              # noqa: E402
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
SHOCKS = (0.10, 0.20, 0.30, 0.40)
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
    if "first 100 dynamic fields" in reason or "not where expected" in reason:
        return "tick_table_not_found"
    if "no ranges built" in reason:
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

    net = [pid for pid, why in book.failed.items() if failure_kind(why) == "network"]
    if net:
        # a transient outage, not missing liquidity: publishing would understate Q*
        raise Unusable(f"{len(net)} pools failed with network errors; rerun")
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
    return params, M, book, live, pos


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


STATUS_RANK = {"red": 3, "amber": 2, "green": 1, "unknown": 0}


def adequacy(params, M, live: dict, unverified, borrowed: dict) -> tuple:
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
    pairs, per_coll = [], {}
    for B in sorted({b for b, _ in M.cells}):
        a = M.assets[B]
        q_now_B, q_cap_B = M.demand(B, "current"), M.demand(B, "cap")
        k = params.beta * params.close_factor * params.seized_factor(a.lb, params.tf(B))
        rows = []
        for (b, A), qs in sorted(M.cells.items()):
            if b != B:
                continue
            pA = M.price(A)
            la = a_live[A]
            b_A = borrowed.get(A, 0.0)
            maxdebt = max(b_A, la["borrow_cap_ratio"] * la["supply_cap"])
            if q_now_B is None or q_cap_B is None:
                q_now = q_cap = None
            elif pA is None:
                # no debt price: leave the collateral side unbounded (conservative)
                q_now, q_cap = q_now_B, q_cap_B
            else:
                q_now = min(q_now_B, k * b_A * pA)
                q_cap = min(q_cap_B, k * maxdebt * pA)
            if q_now is None:
                status = "unknown"
            elif q_now > qs:
                status = "red"
            elif q_cap > qs:
                status = "amber"
            else:
                status = "green"
            path = M.paths.get((B, A))
            rows.append({"collateral": B, "debt": A, "Q_now": q_now, "Q_cap": q_cap,
                         "Q_star": qs, "Q_star_saturated": qs >= GRID_HI * (1 - 1e-9),
                         "status": status, "unverified": unverified(path),
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
            "unverified": worst["unverified"],
            "pairs": {s: sum(r["status"] == s for r in rows)
                      for s in ("red", "amber", "green", "unknown")},
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
    for (b, A), qs in M.cells.items():
        if b != B:
            continue
        pA = M.price(A)
        debt_lim = k * borrowed.get(A, 0.0) * pA if pA else float("inf")
        if debt_lim > qs:
            bounds.append(qs / (k * a.ltv * pB))
    return min(bounds) if bounds else None


def signal_1(params, M) -> dict:
    """
    Per collateral: Q_cap = Matrix.demand(B, "cap") against each pair's Q*,
    with no debt-side bound. Fails if any pair's Q* is below Q_cap; the
    binding pair is the one with the smallest Q*.
    """
    out = {}
    for B in sorted({b for b, _ in M.cells}):
        q_cap = M.demand(B, "cap")
        row = M.row(B)
        if q_cap is None or not row:
            out[B] = {"status": "unknown", "Q_cap": q_cap}
            continue
        A = min(row, key=row.get)
        failing = sorted(D for D, qs in row.items() if q_cap > qs)
        out[B] = {"status": "fail" if failing else "pass", "Q_cap": q_cap,
                  "binding_debt": A, "binding_Q_star": row[A],
                  "failing_debts": len(failing)}
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


def stress(params, M, pos: dict, unverified) -> tuple:
    """
    stress.csv rows and per-collateral breaking shock. See the module docstring.
    Q uses the reserve's normal bonus for every position, as specified; the
    share of D from e-mode positions, which liquidate at the e-mode bonus, is
    reported alongside.
    """
    state = pos["state"]
    sym = reserve_symbols(state)
    groups, how = price_groups(state, sym)
    px = state["prices"]
    lb = {rid: (M.assets[s].lb if s in M.assets else state["reserves"][rid]["lb"])
          for rid, s in sym.items()}
    tf = {rid: state["reserves"][rid]["treasury_factor"] for rid in sym}

    scenarios = [("baseline", 0.0, {})]
    for g, members in sorted(groups.items()):
        for s in SHOCKS:
            scenarios.append((g, s, {rid: 1 - s for rid in members}))

    rows = []
    for name, s, factor in scenarios:
        acc = {}
        for p in pos["positions"]:
            if navi_positions.health_factor(state, p, factor) >= 1:
                continue
            cval = {rid: t * px.get(rid, 0.0) * factor.get(rid, 1.0)
                    for rid, t in p["collateral"].items()}
            total = sum(cval.values())
            if total <= 0:
                continue
            for a_rid, t in p["debt"].items():
                d_usd = t * px.get(a_rid, 0.0) * factor.get(a_rid, 1.0)
                for b_rid, cv in cval.items():
                    if cv <= 0:
                        continue
                    x = acc.setdefault((b_rid, a_rid), {"D": 0.0, "n": 0, "D_protected": 0.0,
                                                        "D_emode": 0.0})
                    share = d_usd * cv / total
                    x["D"] += share
                    x["n"] += 1
                    x["D_protected"] += share if p["protected"] else 0.0
                    x["D_emode"] += share if p["emode"] is not None else 0.0
        for (b_rid, a_rid), x in acc.items():
            B, A = sym[b_rid], sym[a_rid]
            q = params.close_factor * params.seized_factor(lb[b_rid], tf[b_rid]) * x["D"]
            qs = M.cells.get((B, A))
            path = M.paths.get((B, A))
            rows.append({"scenario": name, "shock": s, "collateral": B, "debt": A,
                         "D_usd": x["D"], "Q": q, "Q_star": qs,
                         "fails": (q > qs) if qs is not None else None,
                         "no_capacity_data": qs is None,
                         "n_positions": x["n"], "D_protected_usd": x["D_protected"],
                         "D_emode_usd": x["D_emode"],
                         "unverified": unverified(path) if qs is not None else None,
                         "path": " → ".join(path) if path else ""})

    per_coll = {}
    for B in sorted({b for b, _ in M.cells}):
        failing = [r for r in rows if r["collateral"] == B and r["fails"]]
        if not failing:
            per_coll[B] = {"breaking_shock": None, "scenario": None, "binding_debt": None,
                           "note": f"no pair fails up to {max(SHOCKS):.0%}"}
            continue
        s_min = min(r["shock"] for r in failing)
        worst = max((r for r in failing if r["shock"] == s_min),
                    key=lambda r: r["Q"] / r["Q_star"] if r["Q_star"] else float("inf"))
        per_coll[B] = {"breaking_shock": s_min, "scenario": worst["scenario"],
                       "binding_debt": worst["debt"], "Q": worst["Q"],
                       "Q_star": worst["Q_star"], "D_usd": worst["D_usd"],
                       "n_positions": worst["n_positions"],
                       "D_emode_usd": worst["D_emode_usd"],
                       "unverified": worst["unverified"]}
    return rows, per_coll, how, len(scenarios)


def write(params, M, book, live, pos, out_dir: str):
    meta = {r["pool_id"]: r for r in csv.DictReader(open(POOLS_CSV))}

    failed = []
    for pid, reason in sorted(book.failed.items()):
        r = meta.get(pid, {})
        failed.append({"pool_id": pid, "venue": r.get("venue"),
                       "pair": (f"{r['symbol_a']}/{r['symbol_b']}" if r else None),
                       "kind": failure_kind(reason), "reason": reason})

    # token pairs with a failed pool, named as build() names path tokens:
    # Navi's symbol where the coin type is a Navi asset, else the CSV symbol
    navi_name = {norm_type(v): k for k, v in navi.COIN_TYPES.items()}
    failed_legs = set()
    for pid in book.failed:
        r = meta.get(pid)
        if r:
            a = navi_name.get(norm_type(r["coin_type_a"]), r["symbol_a"])
            b = navi_name.get(norm_type(r["coin_type_b"]), r["symbol_b"])
            failed_legs.add(frozenset((a, b)))

    def unverified(path) -> bool:
        return bool(path) and any(frozenset(path[i:i + 2]) in failed_legs
                                  for i in range(len(path) - 1))

    with open(os.path.join(out_dir, "matrix.csv"), "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["collateral", "debt", "capacity_usd", "path", "unverified"])
        for (B, D), cap in sorted(M.cells.items()):
            path = M.paths.get((B, D))
            w.writerow([B, D, cap, " → ".join(path) if path else "",
                        str(unverified(path)).lower()])

    sym = reserve_symbols(pos["state"])
    borrowed = {sym[rid]: t for rid, t in pos["debt_tokens_all"].items()}
    pairs, per_coll = adequacy(params, M, live, unverified, borrowed)
    sig1 = signal_1(params, M)
    s_rows, s_coll, groups_how, n_scen = stress(params, M, pos, unverified)
    s_cols = ["scenario", "shock", "collateral", "debt", "D_usd", "Q", "Q_star", "fails",
              "no_capacity_data", "n_positions", "D_protected_usd", "D_emode_usd",
              "unverified", "path"]
    with open(os.path.join(out_dir, "stress.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=s_cols)
        w.writeheader()
        for r in sorted(s_rows, key=lambda r: (r["scenario"] != "baseline", r["scenario"],
                                               r["shock"], r["collateral"], r["debt"])):
            w.writerow({k: (str(v).lower() if isinstance(v, bool) else
                            "" if v is None else v) for k, v in r.items()})
    cols = ["collateral", "debt", "Q_now", "Q_cap", "Q_star", "Q_star_saturated",
            "status", "unverified", "debt_price_missing", "path"]
    with open(os.path.join(out_dir, "adequacy.csv"), "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in pairs:
            w.writerow({k: (str(v).lower() if isinstance(v, bool) else
                            "" if v is None else v) for k, v in r.items()})

    collaterals = {}
    for B in sorted({b for b, _ in M.cells}):
        q, binding = M.row_min(B)
        row = M.row(B)
        positive = {D: v for D, v in row.items() if v}
        D_ex = min(positive, key=positive.get) if positive else None
        collaterals[B] = {"Q_star": q, "binding_debt": binding,
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
                 "anomalies": anomalies}
    with open(os.path.join(out_dir, "summary.json"), "w") as fh:
        json.dump({"run": run, "navi_live": navi_live, "collaterals": collaterals},
                  fh, indent=1)

    labels = price_labels(M, book)
    coverage = {"pools_needed": len(book.pools) + len(book.failed),
                "pools_read": len(book.pools), "pools_failed": len(book.failed),
                "failed": failed,
                "untracked_navi_reserves": live["untracked"],
                "prices": {
                    # keyed by normalized coin type; label is for display only
                    "tokens": {ct: {"label": labels.get(ct, ct.rsplit("::", 1)[-1]),
                                    "price_source": "oracle" if M.price_source.get(ct) == "supplied"
                                    else "derived",
                                    "price": M.prices[ct],
                                    "derived_price": M.derived_prices.get(ct),
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
