from __future__ import annotations

from pathlib import Path
from typing import Never


def test_router_fans_out_in_destination_id_order_with_independent_filters() -> None:
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.application.ports import DeliveryReceipt, NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    def destination(events: tuple[str, ...]) -> NotificationDestination:
        return NotificationDestination(
            "generic_webhook",
            True,
            events,
            SecretRef("env", "WEBHOOK_URL"),
            1,
            RetryConfig(1, ()),
            DeduplicationConfig(60, ("event_type", "run_id")),
            BatchingConfig(1, 1),
            RedactionConfig((), ()),
            "warning",
        )

    delivered: list[str] = []

    class Adapter:
        adapter_id = "generic_webhook"

        def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
            delivered.append(self.destination_id)
            return DeliveryReceipt(
                self.destination_id,
                self.adapter_id,
                event.run_id,
                event.run_id,
                None,
                1,
                "SUCCESS",
                None,
                204,
            )

    def adapter_factory(destination_id: str, _destination: NotificationDestination) -> Adapter:
        adapter = Adapter()
        adapter.destination_id = destination_id
        return adapter

    config = NotificationConfig(
        True,
        frozen_mapping(
            {
                "discord_2": destination(("decision_found",)),
                "discord_1": destination(("run_started", "decision_found")),
            }
        ),
    )
    router = NotificationRouter(config, adapter_factory=adapter_factory, clock_seconds=lambda: 1)

    outcome = router.deliver(
        NotificationEvent("decision_found", "run_1", None, "strategy", 1, {"signal": "x"})
    )

    assert delivered == ["discord_1", "discord_2"]
    assert outcome.outcome == "ALL_SUCCESS"
    assert [receipt.destination_id for receipt in outcome.receipts] == ["discord_1", "discord_2"]


def test_router_deduplicates_per_destination_without_suppressing_other_destinations() -> None:
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.application.ports import DeliveryReceipt, NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    def destination(window_seconds: int) -> NotificationDestination:
        return NotificationDestination(
            "generic_webhook",
            True,
            ("decision_found",),
            SecretRef("env", "WEBHOOK_URL"),
            1,
            RetryConfig(1, ()),
            DeduplicationConfig(window_seconds, ("event_type", "run_id")),
            BatchingConfig(1, 1),
            RedactionConfig((), ()),
            "warning",
        )

    calls: list[str] = []

    class Adapter:
        adapter_id = "generic_webhook"

        def __init__(self, destination_id: str) -> None:
            self.destination_id = destination_id

        def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
            calls.append(self.destination_id)
            return DeliveryReceipt(
                self.destination_id,
                self.adapter_id,
                event.run_id,
                event.run_id,
                None,
                1,
                "SUCCESS",
                None,
                204,
            )

    now = [100]
    config = NotificationConfig(
        True,
        frozen_mapping({"first": destination(60), "second": destination(1)}),
    )
    router = NotificationRouter(
        config,
        adapter_factory=lambda destination_id, _destination: Adapter(destination_id),
        clock_seconds=lambda: now[0],
    )
    event = NotificationEvent("decision_found", "run_1", None, "strategy", 1, {})

    assert router.deliver(event).outcome == "ALL_SUCCESS"
    now[0] = 102
    repeated = router.deliver(event)

    assert calls == ["first", "second", "second"]
    assert repeated.outcome == "ALL_SUCCESS"
    assert [receipt.outcome for receipt in repeated.receipts] == ["DEDUPLICATED", "SUCCESS"]


def test_router_does_not_construct_an_adapter_for_a_disabled_destination() -> None:
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    disabled = NotificationDestination(
        "generic_webhook",
        False,
        (),
        SecretRef("env", "UNAVAILABLE_DISABLED_HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(1, 1),
        RedactionConfig((), ()),
        "warning",
    )
    config = NotificationConfig(True, frozen_mapping({"disabled": disabled}))

    def adapter_factory(_destination_id: str, _destination: NotificationDestination) -> Never:
        raise AssertionError("disabled destination adapter must not be constructed")

    NotificationRouter(config, adapter_factory=adapter_factory, clock_seconds=lambda: 1)


def test_router_isolates_enabled_destination_construction_failures() -> None:
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.application.ports import DeliveryReceipt, NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    def destination(secret: str) -> NotificationDestination:
        return NotificationDestination(
            "generic_webhook",
            True,
            ("run_succeeded",),
            SecretRef("env", secret),
            1,
            RetryConfig(1, ()),
            DeduplicationConfig(1, ("event_type", "run_id")),
            BatchingConfig(1, 1),
            RedactionConfig((), ()),
            "warning",
        )

    delivered: list[str] = []

    class Adapter:
        adapter_id = "generic_webhook"

        def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
            delivered.append(event.run_id)
            return DeliveryReceipt(
                "good", self.adapter_id, "event", "dedupe", None, 1, "SUCCESS", None, 204
            )

    def factory(destination_id: str, _destination: NotificationDestination) -> Adapter:
        if destination_id == "bad":
            raise ValueError("notification endpoint is unavailable")
        return Adapter()

    router = NotificationRouter(
        NotificationConfig(
            True, frozen_mapping({"bad": destination("BAD"), "good": destination("GOOD")})
        ),
        adapter_factory=factory,
        clock_seconds=lambda: 1,
    )

    receipt = router.deliver(NotificationEvent("run_succeeded", "run", None, "strategy", 1, {}))

    assert delivered == ["run"]
    assert receipt.outcome == "PARTIAL_FAILURE"
    assert [(item.destination_id, item.reason_code) for item in receipt.receipts] == [
        ("bad", "ADAPTER_UNAVAILABLE"),
        ("good", None),
    ]


def test_router_batches_multiple_events_per_destination_in_order() -> None:
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.application.ports import DeliveryReceipt, NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("decision_found", "run_succeeded"),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(2, 10),
        RedactionConfig((), ()),
        "warning",
    )
    batches: list[tuple[str, ...]] = []

    class Adapter:
        adapter_id = "generic_webhook"

        def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
            raise AssertionError("multi-event routing must use batching")

        def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
            batches.append(tuple(event.event_type for event in events))
            return DeliveryReceipt(
                "only", self.adapter_id, "events", "dedupe", "batch", 1, "SUCCESS", None, 204
            )

    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"only": destination})),
        adapter_factory=lambda _destination_id, _destination: Adapter(),
        clock_seconds=lambda: 20,
    )
    receipt = router.deliver_all(
        (
            NotificationEvent("decision_found", "run", None, "strategy", 1, {}),
            NotificationEvent("run_succeeded", "run", None, "strategy", 1, {}),
        )
    )

    assert receipt.outcome == "ALL_SUCCESS"
    assert batches == [("decision_found", "run_succeeded")]


