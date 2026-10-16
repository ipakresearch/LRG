"""
Stress liquidation flows against hand calculations, on a synthetic state:
single collateral, two collaterals and two debts, E-Mode, protected,
unpriced and zero-collateral positions.

    python -m unittest tests.test_stress -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import run                                       # noqa: E402
from lrg import matrix                           # noqa: E402
from lrg.coins import norm_type                  # noqa: E402
from lrg.protocols import navi                   # noqa: E402

SUI, USDC, VSUI, X = 0, 1, 2, 3
CF = 0.35


def reserve(sym, lt, lb, tf):
    return {"coin_type": norm_type(navi.COIN_TYPES[sym]), "lt": lt, "lb": lb,
            "treasury_factor": tf}


STATE = {
    "reserves": {SUI: reserve("SUI", 0.80, 0.10, 0.65),
                 USDC: reserve("USDC", 0.85, 0.08, 0.55),
                 VSUI: reserve("vSUI", 0.80, 0.10, 0.65),
                 X: {"coin_type": norm_type("0x9::x::X"), "lt": 0.5, "lb": 0.1,
                     "treasury_factor": 0.5}},
    "prices": {SUI: 1.0, USDC: 1.0, VSUI: 1.0},            # X has no price
    "emode_pairs": {1: {VSUI: {"collateral": True, "debt": False, "ltv": 0.90,
                               "lt": 0.93, "lb": 0.05},
                        SUI: {"collateral": False, "debt": True, "ltv": 0.0,
                              "lt": 0.0, "lb": 0.05}}},
    "borrow_weights": {}, "user_emode": {"e": 1}, "protected": {"p"},
}


def position(user, coll, debt, emode=None, protected=False):
    return {"user": user, "collateral": coll, "debt": debt, "emode": emode,
            "protected": protected}


P1 = position("a", {SUI: 100.0}, {USDC: 85.0})                       # HF 0.941
P2 = position("b", {SUI: 100.0, USDC: 100.0}, {USDC: 100.0, SUI: 80.0})  # HF 0.917
P3 = position("e", {VSUI: 100.0}, {SUI: 95.0}, emode=1)              # HF 0.979 (E-Mode LT)
P4 = position("p", {SUI: 100.0}, {USDC: 85.0}, protected=True)
P5 = position("u", {X: 10.0}, {USDC: 100.0})
P6 = position("z", {}, {USDC: 50.0})


def flows_of(p):
    return run.position_flows(STATE, p, {}, CF)


class Flows(unittest.TestCase):
    def test_single_collateral_cf_on_collateral(self):
        status, f = flows_of(P1)
        self.assertEqual(status, "ok")
        ((b, a, D, base, lb, emode),) = f
        self.assertEqual((b, a, lb, emode), (SUI, USDC, 0.10, False))
        self.assertAlmostEqual(D, 85.0)
        self.assertAlmostEqual(base, min(85.0, CF * 100.0))      # 35: CF x collateral value

    def test_two_collateral_two_debt_by_borrowing_power(self):
        status, f = flows_of(P2)
        got = {(b, a): (D, base) for b, a, D, base, _, _ in f}
        power = {SUI: 100 * 0.80, USDC: 100 * 0.85}                 # 80, 85
        tot = sum(power.values())
        for (b, a), d in {(SUI, USDC): 100, (USDC, USDC): 100, (SUI, SUI): 80, (USDC, SUI): 80}.items():
            D = d * power[b] / tot
            base = min(D, CF * 100.0 * d / 180.0)
            self.assertAlmostEqual(got[(b, a)][0], D)
            self.assertAlmostEqual(got[(b, a)][1], base)
        self.assertAlmostEqual(sum(v[0] for v in got.values()), 180.0)

    def test_emode_bonus(self):
        status, ((b, a, D, base, lb, emode),) = flows_of(P3)
        self.assertEqual((lb, emode), (0.05, True))
        self.assertAlmostEqual(base, 35.0)

    def test_unpriced_and_bad_debt(self):
        self.assertEqual(flows_of(P5)[0], "unpriced")
        status, bad = flows_of(P6)
        self.assertEqual(status, "bad_debt")
        self.assertEqual(bad, {USDC: 50.0})


class FakeMatrix:
    def __init__(self, cells, emode_caps):
        self.cells, self.paths, self._e = cells, {}, emode_caps

    def pair_capacity(self, B, A, lb):
        return self._e.get((B, A, lb))


class Aggregate(unittest.TestCase):
    def test_stress_rows(self):
        import lrg.protocols.navi  # noqa: F401
        M = FakeMatrix({("SUI", "USDC"): 70.0, ("vSUI", "SUI"): 1000.0,
                        ("USDC", "USDC"): 1e9, ("SUI", "SUI"): 1e9, ("USDC", "SUI"): 1e9},
                       {("vSUI", "SUI", 0.05): 30.0})
        # floors off: the hand calculations use positions of tens of dollars
        params = matrix.Params(close_factor=CF, beta=1.0, min_position_usd=0, min_sold_usd=0)
        pos = {"state": STATE, "positions": [P1, P2, P3, P4, P5, P6]}
        rows, per_coll, _, stats, _, _ = run.stress(params, M, pos, lambda k: {"status": "complete"})
        base = {r["scenario"]: r for r in stats}["baseline"]
        self.assertEqual((base["liquidatable"], base["unpriced"]), (4, 1))
        self.assertAlmostEqual(base["bad_debt_usd"], 50.0)
        r = {(x["collateral"], x["debt"]): x for x in rows if x["scenario"] == "baseline"}
        # SUI->USDC: P1 + P4 (35 each) + P2's SUI share (19.44); seized at 1 + 0.35*0.10
        q = 1.035 * (35 + 35 + CF * 100 * 100 / 180)
        self.assertAlmostEqual(r[("SUI", "USDC")]["Q"], q)
        self.assertTrue(r[("SUI", "USDC")]["fails"])                 # q ~ 92.6 > 70
        self.assertAlmostEqual(r[("SUI", "USDC")]["D_protected_usd"], 85.0)
        # vSUI->SUI is E-Mode only: Q = (1 + 0.35*0.05)*35 = 35.61 > 30 at the E-Mode budget
        e = r[("vSUI", "SUI")]
        self.assertAlmostEqual(e["Q_emode"], 1.0175 * 35)
        self.assertTrue(e["fails_emode"])
        self.assertTrue(e["fails"])                                  # though 35.6 < 1000 normally


if __name__ == "__main__":
    unittest.main()
