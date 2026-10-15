"""
Failed reads must be loud: no partial GraphQL data, no silent truncation, no
output beyond reserves, and no failed or empty pool holding a leg's slot.

    python -m unittest tests.test_read_robustness
"""
import os
import sys
import unittest
from unittest import mock

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lrg import clmm, graph                      # noqa: E402
from lrg.book import PoolBook, pool_key          # noqa: E402


def _resp(payload):
    r = mock.Mock()
    r.json.return_value = payload
    return r


def _tick(i, net=1000):
    return {"name": {"json": i}, "value": {"json": {"index": {"bits": i & 0xFFFFFFFF},
                                                   "liquidity_net": {"bits": net}}}}


def _page(nodes, more, cursor="c"):
    return {"address": {"dynamicFields": {"nodes": nodes,
                                          "pageInfo": {"hasNextPage": more,
                                                       "endCursor": cursor}}}}


@mock.patch("lrg.clmm.time.sleep", lambda s: None)
class GraphQL(unittest.TestCase):
    def test_partial_error_raises(self):
        payload = {"data": {"object": None}, "errors": [{"message": "boom"}]}
        with mock.patch("lrg.clmm.requests.post", return_value=_resp(payload)):
            with self.assertRaisesRegex(RuntimeError, "GraphQL error: boom"):
                clmm.gql("query { x }")

    def test_retry_then_success(self):
        ok = {"data": {"x": 1}}
        bad = {"data": {"x": 1}, "errors": [{"message": "transient"}]}
        with mock.patch("lrg.clmm.requests.post",
                        side_effect=[_resp(bad), _resp(ok)]):
            self.assertEqual(clmm.gql("query { x }"), {"x": 1})

    def test_timeout_is_a_network_error_not_no_ticks(self):
        import run
        with mock.patch("lrg.clmm.requests.post",
                        side_effect=requests.exceptions.ReadTimeout("Read timed out")):
            with self.assertRaises(RuntimeError) as cm:
                clmm.fetch_ticks("0xabc", verbose=False)
        self.assertNotIn("no ticks", str(cm.exception))
        self.assertEqual(run.failure_kind(str(cm.exception)), "network")


class Truncation(unittest.TestCase):
    def test_size_mismatch(self):
        with mock.patch("lrg.clmm.gql", return_value=_page([_tick(10), _tick(20)], False)):
            with self.assertRaises(clmm.TruncatedRead):
                clmm.fetch_ticks("0xabc", verbose=False, expected=3)

    def test_size_match(self):
        with mock.patch("lrg.clmm.gql", return_value=_page([_tick(10), _tick(20)], False)):
            self.assertEqual(len(clmm.fetch_ticks("0xabc", verbose=False, expected=2)), 2)

    def test_page_cap(self):
        with mock.patch("lrg.clmm.gql", return_value=_page([_tick(10)], True)):
            with self.assertRaisesRegex(clmm.TruncatedRead, "more than 3 pages"):
                clmm.fetch_ticks("0xabc", verbose=False, max_pages=3)

    def test_missing_page_is_not_no_ticks(self):
        with mock.patch("lrg.clmm.gql", return_value={"address": None}):
            with self.assertRaisesRegex(RuntimeError, "no dynamic-field page"):
                clmm.fetch_ticks("0xabc", verbose=False)


class Reread(unittest.TestCase):
    def test_truncation_is_retried_then_succeeds(self):
        calls = []

        def once(pid, venue, verbose):
            calls.append(pid)
            if len(calls) < 2:
                raise clmm.TruncatedRead("truncated: read 700 of 702 fields")
            return "pool"

        with mock.patch("lrg.clmm._fetch_once", side_effect=once):
            self.assertEqual(clmm.fetch("0xabc", verbose=False), "pool")
        self.assertEqual(len(calls), 2)

    def test_truncation_that_persists_fails(self):
        with mock.patch("lrg.clmm._fetch_once",
                        side_effect=clmm.TruncatedRead("truncated: read 1 of 2 fields")):
            with self.assertRaises(clmm.TruncatedRead):
                clmm.fetch("0xabc", verbose=False)


class OutputCap(unittest.TestCase):
    def _pool(self, bal_a, bal_b):
        return clmm.ClmmPool(pool_id="0x1", venue="t", coin_a="0x1::a::A",
                             coin_b="0x1::b::B", sym_a="A", sym_b="B", dec_a=9,
                             dec_b=9, fee=0.0, sqrtP=1.0, liquidity=1e12,
                             tick_spacing=1, bal_a=bal_a, bal_b=bal_b,
                             ranges=[(0.5, 2.0, 1e12)])

    def test_capped_at_reserve(self):
        p = self._pool(bal_a=1_000, bal_b=1_000)
        self.assertGreater(p._out_ranges(1e9, "a"), 1_000)
        self.assertEqual(p.out(1e9, "a"), 1_000)
        self.assertEqual(p.out(1e9, "b"), 1_000)

    def test_unknown_balance_not_capped(self):
        p = self._pool(bal_a=0, bal_b=0)
        self.assertEqual(p.out(1e6, "a"), p._out_ranges(1e6, "a"))


class LegSlots(unittest.TestCase):
    X, Y = "0x1::x::X", "0x1::y::Y"

    def _adj(self, n):
        rows = [{"pool_id": f"0xp{i}", "coin_type_a": self.X, "coin_type_b": self.Y}
                for i in range(n)]
        return {self.X: {self.Y: rows}, self.Y: {self.X: rows}}

    @staticmethod
    def key_of(r):
        return pool_key(r["pool_id"], r["coin_type_a"], r["coin_type_b"])

    def test_failed_top_pool_is_replaced(self):
        book = mock.Mock()
        book.get.side_effect = lambda pid, coin_a=None, coin_b=None: (
            None if pid == "0xp0" else object())
        pools, chosen = graph.read_legs(self._adj(6), [(self.X, self.Y)], book, 4, self.key_of)
        ids = [k[0] for k in chosen[frozenset((self.X, self.Y))]]
        self.assertEqual(ids, ["0xp1", "0xp2", "0xp3", "0xp4"])

    def test_empty_pool_takes_no_slot(self):
        book = PoolBook(verbose=False, venue_of={})

        def fetch(pid, verbose=True):
            if pid == "0xp1":
                raise clmm.EmptyPool("empty pool: liquidity 0 and no initialized ticks")
            return mock.Mock(pool_id=pid)

        with mock.patch("lrg.clmm.fetch", side_effect=fetch):
            pools, chosen = graph.read_legs(self._adj(6), [(self.X, self.Y)],
                                            book, 4, self.key_of)
        ids = [k[0] for k in chosen[frozenset((self.X, self.Y))]]
        self.assertEqual(ids, ["0xp0", "0xp2", "0xp3", "0xp4"])
        self.assertEqual(len(book.empty), 1)
        self.assertEqual(len(book.failed), 0)


if __name__ == "__main__":
    unittest.main()
