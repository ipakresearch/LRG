"""
The adapter interface.

Everything above this layer — route enumeration, cost composition, capacity
inversion, the parameter matrix — depends only on these five members. A new
venue is a class that implements them; nothing downstream changes.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Pool(Protocol):
    """A two-sided venue that can be swapped through in either direction.

    `side` is "a" or "b" and names which of the pool's two tokens is the input.
    Quantities are raw on-chain units, so decimals stay explicit throughout and
    conversion happens only at the boundaries.
    """

    pool_id: str
    venue: str
    coin_a: str          # fully qualified coin type of side a
    coin_b: str
    sym_a: str
    sym_b: str
    dec_a: int
    dec_b: int
    fee: float           # proportional swap fee, e.g. 0.0005

    def price(self) -> float:
        """Human-unit spot price: units of b per unit of a."""
        ...

    def out(self, q: float, side: str = "a") -> float:
        """Raw units in -> raw units out, net of fee and price impact."""
        ...

    def tvl(self, price_a_usd: float, price_b_usd: float) -> float:
        """Value held by the pool, for reporting rather than for execution."""
        ...

    def size_for_slippage(self, target: float, side: str = "a") -> float | None:
        """Raw input size at which price impact reaches `target`.

        Returns None when the target is unreachable; order books return the
        exhaustion point instead, since their depth is finite.
        """
        ...
