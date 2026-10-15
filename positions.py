"""
Navi borrower positions, read live from Sui GraphQL.

Every borrower is found by paging Navi's 35 per-reserve borrow tables. For
borrowers with at least `min_debt_usd` of debt, collateral comes from their
`user_info` entry (which assets they supply) and the per-reserve supply tables.
Amounts are stored balance * reserve index / 1e27, at Navi's 9 decimals.
Prices are Navi's own oracle prices.

Health factor is computed off-chain, because the stress check needs it at
shocked prices:

    HF = sum(collateral_i * price_i * LT_i) / sum(debt_j * price_j * w_j)

LT_i is the user's e-mode LT where the user is in an e-mode pair that lists
the asset as collateral, else the reserve's LT; w_j is the borrow weight
(1 unless the BORROW_WEIGHT table says otherwise). `navi_health_factors`
simulates Navi's own view function so the two can be compared.
"""
from __future__ import annotations

import base64
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Dict, List, Optional

import requests

from lrg.clmm import GRAPHQL, gql, Q_FIELDS, Q_OBJECT

STORAGE = "0xbb4e2f4b6205c2e2a2db47aeb4f830796ec7c005f88537ee775986639bc442fe"
PRICE_ORACLE = "0x1568865ed9a0b5ec414220e8f79b3d04c77acc82358f6e5ae4635687392ffbef"
UI_GETTER = "0x74c2e735aa13768048f8b8b46855a93d9e242ea3336c17e72720197e9675ae35"
RAY = 10 ** 27
DECIMALS = 9                       # Navi stores every balance at 9 decimals
STREAMS = 4                        # parallel GraphQL streams
BATCH = 25                         # keys per multiGetDynamicFields (5000 B payload cap)
PAGE_RETRIES = 8                   # network retries per page, backing off to ~4 min
TABLE_RESTARTS = 2                 # full restarts of a table whose cursor expired

Q_CHECKPOINT = "{ checkpoint { sequenceNumber } }"
Q_MULTI = "query($k:[NameKey!]!){multiGetDynamicFields(keys:$k){value{...on MoveValue{json}}}}"
Q_STORAGE_FIELDS = """query($a: SuiAddress!) { address(address: $a) { dynamicFields(first: 50) {
  nodes { name { type { repr } } value { ... on MoveValue { json } } } } } }"""
Q_SIMULATE = """query($tx: JSON!, $prices: SuiAddress!) {
  simulateTransaction(transaction: $tx, checksEnabled: false) {
    effects { status executionError { abortCode message } }
    outputs { returnValues { value { json } } } }
  address(address: $prices) { dynamicFields(first: 50) {
    nodes { name { json } value { ... on MoveValue { json } } } } } }"""


def norm_type(coin_type: str) -> str:
    addr, rest = coin_type.split("::", 1)
    addr = addr.lower()
    addr = addr[2:] if addr.startswith("0x") else addr
    return "0x" + addr.rjust(64, "0") + "::" + rest


def _addr_bcs(a: str) -> bytes:
    return bytes.fromhex(a[2:].rjust(64, "0"))


class PageError(RuntimeError):
    """GraphQL answered but returned no page, e.g. an expired cursor."""


def _page(table: str, after: Optional[str]) -> dict:
    """One page of a table's dynamic fields. Unlike lrg.clmm.gql, which keeps
    only `data`, this raises PageError with the server's message when the
    page comes back empty, and retries network errors with backoff."""
    for i in range(PAGE_RETRIES):
        try:
            j = requests.post(GRAPHQL, timeout=45, json={
                "query": Q_FIELDS, "variables": {"a": table, "n": 50, "after": after}}).json()
        except requests.RequestException:
            if i == PAGE_RETRIES - 1:
                raise
            time.sleep(min(240, 2 ** (i + 1)))
            continue
        page = ((j.get("data") or {}).get("address") or {}).get("dynamicFields")
        if page is None:
            msgs = "; ".join(e.get("message", "") for e in j.get("errors", [])) or "no data"
            raise PageError(f"{table[:12]}… after {after and after[:16]}: {msgs}")
        return page


def page_all(table: str) -> list:
    """Every dynamic field of a table. A cursor is only valid while its
    checkpoint is in the server's retained range (about an hour), so a table
    whose cursor fails is read again from the start, up to TABLE_RESTARTS."""
    for attempt in range(TABLE_RESTARTS + 1):
        nodes, after = [], None
        try:
            while True:
                d = _page(table, after)
                nodes += d["nodes"]
                if not d["pageInfo"]["hasNextPage"]:
                    return nodes
                after = d["pageInfo"]["endCursor"]
        except PageError:
            if attempt == TABLE_RESTARTS:
                raise


