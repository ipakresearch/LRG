"""
Concentrated-liquidity adapter: Cetus, Bluefin, Momentum, Turbos.

Reads a pool and its tick table over Sui GraphQL and turns them into a
liquidity profile that trades can be executed against. The tick index lives in
a different place per venue — see the encoding notes in the README — and
`fetch` locates fields structurally rather than by a per-venue table, so a new
CLMM venue usually needs no code.

    import clmm
    p = clmm.fetch("0x51e883ba...")          # Cetus USDC/SUI 0.05%
    p.report()                                # price, TVL, depth at slippage levels
    p.out(100_000 * 10**6, "a")               # raw-unit swap: 100k USDC -> SUI
    p.size_for_slippage(0.02, "a")            # trade size at 2% price impact

    clmm.save(p, "usdc_sui.json")             # snapshot
    p = clmm.load("usdc_sui.json")            # replay offline

Notes on the on-chain encoding, verified against a live Cetus pool:
  * current_sqrt_price and tick sqrt_price are X64 fixed point (divide by 2^64)
  * fee_rate has denominator 1e6 (500 -> 0.05%)
  * tick_manager.ticks.id is a UID; its dynamic fields are the ticks
  * the dynamic-field key is a u64 score = tick_index + 443636
  * index.bits is a two's-complement u32; liquidity_net.bits a two's-complement u128
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional, Tuple

import requests

GRAPHQL = "https://graphql.mainnet.sui.io/graphql"
Q64 = 2 ** 64
TICK_OFFSET = 443636          # Cetus skip-list score offset
I32 = 1 << 32
I128 = 1 << 128


# ---------------------------------------------------------------------------
# GraphQL
# ---------------------------------------------------------------------------

def gql(query: str, variables: dict = None, url: str = GRAPHQL, retries: int = 4):
    """
    POST a query and return its data. Any GraphQL error fails the call, even
    when partial data came back: a dropped error would otherwise surface as a
    missing field, and a missing tick page reads as "no ticks". Transport
    errors and GraphQL errors are retried with backoff; the final error keeps
    the original message, so a timeout is still recognisable as one.
    """
    err = None
    for i in range(retries):
        try:
            r = requests.post(url, json={"query": query, "variables": variables or {}},
                              timeout=45)
            j = r.json()
            if j.get("errors") or j.get("data") is None:
                raise RuntimeError("GraphQL error: " + str(
                    (j.get("errors") or [{}])[0].get("message", "no data")))
            return j["data"]
        except Exception as e:
            err = e
            if i < retries - 1:
                time.sleep(2 * (i + 1))
    raise RuntimeError(f"GraphQL failed after {retries} attempts: {err}") from err


Q_OBJECT = """query($a: SuiAddress!) {
  object(address: $a) { asMoveObject { contents { type { repr } json } } }
}"""

Q_FIELDS = """query($a: SuiAddress!, $n: Int!, $after: String) {
  address(address: $a) {
    dynamicFields(first: $n, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name { json } value { ... on MoveValue { json } } }
    }
  }
}"""

Q_FIELDS_TYPED = """query($a: SuiAddress!, $n: Int!, $after: String) {
  address(address: $a) {
    dynamicFields(first: $n, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { name { type { repr } json } value { ... on MoveValue { json } } }
    }
  }
}"""

Q_DECIMALS = """query($t: String!) { coinMetadata(coinType: $t) { decimals symbol } }"""

Q_MULTI_FIELDS = """query($a: SuiAddress!, $keys: [DynamicFieldName!]!) {
  address(address: $a) {
    multiGetDynamicFields(keys: $keys) { name { json } value { ... on MoveValue { json } } }
  }
}"""


class EmptyPool(RuntimeError):
    """The pool exists and was read, but holds no liquidity: liquidity 0 and
    no initialized ticks. Zero depth, not a failed read."""


gql_strict = gql      # kept for callers written before gql became strict


def as_i32(v) -> int:
    b = int(v["bits"] if isinstance(v, dict) else v)
    return b - I32 if b >= I32 // 2 else b


def as_i128(v) -> int:
    b = int(v["bits"] if isinstance(v, dict) else v)
    return b - I128 if b >= I128 // 2 else b


def split_generics(type_repr: str) -> Tuple[str, str]:
    inner = type_repr[type_repr.find("<") + 1: type_repr.rfind(">")]
    depth, cur, out = 0, "", []
    for ch in inner:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(cur.strip()); cur = ""
        else:
            cur += ch
    out.append(cur.strip())
    return out[0], out[1]


# ---------------------------------------------------------------------------
# Pool
# ---------------------------------------------------------------------------

@dataclass
class ClmmPool:
    pool_id: str
    venue: str
    coin_a: str
    coin_b: str
    sym_a: str
    sym_b: str
    dec_a: int
    dec_b: int
    fee: float                                     # fraction, e.g. 0.0005
    sqrtP: float                                   # normalised (not X64)
    liquidity: float
    tick_spacing: int
    bal_a: int                                     # raw units held by the pool
    bal_b: int
    ticks: List[Tuple[int, float]] = field(default_factory=list)   # (index, liquidity_net)
    ranges: List[Tuple[float, float, float]] = field(default_factory=list)
    fetched_at: float = 0.0

    # ---- prices ----------------------------------------------------------

    def price(self) -> float:
        """Human price: units of B per unit of A."""
        return self.sqrtP ** 2 * 10 ** (self.dec_a - self.dec_b)

    def spot_raw(self, src: str) -> float:
        p = self.sqrtP ** 2
        return p if src == "a" else 1.0 / p

    def tvl(self, price_a_usd: float, price_b_usd: float) -> float:
        return (self.bal_a / 10 ** self.dec_a * price_a_usd +
                self.bal_b / 10 ** self.dec_b * price_b_usd)

    def virtual_depth(self, src: str) -> float:
        """Active-range virtual reserve of the input token, in raw units."""
        return self.liquidity / self.sqrtP if src == "a" else self.liquidity * self.sqrtP

    # ---- execution -------------------------------------------------------

    def build_ranges(self):
        """
        Walk outward from the active tick, accumulating liquidity_net.
        Up:   crossing tick t  ->  L += net(t)
        Down: crossing tick t  ->  L -= net(t)
        """
        ts = sorted(self.ticks)
        sp = self.sqrtP
        above = [(1.0001 ** (i / 2.0), n) for i, n in ts if 1.0001 ** (i / 2.0) > sp]
        below = [(1.0001 ** (i / 2.0), n) for i, n in ts if 1.0001 ** (i / 2.0) <= sp]

        out: List[Tuple[float, float, float]] = []
        L, lo = self.liquidity, sp
        for p_t, net in above:
            if p_t > lo and L > 0:
                out.append((lo, p_t, L))
            L += net
            lo = p_t

        L, hi = self.liquidity, sp
        for p_t, net in reversed(below):
            if hi > p_t and L > 0:
                out.append((p_t, hi, L))
            L -= net
            hi = p_t

        self.ranges = sorted(r for r in out if r[2] > 0 and r[1] > r[0])

    def out(self, q: float, src: str = "a") -> float:
        """Raw-unit input -> raw-unit output, never more than the pool holds of
        the output coin (when that balance is known). src is 'a' or 'b'."""
        o = self._out_ranges(q, src)
        held = self.bal_b if src == "a" else self.bal_a
        return min(o, held) if held and held > 0 else o

    def _out_ranges(self, q: float, src: str = "a") -> float:
        if q <= 0:
            return 0.0
        qn, sp, total = q * (1 - self.fee), self.sqrtP, 0.0
        if src == "a":                                   # sell A, price falls
            for lo, hi, L in sorted(self.ranges, reverse=True):
                if lo >= sp or L <= 0:
                    continue
                top = min(hi, sp)
                cap = L * (1 / lo - 1 / top)             # A absorbable in range
                if qn <= cap:
                    new_sp = L * top / (L + qn * top)
                    return total + L * (top - new_sp)
                total += L * (top - lo)
                qn -= cap
                sp = lo
            return total
        else:                                            # sell B, price rises
            for lo, hi, L in sorted(self.ranges):
                if hi <= sp or L <= 0:
                    continue
                bot = max(lo, sp)
                cap = L * (hi - bot)                     # B absorbable in range
                if qn <= cap:
                    new_sp = bot + qn / L
                    return total + L * (1 / bot - 1 / new_sp)
                total += L * (1 / bot - 1 / hi)
                qn -= cap
                sp = hi
            return total

    def slippage(self, q: float, src: str = "a") -> float:
        """Price impact net of fee, against the pool's own spot."""
        if q <= 0:
            return 0.0
        o = self.out(q, src)
        if o <= 0:
            return 1.0
        return 1 - o / (q * self.spot_raw(src) * (1 - self.fee))

    def cost(self, q: float, src: str = "a") -> float:
        """Total shortfall against spot, fee included."""
        if q <= 0:
            return 0.0
        return 1 - self.out(q, src) / (q * self.spot_raw(src))

    def size_for_slippage(self, target: float, src: str = "a") -> Optional[float]:
        """Raw-unit trade size whose slippage reaches `target`."""
        lo, hi = 0.0, max(self.virtual_depth(src), 1.0)
        for _ in range(200):
            if self.slippage(hi, src) >= target:
                break
            hi *= 2
            if hi > 1e40:
                return None
        else:
            return None
        for _ in range(200):
            mid = 0.5 * (lo + hi)
            if self.slippage(mid, src) >= target:
                hi = mid
            else:
                lo = mid
            if hi - lo <= 1e-9 * max(hi, 1.0):
                break
        return hi

    # ---- reporting -------------------------------------------------------

    def report(self, price_a_usd: float = None, price_b_usd: float = None,
               targets=(0.02, 0.03, 0.05, 0.07, 0.10)):
        px = self.price()
        print(f"pool        {self.venue}  {self.pool_id[:12]}…")
        print(f"pair        {self.sym_a} / {self.sym_b}   fee {self.fee*100:.4f}%"
              f"   tick spacing {self.tick_spacing}")
        print(f"price       1 {self.sym_a} = {px:,.6f} {self.sym_b}")
        print(f"balances    {self.bal_a/10**self.dec_a:,.2f} {self.sym_a}   "
              f"{self.bal_b/10**self.dec_b:,.2f} {self.sym_b}")
        if price_a_usd and price_b_usd:
            print(f"TVL         ${self.tvl(price_a_usd, price_b_usd):,.0f}")
        print(f"ticks       {len(self.ticks)}   ranges {len(self.ranges)}")
        print(f"active L    {self.liquidity:,.0f}   "
              f"virtual {self.virtual_depth('a')/10**self.dec_a:,.0f} {self.sym_a} / "
              f"{self.virtual_depth('b')/10**self.dec_b:,.0f} {self.sym_b}")

        for src, sym_in, sym_out, dec_in, px_usd in (
                ("a", self.sym_a, self.sym_b, self.dec_a, price_a_usd),
                ("b", self.sym_b, self.sym_a, self.dec_b, price_b_usd)):
            print(f"\n  sell {sym_in} → {sym_out}")
            head = f"  {'slippage':>9}{'size (' + sym_in + ')':>20}{'cost inc fee':>14}"
            print(head + (f"{'size (USD)':>14}" if px_usd else ""))
            for t in targets:
                q = self.size_for_slippage(t, src)
                if q is None:
                    print(f"  {t*100:>8.0f}%{'not reachable':>20}"); continue
                human = q / 10 ** dec_in
                line = (f"  {t*100:>8.0f}%{human:>20,.2f}"
                        f"{self.cost(q, src)*100:>13.2f}%")
                if px_usd:
                    line += f"{human*px_usd:>14,.0f}"
                print(line)


