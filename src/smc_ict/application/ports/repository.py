"""Durable-state port without SQLite coupling."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from smc_ict.domain import ClosedCandle


@dataclass(frozen=True, slots=True)
class SyncState:
    provider_id: str
    market_type: str
    instrument_id: str
    interval: str
    last_completed_open_time_ms: int
    last_completed_close_time_ms: int
    last_successful_sync_ms: int
    last_error: str | None


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    status: str
    started_at_ms: int
    completed_at_ms: int | None
    strategy_name: str
    strategy_version: str
    strategy_config_hash: str
    provider_id: str
    market_type: str
    market_config_hash: str
    git_commit: str
    data_start_open_ms: int
    data_end_close_ms: int
    data_hash: str
    error: str | None


@dataclass(frozen=True, slots=True)
class ObservationRecord:
    run_id: str
    instrument_id: str
    signal_id: str
    status: str
    event_time_ms: int | None
    known_time_ms: int | None
    reason: str
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class DecisionRecord:
    run_id: str
    instrument_id: str
    decision_status: str
    direction: str | None
    entry_text: str | None
    stop_text: str | None
    target_text: str | None
    first_failed_signal: str | None
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@runtime_checkable
class Repository(Protocol):
    """Provider-neutral atomic persistence seam."""

    repository_id: str

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
    def store_run(self, run: RunRecord) -> None: ...
    def finish_run(
        self, run_id: str, status: str, *, completed_at_ms: int, error: str | None
    ) -> RunRecord: ...
    def store_observations(self, observations: Sequence[ObservationRecord]) -> None: ...
    def load_observations(self, run_id: str) -> tuple[ObservationRecord, ...]: ...
    def store_decisions(self, decisions: Sequence[DecisionRecord]) -> None: ...
    def load_decisions(self, run_id: str) -> tuple[DecisionRecord, ...]: ...
    def commit_run(
        self,
        run_id: str,
        observations: Sequence[ObservationRecord],
        decisions: Sequence[DecisionRecord],
        *,
        completed_at_ms: int,
    ) -> RunRecord: ...
    def recover_running_runs(self, *, completed_at_ms: int, reason: str) -> tuple[str, ...]: ...
