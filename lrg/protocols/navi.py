"""
Navi Protocol — main market, Sui mainnet.

Three inputs the engine needs and cannot infer: the coin types of the listed
assets, their risk parameters, and which venues to route through.

Sources and open questions
--------------------------
COIN_TYPES   Navi docs, plus USDSUI and wBTC recovered from pool data.
ASSETS       the published parameter table for active assets. Borrow-only
             assets carry ltv=None and appear on the debt side only.
CLOSE_FACTOR the docs say "up to 35%", the newer hub says 50%. Unresolved; it
             scales liquidation demand linearly, so confirm on chain.
E-Mode       per-asset boosted LTV and LT are published; the E-Mode
             liquidation bonus is not. Category membership below is inferred
             from the pairs visible in the app, not read from config.
"""
from __future__ import annotations

from typing import Dict, Optional, Sequence

# Protocol's share of the liquidation incentive.
PLATFORM_SHARE = 0.10

# "up to 35%" per the parameters page; the docs hub states 50%.
CLOSE_FACTOR = 0.35

# Venues carrying enough depth on Sui to matter for routing.
VENUES = {"cetus", "bluefin", "turbos-finance", "momentum", "deepbook",
          "aftermath"}

COIN_TYPES: Dict[str, str] = {
    "SUI":     "0x2::sui::SUI",
    "USDC":    "0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC",
    "suiUSDT": "0x375f70cf2ae4c00bf37117d0c85a2c71545e6ee05c4a5c7d282cd66a4504b068::usdt::USDT",
    "suiUSDe": "0x41d587e5336f1c86cad50d38a7136db99333bb9bda91cea4ba69115defeb1402::sui_usde::SUI_USDE",
    "USDSUI":  "0x44f838219cf67b058f3b37907b655f226153c18e33dfcd0da559a844fea9b1c1::usdsui::USDSUI",
    "vSUI":    "0x549e8b69270defbfafd4f94e17ec44cdbdd99820b33bda2278dea3b9a32d3f55::cert::CERT",
    "haSUI":   "0xbde4ba4c2e274a60ce15c1cfff9e5c42e41654ac8b6d906a57efa4bd3c29f47d::hasui::HASUI",
    "stSUI":   "0xd1b72982e40348d069bb1ff701e634c117bb5f741f44dff91e472d3b01461e55::stsui::STSUI",
    "suiETH":  "0xd0e89b2af5e4910726fbcd8b8dd37bb79b29e5f83f7491bca830e94f7f226d29::eth::ETH",
    "USDY":    "0x960b531667636f39e85867775f52f6b1f220a058c4de786905bdf761e06a56bb::usdy::USDY",
    "FDUSD":   "0xf16e6b723f242ec745dfd7634ad072c42d5c1d9ac9d62a39c381303eaa57693a::fdusd::FDUSD",
    "BUCK":    "0xce7ff77a83ea0cb6fd39bd8748e2ec89a3f41e8efdc3f4eb123e0ca37b184db2::buck::BUCK",
    "xBTC":    "0x876a4b7bce8aeaef60464c11f4026903e9afacab79b9b142686158aa86560b50::xbtc::XBTC",
    "wBTC":    "0xaafb102dd0902f5055cadecd687fb5b71ca82ef0e0285d90afde828ec58ca96b::btc::BTC",
    "LBTC":    "0x3e8e9423d80e1774a7ca128fccd8bf5f1f7753be658c5e645929037f7c819040::lbtc::LBTC",
    "MBTC":    "0xd1a91b46bd6d966b62686263609074ad16cfdffc63c31a4775870a2d54d20c6b::mbtc::MBTC",
    "WBTC.lz": "0x0041f9f9344cac094454cd574e333c4fdb132d7bcc9379bcd4aab485b2a63942::wbtc::WBTC",
    "enzoBTC": "0x8f2b5eb696ed88b71fea398d330bccfa52f6e2a5a8e1ac6180fcb25c6de42ebc::coin::COIN",
    "SOL":     "0xb7844e289a8410e50fb3ca48d69eb9cf29e27d223ef90353fe1bd8e27ff8f3f8::coin::COIN",
    "WAL":     "0x356a26eb9e012a68958082340d4c4116e7f55615cf27affcff209cf0ae544f59::wal::WAL",
    "DEEP":    "0xdeeb7a4662eec9f2f3def03fb937a663dddaa2e215b8078a284d026b7946c270::deep::DEEP",
    "NAVX":    "0xa99b8952d4f7d947ea77fe0ecdcc9e5fc0bcab2841d6e2a5aa00c3044e5544b5::navx::NAVX",
    "wUSDC":   "0x5d4b302506645c37ff133b98c4b50a5ae14841659738d6d733d59d0d217a93bf::coin::COIN",
    "XAUm":    "0x9d297676e7a4b771ab023291377b2adfaa4938fb9080b8d12430e4b108b836a9::xaum::XAUM",
}