def multi_get(keys: List[tuple]) -> list:
    """Values of address-keyed dynamic fields; keys are (parent, address)."""
    batches = [keys[i:i + BATCH] for i in range(0, len(keys), BATCH)]

    def one(batch):
        k = [{"parent": p, "name": {"type": "address",
                                    "bcs": base64.b64encode(_addr_bcs(u)).decode()}}
             for p, u in batch]
        return [x["value"]["json"] if x else None
                for x in gql(Q_MULTI, {"k": k})["multiGetDynamicFields"]]

    with ThreadPoolExecutor(STREAMS) as ex:
        return [v for part in ex.map(one, batches) for v in part]


# ---------------------------------------------------------------------------
# Protocol state
# ---------------------------------------------------------------------------

def _prices(reserves: dict, nodes: list) -> tuple:
    """reserve id -> USD price and its oracle timestamp (ms), from the nodes of
    the oracle's price table."""
    raw = {int(n["name"]["json"]): n["value"]["json"] for n in nodes}
    prices, price_ms = {}, {}
    for rid, r in reserves.items():
        o = raw.get(r["oracle_id"])
        if o:
            prices[rid] = int(o["value"]) / 10 ** int(o["decimal"])
            price_ms[rid] = int(o["timestamp"])
    return prices, price_ms


def refresh_prices(state: dict) -> None:
    """Re-read Navi's oracle prices into state. Prices move every few seconds,
    and the position read takes tens of minutes."""
    state["prices"], state["price_ms"] = _prices(state["reserves"],
                                                 page_all(state["price_table"]))
    state["prices_read_at"] = time.time()


def read_state() -> dict:
    """Reserves, oracle prices, e-mode, borrow weights and protected users."""
    st = gql(Q_OBJECT, {"a": STORAGE})["object"]["asMoveObject"]["contents"]["json"]
    reserves = {}
    for n in page_all(st["reserves"]["id"]):
        v = n["value"]["json"]
        lf = v["liquidation_factors"]
        reserves[int(v["id"])] = {
            "coin_type": norm_type(v["coin_type"]),
            "oracle_id": int(v["oracle_id"]),
            "lt": int(lf["threshold"]) / RAY,
            "lb": int(lf["bonus"]) / RAY,
            "supply_index": int(v["current_supply_index"]),
            "borrow_index": int(v["current_borrow_index"]),
            "supply_table": v["supply_balance"]["user_state"]["id"],
            "borrow_table": v["borrow_balance"]["user_state"]["id"],
            "borrow_entries": int(v["borrow_balance"]["user_state"]["size"]),
            "total_borrow_scaled": int(v["borrow_balance"]["total_supply"]),
        }

    oracle = gql(Q_OBJECT, {"a": PRICE_ORACLE})["object"]["asMoveObject"]["contents"]["json"]
    price_table = oracle["price_oracles"]["id"]
    prices, price_ms = _prices(reserves, page_all(price_table))

    extra = {n["name"]["type"]["repr"].rsplit("::", 1)[-1]: n["value"]["json"]
             for n in gql(Q_STORAGE_FIELDS, {"a": STORAGE})["address"]["dynamicFields"]["nodes"]}
    emode_pairs, user_emode, weights, protected = {}, {}, {}, set()
    if "EMODE_KEY" in extra:
        for n in page_all(extra["EMODE_KEY"]["emodes_pair"]["id"]):
            v = n["value"]["json"]
            if not v["isActive"]:
                continue
            emode_pairs[int(n["name"]["json"])] = {
                int(a["assetId"]): {"collateral": a["isCollateral"], "debt": a["isDebt"],
                                    "lt": int(a["lt"]) / RAY,
                                    "lb": int(a["liquidation_bonus"]) / RAY}
                for a in (v["assetA"], v["assetB"])}
        user_emode = {n["name"]["json"]: int(n["value"]["json"])
                      for n in page_all(extra["EMODE_KEY"]["user_emode_id"]["id"])}
    if "BORROW_WEIGHT_KEY" in extra:
        raw_w = page_all(extra["BORROW_WEIGHT_KEY"]["id"])
        if raw_w:
            # empty when this was written, so the u64's scale is unverified
            raise RuntimeError(f"{len(raw_w)} borrow weights are set; their scale "
                               "is unverified, so HF cannot be computed safely")
    if "PROTECTED_LIQUIDATION_USERS_KEY" in extra:
        protected = {n["name"]["json"]
                     for n in page_all(extra["PROTECTED_LIQUIDATION_USERS_KEY"]["id"])
                     if n["value"]["json"] is True}
    market = extra.get("MARKET_KEY", {})
    return {"reserves": reserves, "prices": prices, "price_ms": price_ms,
            "price_table": price_table, "prices_read_at": time.time(),
            "emode_pairs": emode_pairs, "user_emode": user_emode,
            "borrow_weights": weights, "protected": protected,
            "user_info": st["user_info"]["id"], "market": market}


