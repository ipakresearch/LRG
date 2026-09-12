"""
Pool discovery.

Three sources, because no single one covers Sui:

  GeckoTerminal   per-token pool lists for the CLMM and constant-product
                  venues. Returns the Sui object id, the DEX id, both coin
                  types, reserve and 24-hour volume.
  DeepBook        its own indexer lists every pool; order books are not
                  indexed by GeckoTerminal.
  Aftermath       a public endpoint returning every pool, including thousands
                  of dust pools, so it needs hard filtering.

Output is one CSV: the pool universe the route graph is built over. Pool type
and fee tier are inferred from the venue id and the pool name, and should be
confirmed from chain for pools that matter.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, asdict, fields
from typing import Dict, List, Optional

API = "https://api.geckoterminal.com/api/v2"
NET = "sui-network"
HEADERS = {"Accept": "application/json", "User-Agent": "ipak-lrg/0.1"}
PAUSE = 2.2                      # free tier is about 30 calls a minute


def get(url: str, retries: int = 4):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = 12 * (attempt + 1)
                print(f"  rate limited, waiting {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 404:
                return None
            raise
        except Exception as e:
            if attempt == retries - 1:
                raise
            time.sleep(4)
    return None


@dataclass
class Pool:
    symbol_a: str
    symbol_b: str
    coin_type_a: str
    coin_type_b: str
    tvl_usd: float
    volume_24h: float
    venue: str
    pool_type: str
    fee_tier: Optional[float]
    pool_id: str
    both_in_navi: bool
    has_adapter: bool
    pool_name: str


def strip_net(tid: str) -> str:
    return tid.split("_", 1)[1] if tid.startswith(NET + "_") else tid


FEE_RE = re.compile(r"([\d.]+)\s*%")


def parse_fee(name: str) -> Optional[float]:
    m = FEE_RE.search(name or "")
    return float(m.group(1)) / 100.0 if m else None


def parse_pool(item: dict, by_type: Dict[str, str]) -> Optional[Pool]:
    a = item.get("attributes", {})
    rel = item.get("relationships", {})
    try:
        ta = strip_net(rel["base_token"]["data"]["id"])
        tb = strip_net(rel["quote_token"]["data"]["id"])
    except (KeyError, TypeError):
        return None
    venue = (rel.get("dex", {}).get("data", {}) or {}).get("id", "unknown")
    ptype = VENUE_TYPE.get(venue, "unknown")
    name = a.get("name", "") or ""
    parts = [p.strip() for p in name.split("/")] if "/" in name else ["", ""]
    sym_a = by_type.get(ta) or (parts[0] if parts[0] else ta.split("::")[-1])
    sym_b = by_type.get(tb) or (re.sub(r"\s*[\d.]+%\s*$", "", parts[1]).strip()
                                if len(parts) > 1 and parts[1] else tb.split("::")[-1])

    def f(x):
        try:
            return float(x)
        except (TypeError, ValueError):
            return 0.0

    vol = a.get("volume_usd", {}) or {}
    return Pool(
        symbol_a=sym_a, symbol_b=sym_b,
        coin_type_a=ta, coin_type_b=tb,
        tvl_usd=f(a.get("reserve_in_usd")),
        volume_24h=f(vol.get("h24")),
        venue=venue, pool_type=ptype, fee_tier=parse_fee(name),
        pool_id=a.get("address", ""),
        both_in_navi=(ta in by_type and tb in by_type),
        has_adapter=TYPE_BY_ADAPTER.get(ptype, False),
        pool_name=name,
    )


def discover(assets: Dict[str, str], min_tvl: float = 5000.0,
             pages: int = 2, verbose: bool = True) -> List[Pool]:
    """assets maps symbol -> coin type."""
    by_type = {v: k for k, v in assets.items()}
    seen: Dict[str, Pool] = {}

    for i, (sym, ctype) in enumerate(assets.items(), 1):
        enc = urllib.parse.quote(ctype, safe="")
        found = 0
        for page in range(1, pages + 1):
            url = f"{API}/networks/{NET}/tokens/{enc}/pools?page={page}"
            data = get(url)
            time.sleep(PAUSE)
            if not data or not data.get("data"):
                break
            for item in data["data"]:
                p = parse_pool(item, by_type)
                if p and p.pool_id and p.tvl_usd >= min_tvl:
                    if p.pool_id not in seen or p.tvl_usd > seen[p.pool_id].tvl_usd:
                        seen[p.pool_id] = p
                        found += 1
            if len(data["data"]) < 20:
                break
        if verbose:
            print(f"[{i:>2}/{len(assets)}] {sym:<9} +{found:>3} pools "
                  f"(running total {len(seen)})", file=sys.stderr)

    return sorted(seen.values(), key=lambda p: -p.tvl_usd)


# ---------------------------------------------------------------------------

def write_csv(pools: List[Pool], path: str):
    cols = [f.name for f in fields(Pool)]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for p in pools:
            w.writerow(asdict(p))
    print(f"wrote {len(pools)} pools to {path}", file=sys.stderr)


def to_dataframe(pools: List[Pool]):
    import pandas as pd
    return pd.DataFrame([asdict(p) for p in pools])


def summary(pools: List[Pool], assets: Dict[str, str]):
    by_type = {v: k for k, v in assets.items()}

    print(f"\n{len(pools)} pools   "
          f"${sum(p.tvl_usd for p in pools):,.0f} total TVL   "
          f"{sum(1 for p in pools if p.both_in_navi)} with both tokens in the Navi set")

    print("\nby venue")
    venues: Dict[str, list] = {}
    for p in pools:
        venues.setdefault(p.venue, []).append(p)
    print(f"  {'venue':<18}{'type':<11}{'pools':>7}{'TVL $':>16}{'adapter':>9}")
    for v, ps in sorted(venues.items(), key=lambda kv: -sum(x.tvl_usd for x in kv[1])):
        print(f"  {v:<18}{ps[0].pool_type:<11}{len(ps):>7}"
              f"{sum(x.tvl_usd for x in ps):>16,.0f}"
              f"{'yes' if ps[0].has_adapter else 'NO':>9}")

    print("\nby Navi asset  (pools where the asset appears, and their total TVL)")
    print(f"  {'asset':<10}{'pools':>7}{'TVL $':>16}   counterparties")
    for sym, ct in assets.items():
        ps = [p for p in pools if ct in (p.coin_type_a, p.coin_type_b)]
        others = sorted({(by_type.get(p.coin_type_b if p.coin_type_a == ct else p.coin_type_a)
                          or (p.symbol_b if p.coin_type_a == ct else p.symbol_a))
                         for p in ps})
        flag = "   <-- no pools" if not ps else ""
        print(f"  {sym:<10}{len(ps):>7}{sum(x.tvl_usd for x in ps):>16,.0f}   "
              f"{', '.join(others[:10])}{'…' if len(others) > 10 else ''}{flag}")

    inter = {}
    navi_types = set(assets.values())
    for p in pools:
        for ct, sym in ((p.coin_type_a, p.symbol_a), (p.coin_type_b, p.symbol_b)):
            if ct not in navi_types:
                inter.setdefault(sym, [0, 0.0])
                inter[sym][0] += 1
                inter[sym][1] += p.tvl_usd
    if inter:
        print("\nnon-Navi tokens appearing in these pools (candidate intermediaries)")
        for sym, (n, tvl) in sorted(inter.items(), key=lambda kv: -kv[1][1])[:15]:
            print(f"  {sym:<14}{n:>4} pools  ${tvl:>14,.0f}")
