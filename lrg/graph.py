"""
The route graph and its network view.

Builds the graph from the discovered pool list, selects a capacity-optimal path
per ordered pair at each slippage level, and writes a self-contained HTML view.

Two rules the whole pipeline follows:

  * A pair with no priced path is recorded with capacity zero and a reason, not
    dropped. Absence of an exit is the worst case, not a missing datum.
  * A pool with reported liquidity and no 24-hour volume is marked dead, and any
    route crossing one is marked. Standing liquidity that does not trade is not
    a demonstrated exit.
"""
from __future__ import annotations

import csv
import json
import os
import time
from collections import defaultdict
from typing import Dict, List, Optional, Sequence, Tuple

from . import clmm
from .coins import norm_type, USDC
from .book import PoolBook, pool_key
from .curves import REF_USD, LegCurve, OneWay, _out_usd, route_capacity, route_cost, side_of

SLIPPAGES = [0.02, 0.05, 0.07, 0.10]
MAX_POOLS_PER_LEG = 4
MAX_HOPS = 4
MAX_CANDIDATES = 20

# ---------------------------------------------------------------------------
# Pool set
# ---------------------------------------------------------------------------

def load_pool_rows(csv_path: str, venues) -> List[dict]:
    """Pool rows for the given venues, with coin types normalized. The CSV's
    a/b order is the listing's, not necessarily the pool's: never take a swap
    side from it (see leg_pools)."""
    with open(csv_path) as fh:
        # mint venues have no reserves (tvl 0) and unlimited depth
        rows = [r for r in csv.DictReader(fh)
                if r["venue"] in venues
                and (float(r["tvl_usd"] or 0) > 0 or r.get("pool_type") == "MINT")]
    for r in rows:
        r["coin_type_a"] = norm_type(r["coin_type_a"])
        r["coin_type_b"] = norm_type(r["coin_type_b"])
    return rows


def leg_pools(pools, keys, coin_in: str, skipped: Optional[list] = None):
    """[(pool, side)] for the fetched pools among `keys` (pool_key tuples),
    with each side taken from the pool's own coin order. A pool that does not
    hold coin_in is left out and recorded in `skipped`."""
    out = []
    for k in keys:
        p = pools.get(k)
        if p is None:
            continue
        try:
            out.append((p, side_of(p, coin_in)))
        except OneWay:
            continue                      # a one-way venue, not a misread pool
        except ValueError as e:
            if skipped is not None:
                skipped.append(str(e))
    return out


def build_adjacency(rows):
    adj = defaultdict(lambda: defaultdict(list))
    for r in rows:
        adj[r["coin_type_a"]][r["coin_type_b"]].append(r)
        adj[r["coin_type_b"]][r["coin_type_a"]].append(r)
    for a in adj:
        for b in adj[a]:
            adj[a][b].sort(key=lambda x: -float(x["tvl_usd"]))
    return adj


def read_legs(adj, legs, book, key_of) -> tuple:
    """
    Read every pool on the given legs. Returns (pools by key,
    {frozenset(leg): [keys of the readable pools]}). Failed and empty pools
    are left out, so they never take a slot when the leg's pools are chosen.
    """
    pools, readable = {}, {}
    for x, y in legs:
        leg = frozenset((x, y))
        if leg in readable:
            continue
        got = []
        for r in adj[x][y]:
            key = key_of(r)
            if key not in pools:
                q = book.get(key[0], coin_a=key[1], coin_b=key[2])
                if q is None:
                    continue
                pools[key] = q
            got.append(key)
        readable[leg] = got
    return pools, readable


def choose_pools(pools, keys, coin_in: str, prices, n: int,
                 ref_usd: float = REF_USD) -> list:
    """
    The n pools of a leg that give the most USD out for ref_usd of coin_in,
    best first. Ranked by execution rather than listed TVL, which is in no
    common unit across venues and can be stale.
    """
    scored = []
    for k in keys:
        p = pools[k]
        try:
            sd = side_of(p, coin_in)
        except ValueError:
            continue
        scored.append((_out_usd(p, sd, ref_usd, prices), k))
    scored.sort(key=lambda t: -t[0])
    return [k for o, k in scored[:n] if o > 0] or [k for _, k in scored[:n]]


