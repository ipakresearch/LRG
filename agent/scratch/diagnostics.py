# LRG pre-outreach diagnostics. Run from the LRG repo root, in a notebook cell.
# Read-only: makes GraphQL reads and prints results; changes nothing on disk.
#
#   D1  coin order: does the CSV's token order match the pool's on-chain order?
#   D2  Turbos: are ticks dynamic *object* fields (which the reader never asks for)?
#   D3  Cetus/Bluefin failed reads: empty pools or reader bug?
#   D4  MBTC: why every MBTC route has zero capacity
#   D5  SUI -> USDC: exact cost vs the grid curve at the sizes that matter
#   D6  Navi liquidation event: how the bonus and the treasury share are split
#
# Results land in DataFrames named d1_order, d2_turbos, d3_empty, d4_mbtc, d5_sui_usdc,
# and d6_events (raw event JSON).

import csv, json
import pandas as pd
from lrg import clmm, curves
from lrg.clmm import gql, Q_OBJECT, split_generics
from lrg.protocols import navi, navi_oracle

POOLS_CSV = "data/navi_pools.csv"
PRICED = {"cetus", "bluefin", "momentum", "turbos-finance"}

def norm(t):
    a, _, rest = t.partition("::")
    a = a.lower().removeprefix("0x")
    return "0x" + a.rjust(64, "0") + "::" + rest

rows = [r for r in csv.DictReader(open(POOLS_CSV)) if r["venue"] in PRICED]
oracle = navi_oracle.prices()            # {navi symbol: USD}

# ---------------------------------------------------------------- D1 coin order
order = []
for r in rows:
    try:
        rep = gql(Q_OBJECT, {"a": r["pool_id"]})["object"]["asMoveObject"]["contents"]["type"]["repr"]
        ca, cb = split_generics(rep)
        state = ("same" if norm(ca) == norm(r["coin_type_a"])
                 else "reversed" if norm(ca) == norm(r["coin_type_b"]) else "other")
    except Exception as e:
        state = f"error: {e}"[:60]
    order.append({"venue": r["venue"], "pair": f'{r["symbol_a"]}/{r["symbol_b"]}',
                  "tvl_usd": float(r["tvl_usd"] or 0), "pool_id": r["pool_id"], "order": state})
d1_order = pd.DataFrame(order)
print("D1 coin order, CSV vs chain:\n", d1_order.groupby(["venue", "order"]).tvl_usd.agg(["count", "sum"]))
print(d1_order[d1_order.order == "reversed"].sort_values("tvl_usd", ascending=False).head(15).to_string(index=False))

# ---------------------------------------------------------------- D2 Turbos field types
Q_BOTH = """query($a: SuiAddress!, $after: String) { address(address: $a) {
  dynamicFields(first: 50, after: $after) { pageInfo { hasNextPage endCursor }
    nodes { name { type { repr } json }
            value { __typename ... on MoveValue { type { repr } }
                               ... on MoveObject { contents { type { repr } } } } } } } }"""
TURBOS_USDC_SUI = "0x0df4f02d0e210169cb6d5aabd03c3058328c06f2c4dbb0804faa041159c78443"
kinds, cursor = [], None
for _ in range(4):                                   # first 200 fields is enough to see the pattern
    df = gql(Q_BOTH, {"a": TURBOS_USDC_SUI, "after": cursor})["address"]["dynamicFields"]
    for n in df["nodes"]:
        v = n["value"] or {}
        t = (v.get("type") or (v.get("contents") or {}).get("type") or {}).get("repr", "")
        kinds.append({"value_kind": v.get("__typename"), "value_type": t.split("::")[-1][:40]})
    if not df["pageInfo"]["hasNextPage"]:
        break
    cursor = df["pageInfo"]["endCursor"]
d2_turbos = pd.DataFrame(kinds).value_counts().rename("fields").reset_index()
print("\nD2 Turbos USDC/SUI dynamic fields (first 200):\n", d2_turbos.to_string(index=False))
print("   If ticks show up as MoveObject, Q_FIELDS in clmm.py needs `... on MoveObject { contents { json } }`.")

# ---------------------------------------------------------------- D3 empty or broken
FAILED_CB = ["0x40ceeed02e2e82d4f58e88b9524b33c362e00e17368037f4a47c3cf4c5f1df94",
             "0x62d398584d1c5909ee1225fbb846d049175cc3b95fbefba4e0191cee45f1663e",
             "0x8989fbbd6114a5d768eb551e8e6609ac39c91e7c30813c5c8771158ccf2f73d3",
             "0x97bde23c85fa25513f1c4140ba81ddcecf69f6cea5964e4ee28212776082c850",
             "0x2de1a48b8b2047718b320c1f4e76ee3cfb31f046b835b599818b87aed34b7825",
             "0xacf163d7e76641381bcf026ddb7cb4e4876eb03fff183dd4de0fc15f5000d650",
             "0x102d08049c737f8cef0d8a108deefd4aa4404db53c572d02821fb0432ecd6709",
             "0x7db7a7279c6e28cb6c5ac06d81c0f3770621b3215a4c37c4f5fa18f09d10c0bd"]