# symbol: (ltv, lt, lb, supply_cap_tokens, emode_ltv, emode_lt)
ASSETS: Dict[str, tuple] = {
    "USDC":    (.80, .85, .08,  50_000_000, .90, .92),
    "suiUSDT": (.80, .85, .08,  15_000_000, .90, .92),
    "SUI":     (.75, .80, .10,  55_000_000, None, None),
    "suiUSDe": (.75, .80, .10,  10_000_000, .85, .87),
    "USDSUI":  (.75, .78, .08,  10_000_000, .85, .87),
    "vSUI":    (.75, .80, .10,  25_000_000, .90, .93),
    "suiETH":  (.70, .75, .15,       2_900, None, None),
    "USDY":    (.70, .75, .08,   5_000_000, .85, .87),
    "FDUSD":   (.70, .75, .10,      52_000, None, None),
    "BUCK":    (.70, .75, .10,  22_000_000, None, None),
    "stSUI":   (.70, .75, .10,      40_000, None, None),
    "haSUI":   (.70, .75, .10,  27_500_000, None, None),
    "xBTC":    (.67, .70, .15,         205, None, None),
    "wBTC":    (.65, .70, .15,         200, None, None),
    "LBTC":    (.65, .70, .15,          70, None, None),
    "MBTC":    (.65, .70, .10,         300, None, None),
    "WBTC.lz": (.65, .70, .15,         100, None, None),
    "enzoBTC": (.62, .77, .10,         435, None, None),
    "SOL":     (.60, .65, .10,       1_000, None, None),
    "WAL":     (.60, .65, .10, 160_000_000, None, None),
    "DEEP":    (.47, .52, .10, 385_000_000, None, None),
    "NAVX":    (.45, .65, .05, 300_000_000, None, None),
    # borrow-only (no LTV): still valid debt assets
    "wUSDC":   (None, .85, .08,   2_500_000, None, None),
    "XAUm":    (None, .45, .10,          10, None, None),
}

# Inferred from the pairs shown in the app. E-Mode parameters apply only when
# collateral and debt are in the same category.
EMODE_CATEGORIES: Dict[str, Sequence[str]] = {
    "stable": ["USDC", "suiUSDT", "suiUSDe", "USDSUI", "USDY", "BUCK"],
    "sui":    ["vSUI", "SUI", "haSUI", "stSUI"],
}

SYMBOL_OF: Dict[str, str] = {v: k for k, v in COIN_TYPES.items()}


def collaterals() -> list:
    """Assets usable as collateral: active with an LTV set."""
    return [s for s, v in ASSETS.items() if v[0] is not None]


def debts() -> list:
    """Assets that can be borrowed. All active assets, absent a published
    per-collateral restriction — an assumption worth confirming, since it sets
    the debt set the adversarial envelope is taken over."""
    return list(ASSETS)
