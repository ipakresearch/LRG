"""
Aftermath (weighted AMM) and DeepBook (order book) adapters.

Both expose the same interface as clmm.ClmmPool, so route_cost and viz_data can
use them without changes:  out(q_raw, side), price(), tvl(pa, pb), liquidity,
sym_a/sym_b, dec_a/dec_b, coin_a/coin_b, fee, venue, pool_id.

    import venues
    venues.probe_deepbook_orderbook("SUI_USDC")      # run this first

    # prices: USD by normalized coin type (e.g. Navi's oracle)
    rows = venues.discovery_rows(navi.COIN_TYPES, prices)   # CSV rows for both venues
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .coins import norm_type

import requests

UA = {"User-Agent": "ipak-cdl/0.1", "Accept": "application/json"}
AF_POOLS = "https://aftermath.finance/api/pools"
DB_BASE = "https://deepbook-indexer.mainnet.mystenlabs.com"
WAD = 1e18


def _n(v) -> float:
    """Aftermath returns BigInt strings with a trailing 'n'."""
    if v is None:
        return 0.0
    s = str(v)
    return float(s[:-1]) if s.endswith("n") else float(s)


# ---------------------------------------------------------------------------
# Aftermath: weighted AMM (Balancer-style), `flatness` blends toward constant sum
# ---------------------------------------------------------------------------

@dataclass
class AftermathPool:
    pool_id: str
    venue: str
    coin_a: str
    coin_b: str
    sym_a: str
    sym_b: str
    dec_a: int
    dec_b: int
    bal_a: float          # raw units
    bal_b: float
    w_a: float            # normalised weights, sum to 1 over the two legs
    w_b: float
    fee: float
    flatness: float = 0.0
    name: str = ""

    @property
    def liquidity(self) -> float:
        return self.bal_a * self.bal_b

    def price(self) -> float:
        """B per A, from the weighted spot rate."""
        if self.bal_a <= 0 or self.w_a <= 0:
            return 0.0
        spot = (self.bal_b / self.w_b) / (self.bal_a / self.w_a)
        return spot * 10 ** (self.dec_a - self.dec_b)

    def _sides(self, side: str):
        return ((self.bal_a, self.w_a, self.bal_b, self.w_b) if side == "a"
                else (self.bal_b, self.w_b, self.bal_a, self.w_a))

    def out(self, q: float, side: str = "a") -> float:
        """Raw in -> raw out. Balancer weighted; flatness>0 is treated as 0
        (conservative: a flatter curve would give more out, not less)."""
        b_in, w_in, b_out, w_out = self._sides(side)
        if q <= 0 or b_in <= 0 or b_out <= 0 or w_in <= 0 or w_out <= 0:
            return 0.0
        qn = q * (1 - self.fee)
        ratio = b_in / (b_in + qn)
        return b_out * (1 - ratio ** (w_in / w_out))

    def tvl(self, price_a_usd: float, price_b_usd: float) -> float:
        return (self.bal_a / 10 ** self.dec_a * (price_a_usd or 0) +
                self.bal_b / 10 ** self.dec_b * (price_b_usd or 0))

    def size_for_slippage(self, target: float, side: str = "a") -> Optional[float]:
        b_in = self._sides(side)[0]
        lo, hi = 0.0, b_in * 100
        def slip(x):
            o = self.out(x, side)
            sp = self.out(b_in * 1e-9, side) / (b_in * 1e-9)
            return 1.0 if o <= 0 else 1 - o / (x * sp)
        if slip(hi) < target:
            return None
        for _ in range(80):
            mid = 0.5 * (lo + hi)
            if slip(mid) >= target:
                hi = mid
            else:
                lo = mid
        return hi


def aftermath_pools(tokens: Dict[str, str], min_usd: float = 25_000.0,
                    prices: Dict[str, float] = None, verbose: bool = True
                    ) -> List[AftermathPool]:
    """
    Pull Aftermath pools, keep the coin pairs where both coins are in
    `tokens` (symbol -> coin type) and, when prices (USD by coin type) are
    given, TVL clears min_usd. Multi-coin pools are split into all two-coin
    combinations, which share the pool's object id.
    """
    by_type = {norm_type(v): k for k, v in tokens.items()}
    prices = prices or {}

    data = requests.get(AF_POOLS, headers=UA, timeout=60).json()
    if verbose:
        print(f"aftermath: {len(data)} pools returned")

    out = []
    for p in data:
        coins = p.get("coins") or {}
        legs = []
        for ctype, c in coins.items():
            t = norm_type(ctype if ctype.startswith("0x") else "0x" + ctype)
            if t not in by_type:
                continue
            dec = c.get("decimals")
            if dec is None:
                continue
            legs.append((t, by_type[t], int(dec), _n(c["balance"]),
                         _n(c["weight"]) / WAD, _n(c["tradeFeeIn"]) / WAD))
        if len(legs) < 2:
            continue
        flat = _n(p.get("flatness", 0)) / WAD
        for i in range(len(legs)):
            for j in range(i + 1, len(legs)):
                (ta, sa, da, ba, wa, fa), (tb, sb, db, bb, wb, fb) = legs[i], legs[j]
                if ba <= 0 or bb <= 0:
                    continue
                tot = wa + wb
                pool = AftermathPool(
                    pool_id=p["objectId"], venue="aftermath",
                    coin_a=ta, coin_b=tb, sym_a=sa, sym_b=sb,
                    dec_a=da, dec_b=db, bal_a=ba, bal_b=bb,
                    w_a=wa / tot, w_b=wb / tot, fee=max(fa, fb),
                    flatness=flat, name=p.get("name", ""))
                tvl = pool.tvl(prices.get(ta, 0), prices.get(tb, 0))
                if prices and tvl < min_usd:
                    continue
                out.append(pool)
    if verbose:
        print(f"aftermath: {len(out)} legs on Navi assets"
              f"{' above $' + format(min_usd, ',.0f') if prices else ''}")
    return out


# ---------------------------------------------------------------------------
# DeepBook: order book
# ---------------------------------------------------------------------------

def probe_deepbook_orderbook(pool_name: str = "SUI_USDC"):
    """Find the indexer's order-book endpoint and show its shape."""
    paths = [
        f"/orderbook/{pool_name}?level=2&depth=100",
        f"/orderbook/{pool_name}",
        f"/get_orderbook/{pool_name}",
        f"/order_book/{pool_name}",
        f"/level2/{pool_name}",
        f"/depth/{pool_name}",
        f"/book/{pool_name}",
    ]
    hits = []
    for p in paths:
        url = DB_BASE + p
        try:
            r = requests.get(url, headers=UA, timeout=25)
            ok = r.status_code == 200
            print(f"{'OK ' if ok else '   '}{r.status_code}  {url}")
            if ok:
                hits.append((url, r.json()))
        except Exception as e:
            print(f"    FAIL {url}  {e}")
    for url, d in hits[:1]:
        print(f"\n===== {url}")
        print(json.dumps(d, indent=1)[:1500])
    return hits


