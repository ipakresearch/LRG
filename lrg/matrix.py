"""
Capacity matrix and parameter curves.

For each ordered (collateral, debt) pair, invert the route cost function at the
collateral's own cost budget. Row minima govern the collateral side — LTV, LT,
LB, supply cap; column minima govern the debt side — borrow cap.

The budget is the exact profitability condition rather than its first-order
approximation. A liquidator repaying D receives (1+b)D of collateral, where
b = (1 - platform_share)*LB, sells it at cost c, and must end with (1+phi)D:

    (1 + b)(1 - c) >= 1 + phi      ->      c <= 1 - (1 + phi)/(1 + b)

The linear form b - phi overstates the budget by roughly b squared, which is
1.6 percentage points at a 15% bonus.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from . import clmm
from .book import PoolBook
from .curves import LegCurve, route_capacity
from . import graph as _graph


# ---------------------------------------------------------------------------
# Protocol parameters
# ---------------------------------------------------------------------------

@dataclass
class Params:
    close_factor: float = 0.35
    beta: float = 1.00
    platform_share: float = 0.10
    flash_fee: float = 0.0009
    lb_max: float = 0.15
    prices: Optional[Dict[str, float]] = None
    venues: Optional[set] = None
    max_hops: int = 4
    max_pools_per_leg: int = 4
    max_candidates: int = 20
    pick: str = "split"

    def bonus_to_liquidator(self, lb: float) -> float:
        """The share of the bonus the liquidator keeps, after the protocol's cut."""
        return (1 - self.platform_share) * lb

    def budget(self, lb: float, exact: bool = True) -> float:
        """
        Largest total route cost a liquidation can bear.

        The liquidator repays D, receives (1 + b)·D of collateral where
        b = (1 - platform_share)·LB, sells it at cost c, and must end with at
        least (1 + phi)·D:

            (1 + b)(1 - c) >= 1 + phi      ->      c <= 1 - (1 + phi)/(1 + b)

        The first-order form b - phi is optimistic by roughly b², because it
        measures the loss against the pre-bonus base while the quantity sold is
        the post-bonus one. Pass exact=False to use it.
        """
        b = self.bonus_to_liquidator(lb)
        if not exact:
            return b - self.flash_fee
        return 1 - (1 + self.flash_fee) / (1 + b)

    def seized_factor(self, lb: float) -> float:
        """Collateral reaching the DEX per unit of debt repaid. The protocol's
        share of the incentive is retained, not sold."""
        return 1 + self.bonus_to_liquidator(lb)


@dataclass
class Asset:
    symbol: str
    ltv: Optional[float]
    lt: float
    lb: float
    supply_cap: float
    emode_ltv: Optional[float] = None
    emode_lt: Optional[float] = None
    supplied: Optional[float] = None       # current supply in tokens, if known

    @property
    def is_collateral(self) -> bool:
        return self.ltv is not None

    @property
    def delta(self) -> float:
        return self.lt - (self.ltv if self.ltv is not None else self.lt)


def load_assets(table: Dict[str, tuple],
                supplied: Dict[str, float] = None) -> Dict[str, Asset]:
    """table: symbol -> (ltv, lt, lb, supply_cap, emode_ltv, emode_lt)."""
    supplied = supplied or {}
    return {s: Asset(s, *v, supplied=supplied.get(s))
            for s, v in table.items()}


# ---------------------------------------------------------------------------
# The matrix
# ---------------------------------------------------------------------------

