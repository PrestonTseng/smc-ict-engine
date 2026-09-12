"""Provider-neutral historical readiness over the canonical candle store."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from types import MappingProxyType
from typing import Protocol

from trading_research.application.market_sync import MarketSyncService
from trading_research.application.ports import InstrumentMapping, KlineProvider
from trading_research.domain import ClosedCandle, hash_candles

_MINUTE_MS = 60_000


class HistoricalCandleRepository(Protocol):
    def store_candle_page(
        self,
        candles: Sequence[ClosedCandle],
        *,
        successful_sync_ms: int,
        required_start_open_ms: int,
    ) -> None: ...

    def load_candles(
        self,
        provider_id: str,
        market_type: str,
        instrument_id: str,
        start_open_ms: int,
        end_open_ms: int,
    ) -> tuple[ClosedCandle, ...]: ...


@dataclass(frozen=True, slots=True)
class _HistoricalSyncRepositoryAdapter:
    """Let an explicit historical repair use rows, not forward-sync receipts, as authority."""

    repository: HistoricalCandleRepository

    def store_candle_page(
        self,
        candles: Sequence[ClosedCandle],
        *,
        successful_sync_ms: int,
        required_start_open_ms: int,
    ) -> None:
        self.repository.store_candle_page(
            candles,
            successful_sync_ms=successful_sync_ms,
            required_start_open_ms=required_start_open_ms,
        )

    def load_candles(
        self,
        provider_id: str,
        market_type: str,
        instrument_id: str,
        start_open_ms: int,
        end_open_ms: int,
    ) -> tuple[ClosedCandle, ...]:
        return self.repository.load_candles(
            provider_id,
            market_type,
            instrument_id,
            start_open_ms,
            end_open_ms,
        )

    def load_sync_state(self, provider_id: str, market_type: str, instrument_id: str) -> None:
        del provider_id, market_type, instrument_id
        return None


@dataclass(frozen=True, slots=True)
class InstrumentSyncResult:
    candles: tuple[ClosedCandle, ...]
    existing_rows: int
    fetched_rows: int
    total_rows: int
    gap_count: int
    data_hash: str

    def canonical_dict(self) -> dict[str, int | str]:
        return {
            "existing_rows": self.existing_rows,
            "fetched_rows": self.fetched_rows,
            "total_rows": self.total_rows,
            "gap_count": self.gap_count,
            "data_hash": self.data_hash,
        }


@dataclass(frozen=True, slots=True)
class HistoricalSyncReceipt:
    provider: str
    required_start_ms: int
    required_end_ms: int
    instruments: Mapping[str, InstrumentSyncResult]

    def __post_init__(self) -> None:
        object.__setattr__(self, "instruments", MappingProxyType(dict(self.instruments)))

    def canonical_dict(self) -> dict[str, object]:
        return {
            "status": "SUCCEEDED",
            "provider": self.provider,
            "required_start_ms": self.required_start_ms,
            "required_end_ms": self.required_end_ms,
            "instruments": {
                instrument_id: result.canonical_dict()
                for instrument_id, result in sorted(self.instruments.items())
            },
        }


class HistoricalRangeSyncService:
    """Fetch only missing minute ranges and prove final continuity."""

    def __init__(self, provider: KlineProvider, repository: HistoricalCandleRepository) -> None:
        self._provider = provider
        self._repository = repository

    def sync_range(
        self,
        mapping: InstrumentMapping,
        start_open_time_ms: int,
        end_open_time_ms: int,
    ) -> InstrumentSyncResult:
        self._validate_range(start_open_time_ms, end_open_time_ms)
        self._provider.validate_instrument(mapping)
        if end_open_time_ms > self._provider.latest_closed_open_time_ms():
            raise ValueError("requested range includes an open canonical minute")
        existing = self._load(mapping, start_open_time_ms, end_open_time_ms)
        for missing_start, missing_end in self._missing_ranges(
            existing, start_open_time_ms, end_open_time_ms
        ):
            try:
                MarketSyncService(
                    self._provider, _HistoricalSyncRepositoryAdapter(self._repository)
                ).sync_range(mapping, missing_start, missing_end)
            except ValueError as error:
                if str(error) != "completed provider range is not contiguous":
                    raise
                raise ValueError(
                    "historical range is not contiguous after synchronization"
                ) from error
        complete = self._load(mapping, start_open_time_ms, end_open_time_ms)
        expected_rows = (end_open_time_ms - start_open_time_ms) // _MINUTE_MS + 1
        if len(complete) != expected_rows or any(
            right.open_time_ms != left.open_time_ms + _MINUTE_MS
            for left, right in pairwise(complete)
        ):
            raise ValueError("historical range is not contiguous after synchronization")
        return InstrumentSyncResult(
            candles=complete,
            existing_rows=len(existing),
            fetched_rows=len(complete) - len(existing),
            total_rows=len(complete),
            gap_count=0,
            data_hash=hash_candles(complete),
        )

    def sync_all(
        self,
        mappings: Sequence[InstrumentMapping],
        start_open_time_ms: int,
        end_open_time_ms: int,
    ) -> HistoricalSyncReceipt:
        self._validate_range(start_open_time_ms, end_open_time_ms)
        ordered = sorted(mappings, key=lambda mapping: mapping.instrument_id)
        if not ordered or len({mapping.instrument_id for mapping in ordered}) != len(ordered):
            raise ValueError("historical sync requires unique instrument mappings")
        results = {
            mapping.instrument_id: self.sync_range(mapping, start_open_time_ms, end_open_time_ms)
            for mapping in ordered
        }
        return HistoricalSyncReceipt(
            self._provider.provider_id,
            start_open_time_ms,
            end_open_time_ms,
            results,
        )

    def _load(
        self, mapping: InstrumentMapping, start_open_time_ms: int, end_open_time_ms: int
    ) -> tuple[ClosedCandle, ...]:
        return self._repository.load_candles(
            self._provider.provider_id,
            "LINEAR_PERPETUAL",
            mapping.instrument_id,
            start_open_time_ms,
            end_open_time_ms,
        )

    @staticmethod
    def _validate_range(start_open_time_ms: int, end_open_time_ms: int) -> None:
        if (
            type(start_open_time_ms) is not int
            or type(end_open_time_ms) is not int
            or start_open_time_ms < 0
            or start_open_time_ms % _MINUTE_MS != 0
            or end_open_time_ms % _MINUTE_MS != 0
            or start_open_time_ms > end_open_time_ms
        ):
            raise ValueError("historical range must be aligned, non-negative, and ordered")

    @staticmethod
    def _missing_ranges(
        candles: tuple[ClosedCandle, ...], start_open_time_ms: int, end_open_time_ms: int
    ) -> tuple[tuple[int, int], ...]:
        present = {candle.open_time_ms for candle in candles}
        ranges: list[tuple[int, int]] = []
        missing_start: int | None = None
        for open_time in range(start_open_time_ms, end_open_time_ms + 1, _MINUTE_MS):
            if open_time not in present and missing_start is None:
                missing_start = open_time
            if open_time in present and missing_start is not None:
                ranges.append((missing_start, open_time - _MINUTE_MS))
                missing_start = None
        if missing_start is not None:
            ranges.append((missing_start, end_open_time_ms))
        return tuple(ranges)
