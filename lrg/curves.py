"""
Cost composition.

A leg is every pool between two tokens. Its cost curve maps USD in to USD out,
precomputed on a log grid so that route cost composes across hops — the output
of one leg is the input to the next — rather than being approximated by the
worst single leg. Route capacity inverts that composition.

With pick="split" a leg's trade is allocated across its pools by marginal
value, the way an aggregator routes. With "best" the single deepest pool
carries it, which is the conservative floor.
"""
from __future__ import annotations

import bisect
import math
from typing import Dict, List, Optional

from .coins import norm_type

GRID_LO, GRID_HI, GRID_N = 50.0, 5e7, 70


def _out_usd(p, side: str, usd_in: float, prices) -> float:
    """USD out of pool p for usd_in of its side-`side` coin. prices are keyed
    by normalized coin type."""
    coin_i = p.coin_a if side == "a" else p.coin_b
    dec_i = p.dec_a if side == "a" else p.dec_b
    coin_o = p.coin_b if side == "a" else p.coin_a
    dec_o = p.dec_b if side == "a" else p.dec_a
    px_i, px_o = prices.get(norm_type(coin_i)), prices.get(norm_type(coin_o))
    if not px_i or not px_o or usd_in <= 0:
        return 0.0
    return p.out(usd_in / px_i * 10 ** dec_i, side) / 10 ** dec_o * px_o


def side_of(p, coin_in: str) -> str:
    """Which of pool p's sides is coin_in, from the pool's own coin order.
    Raises if p does not hold coin_in."""
    t = norm_type(coin_in)
    if norm_type(p.coin_a) == t:
        return "a"
    if norm_type(p.coin_b) == t:
        return "b"
    raise ValueError(f"pool {p.pool_id} holds {p.coin_a} / {p.coin_b}, not {coin_in}")


def allocate(leg_pools, q_usd, prices, chunks: int = 32):
    """Greedy marginal split across a leg's pools -> (out USD, {pool: in USD})."""
    if not leg_pools or q_usd <= 0:
        return 0.0, {}
    if len(leg_pools) == 1:
        p, sd = leg_pools[0]
        return _out_usd(p, sd, q_usd, prices), {p.pool_id: q_usd}
    alloc = {p.pool_id: 0.0 for p, _ in leg_pools}
    cur = {p.pool_id: 0.0 for p, _ in leg_pools}
    step = q_usd / chunks
    for _ in range(chunks):
        pick, gain, newv = None, -1.0, 0.0
        for p, sd in leg_pools:
            nxt = _out_usd(p, sd, alloc[p.pool_id] + step, prices)
            g = nxt - cur[p.pool_id]
            if g > gain:
                pick, gain, newv = (p, sd), g, nxt
        alloc[pick[0].pool_id] += step
        cur[pick[0].pool_id] = newv
    return sum(cur.values()), {k: v for k, v in alloc.items() if v > 0}


class LegCurve:
    """Input USD -> output USD for one leg, precomputed on a log grid."""

    def __init__(self, leg_pools, prices, pick="split",
                 lo=GRID_LO, hi=GRID_HI, n=GRID_N):
        self.pools = leg_pools if pick == "split" else [
            max(leg_pools, key=lambda ps: getattr(ps[0], "liquidity", 0) or 0)]
        self.prices = prices
        self.xs = [lo * (hi / lo) ** (i / (n - 1)) for i in range(n)]
        self.ys, self.allocs = [], []
        for x in self.xs:
            o, al = allocate(self.pools, x, prices)
            self.ys.append(o)
            self.allocs.append(al)

    def out(self, q: float) -> float:
        if q <= 0:
            return 0.0
        if q <= self.xs[0]:
            return self.ys[0] * q / self.xs[0]
        if q >= self.xs[-1]:
            m = (self.ys[-1] - self.ys[-2]) / (self.xs[-1] - self.xs[-2])
            return self.ys[-1] + m * (q - self.xs[-1])
        i = bisect.bisect_left(self.xs, q)
        x0, x1, y0, y1 = self.xs[i - 1], self.xs[i], self.ys[i - 1], self.ys[i]
        t = (math.log(q) - math.log(x0)) / (math.log(x1) - math.log(x0))
        return y0 + t * (y1 - y0)

    def cost(self, q: float) -> float:
        return 1.0 if q <= 0 or self.out(q) <= 0 else 1 - self.out(q) / q

    def capacity(self, slip: float) -> float:
        if self.cost(self.xs[0]) > slip:
            return 0.0
        lo, hi = self.xs[0], self.xs[-1]
        if self.cost(hi) <= slip:
            return hi
        for _ in range(40):
            mid = 0.5 * (lo + hi)
            if self.cost(mid) <= slip:
                lo = mid
            else:
                hi = mid
        return lo

    def alloc_at(self, q: float) -> Dict[str, float]:
        i = min(range(len(self.xs)), key=lambda k: abs(self.xs[k] - q))
        scale = q / self.xs[i] if self.xs[i] else 0
        return {k: v * scale for k, v in self.allocs[i].items()}


def route_cost(curves: List[LegCurve], q: float) -> float:
    v = q
    for c in curves:
        v = c.out(v)
        if v <= 0:
            return 1.0
    return 1 - v / q


def route_capacity(curves: List[LegCurve], slip: float,
                   lo=GRID_LO, hi=GRID_HI) -> float:
    if route_cost(curves, lo) > slip:
        return 0.0
    if route_cost(curves, hi) <= slip:
        return hi
    for _ in range(42):
        mid = 0.5 * (lo + hi)
        if route_cost(curves, mid) <= slip:
            lo = mid
        else:
            hi = mid
    return lo