@dataclass
class Matrix:
    params: Params
    assets: Dict[str, Asset]
    prices: Dict[str, float]
    cells: Dict[Tuple[str, str], Optional[float]]      # (B, A) -> capacity USD
    paths: Dict[Tuple[str, str], Optional[List[str]]]
    curves: Dict[Tuple[str, str], object] = field(default_factory=dict)
    unreachable: List[Tuple[str, str]] = field(default_factory=list)
    dead_leg: Dict[Tuple[str, str], Optional[str]] = field(default_factory=dict)
    pools_read: int = 0
    pools_failed: int = 0

    # ---- collateral side -------------------------------------------------

    def row(self, B: str) -> Dict[str, Optional[float]]:
        return {A: v for (b, A), v in self.cells.items() if b == B}

    def row_min(self, B: str) -> Tuple[Optional[float], Optional[str]]:
        r = {A: v for A, v in self.row(B).items() if v is not None}
        if not r:
            return None, None
        A = min(r, key=r.get)
        return r[A], A

    def col_min(self, A: str) -> Tuple[Optional[float], Optional[str]]:
        c = {B: v for (B, a), v in self.cells.items() if a == A and v is not None}
        if not c:
            return None, None
        B = min(c, key=c.get)
        return c[B], B

    def q_star(self, B: str, lb: Optional[float] = None) -> Optional[float]:
        """Row minimum at an arbitrary bonus, recomputing from the cached curves."""
        a = self.assets[B]
        lb = a.lb if lb is None else lb
        tau = self.params.budget(lb)
        if tau <= 0:
            return 0.0
        vals = []
        for (b, A), cv in self.curves.items():
            if b != B or cv is None:
                continue
            vals.append(route_capacity(cv, tau))
        return min(vals) if vals else None

    def curve(self, B: str, lbs: Sequence[float] = None) -> List[Tuple[float, float]]:
        lbs = lbs or [i / 100 for i in range(2, 21)]
        return [(lb, self.q_star(B, lb) or 0.0) for lb in lbs]

    # ---- demand and risk --------------------------------------------------

    def demand(self, B: str, at: str = "cap") -> Optional[float]:
        """Liquidation demand in USD. at='cap' uses the supply cap, 'current'
        the supplied amount if known."""
        a, p = self.assets[B], self.params
        if not a.is_collateral:
            return None
        S = a.supply_cap if at == "cap" else (a.supplied or 0.0)
        px = self.prices.get(B)
        if px is None:
            return None
        return (p.beta * p.close_factor * p.seized_factor(a.lb)
                * a.ltv * S * px)

    def car(self, B: str, at: str = "cap") -> Optional[dict]:
        q = self.demand(B, at)
        qs = self.q_star(B)
        if q is None or qs is None:
            return None
        _, binding = self.row_min(B)
        return {"asset": B, "Q": q, "Q_star": qs, "car": q - qs,
                "ratio": (q / qs) if qs else float("inf"), "binding": binding}

    # ---- Step-2 curves ----------------------------------------------------

    def s_max(self, B: str, ltv: float, lb: float) -> Optional[float]:
        """Largest supply cap, in tokens, adequate at (ltv, lb)."""
        a, p = self.assets[B], self.params
        qs = self.q_star(B, lb)
        px = self.prices.get(B)
        if qs is None or not px or ltv <= 0:
            return None
        return qs / (p.beta * p.close_factor * p.seized_factor(lb) * ltv * px)

    def feasible(self, B: str, ltvs: Sequence[float] = None,
                 S: Optional[float] = None) -> List[dict]:
        """(LTV, LB) region at a fixed supply cap: required bonus vs ceiling."""
        a, p = self.assets[B], self.params
        S = S if S is not None else a.supply_cap
        px = self.prices.get(B) or 0.0
        ltvs = ltvs or [i / 100 for i in range(5, 86, 5)]
        delta = a.lt - (a.ltv or a.lt)
        out = []
        for ltv in ltvs:
            lo, hi = 0.001, 0.60                      # required bonus, bisection
            need = None
            for _ in range(40):
                mid = 0.5 * (lo + hi)
                q = (p.beta * p.close_factor * p.seized_factor(mid)
                     * ltv * S * px)
                qs = self.q_star(B, mid)
                if qs is None:
                    break
                if q <= qs:
                    hi = mid; need = mid
                else:
                    lo = mid
            ceiling = min(1 - ltv - delta, p.lb_max)
            out.append({"ltv": ltv, "lb_required": need, "lb_ceiling": ceiling,
                        "feasible": need is not None and need <= ceiling})
        return out

    # ---- reporting --------------------------------------------------------

    def matrix_table(self, syms: Sequence[str] = None, width: int = 9):
        colls = syms or [s for s, a in self.assets.items()
                         if a.is_collateral and self.row_min(s)[0] is not None]
        debts = syms or sorted({A for (_, A) in self.cells})
        print(f"capacity matrix C[B][A], USD, each row at its own budget "
              f"(CF={self.params.close_factor}, β={self.params.beta}, "
              f"platform share={self.params.platform_share:.0%})\n")
        label = "coll \\ debt"
        print(f"{label:<11}" + "".join(f"{d[:8]:>{width}}" for d in debts))
        for B in colls:
            r = self.row(B)
            mn, binding = self.row_min(B)
            line = f"{B:<11}"
            for A in debts:
                v = r.get(A)
                if A == B:
                    line += f"{'·':>{width}}"
                elif v is None:
                    line += f"{'—':>{width}}"
                elif v == 0.0:
                    line += f"{'none*' if A == binding else 'none':>{width}}"
                else:
                    tag = "*" if A == binding else (
                        "!" if self.dead_leg.get((B, A)) else " ")
                    line += f"{_fmt(v) + tag:>{width}}"
            print(line)
        print("\n* row minimum (binding debt asset) · ! a leg with no 24h "
              "volume · 'none' no route within the hop limit")

    def risk_table(self, at: str = "cap"):
        p = self.params
        print(f"capacity at risk, demand at {at}  "
              f"(CF={p.close_factor}, β={p.beta}, LB_eff = "
              f"{1-p.platform_share:.0%}·LB − {p.flash_fee:.2%})\n")
        print(f"  {'asset':<10}{'LB':>5}{'budget':>8}{'Q':>13}{'Q*':>13}"
              f"{'ratio':>8}{'binding':>9}")
        rows = []
        for B, a in self.assets.items():
            r = self.car(B, at)
            if r:
                rows.append((a, r))
        for a, r in sorted(rows, key=lambda x: -(x[1]["ratio"] or 0)):
            flag = "  ◀" if r["ratio"] > 1 else ""
            print(f"  {a.symbol:<10}{a.lb*100:>4.0f}%"
                  f"{p.budget(a.lb)*100:>7.2f}%{_fmt(r['Q']):>13}"
                  f"{_fmt(r['Q_star']):>13}{r['ratio']:>8.2f}"
                  f"{r['binding'] or '—':>9}{flag}")
        print("\n◀ demand exceeds the capacity the routes support at that budget")

    def borrow_table(self):
        print("debt side: column minima (ceiling on the borrow cap)\n")
        print(f"  {'debt':<10}{'min capacity':>15}{'binding collateral':>20}")
        for A in sorted({a for (_, a) in self.cells}):
            v, B = self.col_min(A)
            if v is None:
                continue
            print(f"  {A:<10}{_fmt(v):>15}{B or '—':>20}")

    def curve_table(self, B: str, lbs: Sequence[float] = None):
        a = self.assets[B]
        print(f"Q*({B}) as a function of the bonus\n")
        print(f"  {'LB':>6}{'budget':>9}{'Q*':>14}{'S_max at LTV=' + f'{a.ltv:.0%}':>20}")
        for lb, q in self.curve(B, lbs):
            s = self.s_max(B, a.ltv, lb) if a.ltv else None
            print(f"  {lb*100:>5.0f}%{self.params.budget(lb)*100:>8.2f}%"
                  f"{_fmt(q):>14}{(f'{s:,.0f}' if s else '—'):>20}")

    def feasible_table(self, B: str, S: Optional[float] = None):
        a = self.assets[B]
        S = S if S is not None else a.supply_cap
        print(f"(LTV, LB) feasible region for {B} at S = {S:,.0f} tokens\n")
        print(f"  {'LTV':>6}{'LB required':>13}{'LB ceiling':>12}{'':>4}")
        best = None
        for r in self.feasible(B, S=S):
            mark = "ok" if r["feasible"] else ""
            if r["feasible"]:
                best = r["ltv"]
            need = f"{r['lb_required']*100:.2f}%" if r["lb_required"] else "—"
            print(f"  {r['ltv']*100:>5.0f}%{need:>13}"
                  f"{r['lb_ceiling']*100:>11.2f}%{mark:>4}")
        print(f"\nmax admissible LTV at this cap: "
              f"{f'{best*100:.0f}%' if best else 'none'}")

    # ---- persistence ------------------------------------------------------

    def save(self, path: str):
        d = {
            "params": {k: v for k, v in asdict(self.params).items()
                       if k not in ("venues",)},
            "prices": self.prices,
            "cells": {f"{b}|{a}": v for (b, a), v in self.cells.items()},
            "paths": {f"{b}|{a}": p for (b, a), p in self.paths.items()},
            "unreachable": [f"{b}|{a}" for b, a in self.unreachable],
            "dead_leg": {f"{b}|{a}": v for (b, a), v in self.dead_leg.items() if v},
            "pools_read": self.pools_read, "pools_failed": self.pools_failed,
        }
        with open(path, "w") as fh:
            json.dump(d, fh, indent=1)
        print(f"saved {path}")


