"""
Reading the matrix.

The row minimum is the adversarial capacity — the worst debt asset a
liquidation could be forced into. A row whose minimum comes from one
near-empty debt market says something different from a row that is thin
throughout, so the distribution is reported alongside it: p10, median, p90 and
max, plus which debt asset gives each.
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence

import pandas as pd


# ---------------------------------------------------------------------------
# Frames
# ---------------------------------------------------------------------------

def to_frame(M) -> pd.DataFrame:
    """One row per ordered pair, with the parameters that produced it."""
    rows = []
    for (B, A), cap in M.cells.items():
        a = M.assets.get(B)
        if a is None:
            continue
        path = M.paths.get((B, A))
        rows.append({
            "collateral": B, "debt": A,
            "capacity_usd": cap,
            "path": " → ".join(path) if path else None,
            "hops": (len(path) - 1) if path else None,
            "no_route": (cap == 0.0),
            "dead_leg": getattr(M, "dead_leg", {}).get((B, A)),
            "lb": a.lb, "budget": M.params.budget(a.lb, M.params.tf(B)),
            "ltv": a.ltv, "lt": a.lt,
            "supply_cap_tokens": a.supply_cap,
            "price": M.prices.get(B),
        })
    df = pd.DataFrame(rows)
    df["supply_cap_usd"] = df["supply_cap_tokens"] * df["price"]
    # rank within each row, so the binding pair is rank 1
    df["rank_in_row"] = df.groupby("collateral")["capacity_usd"].rank(
        method="min", na_option="bottom")
    df["is_binding"] = df["rank_in_row"] == 1
    return df.sort_values(["collateral", "capacity_usd"]).reset_index(drop=True)


def to_wide(df: pd.DataFrame, value: str = "capacity_usd") -> pd.DataFrame:
    return df.pivot(index="collateral", columns="debt", values=value)


class LoadedMatrix:
    """
    A saved capacity matrix rehydrated with the risk-parameter table, exposing
    the same read interface as navi_matrix.Matrix.

    Capacities were computed at each collateral's configured budget, so
    q_star(B) is exact. Re-evaluating at a different bonus needs the leg curves,
    which are not saved — use navi_matrix.build() for that.
    """

    def __init__(self, path: str, asset_table=None, assets=None, params=None):
        from . import matrix as nm
        with open(path) as fh:
            d = json.load(fh)
        self.raw = d
        self.prices: Dict[str, float] = d.get("prices", {})
        self.cells = {tuple(k.split("|")): v for k, v in d["cells"].items()}
        self.paths = {tuple(k.split("|")): v for k, v in d.get("paths", {}).items()}
        self.unreachable = [tuple(k.split("|")) for k in d.get("unreachable", [])]
        self.dead_leg = {tuple(k.split("|")): v
                         for k, v in d.get("dead_leg", {}).items()}
        self.pools_read = d.get("pools_read")
        self.pools_failed = d.get("pools_failed")
        if assets is not None:
            self.assets = assets
        elif asset_table is not None:
            self.assets = nm.load_assets(asset_table)
        else:
            raise ValueError("pass asset_table (symbol -> params tuple) or assets")
        if params is not None:
            self.params = params
        else:
            saved = d.get("params", {})
            self.params = nm.Params(**{k: v for k, v in saved.items()
                                       if k in nm.Params.__dataclass_fields__
                                       and k not in ("prices", "venues")})

    # ---- same interface as navi_matrix.Matrix ---------------------------

    def row(self, B):
        return {A: v for (b, A), v in self.cells.items() if b == B}

    def row_min(self, B):
        r = {A: v for A, v in self.row(B).items() if v is not None}
        if not r:
            return None, None
        A = min(r, key=r.get)
        return r[A], A

    def col_min(self, A):
        c = {B: v for (B, a), v in self.cells.items() if a == A and v is not None}
        if not c:
            return None, None
        B = min(c, key=c.get)
        return c[B], B

    def q_star(self, B, lb=None):
        a = self.assets.get(B)
        if a is None:
            return None
        if lb is not None and abs(lb - a.lb) > 1e-9:
            raise ValueError(
                f"the saved matrix was computed at LB={a.lb:.2%} for {B}; "
                "re-evaluating at another bonus needs navi_matrix.build()")
        return self.row_min(B)[0]

    def demand(self, B, at="cap"):
        a, p = self.assets.get(B), self.params
        if a is None or not a.is_collateral:
            return None
        S = a.supply_cap if at == "cap" else (a.supplied or 0.0)
        px = self.prices.get(B)
        if px is None:
            return None
        return (p.beta * p.close_factor * p.seized_factor(a.lb, p.tf(B))
                * a.ltv * S * px)

    def car(self, B, at="cap"):
        q, qs = self.demand(B, at), self.q_star(B)
        if q is None or qs is None:
            return None
        _, binding = self.row_min(B)
        return {"asset": B, "Q": q, "Q_star": qs, "car": q - qs,
                "ratio": (q / qs) if qs else float("inf"), "binding": binding}


def load(path: str, asset_table=None, assets=None, params=None) -> "LoadedMatrix":
    """Rehydrate a saved matrix and join a risk-parameter table to it."""
    M = LoadedMatrix(path, asset_table=asset_table, assets=assets, params=params)
    n = len(M.cells)
    priced = sum(1 for v in M.cells.values() if v is not None)
    print(f"{n} cells, {priced} priced, {n - priced} unpriced · "
          f"CF={M.params.close_factor}, β={M.params.beta}, "
          f"treasury factors={M.params.treasury_factors or M.params.treasury_share}")
    if not M.prices:
        print("no prices in the file — Q and the ratios cannot be computed; "
              "pass prices via M.prices = {...}")
    return M


def frame_from_json(path: str) -> pd.DataFrame:
    with open(path) as fh:
        d = json.load(fh)
    rows = []
    for k, cap in d["cells"].items():
        B, A = k.split("|")
        p = d["paths"].get(k)
        rows.append({"collateral": B, "debt": A, "capacity_usd": cap,
                     "path": " → ".join(p) if p else None,
                     "hops": (len(p) - 1) if p else None})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Views
# ---------------------------------------------------------------------------

def collateral_view(M, at: str = "cap", flag_low: float = 1.0,
                    flag_high: float = 5.0,
                    exclude_debts: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """
    Per collateral: demand the parameters permit against the capacity the routes
    support, described by the whole row rather than only its minimum.

    The adversarial reading is the minimum — the worst debt asset a liquidation
    could be forced into. But a row whose minimum comes from one near-empty debt
    market says something different from a row that is thin throughout, so the
    median, 90th percentile and maximum are reported alongside it.

      q_min      row minimum, the adversarial capacity (= Q_star_usd)
      q_p10      10th percentile: how deep the thin tail of the row is
      q_median   the typical debt asset
      q_p90      the well-supplied end
      q_max      the best exit available
      spread_x   q_median / q_min, how far the minimum sits below the row

    exclude_debts drops named debt assets from every row, so the picture can be
    read without a handful of near-empty markets dominating it.
    """
    ex = set(exclude_debts or ())
    rows = []
    for B, a in M.assets.items():
        if not a.is_collateral:
            continue
        row_all = M.row(B)
        row = {A: v for A, v in row_all.items() if A not in ex}
        vals = sorted(v for v in row.values() if v is not None)
        if not vals:
            continue
        q = M.demand(B, at)
        if q is None:
            continue
        qs = vals[0]
        binding = min((A for A, v in row.items() if v == qs), default=None)
        ser = pd.Series(vals)
        r = {"Q": q, "Q_star": qs, "car": q - qs,
             "ratio": (q / qs) if qs else float("inf"), "binding": binding}
        rows.append({
            "asset": B,
            "ltv": a.ltv, "lt": a.lt, "lb": a.lb,
            "budget": M.params.budget(a.lb, M.params.tf(B)),
            "supply_cap_tokens": a.supply_cap,
            "price": M.prices.get(B),
            "Q_usd": q, "Q_star_usd": qs,
            "ratio": r["ratio"], "car_usd": r["car"],
            "binding_debt": r["binding"],
            "q_min": qs,
            "q_p10": float(ser.quantile(0.10)),
            "q_median": float(ser.median()),
            "q_p90": float(ser.quantile(0.90)),
            "q_max": float(ser.max()),
            "spread_x": (float(ser.median()) / qs) if qs else None,
            "ratio_median": (q / ser.median()) if ser.median() else None,
            "second_worst": vals[1] if len(vals) > 1 else None,
            "implied_cap_tokens": (qs / (M.params.beta * M.params.close_factor *
                                         M.params.seized_factor(a.lb, M.params.tf(B)) * a.ltv *
                                         (M.prices.get(B) or 1))
                                   if qs and a.ltv else None),
            "routes_priced": sum(1 for v in row.values() if v),
            "routes_none": sum(1 for v in row.values() if v == 0.0),
            "routes_missing": sum(1 for v in row.values() if v is None),
            "dead_legs": sum(1 for (b, A2) in getattr(M, "dead_leg", {})
                             if b == B and M.dead_leg[(b, A2)]),
        })
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["cap_utilisation"] = df["Q_usd"] / df["Q_star_usd"]
    df["flag"] = df["ratio"].apply(
        lambda x: "OVER" if x > flag_low
        else ("SLACK" if x < 1 / flag_high else "ok"))
    df["headroom_x"] = 1 / df["ratio"]
    return df.sort_values("ratio", ascending=False).reset_index(drop=True)


def row_stats(M, exclude_debts: Optional[Sequence[str]] = None) -> pd.DataFrame:
    """The capacity distribution of each collateral's row, without the demand
    comparison. Useful for seeing which assets are thin everywhere and which
    have one bad exit."""
    ex = set(exclude_debts or ())
    rows = []
    for B, a in M.assets.items():
        if not a.is_collateral:
            continue
        row = {A: v for A, v in M.row(B).items()
               if A not in ex and v is not None}
        if not row:
            continue
        ser = pd.Series(row)
        rows.append({
            "asset": B, "n_debts": len(ser),
            "min": ser.min(), "binding": ser.idxmin(),
            "p10": ser.quantile(0.10), "median": ser.median(),
            "p90": ser.quantile(0.90), "max": ser.max(), "best": ser.idxmax(),
            "spread_x": ser.median() / ser.min() if ser.min() else None,
            "n_below_100k": int((ser < 1e5).sum()),
            "n_zero": int((ser == 0).sum()),
        })
    return pd.DataFrame(rows).sort_values("median").reset_index(drop=True)


def debt_view(M) -> pd.DataFrame:
    """Per debt asset: the column minimum, a ceiling on its borrow cap."""
    rows = []
    for A in sorted({a for (_, a) in M.cells}):
        v, B = M.col_min(A)
        col = {b: c for (b, a), c in M.cells.items() if a == A and c is not None}
        ser = pd.Series(col) if col else pd.Series(dtype=float)
        rows.append({"debt": A, "min_capacity_usd": v, "binding_collateral": B,
                     "p10": ser.quantile(0.10) if len(ser) else None,
                     "median_capacity_usd": ser.median() if len(ser) else None,
                     "p90": ser.quantile(0.90) if len(ser) else None,
                     "max_capacity_usd": ser.max() if len(ser) else None,
                     "n_collaterals": len(col)})
    return pd.DataFrame(rows).sort_values("min_capacity_usd").reset_index(drop=True)


def concentration(M) -> pd.DataFrame:
    """
    How fragile each row's minimum is. A row whose worst and second-worst are
    close is constrained by its liquidity generally; a row with one outlier is
    constrained by a single route that may be remediable.
    """
    rows = []
    for B, a in M.assets.items():
        if not a.is_collateral:
            continue
        vals = sorted((A, v) for A, v in M.row(B).items() if v is not None)
        vals = sorted([(A, v) for A, v in M.row(B).items() if v is not None],
                      key=lambda x: x[1])
        if len(vals) < 2:
            continue
        worst, second = vals[0], vals[1]
        med = pd.Series([v for _, v in vals]).median()
        rows.append({
            "asset": B,
            "worst_debt": worst[0], "worst": worst[1],
            "second_debt": second[0], "second": second[1],
            "gap_x": second[1] / worst[1] if worst[1] else None,
            "median": med,
            "worst_vs_median_x": med / worst[1] if worst[1] else None,
            "diagnosis": ("single thin route" if worst[1] and second[1] / worst[1] > 2
                          else "generally thin"),
        })
    return pd.DataFrame(rows).sort_values("gap_x", ascending=False).reset_index(drop=True)


def hub_load(M, top: int = 15) -> pd.DataFrame:
    """Which intermediary tokens the binding routes pass through."""
    from collections import Counter
    c, binding = Counter(), Counter()
    for (B, A), path in M.paths.items():
        if not path:
            continue
        mid = path[1:-1]
        for t in mid:
            c[t] += 1
        mn, bind = M.row_min(B)
        if bind == A:
            for t in mid:
                binding[t] += 1
    df = pd.DataFrame([{"token": t, "routes": n, "on_binding_routes": binding[t]}
                       for t, n in c.most_common(top)])
    return df


def summary(M, at: str = "cap"):
    """One printed page: the three views, ordered by what to look at first."""
    cv = collateral_view(M, at)
    print(f"=== collateral side, demand at {at} "
          f"(CF={M.params.close_factor}, β={M.params.beta})\n")
    cols = ["asset", "ltv", "lb", "Q_usd", "Q_star_usd", "ratio",
            "binding_debt", "implied_cap_tokens", "flag"]
    with pd.option_context("display.float_format", lambda v: f"{v:,.3g}"):
        print(cv[cols].to_string(index=False))

    over = cv[cv["flag"] == "OVER"]
    slack = cv[cv["flag"] == "SLACK"]
    print(f"\n{len(over)} over capacity, {len(slack)} with large headroom, "
          f"{len(cv) - len(over) - len(slack)} in range")

    print("\n=== fragility of each row's minimum\n")
    with pd.option_context("display.float_format", lambda v: f"{v:,.3g}"):
        print(concentration(M)[["asset", "worst_debt", "worst", "second_debt",
                                "second", "gap_x", "diagnosis"]].to_string(index=False))

    print("\n=== debt side: borrow-cap ceilings\n")
    with pd.option_context("display.float_format", lambda v: f"{v:,.3g}"):
        print(debt_view(M).to_string(index=False))
    return cv
