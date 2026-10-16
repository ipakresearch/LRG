"""
Grid curves against exact capacity.

A constant-product pool has a closed-form capacity, and a route of two such
pools can be bisected exactly; the refined capacity must land within 0.5% of
both, and its exact cost must stay within the budget.

    python -m unittest tests.test_curves -v
"""
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lrg import curves                                   # noqa: E402
from lrg.curves import (GRID_HI, LegCurve, exact_route_cost, grid_capacity,  # noqa: E402
                        is_saturated, route_capacity)

A, B, C = "0x1::a::A", "0x1::b::B", "0x1::c::C"
PRICES = {curves.norm_type(t): 1.0 for t in (A, B, C)}   # $1 each; raw = USD (0 decimals)


class CPPool:
    """x*y=k with a proportional fee; reserves in raw units (= USD here)."""

    def __init__(self, pid, coin_a, coin_b, reserve, fee):
        self.pool_id, self.coin_a, self.coin_b = pid, coin_a, coin_b
        self.dec_a = self.dec_b = 0
        self.x = self.y = reserve
        self.fee = fee

    def out(self, q, side="a"):
        x, y = (self.x, self.y) if side == "a" else (self.y, self.x)
        qn = q * (1 - self.fee)
        return y * qn / (x + qn)


def cp_capacity(reserve, fee, s):
    """Largest q with 1 - out(q)/q <= s for a balanced pool at spot 1."""
    return reserve * ((1 - fee) / (1 - s) - 1) / (1 - fee)


def old_capacity(legs, s):
    """The previous method: USD out interpolated linearly in log size."""
    def out(c, q):
        if q <= c.xs[0]:
            return c.ys[0] * q / c.xs[0]
        if q >= c.xs[-1]:
            m = (c.ys[-1] - c.ys[-2]) / (c.xs[-1] - c.xs[-2])
            return c.ys[-1] + m * (q - c.xs[-1])
        i = next(k for k, x in enumerate(c.xs) if x >= q)
        t = (math.log(q) - math.log(c.xs[i - 1])) / (math.log(c.xs[i]) - math.log(c.xs[i - 1]))
        return c.ys[i - 1] + t * (c.ys[i] - c.ys[i - 1])

    def cost(q):
        v = q
        for c in legs:
            v = out(c, v)
        return 1 - v / q

    lo, hi = curves.GRID_LO, GRID_HI
    for _ in range(60):
        mid = 0.5 * (lo + hi)
        lo, hi = (mid, hi) if cost(mid) <= s else (lo, mid)
    return lo


BUDGETS = (0.01, 0.02, 0.05, 0.0817, 0.10)
REPORT = []


class SinglePool(unittest.TestCase):
    def test_against_closed_form(self):
        R, f = 10_000_000.0, 0.0025
        leg = LegCurve([(CPPool("p", A, B, R, f), "a")], PRICES)
        for s in BUDGETS:
            exact = cp_capacity(R, f, s)
            refined = route_capacity([leg], s)
            grid = grid_capacity([leg], s)
            old = old_capacity([leg], s)
            REPORT.append(("single", s, exact, old, grid, refined))
            self.assertLess(abs(refined / exact - 1), 0.005, f"budget {s}")
            self.assertLessEqual(exact_route_cost([leg], refined), s + 1e-12)


class TwoHop(unittest.TestCase):
    def test_against_exact_bisection(self):
        p1 = CPPool("p1", A, B, 8_000_000.0, 0.0025)
        p2 = CPPool("p2", B, C, 3_000_000.0, 0.0005)
        legs = [LegCurve([(p1, "a")], PRICES), LegCurve([(p2, "a")], PRICES)]

        def exact_cost(q):
            return 1 - p2.out(p1.out(q, "a"), "a") / q

        for s in BUDGETS:
            lo, hi = 1.0, 1e9
            for _ in range(200):
                mid = math.sqrt(lo * hi)
                lo, hi = (mid, hi) if exact_cost(mid) <= s else (lo, mid)
            refined = route_capacity(legs, s)
            REPORT.append(("two-hop", s, lo, old_capacity(legs, s),
                           grid_capacity(legs, s), refined))
            self.assertLess(abs(refined / lo - 1), 0.005, f"budget {s}")
            self.assertLessEqual(exact_route_cost(legs, refined), s + 1e-12)


class Saturation(unittest.TestCase):
    def test_deep_pool_saturates(self):
        leg = LegCurve([(CPPool("p", A, B, 1e13, 0.0001), "a")], PRICES)
        cap = route_capacity([leg], 0.05)
        self.assertEqual(cap, GRID_HI)
        self.assertTrue(is_saturated(cap))

    def test_no_extrapolation_above_grid(self):
        pool = CPPool("p", A, B, 1e8, 0.0025)
        leg = LegCurve([(pool, "a")], PRICES)
        q = 2 * GRID_HI
        self.assertAlmostEqual(leg.out(q), pool.out(q, "a"), places=6)


def tearDownModule():
    if REPORT:
        print("\n  route    budget      exact        old(out)    grid(cost)   refined   old err  refined err")
        for name, s, exact, old, grid, ref in REPORT:
            print(f"  {name:8} {s:6.2%} {exact:>12,.0f} {old:>12,.0f} {grid:>12,.0f} {ref:>10,.0f}"
                  f"  {old / exact - 1:+7.2%}  {ref / exact - 1:+8.3%}")


if __name__ == "__main__":
    unittest.main()