def _fmt(v: Optional[float]) -> str:
    if v is None:
        return "—"
    if v >= 1e9:
        return f"{v/1e9:.2f}B"
    if v >= 1e6:
        return f"{v/1e6:.2f}M"
    if v >= 1e3:
        return f"{v/1e3:.0f}k"
    return f"{v:.0f}"


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(csv_path: str, tokens: Dict[str, str], asset_table: Dict[str, tuple],
          venues, cache: str = "pools_cache", params: Params = None,
          collaterals: Sequence[str] = None, debts: Sequence[str] = None,
          verbose: bool = True) -> Matrix:
    """
    tokens       symbol -> coin type
    asset_table  symbol -> (ltv, lt, lb, supply_cap, emode_ltv, emode_lt)
    venues       venue ids to route through
    """
    p = params or Params()
    A = load_assets(asset_table)
    venues = p.venues or venues

    rows = _graph.load_pool_rows(csv_path, venues)
    adj = _graph.build_adjacency(rows)
    vol_of = {r["pool_id"]: float(r["volume_24h"] or 0) for r in rows}
    tvl_of = {r["pool_id"]: float(r["tvl_usd"] or 0) for r in rows}
    name = dict(tokens)
    tname = {v: k for k, v in name.items()}
    for r in rows:
        tname.setdefault(r["coin_type_a"], r["symbol_a"])
        tname.setdefault(r["coin_type_b"], r["symbol_b"])

    present = [s for s in A if s in name and name[s] in adj]
    colls = [s for s in (collaterals or present)
             if s in A and A[s].is_collateral and s in present]
    dbts = [s for s in (debts or present) if s in present]
    if verbose:
        absent = [s for s in A if s not in present]
        print(f"{len(colls)} collaterals x {len(dbts)} debts "
              f"= {len(colls)*len(dbts) - len(set(colls) & set(dbts))} pairs")
        if absent:
            print(f"absent from the pool set: {', '.join(absent)}")

    # candidate paths, same selection as the network build
    def leg_ids(x, y):
        return [(c["pool_id"], "a" if c["coin_type_a"] == x else "b")
                for c in adj[x][y][:p.max_pools_per_leg]]

    def bottleneck(path):
        return min(float(adj[path[i]][path[i + 1]][0]["tvl_usd"])
                   for i in range(len(path) - 1))

    cands = {}
    for B in colls:
        for D in dbts:
            if B == D:
                continue
            ps = _graph.all_paths(adj, name[B], name[D], p.max_hops)
            ps.sort(key=lambda x: (-bottleneck(x), len(x)))
            cands[(B, D)] = ps[:p.max_candidates]

    need = sorted({pid for ps in cands.values() for path in ps
                   for i in range(len(path) - 1) for pid, _ in leg_ids(path[i], path[i + 1])})
    venue_of = {r["pool_id"]: r["venue"] for r in rows}
    type_of = {r["pool_id"]: (r["coin_type_a"], r["coin_type_b"]) for r in rows}
    book = PoolBook(cache=cache, verbose=verbose, venue_of=venue_of,
                    tokens=tokens)
    if verbose:
        print(f"reading {len(need)} pools…")
    pools = {}
    for pid in need:
        ca, cb = type_of.get(pid, (None, None))
        q = book.get(pid, coin_a=ca, coin_b=cb)
        if q:
            pools[pid] = q
    prices = p.prices or _graph.derive_prices(pools)
    if verbose:
        print(f"{len(pools)} readable, {len(book.failed)} failed; "
              f"prices for {len(prices)} tokens")

    curve_cache: Dict[Tuple[str, str], object] = {}

    def curve(x, y):
        if (x, y) not in curve_cache:
            lp = [(pools[pid], sd) for pid, sd in leg_ids(x, y) if pid in pools]
            curve_cache[(x, y)] = (LegCurve(lp, prices, pick=p.pick)
                                   if lp else None)
        return curve_cache[(x, y)]

    cells, paths, curves, unreachable = {}, {}, {}, []
    dead_leg: Dict[Tuple[str, str], Optional[str]] = {}

    def leg_volume(x, y):
        """Total 24h volume across the pools used on this leg."""
        return sum(vol_of.get(pid, 0.0) for pid, _ in leg_ids(x, y))
    for (B, D), ps in cands.items():
        tau = p.budget(A[B].lb)
        best_cap, best_path, best_curves = None, None, None
        for path in ps:
            cs = [curve(path[i], path[i + 1]) for i in range(len(path) - 1)]
            if not all(cs):
                continue
            cap = route_capacity(cs, tau) if tau > 0 else 0.0
            if best_cap is None or cap > best_cap:
                best_cap, best_path, best_curves = cap, path, cs
        if best_cap is None:
            # No priced path. That is worse than a thin one, so it enters the
            # row minimum as zero rather than being skipped.
            unreachable.append((B, D))
            cells[(B, D)] = 0.0; paths[(B, D)] = None; curves[(B, D)] = None
        else:
            cells[(B, D)] = round(best_cap, 2)
            paths[(B, D)] = [tname[t] for t in best_path]
            curves[(B, D)] = best_curves
            # flag the thinnest-traded leg on the chosen path
            legs = [(best_path[i], best_path[i + 1])
                    for i in range(len(best_path) - 1)]
            worst = min(legs, key=lambda e: leg_volume(*e))
            dead_leg[(B, D)] = (f"{tname[worst[0]]}→{tname[worst[1]]}"
                                if leg_volume(*worst) <= 0 else None)
        if verbose and len(cells) % 100 == 0:
            print(f"  {len(cells)} cells…", flush=True)

    if verbose:
        print(f"\n{len(cells)} cells, {len(unreachable)} with no route")

    if verbose and dead_leg:
        n = sum(1 for v in dead_leg.values() if v)
        print(f"{n} routes have a leg with no 24h volume")

    return Matrix(params=p, assets=A, prices=prices, cells=cells, paths=paths,
                  curves=curves, unreachable=unreachable, dead_leg=dead_leg,
                  pools_read=len(pools), pools_failed=len(book.failed))