def all_paths(adj, s: str, d: str, max_hops: int) -> List[List[str]]:
    out = []

    def walk(node, chain):
        if len(chain) - 1 >= max_hops:
            return
        for nxt in adj[node]:
            if nxt in chain:
                continue
            if nxt == d:
                out.append(chain + [nxt])
            else:
                walk(nxt, chain + [nxt])

    if s in adj:
        walk(s, [s])
    return out


# Reference sizes for ranking paths. Thin routes (e.g. into LSTs) can be best
# only below ~$1.5k, deep ones only above $100k, so one size is not enough;
# with $10k and $250k alone, stSUI -> SUI -> vSUI ranked 24th of 29 and was
# missed although it sets stSUI's row minimum (checked against an uncapped
# search, 2026-10-16).
RANK_SIZES = (1_000.0, 10_000.0, 250_000.0)


def rank_paths(paths, curve, k: Optional[int] = 6, sizes=RANK_SIZES) -> list:
    """
    [(path, leg curves)] for the paths whose every leg is priced, keeping the
    union of the k cheapest at each reference size (grid route cost; fewer
    hops break ties). k=None keeps every priced path. One cost evaluation per
    path and size, so all paths can be ranked before any capacity search.
    """
    priced = []
    for path in paths:
        cs = [curve(path[i], path[i + 1]) for i in range(len(path) - 1)]
        if all(cs):
            priced.append((path, cs))
    if k is None:
        return sorted(priced, key=lambda pc: (route_cost(pc[1], sizes[0]), len(pc[0])))
    keep, seen = [], set()
    for size in sizes:
        order = sorted(priced, key=lambda pc: (route_cost(pc[1], size), len(pc[0])))
        for path, cs in order[:k]:
            if tuple(path) not in seen:
                seen.add(tuple(path))
                keep.append((path, cs))
    return keep


def derive_prices(pools, anchor: str = USDC,
                  seed: Optional[Dict[str, float]] = None) -> Dict[str, float]:
    """
    USD per normalized coin type, propagated from the anchor (native USDC)
    and any `seed` prices through pool spot prices. Seeded coins keep their
    price. Pools are tried deepest first, by USD TVL under the prices known so
    far, so a thin mispriced pool cannot poison a chain that a deep pool can
    price. (Venue `.liquidity` values are in different units and are not
    compared.)
    """
    prices = {norm_type(k): v for k, v in (seed or {}).items()}
    prices.setdefault(norm_type(anchor), 1.0)

    def usd_tvl(p):
        pa = prices.get(norm_type(p.coin_a))
        pb = prices.get(norm_type(p.coin_b))
        # a pool with one side unpriced is valued at twice its priced side
        v = p.tvl(pa or 0, pb or 0)
        return v if (pa and pb) else 2 * v

    for _ in range(8):
        changed = False
        ordered = sorted(pools.values(), key=lambda p: -usd_tvl(p))
        for p in ordered:
            ta, tb = norm_type(p.coin_a), norm_type(p.coin_b)
            pa, pb, px = prices.get(ta), prices.get(tb), p.price()
            if px <= 0:
                continue
            if pa is not None and pb is None:
                prices[tb] = pa / px; changed = True
            elif pb is not None and pa is None:
                prices[ta] = pb * px; changed = True
        if not changed:
            break
    return prices


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------

