"""
Aftermath (weighted AMM) and DeepBook (order book) adapters.

Both expose the same interface as clmm.ClmmPool, so route_cost and viz_data can
use them without changes:  out(q_raw, side), price(), tvl(pa, pb), liquidity,
sym_a/sym_b, dec_a/dec_b, coin_a/coin_b, fee, venue, pool_id.

    import venues
    venues.probe_deepbook_orderbook("SUI_USDC")      # run this first

    af = venues.aftermath_pools(min_usd=25_000, prices=prices)
    db = venues.deepbook_pools()
    rows = venues.to_csv_rows(af + db)
    venues.append_to_csv(rows, "navi_pools.csv")
"""
from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

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
    Pull Aftermath pools, keep only two-coin pools where both coins are in
    `tokens` (symbol -> coin type) and TVL clears min_usd.
    Multi-coin pools are split into all two-leg combinations.
    """
    by_type = {v: k for k, v in tokens.items()}
    prices = prices or {}

    data = requests.get(AF_POOLS, headers=UA, timeout=60).json()
    if verbose:
        print(f"aftermath: {len(data)} pools returned")

    out = []
    for p in data:
        coins = p.get("coins") or {}
        legs = []
        for ctype, c in coins.items():
            t = ctype if ctype.startswith("0x") else "0x" + ctype
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
                tvl = pool.tvl(prices.get(sa, 0), prices.get(sb, 0))
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
        return bids, asks
    bids = pairs(d.get("bids") or d.get("bid"))
    asks = pairs(d.get("asks") or d.get("ask"))
    return bids, asks


def deepbook_pools(tokens: Dict[str, str], taker_fee: float = 0.001,
                   book_path: str = "/orderbook/{name}?level=2&depth=200",
                   verbose: bool = True) -> List[DeepBookPool]:
    by_type = {v: k for k, v in tokens.items()}
    out = []
    for p in deepbook_pool_list():
        ta = p["base_asset_id"]; tb = p["quote_asset_id"]
        ta = ta if ta.startswith("0x") else "0x" + ta
        tb = tb if tb.startswith("0x") else "0x" + tb
        if ta not in by_type or tb not in by_type:
            continue
        pool = DeepBookPool(
            pool_id=p["pool_id"], venue="deepbook", coin_a=ta, coin_b=tb,
            sym_a=by_type[ta], sym_b=by_type[tb],
            dec_a=int(p["base_asset_decimals"]), dec_b=int(p["quote_asset_decimals"]),
            fee=taker_fee, name=p["pool_name"])
        try:
            url = DB_BASE + book_path.format(name=p["pool_name"])
            d = requests.get(url, headers=UA, timeout=25).json()
            pool.bids, pool.asks = _parse_book(d)
        except Exception as e:
            if verbose:
                print(f"  book failed for {p['pool_name']}: {e}")
        if verbose:
            print(f"  {p['pool_name']:<16}{len(pool.bids):>4} bids {len(pool.asks):>4} asks")
        if pool.bids or pool.asks:
            out.append(pool)
    if verbose:
        print(f"deepbook: {len(out)} pools with a readable book")
    return out


# ---------------------------------------------------------------------------

def to_csv_rows(pools, prices: Dict[str, float] = None) -> List[dict]:
    prices = prices or {}
    rows = []
    for p in pools:
        rows.append({
            "symbol_a": p.sym_a, "symbol_b": p.sym_b,
            "coin_type_a": p.coin_a, "coin_type_b": p.coin_b,
            "tvl_usd": f"{p.tvl(prices.get(p.sym_a, 0), prices.get(p.sym_b, 0)):.2f}",
            "volume_24h": "0", "venue": p.venue,
            "pool_type": "WEIGHTED" if p.venue == "aftermath" else "ORDERBOOK",
            "fee_tier": f"{p.fee}", "pool_id": p.pool_id,
            "both_in_navi": "True", "has_adapter": "True",
            "pool_name": f"{p.sym_a} / {p.sym_b}",
        })
    return rows


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