empty = []
for pid in FAILED_CB:
    j = gql(Q_OBJECT, {"a": pid})["object"]["asMoveObject"]["contents"]["json"]
    tm = j.get("tick_manager") or j.get("ticks_manager") or {}
    size = ((tm.get("ticks") or {}).get("size") if isinstance(tm, dict) else None) or (j.get("ticks") or {}).get("size")
    empty.append({"pool_id": pid[:12], "liquidity": int(j.get("liquidity", 0)), "tick_table_size": size,
                  "verdict": "empty pool" if int(j.get("liquidity", 0)) == 0 and str(size) in ("0", "None") else "check reader"})
d3_empty = pd.DataFrame(empty)
print("\nD3 failed Cetus/Bluefin/Turbos-parse pools:\n", d3_empty.to_string(index=False))

# ---------------------------------------------------------------- D4 MBTC
mb = [r for r in rows if "MBTC" in (r["symbol_a"], r["symbol_b"])]
out = []
for r in mb:
    p = clmm.fetch(r["pool_id"], verbose=False)
    oracle_ratio = oracle["MBTC"] / oracle["wBTC"] if norm(p.coin_a) == norm(navi.COIN_TYPES["MBTC"]) \
        else oracle["wBTC"] / oracle["MBTC"]
    s_a = p.size_for_slippage(0.05, "a"); s_b = p.size_for_slippage(0.05, "b")
    out.append({"pool": r["pool_id"][:12], "venue": r["venue"], "chain_order": f"{p.sym_a}/{p.sym_b}",
                "dec_a": p.dec_a, "dec_b": p.dec_b, "pool_price": p.price(), "oracle_ratio": oracle_ratio,
                "deviation": p.price() / oracle_ratio - 1, "liquidity": p.liquidity, "ranges": len(p.ranges),
                "sell_a_at_5pct": s_a / 10**p.dec_a if s_a else None,
                "sell_b_at_5pct": s_b / 10**p.dec_b if s_b else None})
d4_mbtc = pd.DataFrame(out)
print("\nD4 MBTC pools:\n", d4_mbtc.T.to_string())
print("   A deviation beyond the 8.17% budget, or no ranges on one side, explains zero capacity without a read bug.")

# ---------------------------------------------------------------- D5 SUI -> USDC, grid vs exact
legs = []
for r in rows:
    if {norm(r["coin_type_a"]), norm(r["coin_type_b"])} == {norm(navi.COIN_TYPES["SUI"]), norm(navi.COIN_TYPES["USDC"])}:
        try:
            p = clmm.fetch(r["pool_id"], verbose=False)
        except Exception as e:
            print("  skip", r["venue"], r["pool_id"][:12], str(e)[:60]); continue
        side = "a" if norm(p.coin_a) == norm(navi.COIN_TYPES["SUI"]) else "b"   # side from the chain, not the CSV
        legs.append((p, side))
px = {"SUI": oracle["SUI"], "USDC": oracle["USDC"]}     # the CLMM pools name these coins SUI and USDC
lc = curves.LegCurve(legs, px)
res = []
for q in (250e3, 500e3, 850e3, 1e6, 2e6, 3e6):
    exact_out, alloc = curves.allocate(legs, q, px)
    res.append({"sold_usd": q, "cost_exact": 1 - exact_out / q, "cost_grid": lc.cost(q),
                "pools_used": sum(1 for v in alloc.values() if v > 0)})
d5_sui_usdc = pd.DataFrame(res)
print(f"\nD5 SUI -> USDC over {len(legs)} readable CLMM pools (DeepBook not included):\n",
      d5_sui_usdc.to_string(index=False, float_format=lambda v: f"{v:,.4f}" if abs(v) < 1 else f"{v:,.0f}"))
print("   Budget at LB 10% is 0.0817. Where cost_grid < cost_exact, the grid overstates capacity.")

# ---------------------------------------------------------------- D6 a real liquidation
store = gql(Q_OBJECT, {"a": navi_oracle.STORAGE})["object"]["asMoveObject"]["contents"]["type"]["repr"]
pkg = store.split("::")[0]
Q_EV = """query($t: String!) { events(last: 50, filter: { type: $t }) {
  nodes { contents { type { repr } json } transaction { digest } } } }"""
d6_events = []
try:
    nodes = gql(Q_EV, {"t": pkg})["events"]["nodes"]
    d6_events = [n for n in nodes if "iquidat" in n["contents"]["type"]["repr"]]
except Exception as e:
    print("\nD6 event query failed:", str(e)[:120])
print(f"\nD6 Navi package {pkg[:12]}…: {len(d6_events)} liquidation events in the last 50 events.")
for ev in d6_events[:2]:
    print(ev["transaction"]["digest"], ev["contents"]["type"]["repr"].split("::")[-1])
    print(json.dumps(ev["contents"]["json"], indent=1))
print("   Check: collateral_amount vs debt_amount * (1 + LB). If treasury is extra to that, the liquidator keeps\n"
      "   0.9*LB (current model). If it is carved out of collateral_amount, recompute b.\n"
      "   If no events show here, open any recent Navi liquidation on suiscan and read the same fields.")
