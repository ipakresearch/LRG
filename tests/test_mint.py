"""
The LST mint venue: SUI -> LST at a fixed rate, never LST -> SUI.

    python -m unittest tests.test_mint -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lrg import graph, venues                    # noqa: E402
from lrg.coins import norm_type                  # noqa: E402
from lrg.curves import LegCurve, OneWay, side_of  # noqa: E402

SUI = norm_type("0x2::sui::SUI")
LST = norm_type("0x1::lst::LST")


def mint(rate=1.05):
    return venues.MintPool(pool_id="0xmint", venue="lst-mint", coin_a=SUI, coin_b=LST,
                           sym_a="SUI", sym_b="LST", dec_a=9, dec_b=9, rate=rate)


class Mint(unittest.TestCase):
    def test_rate_and_no_depth_limit(self):
        p = mint(1.05)
        for q in (1e9, 1e15, 1e18):
            self.assertAlmostEqual(p.out(q, "a") / (q / 1.05), 1.0, places=12)

    def test_one_way(self):
        p = mint()
        self.assertEqual(side_of(p, SUI), "a")
        with self.assertRaises(OneWay):
            side_of(p, LST)
        with self.assertRaises(ValueError):
            p.out(1e9, "b")

    def test_leg_uses_mint_only_into_the_lst(self):
        k = ("0xmint", SUI, LST)
        pools = {k: mint()}
        skipped = []
        self.assertEqual(len(graph.leg_pools(pools, [k], SUI, skipped)), 1)
        self.assertEqual(graph.leg_pools(pools, [k], LST, skipped), [])
        self.assertEqual(skipped, [])            # one-way is not a misread pool

    def test_curve_cost_is_the_premium_at_every_size(self):
        # LST priced by the oracle at 1.00 SUI-equivalent, minted at 1.05:
        # cost = 1 - 1/1.05 at any size, with no slippage
        prices = {SUI: 1.0, LST: 1.0}
        c = LegCurve([(mint(1.05), "a")], prices)
        for q in (100.0, 1e6, 4e7):
            self.assertAlmostEqual(c.cost(q), 1 - 1 / 1.05, places=9)


if __name__ == "__main__":
    unittest.main()