# ---------------------------------------------------------------------------
# Positions
# ---------------------------------------------------------------------------

def _read_borrow_table(rid: int, r: dict, sink: list, lock):
    t0 = time.time()
    cp0 = int(gql(Q_CHECKPOINT)["checkpoint"]["sequenceNumber"])
    n = 0
    for node in page_all(r["borrow_table"]):
        n += 1
        if node["value"]["json"] != "0":
            with lock:
                sink.append((node["name"]["json"], rid, int(node["value"]["json"])))
    cp1 = int(gql(Q_CHECKPOINT)["checkpoint"]["sequenceNumber"])
    return {"reserve": rid, "entries": n, "seconds": round(time.time() - t0, 1),
            "checkpoint_start": cp0, "checkpoint_end": cp1}


def read_positions(state: dict, min_debt_usd: float = 1.0, verbose: bool = True) -> dict:
    """
    Every borrower's debt; collateral for those with at least min_debt_usd.

    Returns {"positions": [...], "debt_tokens_all": {reserve: tokens},
             "stats": {...}}. Each position is
             {"user", "debt": {rid: tokens}, "collateral": {rid: tokens},
              "emode": id or None, "protected": bool}.
    debt_tokens_all sums every non-zero debt entry, dust included.
    """
    res, px = state["reserves"], state["prices"]
    t0 = time.time()
    entries, lock = [], threading.Lock()
    order = sorted(res, key=lambda rid: -res[rid]["borrow_entries"])
    with ThreadPoolExecutor(STREAMS) as ex:
        tables = list(ex.map(lambda rid: _read_borrow_table(rid, res[rid], entries, lock), order))
    if verbose:
        print(f"borrow tables: {sum(t['entries'] for t in tables):,} entries, "
              f"{len(entries):,} non-zero, {time.time()-t0:.0f}s", flush=True)

    debt: Dict[str, Dict[int, float]] = {}
    debt_tokens_all: Dict[int, float] = {}
    for user, rid, scaled in entries:
        tok = scaled * res[rid]["borrow_index"] / RAY / 10 ** DECIMALS
        debt.setdefault(user, {})[rid] = tok
        debt_tokens_all[rid] = debt_tokens_all.get(rid, 0.0) + tok

    def usd(d):
        return sum(t * px.get(rid, 0.0) for rid, t in d.items())

    kept = [u for u, d in debt.items() if usd(d) >= min_debt_usd]
    dust = [u for u in debt if usd(debt[u]) < min_debt_usd]

    t1 = time.time()
    infos = multi_get([(state["user_info"], u) for u in kept])
    keys, owners = [], []
    for u, info in zip(kept, infos):
        for rid in (list(base64.b64decode(info["collaterals"])) if info else []):
            if rid in res:
                keys.append((res[rid]["supply_table"], u))
                owners.append((u, rid))
    vals = multi_get(keys)
    coll: Dict[str, Dict[int, float]] = {}
    for (u, rid), v in zip(owners, vals):
        if v and v != "0":
            coll.setdefault(u, {})[rid] = (int(v) * res[rid]["supply_index"]
                                           / RAY / 10 ** DECIMALS)
    if verbose:
        print(f"collateral: {len(kept):,} borrowers, {len(keys):,} balances, "
              f"{time.time()-t1:.0f}s", flush=True)

    positions = [{"user": u, "debt": debt[u], "collateral": coll.get(u, {}),
                  "emode": state["user_emode"].get(u),
                  "protected": u in state["protected"]} for u in kept]
    stats = {
        "borrowers": len(debt), "positions": len(kept),
        "dust_borrowers": len(dust), "dust_debt_usd": sum(usd(debt[u]) for u in dust),
        "debt_usd": sum(usd(debt[u]) for u in kept),
        "min_debt_usd": min_debt_usd,
        "requests": {"borrow_pages": sum(-(-t["entries"] // 50) or 1 for t in tables),
                     "lookup_batches": -(-len(kept) // BATCH) - (-len(keys) // BATCH)},
        "seconds": round(time.time() - t0, 1),
        "checkpoint_start": min(t["checkpoint_start"] for t in tables),
        "checkpoint_end": max(t["checkpoint_end"] for t in tables),
        "emode_positions": sum(1 for p in positions if p["emode"] is not None),
        "protected_positions": sum(1 for p in positions if p["protected"]),
    }
    return {"positions": positions, "debt_tokens_all": debt_tokens_all, "stats": stats}


def read_user_positions(state: dict, users: List[str]) -> List[dict]:
    """Complete current positions of the given users: which reserves they
    borrow from and supply to (user_info), then each of those balances.
    Stored indices, not accrued to now: Navi's own HF view does the same."""
    res = state["reserves"]
    infos = multi_get([(state["user_info"], u) for u in users])
    keys, own = [], []
    for u, info in zip(users, infos):
        if not info:
            continue
        for kind, field, table in (("debt", "loans", "borrow_table"),
                                   ("collateral", "collaterals", "supply_table")):
            for rid in base64.b64decode(info[field]):
                if rid in res:
                    keys.append((res[rid][table], u))
                    own.append((u, kind, rid))
    out = {u: {"user": u, "debt": {}, "collateral": {}, "emode": state["user_emode"].get(u),
               "protected": u in state["protected"]} for u in users}
    for (u, kind, rid), v in zip(own, multi_get(keys)):
        if v and v != "0":
            idx = res[rid]["borrow_index" if kind == "debt" else "supply_index"]
            out[u][kind][rid] = int(v) * idx / RAY / 10 ** DECIMALS
    return [out[u] for u in users]


# ---------------------------------------------------------------------------
# Health factor
# ---------------------------------------------------------------------------

def collateral_lt(state: dict, pos: dict, rid: int) -> float:
    pair = state["emode_pairs"].get(pos["emode"]) if pos["emode"] is not None else None
    if pair and rid in pair and pair[rid]["collateral"]:
        return pair[rid]["lt"]
    return state["reserves"][rid]["lt"]


def health_factor(state: dict, pos: dict, factor: Optional[Dict[int, float]] = None) -> float:
    """HF at Navi oracle prices, each multiplied by factor[rid] (default 1)."""
    px, w = state["prices"], state["borrow_weights"]
    f = factor or {}
    c = sum(t * px.get(rid, 0.0) * f.get(rid, 1.0) * collateral_lt(state, pos, rid)
            for rid, t in pos["collateral"].items())
    d = sum(t * px.get(rid, 0.0) * f.get(rid, 1.0) * w.get(rid, 1.0)
            for rid, t in pos["debt"].items())
    return c / d if d else float("inf")


def navi_health_factors(state: dict, users: List[str]) -> tuple:
    """
    Navi's own HF, simulating logic_getter_unchecked::user_health_factor_batch
    (the checked logic:: version aborts on stale oracle prices), together with
    the oracle prices read in the same request, so an off-chain HF can be
    computed at exactly the prices the simulation saw.

    Returns (hfs, prices {reserve id: USD}).
    """
    vec = bytes([len(users)]) + b"".join(_addr_bcs(u) for u in users)
    arg = lambda i: {"kind": "INPUT", "input": i}
    tx = {"sender": "0x" + "0" * 63 + "1",
          "kind": {"programmableTransaction": {
              "inputs": [{"kind": "SHARED", "objectId": "0x6", "mutable": False},
                         {"kind": "SHARED", "objectId": PRICE_ORACLE, "mutable": False},
                         {"kind": "SHARED", "objectId": STORAGE, "mutable": True},
                         {"kind": "PURE", "pure": base64.b64encode(vec).decode()}],
              "commands": [{"moveCall": {"package": UI_GETTER, "module": "logic_getter_unchecked",
                                         "function": "user_health_factor_batch",
                                         "arguments": [arg(0), arg(1), arg(2), arg(3)]}}]}}}
    d = gql(Q_SIMULATE, {"tx": tx, "prices": state["price_table"]})
    r = d["simulateTransaction"]
    if r["effects"]["status"] != "SUCCESS":
        raise RuntimeError(f"health-factor simulation failed: {r['effects']['executionError']}")
    hfs = [int(x) / RAY for x in r["outputs"][0]["returnValues"][0]["value"]["json"]]
    prices, _ = _prices(state["reserves"], d["address"]["dynamicFields"]["nodes"])
    return hfs, prices
