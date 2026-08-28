from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from pathlib import Path

from smc_ict.adapters.persistence.sqlite import SQLiteRepository
from smc_ict.application.notifications import NotificationRouter
from smc_ict.application.ports import (
    DeliveryReceipt,
    NotificationDedupRecord,
    NotificationEvent,
    RunRecord,
)
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


def _repository(path: Path) -> SQLiteRepository:
    repository = SQLiteRepository(path)
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
    return repository


def _destination(*, window: int = 300, maximum: int = 2) -> NotificationDestination:
    return NotificationDestination(
        "generic_webhook",
        True,
        ("decision_found", "no_decision", "run_succeeded"),
        SecretRef("env", "FICTIONAL_HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(window, ("event_type", "run_id", "instrument_id")),
        BatchingConfig(maximum, 10),
        RedactionConfig((), ()),
        "warning",
    )


def _event(event_type: str, instrument_id: str | None = None) -> NotificationEvent:
    return NotificationEvent(event_type, "run", instrument_id, "strategy", 1, {})


class _Adapter:
    adapter_id = "generic_webhook"

    def __init__(
        self, destination_id: str, calls: list[tuple[str, tuple[str, ...]]], fail: bool
    ) -> None:
        self.destination_id = destination_id
        self.calls = calls
        self.fail = fail

    def _receipt(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        self.calls.append((self.destination_id, tuple(event.event_type for event in events)))
        outcome = "FAILURE" if self.fail else "SUCCESS"
        return DeliveryReceipt(
            self.destination_id,
            self.adapter_id,
            "event",
            "dedupe",
            "batch" if len(events) > 1 else None,
            1,
            outcome,
            "REMOTE_FAILURE" if self.fail else None,
            500 if self.fail else 204,
        )

    def deliver(self, event: NotificationEvent) -> DeliveryReceipt:
        return self._receipt((event,))

    def deliver_batch(self, events: tuple[NotificationEvent, ...]) -> DeliveryReceipt:
        return self._receipt(events)


def _router(
    repository: SQLiteRepository,
    destinations: dict[str, NotificationDestination],
    calls: list[tuple[str, tuple[str, ...]]],
    *,
    now: int,
    failing: frozenset[str] = frozenset(),
) -> NotificationRouter:
    return NotificationRouter(
        NotificationConfig(True, frozen_mapping(destinations)),
        adapter_factory=lambda destination_id, _destination: _Adapter(
            destination_id, calls, destination_id in failing
        ),
        clock_seconds=lambda: now,
        deduplication_store=repository,
    )


def test_mixed_duplicate_and_new_batch_delivers_only_novel_events(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination()
    duplicate = _event("decision_found", "BTC-USDT-PERP")
    novel = (_event("no_decision", "ETH-USDT-PERP"), _event("run_succeeded"))

    _router(repository, {"only": destination}, calls, now=100).deliver(duplicate)
    receipt = _router(
        SQLiteRepository(database), {"only": destination}, calls, now=100
    ).deliver_all((duplicate, *novel))

    assert receipt.outcome == "ALL_SUCCESS"
    assert calls == [
        ("only", ("decision_found",)),
        ("only", ("no_decision", "run_succeeded")),
    ]
    assert [item.outcome for item in receipt.receipts] == ["DEDUPLICATED", "SUCCESS"]


def test_durable_deduplication_is_destination_scoped(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(maximum=1)
    event = _event("run_succeeded")

    _router(repository, {"first": destination}, calls, now=100).deliver(event)
    repeated = _router(
        SQLiteRepository(database),
        {"first": destination, "second": destination},
        calls,
        now=100,
    ).deliver(event)

    assert calls == [("first", ("run_succeeded",)), ("second", ("run_succeeded",))]
    assert [item.outcome for item in repeated.receipts] == ["DEDUPLICATED", "SUCCESS"]


def test_partial_failure_persists_only_successful_destination_outcomes(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(maximum=1)
    event = _event("run_succeeded")

    failed = _router(
        repository,
        {"bad": destination, "good": destination},
        calls,
        now=100,
        failing=frozenset({"bad"}),
    ).deliver(event)
    retried = _router(
        SQLiteRepository(database),
        {"bad": destination, "good": destination},
        calls,
        now=100,
    ).deliver(event)

    assert failed.outcome == "PARTIAL_FAILURE"
    assert calls == [
        ("bad", ("run_succeeded",)),
        ("good", ("run_succeeded",)),
        ("bad", ("run_succeeded",)),
    ]
    assert [item.outcome for item in retried.receipts] == ["SUCCESS", "DEDUPLICATED"]


def test_expired_durable_record_delivers_again_at_window_boundary(tmp_path: Path) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(window=300, maximum=1)
    event = _event("run_succeeded")

    _router(repository, {"only": destination}, calls, now=100).deliver(event)
    expired = _router(SQLiteRepository(database), {"only": destination}, calls, now=400).deliver(
        event
    )

    assert expired.receipts[0].outcome == "SUCCESS"
    assert calls == [("only", ("run_succeeded",)), ("only", ("run_succeeded",))]


def test_sqlite_dedup_json_is_redacted_valid_and_preserves_exact_five_tables(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(maximum=1)

    _router(repository, {"only": destination}, calls, now=100).deliver(_event("run_succeeded"))

    with sqlite3.connect(database) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        encoded = connection.execute(
            "SELECT notification_dedup_json FROM runs WHERE run_id='run'"
        ).fetchone()[0]
    records = json.loads(encoded)

    assert tables == {"candles_1m", "sync_state", "runs", "observations", "decisions"}
    assert len(records) == 1
    assert set(records[0]) == {
        "destination_id",
        "deduplication_id",
        "delivered_at_seconds",
    }
    assert records[0]["destination_id"] == "only"
    assert records[0]["delivered_at_seconds"] == 100
    assert len(records[0]["deduplication_id"]) == 64
    assert "FICTIONAL_HOOK" not in encoded


def test_fresh_manual_or_scheduler_child_suppresses_durable_duplicate(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(maximum=1)
    event = _event("run_succeeded")
    _router(repository, {"only": destination}, calls, now=100).deliver(event)
    marker = tmp_path / "unexpected-delivery"
    probe = """
import sys
from pathlib import Path
from smc_ict.adapters.persistence.sqlite import SQLiteRepository
from smc_ict.application.notifications import NotificationRouter
from smc_ict.application.ports import DeliveryReceipt, NotificationEvent
from smc_ict.configuration.models import (
    BatchingConfig, DeduplicationConfig, NotificationConfig,
    NotificationDestination, RedactionConfig, RetryConfig,
    SecretRef, frozen_mapping,
)
database, marker = sys.argv[1:]
destination = NotificationDestination(
    'generic_webhook', True, ('run_succeeded',),
    SecretRef('env', 'FICTIONAL_HOOK'), 1, RetryConfig(1, ()),
    DeduplicationConfig(300, ('event_type', 'run_id', 'instrument_id')),
    BatchingConfig(1, 10), RedactionConfig((), ()), 'warning'
)
class Adapter:
    adapter_id = 'generic_webhook'
    def deliver(self, event):
        Path(marker).write_text('called', encoding='utf-8')
        return DeliveryReceipt(
            'only', self.adapter_id, 'event', 'dedupe', None,
            1, 'SUCCESS', None, 204
        )
router = NotificationRouter(
    NotificationConfig(True, frozen_mapping({'only': destination})),
    adapter_factory=lambda *_args: Adapter(), clock_seconds=lambda: 100,
    deduplication_store=SQLiteRepository(database)
)
receipt = router.deliver(NotificationEvent('run_succeeded', 'run', None, 'strategy', 1, {}))
print(receipt.outcome, receipt.receipts[0].outcome)
"""

    child = subprocess.run(
        [sys.executable, "-c", probe, str(database), str(marker)],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "ALL_SUCCESS DEDUPLICATED"
    assert not marker.exists()


def test_state_write_failure_returns_bounded_failure_after_successful_delivery() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    class BrokenStore:
        def notification_delivered_at(
            self, destination_id: str, deduplication_id: str
        ) -> int | None:
            del destination_id, deduplication_id
            return None

        def store_notification_deliveries(
            self, records: tuple[NotificationDedupRecord, ...]
        ) -> None:
            del records
            raise OSError("fictional endpoint payload must not escape")

    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"only": _destination(maximum=1)})),
        adapter_factory=lambda destination_id, _destination: _Adapter(destination_id, calls, False),
        clock_seconds=lambda: 100,
        deduplication_store=BrokenStore(),
    )

    receipt = router.deliver(_event("run_succeeded"))

    assert calls == [("only", ("run_succeeded",))]
    assert receipt.outcome == "ALL_FAILURE"
    assert receipt.receipts[0].reason_code == "DEDUPLICATION_STATE_UNAVAILABLE"
    assert "payload" not in repr(receipt)
