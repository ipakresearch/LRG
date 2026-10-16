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
confirmed from chain for pools that matter. Coin types are normalized, and
each row records the UTC date it was discovered.

    from lrg import discovery
    from lrg.protocols import navi, navi_oracle
    prices = {ct: v["price"] for ct, v in navi_oracle.quotes().items()}
    rows, dropped = discovery.discover_all(navi.COIN_TYPES, navi.VENUES, prices)
    discovery.write_rows(rows, "data/navi_pools.new.csv")

GeckoTerminal's TVL (reserve_in_usd) is its own estimate and can be stale or
absurd for thinly traded tokens, so tvl_usd is replaced by the pool's
on-chain reserves at the given prices (verify_tvl); the listed figure is kept
as tvl_gecko. Either way it is a size, not a measure of executable depth.
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

from .coins import norm_type

API = "https://api.geckoterminal.com/api/v2"
NET = "sui-network"
HEADERS = {"Accept": "application/json", "User-Agent": "ipak-lrg/0.1"}
PAUSE = 2.2                      # free tier is about 30 calls a minute
MAX_PAGES = 10                   # free tier refuses page 11 (HTTP 401)

# GeckoTerminal dex id -> pool type
VENUE_TYPE: Dict[str, str] = {
    "cetus": "CLMM", "bluefin": "CLMM", "momentum": "CLMM",
    "turbos-finance": "CLMM", "flowx-clmm": "CLMM", "ferra-clmm": "CLMM",
    "magma-finance": "CLMM", "fullsail-finance": "CLMM",
    "ferra-dlmm": "DLMM", "kriya-dex": "AMM", "flow-x": "AMM",
    "suiswap": "AMM", "steamm": "AMM",
    "deepbook": "ORDERBOOK", "aftermath": "WEIGHTED",
}


def get(url: str, retries: int = 6):
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
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(4)
    # still rate limited: a silent None would truncate the token's pool list
    raise RuntimeError(f"rate limited after {retries} attempts: {url}")


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
    discovered_at: str = ""
    tvl_gecko: Optional[float] = None    # GeckoTerminal's reserve_in_usd, as listed
    tvl_source: str = "geckoterminal"    # what tvl_usd is: onchain | venue | geckoterminal


def strip_net(tid: str) -> str:
    return tid.split("_", 1)[1] if tid.startswith(NET + "_") else tid


FEE_RE = re.compile(r"([\d.]+)\s*%")


def parse_fee(name: str) -> Optional[float]:
    m = FEE_RE.search(name or "")
    return float(m.group(1)) / 100.0 if m else None


def parse_pool(item: dict, by_type: Dict[str, str], adapters=()) -> Optional[Pool]:
    a = item.get("attributes", {})
    rel = item.get("relationships", {})
    try:
        ta = norm_type(strip_net(rel["base_token"]["data"]["id"]))
        tb = norm_type(strip_net(rel["quote_token"]["data"]["id"]))
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
        has_adapter=venue in adapters,
        pool_name=name,
        discovered_at=time.strftime("%Y-%m-%d", time.gmtime()),
    )


def discover(assets: Dict[str, str], min_tvl: float = 5000.0,
             pages: int = MAX_PAGES, adapters=(), verbose: bool = True) -> List[Pool]:
    """
    assets maps symbol -> coin type; adapters is the set of venue ids the
    readers handle (sets has_adapter). Pages each token's pool list until a
    short page, up to `pages` (GeckoTerminal's free tier stops at 10); a token
    that reaches the cap is reported, since its list is truncated.
    """
    pages = min(pages, MAX_PAGES)
    by_type = {norm_type(v): k for k, v in assets.items()}
    seen: Dict[str, Pool] = {}
    capped = []

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
                p = parse_pool(item, by_type, adapters)
                if p and p.pool_id and p.tvl_usd >= min_tvl:
                    if p.pool_id not in seen or p.tvl_usd > seen[p.pool_id].tvl_usd:
                        seen[p.pool_id] = p
                        found += 1
            if len(data["data"]) < 20:
                break
        else:
            capped.append(sym)
        if verbose:
            print(f"[{i:>2}/{len(assets)}] {sym:<9} +{found:>3} pools "
                  f"(running total {len(seen)})", file=sys.stderr)

    if capped:
        print(f"WARNING: page cap ({pages}) reached for {', '.join(capped)}; "
              f"their pool lists are truncated", file=sys.stderr)
    return sorted(seen.values(), key=lambda p: -p.tvl_usd)


def discover_all(assets: Dict[str, str], adapters, prices: Dict[str, float],
                 min_tvl: float = 5000.0, extra_min_usd: float = 25_000.0,
                 verbose: bool = True) -> List[dict]:
    """
    The full pool universe as CSV rows: GeckoTerminal pools plus DeepBook and
    Aftermath, which it does not index. prices are USD by coin type (Navi's
    oracle), used for the DeepBook and Aftermath TVL estimates and floor.
    """
    from . import venues
    gecko = [asdict(p) for p in discover(assets, min_tvl=0.0, adapters=adapters,
                                         verbose=verbose)]
    rows, dropped = verify_tvl(gecko, prices, adapters, min_tvl=min_tvl, verbose=verbose)
    today = time.strftime("%Y-%m-%d", time.gmtime())
    for r in venues.discovery_rows(assets, prices, min_usd=extra_min_usd, verbose=verbose):
        r["discovered_at"] = today
        r["tvl_source"] = "venue"
        rows.append(r)
    for r in venues.mint_rows(assets):
        r["discovered_at"] = today
        rows.append(r)
    return rows, dropped