@dataclass
class DeepBookPool:
    pool_id: str
    venue: str
    coin_a: str            # base
    coin_b: str            # quote
    sym_a: str
    sym_b: str
    dec_a: int
    dec_b: int
    fee: float             # taker fee
    bids: List[Tuple[float, float]] = field(default_factory=list)   # (price, base qty)
    asks: List[Tuple[float, float]] = field(default_factory=list)
    name: str = ""
    levels: int = 0        # book levels fetched per side; depth beyond them is unseen

    @property
    def liquidity(self) -> float:
        return sum(p * q for p, q in self.bids) + sum(p * q for p, q in self.asks)

    def price(self) -> float:
        """Mid, in quote per base (human units — the indexer already scales)."""
        b = self.bids[0][0] if self.bids else 0.0
        a = self.asks[0][0] if self.asks else 0.0
        if b and a:
            return (a + b) / 2
        return b or a

    def out(self, q: float, side: str = "a") -> float:
        """Raw in -> raw out, walking the ladder."""
        if q <= 0:
            return 0.0
        if side == "a":                                   # sell base, hit bids
            rem = q / 10 ** self.dec_a * (1 - self.fee)
            got = 0.0
            for px, qty in self.bids:
                take = min(rem, qty)
                got += take * px
                rem -= take
                if rem <= 0:
                    break
            return got * 10 ** self.dec_b
        else:                                             # sell quote, lift asks
            rem = q / 10 ** self.dec_b * (1 - self.fee)
            got = 0.0
            for px, qty in self.asks:
                cost = px * qty
                take = min(rem, cost)
                got += take / px
                rem -= take
                if rem <= 0:
                    break
            return got * 10 ** self.dec_a

    def tvl(self, price_a_usd: float, price_b_usd: float) -> float:
        base = sum(q for _, q in self.asks) * (price_a_usd or 0)
        quote = sum(p * q for p, q in self.bids) * (price_b_usd or 0)
        return base + quote

    def size_for_slippage(self, target: float, side: str = "a") -> Optional[float]:
        top = (self.bids[0][0] if side == "a" else 1 / self.asks[0][0]) if (
            self.bids and self.asks) else None
        if not top:
            return None
        d_in = self.dec_a if side == "a" else self.dec_b
        d_out = self.dec_b if side == "a" else self.dec_a
        if side == "a":
            depth = sum(q for _, q in self.bids) * 10 ** d_in
        else:
            depth = sum(p * q for p, q in self.asks) * 10 ** d_in
        if depth <= 0:
            return None
        lo, hi = 0.0, depth * 0.999
        def slip(x):
            o = self.out(x, side)
            if o <= 0:
                return 1.0
            realised = (o / 10 ** d_out) / (x / 10 ** d_in)
            return 1 - realised / top
        if slip(hi) < target:
            return hi          # book exhausts before reaching the target
        for _ in range(70):
            mid = 0.5 * (lo + hi)
            if slip(mid) >= target:
                hi = mid
            else:
                lo = mid
        return hi