def test_router_flushes_pending_destination_before_accepting_event_at_deadline() -> None:
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.application.ports import DeliveryReceipt, NotificationEvent
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("decision_found",),
        SecretRef("env", "HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(60, ("event_type", "run_id", "instrument_id")),
        BatchingConfig(3, 10),
        RedactionConfig((), ()),
        "warning",
    )
    now = [100]
    batches: list[tuple[str | None, ...]] = []

    class Adapter:
        adapter_id = "generic_webhook"

        def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
            batches.append((event.instrument_id,))
            return DeliveryReceipt(
                "only", self.adapter_id, "event", "dedupe", None, 1, "SUCCESS", None, 204
            )

        def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
            batches.append(tuple(event.instrument_id for event in events))
            return DeliveryReceipt(
                "only", self.adapter_id, "events", "dedupe", "batch", 1, "SUCCESS", None, 204
            )

    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"only": destination})),
        adapter_factory=lambda _destination_id, _destination: Adapter(),
        clock_seconds=lambda: now[0],
    )
    first = NotificationEvent("decision_found", "run", "BTC-USDT-PERP", "strategy", 1, {})
    second = NotificationEvent("decision_found", "run", "ETH-USDT-PERP", "strategy", 1, {})
    third = NotificationEvent("decision_found", "run", "XRP-USDT-PERP", "strategy", 1, {})

    assert router.deliver_all((first,)).outcome == "QUEUED"
    now[0] = 109
    assert router.deliver_all((second,)).outcome == "QUEUED"
    assert batches == []
    now[0] = 110
    assert router.deliver_all((third,)).outcome == "ALL_SUCCESS"
    assert batches == [("BTC-USDT-PERP", "ETH-USDT-PERP")]

    router.close()
    assert batches == [("BTC-USDT-PERP", "ETH-USDT-PERP"), ("XRP-USDT-PERP",)]


def test_repeated_two_event_batch_is_deduplicated_by_a_fresh_router_using_sqlite(
    tmp_path: Path,
) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.notifications import NotificationRouter
    from smc_ict.application.ports import DeliveryReceipt, NotificationEvent, RunRecord
    from smc_ict.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    repository.store_run(
        RunRecord(
            "run",
            "RUNNING",
            1,
            None,
            "strategy",
            "1",
            "0" * 64,
            "provider",
            "LINEAR_PERPETUAL",
            "1" * 64,
            "2" * 40,
            0,
            59_999,
            "3" * 64,
            None,
        )
    )
    destination = NotificationDestination(
        "generic_webhook",
        True,
        ("decision_found", "run_succeeded"),
        SecretRef("env", "FICTIONAL_HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(300, ("event_type", "run_id", "instrument_id")),
        BatchingConfig(2, 10),
        RedactionConfig((), ()),
        "warning",
    )
    config = NotificationConfig(True, frozen_mapping({"only": destination}))
    calls: list[tuple[str, ...]] = []

    class Adapter:
        adapter_id = "generic_webhook"

        def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
            raise AssertionError("two events must use the batch path")

        def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
            calls.append(tuple(event.event_type for event in events))
            return DeliveryReceipt(
                "only", self.adapter_id, "events", "batch", "batch", 1, "SUCCESS", None, 204
            )

    events = (
        NotificationEvent("decision_found", "run", "BTC-USDT-PERP", "strategy", 1, {"signal": "x"}),
        NotificationEvent("run_succeeded", "run", None, "strategy", 1, {}),
    )

    first = NotificationRouter(
        config,
        adapter_factory=lambda _destination_id, _destination: Adapter(),
        clock_seconds=lambda: 100,
        deduplication_store=repository,
    ).deliver_all(events)
    repeated = NotificationRouter(
        config,
        adapter_factory=lambda _destination_id, _destination: Adapter(),
        clock_seconds=lambda: 100,
        deduplication_store=SQLiteRepository(tmp_path / "runtime.sqlite3"),
    ).deliver_all(events)

    assert first.outcome == repeated.outcome == "ALL_SUCCESS"
    assert calls == [("decision_found", "run_succeeded")]
    assert [receipt.outcome for receipt in repeated.receipts] == [
        "DEDUPLICATED",
        "DEDUPLICATED",
    ]
