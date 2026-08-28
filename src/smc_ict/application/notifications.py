"""Provider-neutral fan-out routing for configured notification destinations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256

from smc_ict.application.ports.notifications import (
    DeliveryReceipt,
    NotificationDeduplicationStore,
    NotificationDedupRecord,
    NotificationEvent,
    Notifier,
)
from smc_ict.configuration.models import NotificationConfig, NotificationDestination

AdapterFactory = Callable[[str, NotificationDestination], Notifier]


@dataclass(frozen=True, slots=True)
class RoutingReceipt:
    """Non-secret aggregate outcome for one event fan-out."""

    outcome: str
    receipts: tuple[DeliveryReceipt, ...]


class NotificationRouter:
    """Route an event to matching destinations in deterministic identifier order."""

    def __init__(
        self,
        config: NotificationConfig,
        *,
        adapter_factory: AdapterFactory,
        clock_seconds: Callable[[], int],
        deduplication_store: NotificationDeduplicationStore | None = None,
    ) -> None:
        self._config = config
        self._adapter_factory = adapter_factory
        self._adapters: dict[str, Notifier] = {}
        self._clock_seconds = clock_seconds
        self._deduplication_store = deduplication_store
        self._deduplicated_at: dict[tuple[str, str], int] = {}
        self._pending: dict[str, list[NotificationEvent]] = {}
        self._deadlines: dict[str, int] = {}

    def deliver(self, event: NotificationEvent) -> RoutingReceipt:
        if not self._config.enabled:
            return RoutingReceipt("NO_MATCH", ())
        receipts = tuple(
            self._deliver_to_destination(destination_id, destination, event)
            for destination_id, destination in sorted(self._config.destinations.items())
            if destination.enabled and event.event_type in destination.enabled_events
        )
        if not receipts:
            return RoutingReceipt("NO_MATCH", ())
        successful = sum(receipt.outcome in {"SUCCESS", "DEDUPLICATED"} for receipt in receipts)
        if successful == len(receipts):
            return RoutingReceipt("ALL_SUCCESS", receipts)
        if successful:
            return RoutingReceipt("PARTIAL_FAILURE", receipts)
        return RoutingReceipt("ALL_FAILURE", receipts)

    def deliver_all(self, events: tuple[NotificationEvent, ...]) -> RoutingReceipt:
        """Queue matching events and flush each destination at its deterministic bounds."""
        if not self._config.enabled:
            return RoutingReceipt("NO_MATCH", ())
        receipts: list[DeliveryReceipt] = []
        for destination_id, destination in sorted(self._config.destinations.items()):
            if not destination.enabled:
                continue
            for event in events:
                if event.event_type not in destination.enabled_events:
                    continue
                now = self._clock_seconds()
                deduplication_id = self._deduplication_id(destination_id, destination, event)
                previous = self._delivered_at(destination_id, deduplication_id)
                if (
                    previous is not None
                    and now - previous < destination.deduplication.window_seconds
                ):
                    receipts.append(
                        self._deduplicated_receipt(
                            destination_id, destination, event, deduplication_id
                        )
                    )
                    continue
                deadline = self._deadlines.get(destination_id)
                if deadline is not None and now >= deadline:
                    receipts.extend(self._flush_destination(destination_id, destination))
                pending = self._pending.setdefault(destination_id, [])
                if not pending:
                    self._deadlines[destination_id] = now + destination.batching.flush_seconds
                pending.append(event)
                if len(pending) >= destination.batching.maximum_events:
                    receipts.extend(self._flush_destination(destination_id, destination))
        if not receipts:
            return RoutingReceipt("QUEUED" if any(self._pending.values()) else "NO_MATCH", ())
        return self._routing_receipt(receipts)

    def close(self) -> RoutingReceipt:
        """Synchronously flush every pending destination in identifier order."""
        receipts: list[DeliveryReceipt] = []
        for destination_id, destination in sorted(self._config.destinations.items()):
            receipts.extend(self._flush_destination(destination_id, destination))
        if not receipts:
            return RoutingReceipt("NO_MATCH", ())
        return self._routing_receipt(receipts)

    def _flush_destination(
        self, destination_id: str, destination: NotificationDestination
    ) -> tuple[DeliveryReceipt, ...]:
        batch = tuple(self._pending.pop(destination_id, ()))
        self._deadlines.pop(destination_id, None)
        if not batch:
            return ()
        now = self._clock_seconds()
        eligible: list[NotificationEvent] = []
        receipts: list[DeliveryReceipt] = []
        for event in batch:
            deduplication_id = self._deduplication_id(destination_id, destination, event)
            previous = self._delivered_at(destination_id, deduplication_id)
            if previous is not None and now - previous < destination.deduplication.window_seconds:
                receipts.append(
                    self._deduplicated_receipt(destination_id, destination, event, deduplication_id)
                )
            else:
                eligible.append(event)
        if not eligible:
            return tuple(receipts)
        try:
            adapter = self._adapters.get(destination_id)
            if adapter is None:
                adapter = self._adapter_factory(destination_id, destination)
                self._adapters[destination_id] = adapter
            deliver_batch = getattr(adapter, "deliver_batch", None)
            if len(eligible) == 1 or not callable(deliver_batch):
                for event in eligible:
                    receipt = adapter.deliver(event)
                    if receipt.outcome == "SUCCESS" and not self._record_successes(
                        destination_id, destination, (event,), now
                    ):
                        receipt = self._deduplication_state_failure(
                            destination_id, destination, event
                        )
                    receipts.append(receipt)
                return tuple(receipts)
            receipt = deliver_batch(tuple(eligible))
            if not isinstance(receipt, DeliveryReceipt):
                raise RuntimeError("notification adapter returned an invalid batch receipt")
            if receipt.outcome == "SUCCESS" and not self._record_successes(
                destination_id, destination, tuple(eligible), now
            ):
                receipt = self._deduplication_state_failure(
                    destination_id, destination, eligible[0]
                )
            receipts.append(receipt)
            return tuple(receipts)
        except (OSError, RuntimeError, ValueError):
            event = batch[0]
            receipts.append(
                DeliveryReceipt(
                    destination_id,
                    destination.adapter,
                    self._event_id(event),
                    self._deduplication_id(destination_id, destination, event),
                    None,
                    0,
                    "FAILURE",
                    "ADAPTER_UNAVAILABLE",
                    None,
                )
            )
            return tuple(receipts)

    @staticmethod
    def _routing_receipt(receipts: list[DeliveryReceipt]) -> RoutingReceipt:
        successful = sum(receipt.outcome in {"SUCCESS", "DEDUPLICATED"} for receipt in receipts)
        if successful == len(receipts):
            return RoutingReceipt("ALL_SUCCESS", tuple(receipts))
        if successful:
            return RoutingReceipt("PARTIAL_FAILURE", tuple(receipts))
        return RoutingReceipt("ALL_FAILURE", tuple(receipts))

    def _deliver_to_destination(
        self,
        destination_id: str,
        destination: NotificationDestination,
        event: NotificationEvent,
    ) -> DeliveryReceipt:
        deduplication_id = self._deduplication_id(destination_id, destination, event)
        now = self._clock_seconds()
        previous = self._delivered_at(destination_id, deduplication_id)
        if previous is not None and now - previous < destination.deduplication.window_seconds:
            return self._deduplicated_receipt(destination_id, destination, event, deduplication_id)
        try:
            adapter = self._adapters.get(destination_id)
            if adapter is None:
                adapter = self._adapter_factory(destination_id, destination)
                self._adapters[destination_id] = adapter
            receipt = adapter.deliver(event)
        except (OSError, RuntimeError, ValueError):
            return DeliveryReceipt(
                destination_id,
                destination.adapter,
                self._event_id(event),
                deduplication_id,
                None,
                0,
                "FAILURE",
                "ADAPTER_UNAVAILABLE",
                None,
            )
        if receipt.outcome == "SUCCESS":
            if not self._record_successes(destination_id, destination, (event,), now):
                return self._deduplication_state_failure(destination_id, destination, event)
        return receipt

    def _delivered_at(self, destination_id: str, deduplication_id: str) -> int | None:
        if self._deduplication_store is not None:
            return self._deduplication_store.notification_delivered_at(
                destination_id, deduplication_id
            )
        return self._deduplicated_at.get((destination_id, deduplication_id))

    def _record_successes(
        self,
        destination_id: str,
        destination: NotificationDestination,
        events: tuple[NotificationEvent, ...],
        delivered_at: int,
    ) -> bool:
        records = tuple(
            NotificationDedupRecord(
                event.run_id,
                destination_id,
                self._deduplication_id(destination_id, destination, event),
                delivered_at,
            )
            for event in events
        )
        try:
            if self._deduplication_store is not None:
                self._deduplication_store.store_notification_deliveries(records)
            else:
                for record in records:
                    self._deduplicated_at[(record.destination_id, record.deduplication_id)] = (
                        delivered_at
                    )
        except Exception:
            return False
        return True

    def _deduplicated_receipt(
        self,
        destination_id: str,
        destination: NotificationDestination,
        event: NotificationEvent,
        deduplication_id: str,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            destination_id,
            destination.adapter,
            self._event_id(event),
            deduplication_id,
            None,
            0,
            "DEDUPLICATED",
            "DEDUPLICATION_WINDOW",
            None,
        )

    def _deduplication_state_failure(
        self,
        destination_id: str,
        destination: NotificationDestination,
        event: NotificationEvent,
    ) -> DeliveryReceipt:
        return DeliveryReceipt(
            destination_id,
            destination.adapter,
            self._event_id(event),
            self._deduplication_id(destination_id, destination, event),
            None,
            0,
            "FAILURE",
            "DEDUPLICATION_STATE_UNAVAILABLE",
            None,
        )

    @staticmethod
    def _event_id(event: NotificationEvent) -> str:
        return NotificationRouter._hash({"event_type": event.event_type, "run_id": event.run_id})

    @staticmethod
    def _deduplication_id(
        destination_id: str,
        destination: NotificationDestination,
        event: NotificationEvent,
    ) -> str:
        values = {
            key: event.payload[key] if key in event.payload else getattr(event, key)
            for key in destination.deduplication.key_fields
        }
        return NotificationRouter._hash({"destination_id": destination_id, "key": values})

    @staticmethod
    def _hash(value: object) -> str:
        encoded = json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        return sha256(encoded).hexdigest()