# ---------------------------------------------------------------------------
# Fetch
# ---------------------------------------------------------------------------

# Package id -> venue name. Extend as venues are added; detection does not
# depend on this, it only labels the result.
PACKAGES = {
    "0x1eabed72c53feb3805120a081dc15963c204dc8d091542592abaf7a35689b2fb": "cetus",
    "0x3492c874c1e3b3e2984e8c41b589e642d4d0a5d6459e5a9cfc2d52fd7c89c267": "bluefin",
    "0x91bfbc386a41afcfd9b2533058d7e915a1d3829089cc268ff4333d54d6339ca1": "turbos-finance",
    "0x70285592c97965e811e0c6f98dccc3a9c2b4ad854b3594faab9597ada267b860": "momentum",
}


def _find_key(d: dict, *candidates, contains: str = None):
    """First matching key in a struct, by exact name then by substring."""
    for c in candidates:
        if c in d:
            return c
    if contains:
        for k in d:
            if contains in k:
                return k
    return None


def _find_ticks_table(j: dict) -> Tuple[Optional[str], Optional[int]]:
    """
    Locate the tick table: (UID, size if the table records one). Cetus:
    tick_manager.ticks (skip list). Bluefin: ticks_manager.ticks (Table).
    Momentum: ticks (Table). Generic: any nested dict whose key contains
    'tick' and which holds a 'ticks' dict with an 'id'.
    """
    def size(t):
        try:
            return int(t["size"]) if "size" in t else None
        except (TypeError, ValueError):
            return None

    for k, v in j.items():
        if "tick" not in k.lower() or not isinstance(v, dict):
            continue
        t = v.get("ticks")
        if isinstance(t, dict) and "id" in t:
            return t["id"], size(t)
    for k, v in j.items():                     # second pass: any tick-ish UID
        if "tick" not in k.lower() or not isinstance(v, dict):
            continue
        if "id" in v and not any(x in k.lower() for x in ("bitmap", "map")):
            return v["id"], size(v)
    return None, None