def deepbook_pool_list() -> List[dict]:
    return requests.get(DB_BASE + "/get_pools", headers=UA, timeout=30).json()


def _parse_book(d) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]]]:
    """Accept the common shapes: {bids:[[p,q]…]}, {bid_prices,bid_quantities}, …"""
    def pairs(x):
        if not x:
            return []
        if isinstance(x, dict):
            ps = x.get("prices") or x.get("price") or []
            qs = x.get("quantities") or x.get("quantity") or []
            return [(float(p), float(q)) for p, q in zip(ps, qs)]
        return [(float(e[0]), float(e[1])) if isinstance(e, (list, tuple))
                else (float(e["price"]), float(e.get("quantity") or e.get("size")))
                for e in x]
    if not isinstance(d, dict):
        return [], []
    if "bid_prices" in d:
        bids = list(zip(map(float, d["bid_prices"]), map(float, d["bid_quantities"])))
        asks = list(zip(map(float, d["ask_prices"]), map(float, d["ask_quantities"])))
    else:
        bids = pairs(d.get("bids") or d.get("bid"))
        asks = pairs(d.get("asks") or d.get("ask"))
    # out() walks from the best level: highest bid first, lowest ask first
    bids.sort(key=lambda e: -e[0])
    asks.sort(key=lambda e: e[0])
    return bids, asks


def deepbook_pools(tokens: Dict[str, str], taker_fee: float = 0.001,
                   book_path: str = "/orderbook/{name}?level=2&depth={depth}",
                   verbose: bool = True) -> List[DeepBookPool]:
    by_type = {norm_type(v): k for k, v in tokens.items()}
    out = []
    for p in deepbook_pool_list():
        ta = p["base_asset_id"]; tb = p["quote_asset_id"]
        ta = norm_type(ta if ta.startswith("0x") else "0x" + ta)
        tb = norm_type(tb if tb.startswith("0x") else "0x" + tb)
        if ta not in by_type or tb not in by_type:
            continue
        pool = DeepBookPool(
            pool_id=p["pool_id"], venue="deepbook", coin_a=ta, coin_b=tb,
            sym_a=by_type[ta], sym_b=by_type[tb],
            dec_a=int(p["base_asset_decimals"]), dec_b=int(p["quote_asset_decimals"]),
            fee=taker_fee, name=p["pool_name"])
        # the indexer simulates the read and runs out of gas on deep books at
        # 200 levels, so step down until one depth answers
        err = None
        for depth in (200, 100, 50):
            try:
                url = DB_BASE + book_path.format(name=p["pool_name"], depth=depth)
                r = requests.get(url, headers=UA, timeout=25)
                r.raise_for_status()
                pool.bids, pool.asks = _parse_book(r.json())
                pool.levels = depth
                break
            except Exception as e:
                err = e
        else:
            if verbose:
                print(f"  book failed for {p['pool_name']}: {err}")
        if verbose:
            print(f"  {p['pool_name']:<16}{len(pool.bids):>4} bids {len(pool.asks):>4} asks"
                  f"  (depth {pool.levels})")
        if pool.bids or pool.asks:
            out.append(pool)
    if verbose:
        print(f"deepbook: {len(out)} pools with a readable book")
    return out


# ---------------------------------------------------------------------------

