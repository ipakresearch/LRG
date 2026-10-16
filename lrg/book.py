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
from typing import Dict, List, Optional, Sequence, Tuple

from . import clmm, venues
from .coins import norm_type


def pool_key(pool_id: str, coin_a: Optional[str] = None,
             coin_b: Optional[str] = None) -> Tuple[str, Optional[str], Optional[str]]:
    """
    A pool's identity: its object id plus the coin pair, normalized and
    sorted. Aftermath multi-coin pools share one object id across several
    pairs, so the id alone does not name a two-coin pool.
    """
    if coin_a is None or coin_b is None:
        return (pool_id, None, None)
    a, b = sorted((norm_type(coin_a), norm_type(coin_b)))
    return (pool_id, a, b)


class PoolBook:
    """Fetches pool state once, keeps it in memory and optionally on disk."""

    def __init__(self, cache: Optional[str] = None, verbose: bool = True,
                 venue_of: Optional[Dict[str, str]] = None,
                 tokens: Optional[Dict[str, str]] = None):
        """venue_of: pool_id -> venue, so non-CLMM pools are read by the right
        adapter instead of being sent through the tick walker."""
        self.pools: Dict[tuple, object] = {}       # pool_key -> pool
        self.failed: Dict[tuple, str] = {}         # pool_key -> reason
        self.empty: Dict[tuple, str] = {}          # pool_key -> reason; zero depth, not a failure
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
            elif venue == "lst-mint":
                pools = venues.mint_pools(self.tokens, verbose=self.verbose)
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
        key = pool_key(pool_id, coin_a, coin_b)
        if key in self.pools:
            return self.pools[key]
        if key in self.failed or key in self.empty:
            return None

        v = venue or self.venue_of.get(pool_id)
        if v in ("deepbook", "aftermath", "lst-mint"):
            self._load_venue(v)
            cands = self._extra.get(v, {}).get(pool_id, [])
            # the exact pair, never another pair of the same multi-coin pool
            pick = next((p for p in cands
                         if pool_key(pool_id, p.coin_a, p.coin_b) == key
                         or (coin_a is None and len(cands) == 1)), None)
            if pick is None:
                self.failed[key] = (f"{v}: pair not in venue listing" if cands
                                    else f"{v}: pool not in venue listing")
                if self.verbose:
                    print(f"  SKIP {pool_id[:12]}…  {self.failed[key]}")
                return None
            self.pools[key] = pick
            return pick
        if self.cache and os.path.exists(self._path(pool_id)):
            try:
                p = clmm.load(self._path(pool_id))
                self.pools[key] = p
                return p
            except Exception:
                pass
        try:
            p = clmm.fetch(pool_id, verbose=self.verbose)
            self.pools[key] = p
            if self.cache:
                clmm.save(p, self._path(pool_id))
            return p
        except clmm.EmptyPool as e:
            self.empty[key] = str(e)
            if self.verbose:
                print(f"  EMPTY {pool_id[:12]}…  {e}", flush=True)
            return None
        except Exception as e:
            self.failed[key] = str(e)
            if self.verbose:
                print(f"  SKIP {pool_id[:12]}…  {e}", flush=True)
            return None

    def load_leg(self, pool_ids: Sequence[str]) -> List[clmm.ClmmPool]:
        return [p for p in (self.get(i) for i in pool_ids) if p is not None]