def _find_ticks_uid(j: dict) -> Optional[str]:
    return _find_ticks_table(j)[0]


MAX_TICK = 443636


def _index_from_name(name) -> Optional[int]:
    """
    The tick index sometimes lives in the dynamic-field key rather than the
    value. Cetus keys by a u64 score (tick + 443636); Momentum keys by an
    I32 struct; others by the raw integer.
    """
    if name is None:
        return None
    if isinstance(name, dict):
        try:
            return as_i32(name)
        except (KeyError, TypeError, ValueError):
            return None
    try:
        v = int(name)
    except (TypeError, ValueError):
        return None
    return v - TICK_OFFSET if abs(v) > MAX_TICK else v


def _tick_from_json(v: dict, name=None) -> Optional[Tuple[int, float]]:
    """
    Handles the three layouts seen so far:
      Cetus     skip_list Node -> value.{index, liquidity_net}
      Bluefin   Table entry    -> {index, liquidity_net}
      Momentum  Table entry    -> {liquidity_net, …}, index in the field key
    """
    if not isinstance(v, dict):
        return None
    if "index" not in v and isinstance(v.get("value"), dict):
        v = v["value"]
    net_key = _find_key(v, "liquidity_net", contains="liquidity_net")
    if not net_key:
        return None
    idx = None
    if "index" in v:
        try:
            idx = as_i32(v["index"])
        except (KeyError, TypeError, ValueError):
            idx = None
    if idx is None:
        idx = _index_from_name(name)
    if idx is None:
        return None
    try:
        return idx, float(as_i128(v[net_key]))
    except (KeyError, TypeError, ValueError):
        return None