def to_csv_rows(pools, prices: Dict[str, float] = None) -> List[dict]:
    """CSV rows for adapter pools; prices are USD by coin type."""
    prices = prices or {}
    rows = []
    for p in pools:
        ta, tb = norm_type(p.coin_a), norm_type(p.coin_b)
        rows.append({
            "symbol_a": p.sym_a, "symbol_b": p.sym_b,
            "coin_type_a": ta, "coin_type_b": tb,
            "tvl_usd": f"{p.tvl(prices.get(ta, 0), prices.get(tb, 0)):.2f}",
            "volume_24h": "0", "venue": p.venue,
            "pool_type": "WEIGHTED" if p.venue == "aftermath" else "ORDERBOOK",
            "fee_tier": f"{p.fee}", "pool_id": p.pool_id,
            "both_in_navi": "True", "has_adapter": "True",
            "pool_name": f"{p.sym_a} / {p.sym_b}",
        })
    return rows


def discovery_rows(tokens: Dict[str, str], prices: Dict[str, float],
                   min_usd: float = 25_000.0, verbose: bool = True) -> List[dict]:
    """
    CSV rows for DeepBook and Aftermath pools between coins in `tokens`.
    Aftermath pairs below min_usd are dropped. DeepBook pools with a readable
    book are all kept; their TVL is the depth of the levels fetched.
    """
    af = aftermath_pools(tokens, min_usd=min_usd, prices=prices, verbose=verbose)
    db = deepbook_pools(tokens, verbose=verbose)
    return to_csv_rows(af + db, prices)


def append_to_csv(rows: List[dict], csv_path: str):
    with open(csv_path) as fh:
        rdr = csv.DictReader(fh)
        cols = rdr.fieldnames
        seen = {(r["pool_id"], r["coin_type_a"], r["coin_type_b"]) for r in rdr}
    added = 0
    with open(csv_path, "a", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=cols)
        for r in rows:
            k = (r["pool_id"], r["coin_type_a"], r["coin_type_b"])
            if k in seen:
                continue
            w.writerow({c: r.get(c, "") for c in cols})
            seen.add(k); added += 1
    print(f"appended {added} pools to {csv_path}")
    return added


# ---------------------------------------------------------------------------
# LST minting: SUI -> LST at the protocol's own rate, one direction only
# ---------------------------------------------------------------------------
#
# Volo (vSUI), Haedal (haSUI) and AlphaFi (stSUI) mint atomically and
# permissionlessly, with no mint fee and no cap found: the public mint call
# returns the LST coin in the same transaction. Each LST can also be redeemed
# atomically, but redemption is deliberately not modelled as an exit (a
# liquidator's route never sells an LST back to its protocol).
#
# The rate is read by simulating a mint of MINT_PROBE_SUI and dividing, so it
# includes any mint fee and the protocol's refresh at mint time.

MINT_PROBE_SUI = 1_000 * 10 ** 9
SUI_TYPE = "0x2::sui::SUI"

# symbol -> how to call the protocol's mint (shared objects after the coin
# are passed in this order; "sys" is the Sui system state, "addr" a zero
# referral address)
MINTERS: Dict[str, dict] = {
    "vSUI": {"package": "0x68d22cf8bdbcd11ecba1e094922873e4080d4d11133e2443fddda0bfd11dae20",
             "module": "stake_pool", "function": "stake", "type_args": [],
             "pool_id": "0x2d914e23d82fedef1b5f56a32d5c64bdcc3087ccfea2b4d6ea51a71f587840e5",
             "args": ["pool", "0x680cd26af32b2bde8d3361e804c53ec1d1cfe24c7f039eb7f549e8dfde389a60",
                      "sys", "coin"]},
    "haSUI": {"package": "0x19e6ea7f5ced4f090e20da794cc80349a03e638940ddb95155a4e301f5f4967c",
              "module": "staking", "function": "request_stake_coin", "type_args": [],
              "pool_id": "0x47b224762220393057ebf4f70501b6e657c3e56684737568439a04f80849b2ca",
              "args": ["sys", "pool", "coin", "addr"]},
    "stSUI": {"package": "0x059f94b85c07eb74d2847f8255d8cc0a67c9a8dcc039eabf9f8b9e23a0de2700",
              "module": "liquid_staking", "function": "mint",
              "type_args": ["0xd1b72982e40348d069bb1ff701e634c117bb5f741f44dff91e472d3b01461e55::stsui::STSUI"],
              "pool_id": "0x1adb343ab351458e151bc392fbf1558b3332467f23bda45ae67cd355a57fd5f5",
              "args": ["pool", "sys", "coin"]},
}

_Q_SIMULATE = """query($tx: JSON!) { simulateTransaction(transaction: $tx, checksEnabled: false) {
  effects { status executionError { abortCode message } }
  outputs { returnValues { value { json } } } } }"""


