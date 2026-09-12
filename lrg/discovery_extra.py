"""
DeepBook and Aftermath pool discovery — neither is indexed by GeckoTerminal.

Step 1, probe what actually responds:

    import discover_extra as dx
    dx.probe_deepbook()
    dx.probe_aftermath()

Step 2, once an endpoint works, pull pools and append them to the topology CSV:

    rows = dx.deepbook_pools()
    dx.append_to_csv(rows, "navi_pools.csv")

The probes print raw responses so the parsers can be written against real shapes
rather than guessed ones.
"""
from __future__ import annotations

import csv
import json
from typing import Dict, List, Optional

import requests

UA = {"User-Agent": "ipak-cdl/0.1", "Accept": "application/json"}

DEEPBOOK_BASES = [
    "https://deepbook-indexer.mainnet.mystenlabs.com",
    "https://deepbook-indexer.mainnet.mystenlabs.com/v1",
    "https://api.deepbook.tech",
]
DEEPBOOK_PATHS = ["/get_pools", "/pools", "/all_pools", "/v1/get_pools",
                  "/summary", "/ticker", "/assets"]

AFTERMATH_URLS = [
    "https://aftermath.finance/api/pools",
    "https://aftermath.finance/api/pools/all",
    "https://api.aftermath.finance/pools",
    "https://aftermath.finance/api/pools/summary",
    "https://aftermath.finance/api/router/pools",
]


def _try(url: str, timeout: int = 25):
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        body = r.text
        ok = r.status_code == 200
        try:
            data = r.json()
        except Exception:
            data = None
        return ok, r.status_code, data, body[:400]
    except Exception as e:
        return False, None, None, f"FAILED {e}"


def probe_deepbook(show: int = 1200):
    """Try the documented indexer endpoints; print whichever returns JSON."""
    hits = []
    for base in DEEPBOOK_BASES:
        for path in DEEPBOOK_PATHS:
            url = base + path
            ok, code, data, snip = _try(url)
            mark = "OK " if (ok and data is not None) else "   "
            print(f"{mark}{code}  {url}")
            if ok and data is not None:
                hits.append((url, data))
    for url, data in hits[:2]:
        print(f"\n===== {url}")
        print(json.dumps(data, indent=1)[:show])
    if not hits:
        print("\nnothing responded with JSON — the indexer host may have moved")
    return hits


def probe_aftermath(show: int = 1200):
    hits = []
    for url in AFTERMATH_URLS:
        ok, code, data, snip = _try(url)
        mark = "OK " if (ok and data is not None) else "   "
        print(f"{mark}{code}  {url}")
        if ok and data is not None:
            hits.append((url, data))
    # Aftermath's API is partly POST-based
    for url in ["https://aftermath.finance/api/pools", "https://aftermath.finance/api/pools/all"]:
        try:
            r = requests.post(url, headers=UA, json={}, timeout=25)
            if r.status_code == 200:
                print(f"OK  POST {r.status_code}  {url}")
                hits.append((url + " [POST]", r.json()))
        except Exception as e:
            print(f"    POST failed {url}: {e}")
    for url, data in hits[:2]:
        print(f"\n===== {url}")
        print(json.dumps(data, indent=1)[:show])
    if not hits:
        print("\nnothing responded — Aftermath pools may need their TS SDK "
              "or on-chain registry traversal")
    return hits


# ---------------------------------------------------------------------------
# Parsers — adjust field names once the probe output is known
# ---------------------------------------------------------------------------

def deepbook_pools(url: Optional[str] = None) -> List[dict]:
    """
    Pull DeepBook pools into topology CSV rows.
    Expected fields per pool (v3 indexer): pool_id, pool_name, base_asset_id,
    quote_asset_id, base_asset_symbol, quote_asset_symbol.
    """
    urls = [url] if url else [b + p for b in DEEPBOOK_BASES for p in ("/get_pools", "/pools")]
    data = None
    for u in urls:
        ok, _, d, _ = _try(u)
        if ok and d:
            data = d; break
    if data is None:
        raise SystemExit("no DeepBook endpoint responded — run probe_deepbook() first")

    items = data if isinstance(data, list) else next(
        (v for v in data.values() if isinstance(v, list)), [])
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        pid = it.get("pool_id") or it.get("poolId") or it.get("id")
        ta = it.get("base_asset_id") or it.get("base_asset") or it.get("baseAsset")
        tb = it.get("quote_asset_id") or it.get("quote_asset") or it.get("quoteAsset")
        if not (pid and ta and tb):
            continue
        sa = it.get("base_asset_symbol") or (ta.split("::")[-1] if "::" in str(ta) else "?")
        sb = it.get("quote_asset_symbol") or (tb.split("::")[-1] if "::" in str(tb) else "?")
        out.append({
            "symbol_a": sa, "symbol_b": sb,
            "coin_type_a": str(ta) if str(ta).startswith("0x") else "0x" + str(ta),
            "coin_type_b": str(tb) if str(tb).startswith("0x") else "0x" + str(tb),
            "tvl_usd": "0", "volume_24h": "0", "venue": "deepbook",
            "pool_type": "ORDERBOOK", "fee_tier": "",
            "pool_id": pid, "both_in_navi": "", "has_adapter": "False",
            "pool_name": it.get("pool_name") or f"{sa} / {sb}",
        })
    return out


def append_to_csv(rows: List[dict], csv_path: str, tokens: Dict[str, str],
                  known_only: bool = True):
    """Append discovered pools, skipping duplicates and unrelated pairs."""
    navi = set(tokens.values())
    existing = set()
    with open(csv_path) as fh:
        rdr = csv.DictReader(fh)
        cols = rdr.fieldnames
        for r in rdr:
            existing.add(r["pool_id"])

    added = 0
    with open(csv_path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        for r in rows:
            if r["pool_id"] in existing:
                continue
            both = r["coin_type_a"] in navi and r["coin_type_b"] in navi
            if known_only and not (r["coin_type_a"] in navi or r["coin_type_b"] in navi):
                continue
            r["both_in_navi"] = str(both)
            w.writerow({c: r.get(c, "") for c in cols})
            added += 1
    print(f"appended {added} pools to {csv_path}")
    return added
