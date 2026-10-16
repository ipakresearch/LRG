"""
Stress materiality: the dust floor on positions, the floor on a pair's Q,
the first failing shock with the monotonicity flag, Q* over material debts,
and the saved positions and matrix that --from-run reloads.

    python -m unittest tests.test_stress_floors -v
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import positions as navi_positions              # noqa: E402
import run                                       # noqa: E402
from lrg import matrix                           # noqa: E402
from lrg.analysis import LoadedMatrix            # noqa: E402
from tests.test_stress import STATE, SUI, USDC, VSUI, position, FakeMatrix, CF  # noqa: E402

COMPLETE = lambda k: {"status": "complete"}               # noqa: E731

BIG = position("a", {SUI: 100_000.0}, {USDC: 85_000.0})   # HF 0.94, Q = 1.035 * 35,000
DUST = position("d", {SUI: 100.0}, {USDC: 85.0})          # $85 of debt: dust at $100
MID = position("m", {SUI: 1_000.0}, {USDC: 850.0})        # $850: kept, Q = 362.25


def params(**kw):
    return matrix.Params(close_factor=CF, beta=1.0, **kw)


def baseline(rows):
    return {(r["collateral"], r["debt"]): r for r in rows if r["scenario"] == "baseline"}


class Floors(unittest.TestCase):
    def test_dust_dropped_and_counted(self):
        M = FakeMatrix({("SUI", "USDC"): 1e9}, {})
        pos = {"state": STATE, "positions": [BIG, DUST]}
        rows, _, _, _, _, info = run.stress(params(), M, pos, COMPLETE)
        self.assertEqual((info["dust_positions"], info["positions_stressed"]), (1, 1))
        self.assertAlmostEqual(info["dust_debt_usd"], 85.0)
        r = baseline(rows)[("SUI", "USDC")]
        self.assertEqual(r["n_positions"], 1)
        self.assertAlmostEqual(r["Q"], 1.035 * 35_000)

    def test_pair_floor(self):
        # Q* = 100: MID alone (Q 362) exceeds it but is under $1,000
        M = FakeMatrix({("SUI", "USDC"): 100.0}, {})
        rows, per_coll, _, _, _, _ = run.stress(params(), M,
                                                {"state": STATE, "positions": [MID]}, COMPLETE)
        r = baseline(rows)[("SUI", "USDC")]
        self.assertAlmostEqual(r["Q"], 1.035 * 350)
        self.assertEqual((r["material"], r["fails"]), (False, False))
        self.assertEqual(per_coll["SUI"]["stress_status"], "holds")
        self.assertIsNone(per_coll["SUI"]["breaking_shock"])
        # with BIG it is material and fails at baseline
        rows, per_coll, _, _, _, _ = run.stress(params(), M,
                                                {"state": STATE, "positions": [MID, BIG]}, COMPLETE)
        r = baseline(rows)[("SUI", "USDC")]
        self.assertEqual((r["material"], r["fails"]), (True, True))
        self.assertEqual((per_coll["SUI"]["breaking_shock"], per_coll["SUI"]["scenario"]),
                         (0.0, "baseline"))


def row(scenario, shock, fails, Q=2000.0, kind="grid"):
    return {"collateral": "SUI", "debt": "USDC", "scenario": scenario, "kind": kind,
            "shock": shock, "fails": fails, "D_usd": Q, "Q": Q, "Q_star": 1500.0,
            "n_positions": 3, "fails_emode": None}


class Breaks(unittest.TestCase):
    def test_first_shock_and_monotonicity(self):
        rows = [row("baseline", 0.0, False, 100.0),
                row("SUI family", 0.1, True), row("SUI family", 0.15, False, 900.0),
                row("SUI family", 0.2, True, 3000.0)]
        (b,) = [x for x in run.stress_breaks(rows, ["SUI family"], COMPLETE)
                if x["scenario"] == "SUI family"]
        self.assertEqual((b["status"], b["first_fail_shock"], b["Q"]), ("breaks", 0.1, 2000.0))
        self.assertTrue(b["non_monotone"])
        # 0.15 fails no more, nor do 0.25..0.5, which have no liquidations
        self.assertEqual(b["recovers_at"].split(";")[0], "0.15")
        self.assertEqual(b["max_Q"], 3000.0)

    def test_holds_and_baseline_in_every_series(self):
        rows = [row("baseline", 0.0, True)]
        out = run.stress_breaks(rows, ["SUI family", "BTC family"], COMPLETE)
        # baseline is shock 0 of both groups and of the three depegs
        self.assertEqual(len(out), 5)
        self.assertTrue(all(b["first_fail_shock"] == 0.0 for b in out))
        rows = [row("baseline", 0.0, False), row("SUI family", 0.5, False)]
        (b,) = [x for x in run.stress_breaks(rows, ["SUI family"], COMPLETE)
                if x["scenario"] == "SUI family"]
        self.assertEqual((b["status"], b["first_fail_shock"], b["non_monotone"]),
                         ("holds", None, False))


class StarFields(unittest.TestCase):
    def test_q_star_over_material_debts(self):
        M = matrix.Matrix(params=params(), assets={}, prices={},
                          cells={("SUI", "haSUI"): 0.0, ("SUI", "USDC"): 5e6, ("SUI", "DEEP"): 2e5},
                          paths={})
        s_rows = [{"collateral": "SUI", "debt": "haSUI", "material": False},
                  {"collateral": "SUI", "debt": "USDC", "material": True},
                  {"collateral": "SUI", "debt": "DEEP", "material": True}]
        f = run.star_fields(M, "SUI", s_rows)
        self.assertEqual((f["Q_star"], f["binding_debt"]), (2e5, "DEEP"))
        self.assertEqual((f["Q_star_all"], f["binding_debt_all"]), (0.0, "haSUI"))
        f = run.star_fields(M, "SUI", [])
        self.assertEqual((f["Q_star"], f["binding_debt"], f["material_debts"]), (None, None, []))


class SavedRun(unittest.TestCase):
    def test_positions_round_trip(self):
        state = dict(STATE, price_ms={SUI: 1}, user_emode={"e": 1})
        pos = {"positions": [BIG, position("e", {VSUI: 1.0}, {SUI: 0.9}, emode=1)],
               "debt_tokens_all": {USDC: 85_000.0}, "stats": {"positions": 2},
               "state": state, "borrowed": {"USDC": 85_000.0}}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "positions.json.gz")
            navi_positions.save_positions(pos, path)
            back = navi_positions.load_positions(path)
        self.assertEqual(back["positions"], pos["positions"])
        self.assertEqual(back["debt_tokens_all"], pos["debt_tokens_all"])
        for k in ("reserves", "prices", "emode_pairs", "protected"):
            self.assertEqual(back["state"][k], state[k])
        self.assertEqual(navi_positions.health_factor(back["state"], back["positions"][0]),
                         navi_positions.health_factor(state, BIG))

    def test_matrix_round_trip_with_emode_capacity(self):
        key = ("0xabc", ("0x2::sui::SUI", "0xdba::usdc::USDC"))
        M = matrix.Matrix(params=params(treasury_factors={"vSUI": 0.65}), assets={}, prices={},
                          cells={("vSUI", "SUI"): 1000.0}, paths={("vSUI", "SUI"): ["vSUI", "SUI"]},
                          coverage={("vSUI", "SUI"): {"status": "incomplete", "failed": [key]}})
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "matrix.json")
            M.save(path, extra={"emode_capacity": {"vSUI|SUI": {run.emode_key(0.05): 30.0}}})
            L = LoadedMatrix(path, assets={})
        self.assertEqual(L.pair_capacity("vSUI", "SUI", 0.05), 30.0)
        self.assertIsNone(L.pair_capacity("vSUI", "USDC", 0.05))
        with self.assertRaises(KeyError):
            L.pair_capacity("vSUI", "SUI", 0.07)
        self.assertEqual(L.coverage[("vSUI", "SUI")], {"status": "incomplete", "failed": [key]})
        self.assertEqual((L.params.min_position_usd, L.params.min_sold_usd), (100.0, 1000.0))
        self.assertEqual(L.params.tf("vSUI"), 0.65)


if __name__ == "__main__":
    unittest.main()