def _decimals(coin_type: str) -> Tuple[int, str]:
    d = gql(Q_DECIMALS, {"t": coin_type})
    md = d.get("coinMetadata") or {}
    return int(md.get("decimals", 9)), md.get("symbol") or coin_type.split("::")[-1]


TICK_BATCH = 25          # names per multiGetDynamicFields call (5000-byte payload cap)


def fetch_ticks_bitmap(pool_id: str, tick_map_id: str, spacing: int,
                       verbose: bool = True) -> List[Tuple[int, float]]:
    """
    Ticks of a pool that indexes them with a bitmap (Turbos): tick_map is a
    Table<I32, u256> of words, and bit b of word w marks tick
    (w * 256 + b) * spacing as initialized. Each tick is a dynamic field of
    the pool keyed by that I32, so it is fetched by key: paging the pool's
    fields instead would walk every LP position (20,000+ on deep pools)
    before reaching a tick. Raises if any marked tick is missing.
    """
    import base64
    import struct

    words, after, key_type = [], None, None
    while True:
        d = gql_strict(Q_FIELDS_TYPED, {"a": tick_map_id, "n": 50, "after": after})
        df = d["address"]["dynamicFields"]
        for n in df["nodes"]:
            key_type = key_type or n["name"]["type"]["repr"]
            words.append((as_i32(n["name"]["json"]), int(n["value"]["json"])))
        if not df["pageInfo"]["hasNextPage"]:
            break
        after = df["pageInfo"]["endCursor"]
    idx = sorted((w * 256 + b) * spacing for w, bm in words
                 for b in range(256) if bm >> b & 1)
    if not idx:
        return []

    ticks = []
    for i in range(0, len(idx), TICK_BATCH):
        chunk = idx[i:i + TICK_BATCH]
        keys = [{"type": key_type,
                 "bcs": base64.b64encode(struct.pack("<I", t & 0xFFFFFFFF)).decode()}
                for t in chunk]
        got = gql_strict(Q_MULTI_FIELDS, {"a": pool_id, "keys": keys})
        nodes = got["address"]["multiGetDynamicFields"]
        for t, node in zip(chunk, nodes):
            parsed = _tick_from_json(((node or {}).get("value") or {}).get("json"), t)
            if parsed is None:
                raise RuntimeError(f"tick {t} is marked in tick_map but did not read")
            ticks.append(parsed)
        if verbose and (i // TICK_BATCH) % 10 == 9:
            print(f"    …{len(ticks)}/{len(idx)} ticks", flush=True)
    return sorted(ticks)


MAX_TICK_PAGES = 200     # 10,000 fields; reaching it with more pages left is truncation
max_ticks_seen = 0       # largest tick count read in this process, for run logs


class TruncatedRead(RuntimeError):
    """A tick table was only partly read. Treated as a failed read."""


def fetch_ticks(uid: str, page: int = 50, verbose: bool = True,
                max_pages: int = MAX_TICK_PAGES,
                expected: Optional[int] = None) -> List[Tuple[int, float]]:
    """
    Page the tick table's dynamic fields. Works for skip list and Table alike.
    Bails out after two empty pages: if the UID does not hold ticks, paging on
    would walk thousands of unrelated objects (positions, bins) for nothing.
    With `expected` (the table's own size), the count of fields read must
    match it; reaching max_pages with pages left also raises TruncatedRead.
    """
    global max_ticks_seen
    ticks, cursor, n, seen = [], None, 0, 0
    while True:
        d = gql(Q_FIELDS, {"a": uid, "n": page, "after": cursor})
        if not (d.get("address") or {}).get("dynamicFields"):
            raise RuntimeError(f"no dynamic-field page returned for {uid[:12]}…")
        df = d["address"]["dynamicFields"]
        nodes = df.get("nodes", [])
        seen += len(nodes)
        for node in nodes:
            t = _tick_from_json(((node.get("value") or {}).get("json")),
                                ((node.get("name") or {}).get("json")))
            if t:
                ticks.append(t)
        n += 1
        if n >= 2 and not ticks:
            raise RuntimeError(
                f"no ticks in the first {seen} dynamic fields under {uid[:12]}… "
                "— not this pool's tick table")
        info = df.get("pageInfo") or {}
        if seen and not ticks and not info.get("hasNextPage"):
            raise RuntimeError(
                f"{seen} dynamic fields under {uid[:12]}… but none parsed as a tick "
                "— reader bug for this layout")
        if not info.get("hasNextPage"):
            break
        if n >= max_pages:
            raise TruncatedRead(f"truncated: tick table {uid[:12]}… has more than "
                                f"{max_pages} pages ({seen} fields read)")
        cursor = info.get("endCursor")
        if verbose and n % 5 == 0:
            print(f"    …{len(ticks)} ticks", flush=True)
    if expected is not None and seen != expected:
        raise TruncatedRead(f"truncated: read {seen} of {expected} fields "
                            f"in tick table {uid[:12]}…")
    max_ticks_seen = max(max_ticks_seen, len(ticks))
    return sorted(ticks)


def fetch(pool_id: str, venue: str = None, verbose: bool = True,
          attempts: int = 3) -> ClmmPool:
    """
    Read any Cetus- or Bluefin-style CLMM pool. Fields are located structurally,
    so a venue argument is only used as a label.

    Each GraphQL page is served at the latest checkpoint, so a busy pool's
    tick table can change between reading the pool and finishing its pages;
    the size check then raises TruncatedRead. The whole pool is re-read up to
    `attempts` times before the error stands.
    """
    for i in range(attempts):
        try:
            return _fetch_once(pool_id, venue, verbose)
        except TruncatedRead:
            if i == attempts - 1:
                raise
            if verbose:
                print(f"  tick table changed during the read; re-reading {pool_id[:12]}…",
                      flush=True)


def _fetch_once(pool_id: str, venue: str = None, verbose: bool = True) -> ClmmPool:
    d = gql(Q_OBJECT, {"a": pool_id})
    mo = (d.get("object") or {}).get("asMoveObject")
    if not mo:
        raise RuntimeError(f"no Move object at {pool_id}")
    j = mo["contents"]["json"]
    type_repr = mo["contents"]["type"]["repr"]
    ca, cb = split_generics(type_repr)

    pkg = "0x" + type_repr.split("::")[0].lstrip("0x")
    venue = venue or PACKAGES.get(pkg) or (j.get("name") or pkg[:10])

    k_sqrt = _find_key(j, "current_sqrt_price", "sqrt_price", contains="sqrt_price")
    k_liq = _find_key(j, "liquidity", "current_liquidity", contains="liquidity")
    k_fee = _find_key(j, "fee_rate", "swap_fee_rate", "fee", contains="fee_rate")
    if not (k_sqrt and k_liq and k_fee):
        raise RuntimeError(f"unrecognised pool struct; keys: {list(j)}")

    sqrtP = int(j[k_sqrt]) / Q64
    liq = float(j[k_liq])
    fee = float(j[k_fee]) / 1e6                      # both venues use 1e6

    spacing = j.get("tick_spacing")
    if spacing is None:                              # Bluefin nests it
        for v in j.values():
            if isinstance(v, dict) and "tick_spacing" in v:
                spacing = v["tick_spacing"]; break

    def _bal(*names):
        for n in names:
            v = j.get(n)
            if v is not None and not isinstance(v, (dict, list)):
                try:
                    return int(v)
                except (TypeError, ValueError):
                    pass
        return 0

    bal_a = _bal("coin_a", "reserve_x", "reserve_a", "balance_x", "balance_a")
    bal_b = _bal("coin_b", "reserve_y", "reserve_b", "balance_y", "balance_b")

    dec_a, sym_a = _decimals(ca)
    dec_b, sym_b = _decimals(cb)

    if verbose:
        print(f"  {venue}  {sym_a}/{sym_b}  fee {fee*100:.4f}%  fetching ticks…", flush=True)
    tick_map = j.get("tick_map")
    if isinstance(tick_map, dict) and "id" in tick_map:
        # Turbos: a bitmap indexes the ticks, which sit on the pool's own UID
        # among its LP positions
        ticks = fetch_ticks_bitmap(pool_id, tick_map["id"], int(spacing or 0),
                                   verbose=verbose)
    else:
        uid, size = _find_ticks_table(j)
        if uid is None:
            raise RuntimeError(f"no tick table found (struct keys: {list(j)})")
        ticks = fetch_ticks(uid, verbose=verbose, expected=size)

    p = ClmmPool(pool_id=pool_id, venue=venue, coin_a=ca, coin_b=cb,
                 sym_a=sym_a, sym_b=sym_b, dec_a=dec_a, dec_b=dec_b,
                 fee=fee, sqrtP=sqrtP, liquidity=liq, tick_spacing=int(spacing or 0),
                 bal_a=bal_a, bal_b=bal_b, ticks=ticks, fetched_at=time.time())
    if not ticks:
        # the tick table was found and is empty (a populated table that parses
        # to nothing raises inside the readers)
        if liq == 0:
            raise EmptyPool("empty pool: liquidity 0 and no initialized ticks")
        raise RuntimeError(f"tick table is empty but liquidity is {liq:.0f}")
    p.build_ranges()
    if not p.ranges:
        raise RuntimeError("ticks parsed but no ranges built — check sign convention")
    if verbose:
        print(f"  done: {len(ticks)} ticks, {len(p.ranges)} ranges", flush=True)
    return p


def fetch_many(pool_ids: List[str], verbose: bool = True) -> Dict[str, ClmmPool]:
    """Fetch several pools (e.g. every pool on one leg of a route)."""
    out = {}
    for pid in pool_ids:
        try:
            out[pid] = fetch(pid, verbose=verbose)
        except Exception as e:
            print(f"  SKIP {pid[:12]}…  {e}", flush=True)
    return out


# ---------------------------------------------------------------------------
# Checks and persistence
# ---------------------------------------------------------------------------

def sanity(p: ClmmPool, oracle_price: Optional[float] = None, tol: float = 0.05):
    """Catch decimal / X64 / direction errors before trusting any number."""
    ok = True
    px = p.price()
    print(f"price {px:,.6f} {p.sym_b} per {p.sym_a}")
    if oracle_price:
        dev = abs(px - oracle_price) / oracle_price
        print(f"  vs oracle {oracle_price:,.6f}  deviation {dev*100:.2f}%"
              f"  {'OK' if dev <= tol else 'CHECK DECIMALS / X64'}")
        ok &= dev <= tol

    tiny = max(p.virtual_depth("a") * 1e-6, 1.0)
    c = p.cost(tiny, "a")
    print(f"  tiny-trade cost {c*100:.4f}%  vs fee {p.fee*100:.4f}%  "
          f"{'OK' if abs(c - p.fee) < 5e-4 else 'CHECK'}")
    ok &= abs(c - p.fee) < 5e-4

    mono = all(p.slippage(q, "a") <= p.slippage(q * 2, "a")
               for q in [tiny * 10 ** k for k in range(1, 6)])
    print(f"  slippage monotone in size  {'OK' if mono else 'CHECK'}")
    ok &= mono

    below = sum(1 for lo, hi, _ in p.ranges if hi <= p.sqrtP)
    above = sum(1 for lo, hi, _ in p.ranges if lo >= p.sqrtP)
    print(f"  ranges: {below} below spot, {above} above")
    ok &= below > 0 and above > 0
    return ok


def save(p: ClmmPool, path: str):
    json.dump(asdict(p), open(path, "w"))
    print(f"saved {path}")


def load(path: str) -> ClmmPool:
    d = json.load(open(path))
    d["ticks"] = [tuple(t) for t in d["ticks"]]
    d["ranges"] = [tuple(r) for r in d["ranges"]]
    return ClmmPool(**d)


def inspect(pool_id: str, chars: int = 3000):
    """Dump a pool's type and struct so a new venue's layout can be read off."""
    d = gql(Q_OBJECT, {"a": pool_id})
    mo = (d.get("object") or {}).get("asMoveObject")
    if not mo:
        print(f"no Move object at {pool_id}"); return None
    c = mo["contents"]
    print("TYPE:", c["type"]["repr"][:300])
    print("\nKEYS:", list(c["json"]))
    print("\nJSON:")
    print(json.dumps(c["json"], indent=1)[:chars])
    return c["json"]


def inspect_fields(uid: str, n: int = 2):
    """Dump a few dynamic fields under a UID (ticks, orders, bins)."""
    d = gql(Q_FIELDS, {"a": uid, "n": n, "after": None})
    print(json.dumps(d, indent=1)[:3000])
    return d
