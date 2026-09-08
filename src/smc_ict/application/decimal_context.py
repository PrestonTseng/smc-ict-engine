"""Canonical decimal arithmetic policy for deterministic backtest accounting."""

from contextlib import AbstractContextManager
from decimal import ROUND_HALF_EVEN, Context, localcontext

DETERMINISTIC_DECIMAL_PRECISION = 28


def deterministic_decimal_context() -> AbstractContextManager[Context]:
    """Return the isolated 28-digit, half-even context used for all derived backtest values."""
    return localcontext(
        Context(
            prec=DETERMINISTIC_DECIMAL_PRECISION,
            rounding=ROUND_HALF_EVEN,
            Emin=-999_999,
            Emax=999_999,
            capitals=1,
            clamp=0,
        )
    )
