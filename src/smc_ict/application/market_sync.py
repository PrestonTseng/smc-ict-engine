"""Provider-neutral bounded synchronization of canonical one-minute candle pages."""

from __future__ import annotations

from collections.abc import Sequence
from itertools import pairwise
from typing import Protocol

from smc_ict.application.ports import (
    InstrumentMapping,
    KlinePage,
    KlineProvider,
    KlineRequest,
    SyncState,
)
from smc_ict.domain import ClosedCandle


class CandlePageRepository(Protocol):
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

    def load_sync_state(
        self, provider_id: str, market_type: str, instrument_id: str
    ) -> SyncState | None: ...


class MarketSyncService:
    """Validate provider pages before handing each atomic page to persistence."""

    def __init__(self, provider: KlineProvider, repository: CandlePageRepository) -> None:
        self._provider = provider
        self._repository = repository

    def sync_range(
        self,
        mapping: InstrumentMapping,
        start_open_time_ms: int,
        end_open_time_ms: int,
    ) -> tuple[ClosedCandle, ...]:
        self._provider.validate_instrument(mapping)
        successful_sync_ms = self._provider.server_time_ms()
        latest_closed = self._provider.latest_closed_open_time_ms()
        if end_open_time_ms > latest_closed:
            raise ValueError("requested range includes an open canonical minute")

        existing = self._repository.load_candles(
            self._provider.provider_id,
            "LINEAR_PERPETUAL",
            mapping.instrument_id,
            start_open_time_ms,
            end_open_time_ms,
        )
        self._validate_existing(mapping, existing)
        present = {candle.open_time_ms for candle in existing}
        self._validate_sync_state(mapping, start_open_time_ms, present)

        for missing_start, missing_end in self._missing_ranges(
            start_open_time_ms, end_open_time_ms, present
        ):
            self._sync_missing_range(
                mapping,
                missing_start,
                missing_end,
                successful_sync_ms=successful_sync_ms,
                required_start_open_ms=start_open_time_ms,
            )

        ordered = self._repository.load_candles(
            self._provider.provider_id,
            "LINEAR_PERPETUAL",
            mapping.instrument_id,
            start_open_time_ms,
            end_open_time_ms,
        )
        self._validate_existing(mapping, ordered)
        expected = tuple(range(start_open_time_ms, end_open_time_ms + 1, 60_000))
        if tuple(candle.open_time_ms for candle in ordered) != expected:
            raise ValueError("completed provider range is not contiguous")
        return ordered

    def _sync_missing_range(
        self,
        mapping: InstrumentMapping,
        start_open_time_ms: int,
        end_open_time_ms: int,
        *,
        successful_sync_ms: int,
        required_start_open_ms: int,
    ) -> None:
        current_start = start_open_time_ms
        current_end = end_open_time_ms
        requested_cursors: set[int] = set()
        accepted: dict[tuple[str, str, str, str, int], ClosedCandle] = {}

        while True:
            request = KlineRequest(
                provider_id=self._provider.provider_id,
                market_type="LINEAR_PERPETUAL",
                instrument_id=mapping.instrument_id,
                provider_symbol=mapping.provider_symbol,
                interval="1m",
                start_open_time_ms=current_start,
                end_open_time_ms=current_end,
            )
            page = self._provider.fetch_page(request)
            candles = self._validate_page(request, page)
            for candle in candles:
                existing = accepted.get(candle.primary_key)
                if existing is not None and existing != candle:
                    raise ValueError("provider page contains a conflicting duplicate")
                accepted[candle.primary_key] = candle
            self._repository.store_candle_page(
                candles,
                successful_sync_ms=successful_sync_ms,
                required_start_open_ms=required_start_open_ms,
            )
            if page.complete:
                break
            cursor = page.next_start_open_time_ms
            if cursor is None or cursor in requested_cursors:
                raise ValueError("provider page cursor did not make progress")
            requested_cursors.add(cursor)
            if cursor == candles[-1].open_time_ms + 60_000 and cursor <= current_end:
                current_start = cursor
            elif cursor == candles[0].open_time_ms - 60_000 and cursor >= current_start:
                current_end = cursor
            else:
                raise ValueError("provider page cursor is outside bounded progress")

        expected = tuple(range(start_open_time_ms, end_open_time_ms + 1, 60_000))
        ordered = tuple(sorted(accepted.values(), key=lambda candle: candle.open_time_ms))
        if tuple(candle.open_time_ms for candle in ordered) != expected:
            raise ValueError("completed provider range is not contiguous")

    def _validate_existing(
        self, mapping: InstrumentMapping, candles: Sequence[ClosedCandle]
    ) -> None:
        for candle in candles:
            if (
                candle.provider_id != self._provider.provider_id
                or candle.market_type != "LINEAR_PERPETUAL"
                or candle.instrument_id != mapping.instrument_id
                or candle.provider_symbol != mapping.provider_symbol
                or candle.interval != "1m"
            ):
                raise ValueError("stored candle identity does not match the requested market")

    def _validate_sync_state(
        self, mapping: InstrumentMapping, start_open_time_ms: int, present: set[int]
    ) -> None:
        state = self._repository.load_sync_state(
            self._provider.provider_id, "LINEAR_PERPETUAL", mapping.instrument_id
        )
        if state is None or state.last_completed_open_time_ms < start_open_time_ms:
            return
        expected_prefix = set(
            range(start_open_time_ms, state.last_completed_open_time_ms + 1, 60_000)
        )
        if not expected_prefix <= present:
            raise ValueError("sync state hides a canonical candle gap")

    @staticmethod
    def _missing_ranges(
        start_open_time_ms: int, end_open_time_ms: int, present: set[int]
    ) -> tuple[tuple[int, int], ...]:
        ranges: list[tuple[int, int]] = []
        missing_start: int | None = None
        for open_time_ms in range(start_open_time_ms, end_open_time_ms + 1, 60_000):
            if open_time_ms not in present and missing_start is None:
                missing_start = open_time_ms
            if open_time_ms in present and missing_start is not None:
                ranges.append((missing_start, open_time_ms - 60_000))
                missing_start = None
        if missing_start is not None:
            ranges.append((missing_start, end_open_time_ms))
        return tuple(ranges)

    @staticmethod
    def _validate_page(request: KlineRequest, page: KlinePage) -> tuple[ClosedCandle, ...]:
        unique: list[ClosedCandle] = []
        by_key: dict[tuple[str, str, str, str, int], ClosedCandle] = {}
        for candle in page.candles:
            if (
                candle.provider_id != request.provider_id
                or candle.market_type != request.market_type
                or candle.instrument_id != request.instrument_id
                or candle.provider_symbol != request.provider_symbol
                or candle.interval != request.interval
                or not request.start_open_time_ms <= candle.open_time_ms <= request.end_open_time_ms
            ):
                raise ValueError("provider candle identity or bounds do not match the request")
            existing = by_key.get(candle.primary_key)
            if existing is not None:
                if existing != candle:
                    raise ValueError("provider page contains a conflicting duplicate")
                continue
            by_key[candle.primary_key] = candle
            unique.append(candle)
        if not unique:
            raise ValueError("provider page did not make candle progress")
        if tuple(candle.open_time_ms for candle in unique) != tuple(
            sorted(candle.open_time_ms for candle in unique)
        ):
            raise ValueError("provider page is non-monotonic")
        if any(
            right.open_time_ms != left.open_time_ms + 60_000 for left, right in pairwise(unique)
        ):
            raise ValueError("provider page is not contiguous")
        return tuple(unique)
