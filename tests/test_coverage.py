"""
Coverage status per pair: a failed read marks a pair incomplete only where it
could have changed the pair's capacity.

    python -m unittest tests.test_coverage -v
"""
import os
import sys
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lrg import matrix                       # noqa: E402
from lrg.book import pool_key                # noqa: E402
from lrg.coins import norm_type              # noqa: E402

# normalized, as build() normalizes every coin type it handles
X, Y, Z = (norm_type(t) for t in ("0x1::x::X", "0x1::y::Y", "0x1::z::Z"))
NAME = {"X": X, "Y": Y, "Z": Z}


def row(pid, a, b, tvl):
    return {"pool_id": pid, "coin_type_a": a, "coin_type_b": b, "tvl_usd": str(tvl)}


def key_of(r):
    return pool_key(r["pool_id"], r["coin_type_a"], r["coin_type_b"])


class Coverage(unittest.TestCase):
    def setUp(self):
        # X-Y leg: five pools, four read (top 4 slots), one failed
        self.xy = [row(f"0xy{i}", X, Y, t) for i, t in enumerate((900, 800, 700, 600, 500))]
        # Y-Z leg: one pool, failed
        self.yz = [row("0xyz", Y, Z, 1000)]
        self.adj = {X: {Y: self.xy}, Y: {X: self.xy, Z: self.yz}, Z: {Y: self.yz}}
        self.tvl = {key_of(r): float(r["tvl_usd"]) for r in self.xy + self.yz}

    def run_cov(self, failed, cells, kept, cands, chosen_xy, curves_none=()):
        chosen = {(X, Y): chosen_xy, (Y, X): chosen_xy, (Y, Z): [], (Z, Y): []}
        curve = lambda a, b: None if (a, b) in curves_none else object()
        return matrix._coverage(cells, cands, kept, curve, lambda a, b: chosen[(a, b)],
                                self.adj, key_of, self.tvl, set(failed), 4, NAME)

    def test_failed_pool_in_a_top_slot(self):
        failed = [key_of(self.xy[0])]                       # the $900 pool
        chosen = [key_of(r) for r in self.xy[1:]]           # 800..500 read
        cov = self.run_cov(failed, {("X", "Y"): 1000.0}, {("X", "Y"): [[X, Y]]},
                           {("X", "Y"): [[X, Y]]}, chosen)
        self.assertEqual(cov[("X", "Y")]["status"], "incomplete")
        self.assertEqual(cov[("X", "Y")]["failed"], failed)

    def test_failed_pool_below_the_slots_does_not_count(self):
        failed = [key_of(self.xy[4])]                       # the $500 pool, 5th
        chosen = [key_of(r) for r in self.xy[:4]]           # 900..600 read and chosen
        cov = self.run_cov(failed, {("X", "Y"): 1000.0}, {("X", "Y"): [[X, Y]]},
                           {("X", "Y"): [[X, Y]]}, chosen)
        self.assertEqual(cov[("X", "Y")]["status"], "complete")

    def test_route_lost_to_a_failed_read(self):
        # X->Z only via Y; the Y-Z pool failed, so the path is unpriced and the
        # pair has capacity 0: incomplete, not a trustworthy zero
        failed = [key_of(self.yz[0])]
        chosen = [key_of(r) for r in self.xy[:4]]
        cov = self.run_cov(failed, {("X", "Z"): 0.0}, {("X", "Z"): []},
                           {("X", "Z"): [[X, Y, Z]]}, chosen, curves_none={(Y, Z)})
        self.assertEqual(cov[("X", "Z")]["status"], "incomplete")

    def test_zero_capacity_with_failed_pool_on_either_coin(self):
        failed = [key_of(self.yz[0])]
        cov = self.run_cov(failed, {("Z", "X"): 0.0}, {}, {("Z", "X"): []}, [])
        self.assertEqual(cov[("Z", "X")]["status"], "incomplete")

    def test_no_failures_is_complete(self):
        chosen = [key_of(r) for r in self.xy[:4]]
        cov = self.run_cov([], {("X", "Y"): 1000.0, ("X", "Z"): 0.0},
                           {("X", "Y"): [[X, Y]]}, {("X", "Y"): [[X, Y]], ("X", "Z"): []}, chosen)
        self.assertTrue(all(c["status"] == "complete" for c in cov.values()))


class Overtaking(unittest.TestCase):
    """A failed pool on a losing path counts only if it could lift that path
    past the winner: capacity scales at most by (1 + T/L) on the leg."""

    def setUp(self):
        # winner: X->Z direct (one pool, read). Loser: X->Y->Z, where X-Y holds
        # $1,000 in its chosen pool and a failed pool of $T sits on that leg.
        self.xz = [row("0xxz", X, Z, 5000)]
        self.yz = [row("0xyz", Y, Z, 5000)]

    def cov(self, failed_tvl, loser_cap, winner_cap=1000.0):
        xy = [row("0xa", X, Y, failed_tvl), row("0xb", X, Y, 1000)]
        adj = {X: {Y: xy, Z: self.xz}, Y: {X: xy, Z: self.yz}, Z: {X: self.xz, Y: self.yz}}
        tvl = {key_of(r): float(r["tvl_usd"]) for r in xy + self.xz + self.yz}
        chosen = {(X, Y): [key_of(xy[1])], (Y, Z): [key_of(self.yz[0])],
                  (X, Z): [key_of(self.xz[0])]}
        kept = {("X", "Z"): [[X, Z], [X, Y, Z]]}
        caps = iter([loser_cap])
        with unittest.mock.patch("lrg.matrix.grid_capacity", lambda cs, tau: next(caps)):
            return matrix._coverage({("X", "Z"): winner_cap}, kept, kept, lambda a, b: object(),
                                    lambda a, b: chosen[(a, b)], adj, key_of, tvl,
                                    {key_of(xy[0])}, 4, NAME, budgets={("X", "Z"): 0.03},
                                    kept_curves={("X", "Z"): [["w"], ["l1", "l2"]]},
                                    winners={("X", "Z"): 0})[("X", "Z")]["status"]

    def test_small_missing_depth_cannot_overtake(self):
        # loser 400, failed pool doubles the leg at most -> 800 < 1000
        self.assertEqual(self.cov(failed_tvl=1000, loser_cap=400.0), "complete")

    def test_large_missing_depth_could_overtake(self):
        # loser 400, failed pool could lift it 4x -> 1600 >= 1000
        self.assertEqual(self.cov(failed_tvl=3000, loser_cap=400.0), "incomplete")


class Signal1(unittest.TestCase):
    def test_fails_only_on_complete_pairs(self):
        import run
        rows = [{"collateral": "B", "debt": "A1", "Q_cap": 10.0, "Q_star": 5.0, "coverage": "incomplete"},
                {"collateral": "B", "debt": "A2", "Q_cap": 1.0, "Q_star": 5.0, "coverage": "complete"},
                {"collateral": "C", "debt": "A1", "Q_cap": 10.0, "Q_star": 5.0, "coverage": "complete"}]
        out = run.signal_1(rows)
        self.assertEqual(out["B"]["status"], "unresolved")
        self.assertEqual(out["C"]["status"], "fail")


if __name__ == "__main__":
    unittest.main()