# Reserve fields across Sui DEX pool structs, each pair in the pool's own
# generic (coin) order.
_RESERVE_KEYS = (("coin_a", "coin_b"), ("reserve_x", "reserve_y"),
                 ("balance_x", "balance_y"), ("balance_a", "balance_b"),
                 ("reserve0", "reserve1"))


def onchain_tvl(pool_id: str, prices: Dict[str, float], _dec: dict) -> Optional[dict]:
    """
    USD value of a pool's on-chain reserves at `prices` (USD by coin type).
    A side with no price is valued as equal to the priced side; with neither
    priced, or no recognised reserve fields, returns None. These are total
    reserves, not depth near the current price.
    """
    from .clmm import gql, Q_OBJECT, Q_DECIMALS, split_generics
    mo = (gql(Q_OBJECT, {"a": pool_id}).get("object") or {}).get("asMoveObject")
    if not mo:
        return None
    j = mo["contents"]["json"]
    try:
        ca, cb = (norm_type(t) for t in split_generics(mo["contents"]["type"]["repr"]))
    except Exception:
        return None
    bal = next(((j[a], j[b]) for a, b in _RESERVE_KEYS
                if not isinstance(j.get(a), (dict, list, type(None)))
                and not isinstance(j.get(b), (dict, list, type(None)))), None)
    if bal is None:
        return None
    usd = []
    for t, raw in zip((ca, cb), bal):
        px = prices.get(t)
        if px is None:
            usd.append(None)
            continue
        if t not in _dec:
            _dec[t] = int((gql(Q_DECIMALS, {"t": t})["coinMetadata"] or {}).get("decimals", 9))
        usd.append(int(raw) / 10 ** _dec[t] * px)
    priced = [u for u in usd if u is not None]
    if not priced:
        return None
    return {"tvl": sum(priced) * (2 if len(priced) == 1 else 1),
            "coin_a": ca, "coin_b": cb}


def verify_tvl(rows: List[dict], prices: Dict[str, float], adapters,
               min_tvl: float = 5000.0, workers: int = 8,
               verbose: bool = True) -> tuple:
    """
    Replace GeckoTerminal's TVL with on-chain reserves at `prices`, keeping
    the listed figure as tvl_gecko. Pools on routed venues (`adapters`) are
    dropped when their on-chain TVL is under min_tvl or cannot be valued.
    Pools on other venues keep the listed figure when the chain gives none.
    Returns (kept rows, dropped [{pool_id, venue, pair, tvl_gecko, tvl_onchain, why}]).
    """
    from concurrent.futures import ThreadPoolExecutor
    dec: dict = {}

    def one(r):
        try:
            return onchain_tvl(r["pool_id"], prices, dec)
        except Exception as e:
            return {"error": str(e)[:120]}

    with ThreadPoolExecutor(workers) as ex:
        res = list(ex.map(one, rows))
    kept, dropped = [], []
    for r, v in zip(rows, res):
        r["tvl_gecko"] = r["tvl_usd"]
        routed = r["venue"] in adapters
        tvl = v.get("tvl") if v else None
        if tvl is not None:
            r["tvl_usd"], r["tvl_source"] = round(tvl, 2), "onchain"
        if (tvl is None and routed) or (tvl is not None and tvl < min_tvl) or (
                tvl is None and float(r["tvl_gecko"] or 0) < min_tvl):
            why = (v or {}).get("error") or ("below floor" if tvl is not None
                                             else "cannot value on chain")
            dropped.append({"pool_id": r["pool_id"], "venue": r["venue"],
                            "pair": f'{r["symbol_a"]}/{r["symbol_b"]}',
                            "tvl_gecko": r["tvl_gecko"], "tvl_onchain": tvl, "why": why})
            continue
        kept.append(r)
    if verbose:
        print(f"on-chain TVL: {len(kept)} kept, {len(dropped)} dropped "
              f"(floor ${min_tvl:,.0f})", file=sys.stderr)
    return sorted(kept, key=lambda r: -float(r["tvl_usd"] or 0)), dropped


def write_rows(rows: List[dict], path: str):
    cols = [f.name for f in fields(Pool)]
    with open(path, "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({c: r.get(c, "") for c in cols})
    print(f"wrote {len(rows)} pools to {path}", file=sys.stderr)


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
    by_type = {norm_type(v): k for k, v in assets.items()}

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
        ct = norm_type(ct)
        ps = [p for p in pools if ct in (p.coin_type_a, p.coin_type_b)]
        others = sorted({(by_type.get(p.coin_type_b if p.coin_type_a == ct else p.coin_type_a)
                          or (p.symbol_b if p.coin_type_a == ct else p.symbol_a))
                         for p in ps})
        flag = "   <-- no pools" if not ps else ""
        print(f"  {sym:<10}{len(ps):>7}{sum(x.tvl_usd for x in ps):>16,.0f}   "
              f"{', '.join(others[:10])}{'…' if len(others) > 10 else ''}{flag}")

    inter = {}
    navi_types = {norm_type(v) for v in assets.values()}
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