def build(csv_path: str, tokens: Dict[str, str], venues,
          cache: str = "pools_cache", out: str = "liq_network.html",
          template: str = "templates/network.html", slippages=SLIPPAGES,
          pick: str = "split", max_hops: int = MAX_HOPS,
          max_pools_per_leg: int = MAX_POOLS_PER_LEG,
          max_candidates: int = MAX_CANDIDATES, verbose: bool = True):
    """tokens maps symbol -> coin type; venues is the set of venue ids to use."""

    rows = load_pool_rows(csv_path, venues)
    adj = build_adjacency(rows)
    key_of = lambda r: pool_key(r["pool_id"], r["coin_type_a"], r["coin_type_b"])
    vol_of = {key_of(r): float(r["volume_24h"] or 0) for r in rows}
    name = {s: norm_type(t) for s, t in tokens.items()}
    tname = {v: k for k, v in name.items()}
    for r in rows:
        tname.setdefault(r["coin_type_a"], r["symbol_a"])
        tname.setdefault(r["coin_type_b"], r["symbol_b"])

    assets = [s for s in name if name[s] in adj]
    absent = [s for s in name if s not in assets]

    readable: Dict[frozenset, list] = {}     # filled by read_legs below
    chosen: Dict[Tuple[str, str], list] = {}

    def leg_ids(x, y):
        """The leg's chosen pools for x -> y, best first (after the read)."""
        if (x, y) not in chosen:
            chosen[(x, y)] = choose_pools(pools, readable.get(frozenset((x, y)), []),
                                          x, prices, max_pools_per_leg)
        return chosen[(x, y)]

    def leg_volume(x, y):
        return sum(vol_of.get(pid, 0.0) for pid in leg_ids(x, y))

    # ---- candidate paths: all of them, ranked once pools are read ----------
    cands: Dict[Tuple[str, str], List[List[str]]] = {}
    for a in assets:
        for b in assets:
            if a == b:
                continue
            cands[(a, b)] = all_paths(adj, name[a], name[b], max_hops)
    if verbose:
        tot = sum(len(v) for v in cands.values())
        empty = sum(1 for v in cands.values() if not v)
        print(f"{len(cands)} pairs · {tot} candidate paths (≤{max_hops} hops) · "
              f"{empty} pairs with no path at all")

    # ---- pool state -------------------------------------------------------
    legs = sorted({(p[i], p[i + 1]) for ps in cands.values() for p in ps
                   for i in range(len(p) - 1)})
    venue_of = {r["pool_id"]: r["venue"] for r in rows}
    book = PoolBook(cache=cache, verbose=verbose, venue_of=venue_of,
                    tokens=tokens)
    if verbose:
        print(f"reading pools for {len(legs)} legs…")
    pools, legs_read = read_legs(adj, legs, book, key_of)
    readable.update(legs_read)
    prices = derive_prices(pools)
    if verbose:
        print(f"{len(pools)} readable, {len(book.failed)} unreadable · "
              f"prices for {len(prices)} tokens")

    # ---- leg curves -------------------------------------------------------
    curve_cache: Dict[Tuple[str, str], Optional[LegCurve]] = {}

    def curve(x, y):
        if (x, y) not in curve_cache:
            lp = leg_pools(pools, leg_ids(x, y), x)
            curve_cache[(x, y)] = LegCurve(lp, prices, pick=pick) if lp else None
        return curve_cache[(x, y)]

    if verbose:
        distinct = {(p[i], p[i + 1]) for ps in cands.values() for p in ps
                    for i in range(len(p) - 1)}
        print(f"building {len(distinct)} leg curves…")
        for x, y in distinct:
            curve(x, y)

    # ---- routes: every pair emitted, with a reason when there is none ------
    routes_out, pressure = [], defaultdict(lambda: defaultdict(float))
    no_route, dead_routes = [], []

    for (a, b), paths in sorted(cands.items()):
        usable = rank_paths(paths, curve, k=max(1, max_candidates // len(RANK_SIZES)))

        if not usable:
            reason = ("no path within the hop limit" if not paths
                      else "no readable pool on any candidate path")
            no_route.append(f"{a} → {b}")
            routes_out.append({
                "pair": f"{a} → {b}", "src": a, "dst": b, "reachable": False,
                "reason": reason,
                "by_slip": {f"{s:.2f}": {"path": None, "cap": 0.0, "legs": [],
                                         "dead": False} for s in slippages},
                "alts": [[tname[t] for t in p] for p in paths[:5]],
            })
            continue

        by_slip, route_dead = {}, False
        for s in slippages:
            key = f"{s:.2f}"
            best, best_cap = None, -1.0
            for p, cs in usable:
                cap = route_capacity(cs, s)
                if cap > best_cap:
                    best, best_cap = (p, cs), cap
            p, cs = best
            legs, v = [], best_cap
            for i, c in enumerate(cs):
                al = c.alloc_at(v)
                # within one leg an object id names one pool, even for Aftermath
                pk_of = {k[0]: k for k in leg_ids(p[i], p[i + 1])}
                for pid, amt in al.items():
                    pk = pk_of[pid]
                    sd = side_of(pools[pk], p[i])
                    pressure[pk][f"{key}:{sd}"] += amt
                lv = leg_volume(p[i], p[i + 1])
                legs.append({
                    "from": tname[p[i]], "to": tname[p[i + 1]],
                    "in": round(v, 2), "out": round(c.out(v), 2),
                    "cap": round(c.capacity(s), 2),
                    "volume_24h": round(lv, 0), "dead": lv <= 0,
                    "pools": [{"pool": "|".join(pk_of[pid]), "usd": round(amt, 2)}
                              for pid, amt in sorted(al.items(), key=lambda kv: -kv[1])],
                })
                v = c.out(v)
            d = any(l["dead"] for l in legs)
            route_dead |= d
            by_slip[key] = {"path": [tname[t] for t in p],
                            "cap": round(best_cap, 2), "legs": legs, "dead": d}

        if route_dead:
            dead_routes.append(f"{a} → {b}")
        routes_out.append({"pair": f"{a} → {b}", "src": a, "dst": b,
                           "reachable": True, "reason": None, "by_slip": by_slip,
                           "alts": [[tname[t] for t in p] for p, _ in usable[:5]]})
        if verbose and len(routes_out) % 100 == 0:
            print(f"  {len(routes_out)} pairs costed…", flush=True)

    # ---- nodes and edges --------------------------------------------------
    nodes = []
    for pk, p in pools.items():
        vol = vol_of.get(pk, 0.0)
        nodes.append({
            "id": "|".join(x for x in pk if x), "venue": p.venue, "sym_a": p.sym_a, "sym_b": p.sym_b,
            "label": f"{p.sym_a}/{p.sym_b}", "fee": p.fee,
            "tvl": round(p.tvl(prices.get(norm_type(p.coin_a), 0),
                               prices.get(norm_type(p.coin_b), 0)), 0),
            "volume_24h": round(vol, 0), "dead": vol <= 0,
            "cap": {f"{s:.2f}": {sd: round(LegCurve([(p, sd)], prices,
                                                    pick="best").capacity(s), 2)
                                 for sd in ("a", "b")} for s in slippages},
            "pressure": {k: round(v, 2) for k, v in pressure[pk].items()},
        })

    edges = defaultdict(int)
    for r in routes_out:
        if not r["reachable"]:
            continue
        for sl in r["by_slip"].values():
            legs = sl["legs"]
            for i in range(len(legs) - 1):
                for x in legs[i]["pools"]:
                    for y in legs[i + 1]["pools"]:
                        if x["pool"] != y["pool"]:
                            edges[(x["pool"], y["pool"])] += 1

    data = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime()),
        "slippages": [f"{s:.2f}" for s in slippages],
        "execution": pick, "venues": sorted(venues), "max_hops": max_hops,
        "max_candidates": max_candidates,
        "nodes": nodes,
        "edges": [{"source": s, "target": t, "n": n} for (s, t), n in edges.items()],
        "routes": routes_out,
        "no_route": sorted(no_route), "dead_routes": sorted(dead_routes),
        "absent_assets": absent,
        "prices": {k: round(v, 6) for k, v in prices.items()},   # by coin type
        "labels": {k: tname.get(k, k.rsplit("::", 1)[-1]) for k in prices},
    }
    with open(template) as fh:
        html = fh.read()
    with open(out, "w") as fh:
        fh.write(html.replace("__DATA__", json.dumps(data)))
    if verbose:
        dead_pools = sum(1 for n in nodes if n["dead"])
        print(f"\n{len(nodes)} pools ({dead_pools} with no 24h volume) · "
              f"{len(routes_out)} pairs · {len(no_route)} with no route · "
              f"{len(dead_routes)} crossing a dead pool")
        print(f"execution={pick} · max_hops={max_hops} · "
              f"max_candidates={max_candidates}")
        print(f"wrote {out}")
    return data
