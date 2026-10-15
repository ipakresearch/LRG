"""
Coin identity.

A coin is its fully qualified Move type. Symbols are display labels only: two
different coins can share one (native USDC and Wormhole USDC are both "USDC"
on chain), so nothing is keyed by symbol. Types are compared after
normalization, since the same type can be written with a short or a
zero-padded address (0x2::sui::SUI).
"""
from __future__ import annotations


def norm_type(coin_type: str) -> str:
    """Coin type with the address zero-padded to 64 hex digits."""
    addr, rest = coin_type.split("::", 1)
    addr = addr.lower()
    addr = addr[2:] if addr.startswith("0x") else addr
    return "0x" + addr.rjust(64, "0") + "::" + rest


# Native (Circle) USDC on Sui: the anchor for derived prices.
USDC = norm_type("0xdba34672e30cb065b1f93e3ab55318768fd6fef66c15942c9f7cb846e2f900e7::usdc::USDC")
