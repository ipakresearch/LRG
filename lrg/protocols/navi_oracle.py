"""
Navi's on-chain oracle prices, Sui mainnet.

Navi prices every reserve from one PriceOracle object. Its `price_oracles`
table maps an oracle id (u8) to {value, decimal, timestamp}; each reserve in
Storage names its oracle id. The USD price is value / 10**decimal, with the
decimal stored per entry: 6, 8 and 9 all occur, so no single exponent is
right for every asset.

    from lrg.protocols import navi_oracle
    navi_oracle.prices()          # {symbol: USD} for the assets in navi.py
    navi_oracle.quotes()          # per reserve: oracle id, raw value, decimal, age

Several reserves share one feed: the BTC wrappers, suiETH and Wormhole WETH,
suiUSDT and Wormhole USDT.
"""
from __future__ import annotations

import time
from typing import Dict

from ..clmm import gql, Q_FIELDS, Q_OBJECT
from .navi import COIN_TYPES

STORAGE = "0xbb4e2f4b6205c2e2a2db47aeb4f830796ec7c005f88537ee775986639bc442fe"
PRICE_ORACLE = "0x1568865ed9a0b5ec414220e8f79b3d04c77acc82358f6e5ae4635687392ffbef"


def norm_type(coin_type: str) -> str:
    """Coin type with the address zero-padded to 64 hex digits."""
    addr, rest = coin_type.split("::", 1)
    addr = addr.lower()
    addr = addr[2:] if addr.startswith("0x") else addr
    return "0x" + addr.rjust(64, "0") + "::" + rest


def decode(value, decimal) -> float:
    """USD price from a raw oracle value and that entry's own decimal."""
    return int(value) / 10 ** int(decimal)


def _page_all(table: str) -> list:
    nodes, after = [], None
    while True:
        d = gql(Q_FIELDS, {"a": table, "n": 50, "after": after})["address"]["dynamicFields"]
        nodes += d["nodes"]
        if not d["pageInfo"]["hasNextPage"]:
            return nodes
        after = d["pageInfo"]["endCursor"]


def price_table() -> str:
    """Object id of the oracle's price table."""
    o = gql(Q_OBJECT, {"a": PRICE_ORACLE})["object"]["asMoveObject"]["contents"]["json"]
    return o["price_oracles"]["id"]


def entries(nodes: list) -> Dict[int, dict]:
    """Decode price-table nodes: oracle id -> {raw, decimal, price, timestamp_ms}."""
    out = {}
    for n in nodes:
        v = n["value"]["json"]
        out[int(n["name"]["json"])] = {"raw": v["value"], "decimal": int(v["decimal"]),
                                       "price": decode(v["value"], v["decimal"]),
                                       "timestamp_ms": int(v["timestamp"])}
    return out


def reserve_oracles() -> Dict[str, dict]:
    """coin type -> {reserve, oracle_id}, from Navi's Storage reserves."""
    st = gql(Q_OBJECT, {"a": STORAGE})["object"]["asMoveObject"]["contents"]["json"]
    return {norm_type(n["value"]["json"]["coin_type"]):
            {"reserve": int(n["value"]["json"]["id"]),
             "oracle_id": int(n["value"]["json"]["oracle_id"])}
            for n in _page_all(st["reserves"]["id"])}


def quotes() -> Dict[str, dict]:
    """
    coin type -> {symbol, reserve, oracle_id, raw, decimal, price, timestamp_ms,
    age_s} for every Navi reserve with an oracle entry. symbol is navi.py's
    where the coin type is listed there, else None.
    """
    res = reserve_oracles()
    table = entries(_page_all(price_table()))
    sym_of = {norm_type(t): s for s, t in COIN_TYPES.items()}
    now = time.time()
    out = {}
    for ct, r in res.items():
        e = table.get(r["oracle_id"])
        if e is None:
            continue
        out[ct] = {"symbol": sym_of.get(ct), **r, **e,
                   "age_s": max(0.0, now - e["timestamp_ms"] / 1000)}
    return out


def prices(q: Dict[str, dict] = None) -> Dict[str, float]:
    """{symbol: USD price} for the navi.py assets that have an oracle price."""
    q = quotes() if q is None else q
    return {v["symbol"]: v["price"] for v in q.values() if v["symbol"]}
