"""Provider-neutral closed-kline port."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, runtime_checkable

from smc_ict.domain import ClosedCandle, InstrumentId


@dataclass(frozen=True, slots=True)
class InstrumentMapping:
    instrument_id: str
    provider_symbol: str

    def __post_init__(self) -> None:
        InstrumentId(self.instrument_id)
        if type(self.provider_symbol) is not str or not 1 <= len(self.provider_symbol) <= 64:
            raise ValueError("invalid provider symbol")


@dataclass(frozen=True, slots=True)
class KlineRequest:
    provider_id: str
    market_type: str
    instrument_id: str
    provider_symbol: str
    interval: Literal["1m"]
    start_open_time_ms: int
    end_open_time_ms: int

    def __post_init__(self) -> None:
        if type(self.provider_id) is not str or not self.provider_id:
            raise ValueError("provider ID is required")
        if self.market_type != "LINEAR_PERPETUAL":
            raise ValueError("market type must be LINEAR_PERPETUAL")
        InstrumentId(self.instrument_id)
        if type(self.provider_symbol) is not str:
            raise TypeError("provider symbol must be a string")
        if not 1 <= len(self.provider_symbol) <= 64:
            raise ValueError("invalid provider symbol")
        if self.interval != "1m":
            raise ValueError("provider requests use canonical 1m only")
        if type(self.start_open_time_ms) is not int or type(self.end_open_time_ms) is not int:
            raise TypeError("request times must be integers")
        if self.start_open_time_ms < 0 or self.start_open_time_ms % 60_000 != 0:
            raise ValueError("request start must align to one minute")
        if self.end_open_time_ms < self.start_open_time_ms or self.end_open_time_ms % 60_000 != 0:
            raise ValueError("request end must be aligned and not precede start")


CanonicalCandleInput = ClosedCandle


@dataclass(frozen=True, slots=True)
class KlinePage:
    candles: tuple[CanonicalCandleInput, ...]
    next_start_open_time_ms: int | None
    complete: bool

    def __post_init__(self) -> None:
        if type(self.candles) is not tuple:
            raise TypeError("page candles must be an immutable tuple")
        if (
            self.next_start_open_time_ms is not None
            and type(self.next_start_open_time_ms) is not int
        ):
            raise TypeError("page cursor must be an integer or null")
        if type(self.complete) is not bool:
            raise TypeError("page completeness must be Boolean")
        if not self.complete and not self.candles:
            raise ValueError("a non-complete page must make progress")


@runtime_checkable
class KlineProvider(Protocol):
    """Adapter contract; generic application code knows no transport details."""

    provider_id: str

    def validate_instrument(self, mapping: object) -> None: ...
    def server_time_ms(self) -> int: ...
    def latest_closed_open_time_ms(self) -> int: ...
    def fetch_page(self, request: KlineRequest) -> KlinePage: ...


class ProviderError(RuntimeError):
    """Base for bounded provider-neutral failures."""


class ProviderConfigurationError(ProviderError):
    pass


class ProviderInstrumentError(ProviderError):
    pass


class ProviderProtocolError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    def __init__(self, retry_after_ms: int | None) -> None:
        self.retry_after_ms = retry_after_ms
        super().__init__("provider rate limit")


class ProviderTemporaryError(ProviderError):
    pass


class ProviderPermanentError(ProviderError):
    pass