@dataclass
class MintPool:
    """SUI in, LST out at `rate` SUI per LST; no depth limit, no reverse."""
    pool_id: str
    venue: str
    coin_a: str            # SUI
    coin_b: str            # the LST
    sym_a: str
    sym_b: str
    dec_a: int
    dec_b: int
    rate: float            # SUI per LST, fee included
    fee: float = 0.0
    one_way: str = "a"     # only coin_a (SUI) is accepted as input
    name: str = ""

    @property
    def liquidity(self) -> float:
        return float("inf")

    def price(self) -> float:
        """LST per SUI."""
        return 1.0 / self.rate

    def out(self, q: float, side: str = "a") -> float:
        if side != "a":
            raise ValueError(f"{self.name}: minting only takes SUI in")
        if q <= 0:
            return 0.0
        return q / 10 ** self.dec_a / self.rate * 10 ** self.dec_b

    def tvl(self, price_a_usd: float, price_b_usd: float) -> float:
        return 0.0             # not a pool of reserves; depth is unlimited

    def size_for_slippage(self, target: float, side: str = "a") -> Optional[float]:
        return None            # no price impact


def mint_rate(sym: str, coin_type: str) -> float:
    """SUI per LST from a simulated mint of MINT_PROBE_SUI."""
    import base64
    import struct
    from .clmm import gql
    m = MINTERS[sym]
    inputs = [{"kind": "PURE", "pure": base64.b64encode(struct.pack("<Q", MINT_PROBE_SUI)).decode()}]
    args = []
    for a in m["args"]:
        if a == "coin":
            args.append({"kind": "RESULT", "result": 0, "subresult": 0})
            continue
        if a == "addr":
            inputs.append({"kind": "PURE", "pure": base64.b64encode(bytes(32)).decode()})
        else:
            oid = {"pool": m["pool_id"], "sys": "0x5"}.get(a, a)
            inputs.append({"kind": "SHARED", "objectId": oid, "mutable": True})
        args.append({"kind": "INPUT", "input": len(inputs) - 1})
    tx = {"sender": "0x" + "ab" * 32, "kind": {"programmableTransaction": {
        "inputs": inputs,
        "commands": [{"splitCoins": {"coin": {"kind": "GAS"}, "amounts": [{"kind": "INPUT", "input": 0}]}},
                     {"moveCall": {"package": m["package"], "module": m["module"],
                                   "function": m["function"], "typeArguments": m["type_args"],
                                   "arguments": args}}]}}}
    r = gql(_Q_SIMULATE, {"tx": tx})["simulateTransaction"]
    if r["effects"]["status"] != "SUCCESS":
        raise RuntimeError(f"{sym} mint simulation failed: {r['effects']['executionError']}")
    minted = int(r["outputs"][1]["returnValues"][0]["value"]["json"]["balance"])
    if minted <= 0:
        raise RuntimeError(f"{sym} mint simulation returned nothing")
    return MINT_PROBE_SUI / minted


def mint_pools(tokens: Dict[str, str], verbose: bool = True) -> List[MintPool]:
    """A MintPool for each LST in MINTERS that is also in `tokens`."""
    out = []
    for sym, m in MINTERS.items():
        if sym not in tokens or "SUI" not in tokens:
            continue
        rate = mint_rate(sym, tokens[sym])
        out.append(MintPool(pool_id=m["pool_id"], venue="lst-mint",
                            coin_a=norm_type(tokens["SUI"]), coin_b=norm_type(tokens[sym]),
                            sym_a="SUI", sym_b=sym, dec_a=9, dec_b=9, rate=rate,
                            name=f"mint {sym}"))
        if verbose:
            print(f"  mint {sym}: {rate:.6f} SUI per {sym}")
    return out


def mint_rows(tokens: Dict[str, str]) -> List[dict]:
    """CSV rows for the mint venue (one per LST); TVL is not meaningful."""
    rows = []
    for sym, m in MINTERS.items():
        if sym in tokens and "SUI" in tokens:
            rows.append({"symbol_a": "SUI", "symbol_b": sym,
                         "coin_type_a": norm_type(tokens["SUI"]),
                         "coin_type_b": norm_type(tokens[sym]),
                         "tvl_usd": "0", "volume_24h": "0", "venue": "lst-mint",
                         "pool_type": "MINT", "fee_tier": "0", "pool_id": m["pool_id"],
                         "both_in_navi": "True", "has_adapter": "True",
                         "pool_name": f"mint {sym} (SUI -> {sym} only)",
                         "tvl_source": "venue"})
    return rows
