"""
Cost composition.

A leg is every pool between two tokens. Its cost curve maps USD in to USD out,
precomputed on a log grid so that route cost composes across hops — the output
of one leg is the input to the next — rather than being approximated by the
worst single leg. Route capacity inverts that composition.

Between grid points the curve interpolates cost (1 - out/in) linearly in
log(size). In the range that matters (cost well under 50%) cost is convex in
log size, so the chord lies above it and the grid errs toward too much cost.
route_capacity then refines the grid answer on the exact composition (each
leg's allocate() feeding the next), so a reported capacity's exact cost is
within the budget. Nothing is extrapolated past GRID_HI: above it the leg is
computed exactly, and a capacity at GRID_HI means "at least GRID_HI".

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
REF_USD = 10_000.0               # reference size for ranking pools


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
        # "best": the pool giving the most USD out at REF_USD. Venue
        # `.liquidity` values are in different units and are not compared.
        self.pools = leg_pools if pick == "split" else [
            max(leg_pools, key=lambda ps: _out_usd(ps[0], ps[1], REF_USD, prices))]
        self.prices = prices
        self.xs = [lo * (hi / lo) ** (i / (n - 1)) for i in range(n)]
        self.logxs = [math.log(x) for x in self.xs]
        self.ys, self.cs, self.allocs = [], [], []
        for x in self.xs:
            o, al = allocate(self.pools, x, prices)
            self.ys.append(o)
            self.cs.append(1 - o / x)
            self.allocs.append(al)

    def exact_out(self, q: float) -> float:
        """USD out with the leg's split computed for exactly q."""
        return allocate(self.pools, q, self.prices)[0] if q > 0 else 0.0

    def out(self, q: float) -> float:
        """USD out, from cost interpolated linearly in log size; exact above
        the grid (never extrapolated)."""
        if q <= 0:
            return 0.0
        if q <= self.xs[0]:
            return q * (1 - self.cs[0])
        if q > self.xs[-1]:
            return self.exact_out(q)
        i = bisect.bisect_left(self.xs, q)
        if self.xs[i] == q:
            return self.ys[i]
        t = (math.log(q) - self.logxs[i - 1]) / (self.logxs[i] - self.logxs[i - 1])
        c = self.cs[i - 1] + t * (self.cs[i] - self.cs[i - 1])
        return max(0.0, q * (1 - c))

    def cost(self, q: float) -> float:
        return 1.0 if q <= 0 or self.out(q) <= 0 else 1 - self.out(q) / q

    def capacity(self, slip: float) -> float:
        return route_capacity([self], slip)

    def alloc_at(self, q: float) -> Dict[str, float]:
        i = min(range(len(self.xs)), key=lambda k: abs(self.xs[k] - q))
        scale = q / self.xs[i] if self.xs[i] else 0
        return {k: v * scale for k, v in self.allocs[i].items()}


def route_cost(curves: List[LegCurve], q: float) -> float:
    """Route cost at q from the grid curves."""
    v = q
    for c in curves:
        v = c.out(v)
        if v <= 0:
            return 1.0
    return 1 - v / q


def exact_route_cost(curves: List[LegCurve], q: float) -> float:
    """Route cost at q with every leg's split computed for its actual input."""
    v = q
    for c in curves:
        v = c.exact_out(v)
        if v <= 0:
            return 1.0
    return 1 - v / q


REFINE_STEPS = 20


def grid_capacity(curves: List[LegCurve], slip: float,
                  lo=GRID_LO, hi=GRID_HI) -> float:
    """Capacity from the grid curves alone (bisection in log size)."""
    if route_cost(curves, lo) > slip:
        return 0.0
    if route_cost(curves, hi) <= slip:
        return hi
    for _ in range(42):
        mid = math.sqrt(lo * hi)
        if route_cost(curves, mid) <= slip:
            lo = mid
        else:
            hi = mid
    return lo


def route_capacity(curves: List[LegCurve], slip: float, lo=GRID_LO, hi=GRID_HI,
                   refine: bool = True, start: Optional[float] = None) -> float:
    """
    Largest size whose route cost stays within `slip`. The grid gives a first
    answer; with refine=True it is bracketed and bisected on the exact
    composition, so the returned size's exact cost is within `slip`. Returns
    `hi` when even `hi` fits ("at least hi"), 0 when not even `lo` does.
    """
    g = grid_capacity(curves, slip, lo, hi) if start is None else start
    if not refine:
        return g
    ok = lambda q: exact_route_cost(curves, q) <= slip
    # bracket [a, b]: a fits exactly (or is 0), b does not (or is hi and fits)
    a = g if g > 0 else lo
    if ok(a):
        b = min(a * 1.25, hi)
        while ok(b):
            if b >= hi:
                return hi
            a, b = b, min(b * 1.25, hi)
    else:
        b = a
        a = a / 1.25
        while a >= lo and not ok(a):
            b, a = a, a / 1.25
        if a < lo:
            if not ok(lo):
                return 0.0
            a = lo
    for _ in range(REFINE_STEPS):
        mid = math.sqrt(a * b)
        if ok(mid):
            a = mid
        else:
            b = mid
    return a


def is_saturated(cap: Optional[float], hi: float = GRID_HI) -> bool:
    """A capacity at the top of the grid: the true value is at least `hi`."""
    return cap is not None and cap >= hi * (1 - 1e-9)
