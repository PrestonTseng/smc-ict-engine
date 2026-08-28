"""Provider-neutral notification event and delivery port."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol, runtime_checkable

from smc_ict.domain import EventType, InstrumentId


@dataclass(frozen=True, slots=True)
class NotificationEvent:
    event_type: str
    run_id: str
    instrument_id: str | None
    strategy_id: str
    payload_schema_version: int
    payload: Mapping[str, None | bool | int | str]

    def __post_init__(self) -> None:
        EventType(self.event_type)
        if type(self.run_id) is not str or not self.run_id:
            raise ValueError("run ID is required")
        if self.instrument_id is not None:
            InstrumentId(self.instrument_id)
        if type(self.strategy_id) is not str or not self.strategy_id:
            raise ValueError("strategy ID is required")
        if type(self.payload_schema_version) is not int or self.payload_schema_version < 1:
            raise ValueError("payload schema version must be a positive integer")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    destination_id: str
    adapter_id: str
    event_id: str
    deduplication_id: str
    batch_id: str | None
    attempts: int
    outcome: str
    reason_code: str | None
    status_code: int | None


@dataclass(frozen=True, slots=True)
class NotificationDedupRecord:
    """The complete redacted durable identity of one successful delivery."""

    run_id: str
    destination_id: str
    deduplication_id: str
    delivered_at_seconds: int


@runtime_checkable
class NotificationDeduplicationStore(Protocol):
    def notification_delivered_at(
        self, destination_id: str, deduplication_id: str
    ) -> int | None: ...

    def store_notification_deliveries(
        self, records: tuple[NotificationDedupRecord, ...]
    ) -> None: ...


@runtime_checkable
class Notifier(Protocol):
    """One destination adapter; routing remains outside the adapter."""

    adapter_id: str

    def deliver(self, event: NotificationEvent) -> DeliveryReceipt: ...
