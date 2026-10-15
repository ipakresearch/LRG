"""
Pool state, fetched once and cached.

`PoolBook` dispatches on venue: CLMM pools are read individually over GraphQL
(each needs its tick table walked), while DeepBook and Aftermath are pulled in
bulk from their own endpoints on first use. Reading one pool is expensive, and
a route table touches the same pools repeatedly, so the cache is what makes the
whole pipeline practical.
"""
from __future__ import annotations

import os
from typing import Dict, Optional, Sequence

from . import clmm, venues
from .coins import norm_type


class PoolBook:
    """Fetches pool state once, keeps it in memory and optionally on disk."""

    def __init__(self, cache: Optional[str] = None, verbose: bool = True,
                 venue_of: Optional[Dict[str, str]] = None,
                 tokens: Optional[Dict[str, str]] = None):
        """venue_of: pool_id -> venue, so non-CLMM pools are read by the right
        adapter instead of being sent through the tick walker."""
        self.pools: Dict[str, object] = {}
        self.failed: Dict[str, str] = {}
        self.cache = cache
        self.verbose = verbose
        self.venue_of = dict(venue_of or {})
        self.tokens = dict(tokens or {})
        self._extra: Dict[str, object] = {}      # venue -> {pool_id: pool}
        if cache:
            os.makedirs(cache, exist_ok=True)

    # ---- non-CLMM venues, loaded in bulk on first use --------------------

    def _load_venue(self, venue: str):
        if venue in self._extra:
            return
        try:
            if venue == "deepbook":
                pools = venues.deepbook_pools(self.tokens, verbose=self.verbose)
            elif venue == "aftermath":
                pools = venues.aftermath_pools(self.tokens, verbose=self.verbose)
            else:
                pools = []
        except Exception as e:
            if self.verbose:
                print(f"  {venue} load failed: {e}")
            pools = []
        # aftermath splits multi-coin pools, so key by (id, pair)
        d = {}
        for p in pools:
            d.setdefault(p.pool_id, []).append(p)
        self._extra[venue] = d

    def _path(self, pool_id: str) -> str:
        return os.path.join(self.cache, f"{pool_id}.json")

    def get(self, pool_id: str, venue: Optional[str] = None,
            coin_a: Optional[str] = None, coin_b: Optional[str] = None):
        if pool_id in self.pools:
            return self.pools[pool_id]
        if pool_id in self.failed:
            return None

        v = venue or self.venue_of.get(pool_id)
        if v in ("deepbook", "aftermath"):
            self._load_venue(v)
            cands = self._extra.get(v, {}).get(pool_id, [])
            pick = None
            for p in cands:
                if coin_a is None or ({norm_type(p.coin_a), norm_type(p.coin_b)}
                                      == {norm_type(coin_a), norm_type(coin_b)}):
                    pick = p; break
            if pick is None and cands:
                pick = cands[0]
            if pick is None:
                self.failed[pool_id] = f"{v}: pool not in venue listing"
                if self.verbose:
                    print(f"  SKIP {pool_id[:12]}…  {self.failed[pool_id]}")
                return None
            self.pools[pool_id] = pick
            return pick
        if self.cache and os.path.exists(self._path(pool_id)):
            try:
                p = clmm.load(self._path(pool_id))
                self.pools[pool_id] = p
                return p
            except Exception:
                pass
        try:
            p = clmm.fetch(pool_id, verbose=self.verbose)
            self.pools[pool_id] = p
            if self.cache:
                clmm.save(p, self._path(pool_id))
            return p
        except Exception as e:
            self.failed[pool_id] = str(e)
            if self.verbose:
                print(f"  SKIP {pool_id[:12]}…  {e}", flush=True)
            return None

    def load_leg(self, pool_ids: Sequence[str]) -> List[clmm.ClmmPool]:
        return [p for p in (self.get(i) for i in pool_ids) if p is not None]


