from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from trading_research.adapters.persistence.sqlite import SQLiteRepository
from trading_research.application.notifications import NotificationRouter
from trading_research.application.ports import (
    DecisionRecord,
    DeliveryReceipt,
    NotificationDeduplicationStore,
    NotificationDedupRecord,
    NotificationDeliveryRecord,
    NotificationEvent,
    ObservationRecord,
    RunRecord,
)
from trading_research.configuration.models import (
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
            None,
            "2" * 64,
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
            "HTTP_500" if self.fail else None,
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
        ("only", ("no_decision",)),
        ("only", ("run_succeeded",)),
    ]
    assert [item.outcome for item in receipt.receipts] == [
        "DEDUPLICATED",
        "SUCCESS",
        "SUCCESS",
    ]


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


def test_restart_retries_failed_delivery_then_recovers_successful_deduplication(
    tmp_path: Path,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(maximum=1)
    event = _event("run_succeeded")

    first = _router(
        repository,
        {"only": destination},
        calls,
        now=100,
        failing=frozenset({"only"}),
    ).deliver(event)
    restarted = SQLiteRepository(database)
    second = _router(restarted, {"only": destination}, calls, now=101).deliver(event)
    third = _router(SQLiteRepository(database), {"only": destination}, calls, now=102).deliver(
        event
    )

    outcomes = SQLiteRepository(database).load_notification_outcomes("run")
    assert first.receipts[0].outcome == "FAILURE"
    assert second.receipts[0].outcome == "SUCCESS"
    assert third.receipts[0].outcome == "DEDUPLICATED"
    assert calls == [
        ("only", ("run_succeeded",)),
        ("only", ("run_succeeded",)),
    ]
    assert [(item.outcome, item.reason_code) for item in outcomes] == [
        ("FAILURE", "HTTP_500"),
        ("SUCCESS", None),
    ]


def test_failed_delivery_persists_a_bounded_redacted_outcome(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []

    with caplog.at_level("INFO", logger="trading_research.application.notifications"):
        receipt = _router(
            repository,
            {"bad": _destination(maximum=1)},
            calls,
            now=123,
            failing=frozenset({"bad"}),
        ).deliver(_event("run_succeeded"))

    with sqlite3.connect(database) as connection:
        encoded = connection.execute(
            "SELECT notification_outcomes_json FROM runs WHERE run_id='run'"
        ).fetchone()[0]
    outcomes = json.loads(encoded)

    assert receipt.outcome == "ALL_FAILURE"
    assert outcomes == [
        {
            "adapter_id": "generic_webhook",
            "attempted_at_seconds": 123,
            "attempts": 1,
            "destination_id": "bad",
            "outcome": "FAILURE",
            "reason_code": "HTTP_500",
            "status_code": 500,
        }
    ]
    assert len(encoded) <= 1_000
    assert "FICTIONAL_HOOK" not in encoded
    records = [record for record in caplog.records if record.msg == "notification_delivery_outcome"]
    assert len(records) == 1
    assert records[0].destination_id == "bad"
    assert records[0].adapter_id == "generic_webhook"
    assert records[0].outcome == "FAILURE"
    assert records[0].reason_code == "HTTP_500"
    assert records[0].status_code == 500
    assert records[0].attempts == 1
    assert "FICTIONAL_HOOK" not in caplog.text


def test_adapter_construction_error_is_mapped_before_persistence_and_logging(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    secret_text = "https://discord.invalid/api/webhooks/id/FICTIONAL_SECRET"
    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"discord": _destination(maximum=1)})),
        adapter_factory=lambda *_args: (_ for _ in ()).throw(ValueError(secret_text)),
        clock_seconds=lambda: 789,
        deduplication_store=repository,
    )

    with caplog.at_level("INFO", logger="trading_research.application.notifications"):
        receipt = router.deliver(_event("run_succeeded"))

    outcomes = SQLiteRepository(database).load_notification_outcomes("run")
    assert receipt.receipts[0].reason_code == "ADAPTER_UNAVAILABLE"
    assert [(item.attempts, item.reason_code, item.status_code) for item in outcomes] == [
        (0, "ADAPTER_UNAVAILABLE", None)
    ]
    assert secret_text not in caplog.text
    assert secret_text not in repr(outcomes)


def test_successful_delivery_persists_redacted_outcome_and_dedup_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    calls: list[tuple[str, tuple[str, ...]]] = []

    with caplog.at_level("INFO", logger="trading_research.application.notifications"):
        receipt = _router(repository, {"good": _destination(maximum=1)}, calls, now=456).deliver(
            _event("run_succeeded")
        )

    with sqlite3.connect(database) as connection:
        encoded_outcomes, encoded_dedup = connection.execute(
            "SELECT notification_outcomes_json,notification_dedup_json FROM runs WHERE run_id='run'"
        ).fetchone()

    assert receipt.outcome == "ALL_SUCCESS"
    assert json.loads(encoded_outcomes) == [
        {
            "adapter_id": "generic_webhook",
            "attempted_at_seconds": 456,
            "attempts": 1,
            "destination_id": "good",
            "outcome": "SUCCESS",
            "reason_code": None,
            "status_code": 204,
        }
    ]
    assert len(json.loads(encoded_dedup)) == 1
    records = [record for record in caplog.records if record.msg == "notification_delivery_outcome"]
    assert len(records) == 1
    assert records[0].levelname == "INFO"
    assert records[0].outcome == "SUCCESS"


def _valid_retained_outcome() -> dict[str, object]:
    return {
        "destination_id": "retained",
        "adapter_id": "generic_webhook",
        "attempted_at_seconds": 100,
        "attempts": 1,
        "outcome": "SUCCESS",
        "reason_code": None,
        "status_code": 204,
    }


def _private_marker() -> str:
    return "PRIVATE" + "_RETAINED_VALUE"


def _malformed_retained_outcome(case: str) -> str:
    valid = _valid_retained_outcome()
    if case == "malformed-json":
        return "{"
    if case == "non-list":
        return "{}"
    if case == "extra-field":
        return json.dumps([{**valid, "end" + "point": _private_marker()}])
    if case == "wrong-type":
        return json.dumps([{**valid, "attempts": "1"}])
    if case == "malformed-object":
        return json.dumps(["not-an-outcome"])
    if case == "missing-field":
        del valid["adapter_id"]
        return json.dumps([valid])
    if case == "unsupported-outcome":
        return json.dumps([{**valid, "outcome": "UNKNOWN"}])
    if case == "unsupported-reason":
        return json.dumps([{**valid, "reason_code": "UNKNOWN"}])
    if case == "unsupported-status":
        return json.dumps([{**valid, "status_code": 500}])
    if case == "invalid-attempts":
        return json.dumps([{**valid, "attempts": 6}])
    if case == "invalid-timestamp":
        return json.dumps([{**valid, "attempted_at_seconds": -1}])
    if case == "over-bound":
        return json.dumps([valid] * 101)
    raise AssertionError("unknown malformed outcome fixture")


@pytest.mark.parametrize(
    "case",
    (
        "malformed-json",
        "non-list",
        "extra-field",
        "wrong-type",
        "malformed-object",
        "missing-field",
        "unsupported-outcome",
        "unsupported-reason",
        "unsupported-status",
        "invalid-attempts",
        "invalid-timestamp",
        "over-bound",
    ),
)
def test_outcome_append_rejects_malformed_retained_state_without_changing_row(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    case: str,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    encoded = _malformed_retained_outcome(case)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE runs SET notification_outcomes_json=? WHERE run_id='run'", (encoded,)
        )
        before = connection.execute(
            "SELECT CAST(notification_outcomes_json AS BLOB) FROM runs WHERE run_id='run'"
        ).fetchone()[0]

    addition = NotificationDeliveryRecord(
        "run", "new", "generic_webhook", 101, 1, "SUCCESS", None, 204
    )
    with (
        caplog.at_level("INFO"),
        pytest.raises(RuntimeError, match="^invalid notification outcome state$"),
    ):
        repository.store_notification_outcomes((addition,))

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT CAST(notification_outcomes_json AS BLOB) FROM runs WHERE run_id='run'"
        ).fetchone()[0]
    assert sha256(after).digest() == sha256(before).digest()
    assert _private_marker() not in caplog.text


def test_malformed_retained_outcome_returns_bounded_receipt_without_disclosure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    encoded = _malformed_retained_outcome("extra-field")
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE runs SET notification_outcomes_json=? WHERE run_id='run'", (encoded,)
        )
        before = connection.execute(
            "SELECT CAST(notification_outcomes_json AS BLOB) FROM runs WHERE run_id='run'"
        ).fetchone()[0]
    calls: list[tuple[str, tuple[str, ...]]] = []

    with caplog.at_level("INFO", logger="trading_research.application.notifications"):
        receipt = _router(repository, {"only": _destination(maximum=1)}, calls, now=101).deliver(
            _event("run_succeeded")
        )

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT CAST(notification_outcomes_json AS BLOB) FROM runs WHERE run_id='run'"
        ).fetchone()[0]
    assert receipt.outcome == "ALL_FAILURE"
    assert receipt.receipts[0].reason_code == "DEDUPLICATION_STATE_UNAVAILABLE"
    assert sha256(after).digest() == sha256(before).digest()
    assert _private_marker() not in caplog.text
    assert _private_marker() not in repr(receipt)


def test_valid_outcome_append_retains_only_the_newest_hundred_records(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "runtime.sqlite3")
    records = tuple(
        NotificationDeliveryRecord(
            "run", "only", "generic_webhook", attempted_at, 1, "SUCCESS", None, 204
        )
        for attempted_at in range(101)
    )

    repository.store_notification_outcomes(records)

    retained = repository.load_notification_outcomes("run")
    assert len(retained) == 100
    assert [record.attempted_at_seconds for record in retained] == list(range(1, 101))


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
from trading_research.adapters.persistence.sqlite import SQLiteRepository
from trading_research.application.notifications import NotificationRouter
from trading_research.application.ports import DeliveryReceipt, NotificationEvent
from trading_research.configuration.models import (
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


def test_success_state_write_failure_never_persists_success_without_deduplication() -> None:
    calls: list[tuple[str, tuple[str, ...]]] = []

    class BrokenStore:
        def __init__(self) -> None:
            self.outcomes: list[NotificationDeliveryRecord] = []
            self.atomic_calls = 0

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

        def store_notification_outcomes(
            self, records: tuple[NotificationDeliveryRecord, ...]
        ) -> None:
            self.outcomes.extend(records)

        def store_successful_notification_delivery(
            self,
            outcome: NotificationDeliveryRecord,
            dedup_records: tuple[NotificationDedupRecord, ...],
        ) -> None:
            del outcome, dedup_records
            self.atomic_calls += 1
            raise OSError("fictional endpoint payload must not escape")

    store = BrokenStore()

    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"only": _destination(maximum=1)})),
        adapter_factory=lambda destination_id, _destination: _Adapter(destination_id, calls, False),
        clock_seconds=lambda: 100,
        deduplication_store=store,
    )

    receipt = router.deliver(_event("run_succeeded"))

    assert calls == [("only", ("run_succeeded",))]
    assert receipt.outcome == "ALL_FAILURE"
    assert receipt.receipts[0].reason_code == "DEDUPLICATION_STATE_UNAVAILABLE"
    assert store.atomic_calls == 1
    assert store.outcomes == []
    assert "payload" not in repr(receipt)


class _RecordingStore:
    def __init__(self, *, fail_atomic: bool = False) -> None:
        self.fail_atomic = fail_atomic
        self.atomic_calls: list[
            tuple[NotificationDeliveryRecord, tuple[NotificationDedupRecord, ...]]
        ] = []

    def notification_delivered_at(self, destination_id: str, deduplication_id: str) -> int | None:
        del destination_id, deduplication_id
        return None

    def store_notification_deliveries(self, records: tuple[NotificationDedupRecord, ...]) -> None:
        del records
        raise AssertionError("router used the legacy dedup-only path")

    def store_notification_outcomes(self, records: tuple[NotificationDeliveryRecord, ...]) -> None:
        del records
        raise AssertionError("router used the outcome-only path for success")

    def store_successful_notification_delivery(
        self,
        outcome: NotificationDeliveryRecord,
        dedup_records: tuple[NotificationDedupRecord, ...],
    ) -> None:
        self.atomic_calls.append((outcome, dedup_records))
        if self.fail_atomic:
            raise OSError("PRIVATE_ATOMIC_FAILURE")


@pytest.mark.parametrize(
    "events",
    [(_event("run_succeeded"),), (_event("decision_found"), _event("no_decision"))],
)
def test_router_submits_each_success_receipt_once_through_atomic_store(
    events: tuple[NotificationEvent, ...],
) -> None:
    store = _RecordingStore()
    calls: list[tuple[str, tuple[str, ...]]] = []
    destination = _destination(maximum=len(events))
    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"only": destination})),
        adapter_factory=lambda destination_id, _destination: _Adapter(destination_id, calls, False),
        clock_seconds=lambda: 100,
        deduplication_store=store,
    )

    receipt = router.deliver(events[0]) if len(events) == 1 else router.deliver_all(events)

    assert isinstance(store, NotificationDeduplicationStore)
    assert receipt.outcome == "ALL_SUCCESS"
    assert len(store.atomic_calls) == 1
    outcome, dedup_records = store.atomic_calls[0]
    assert outcome.outcome == "SUCCESS"
    assert outcome.run_id == "run"
    assert len(dedup_records) == len(events)
    assert {record.destination_id for record in dedup_records} == {"only"}


def _successful_evidence(
    *, count: int = 1, destination_id: str = "only", now: int = 100
) -> tuple[NotificationDeliveryRecord, tuple[NotificationDedupRecord, ...]]:
    outcome = NotificationDeliveryRecord(
        "run", destination_id, "generic_webhook", now, 1, "SUCCESS", None, 204
    )
    dedup_records = tuple(
        NotificationDedupRecord("run", destination_id, f"{index:064x}", now)
        for index in range(1, count + 1)
    )
    return outcome, dedup_records


@pytest.mark.parametrize(
    "malformed_column", ["notification_outcomes_json", "notification_dedup_json"]
)
def test_atomic_success_prevalidates_retained_json_before_writing_either_column(
    tmp_path: Path, malformed_column: str
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE runs SET {malformed_column}='{{' WHERE run_id='run'")
        before = connection.execute(
            "SELECT notification_outcomes_json,notification_dedup_json FROM runs WHERE run_id='run'"
        ).fetchone()
    outcome, dedup_records = _successful_evidence()

    with pytest.raises(RuntimeError, match="^invalid notification .* state$"):
        repository.store_successful_notification_delivery(outcome, dedup_records)

    with sqlite3.connect(database) as connection:
        after = connection.execute(
            "SELECT notification_outcomes_json,notification_dedup_json FROM runs WHERE run_id='run'"
        ).fetchone()
    assert after == before


@pytest.mark.parametrize(
    ("outcome", "dedup_records"),
    [
        (*_successful_evidence(),),
        (
            NotificationDeliveryRecord(
                "run", "only", "generic_webhook", 100, 1, "FAILURE", "HTTP_500", 500
            ),
            _successful_evidence()[1],
        ),
        (_successful_evidence()[0], ()),
        (
            _successful_evidence()[0],
            (NotificationDedupRecord("other-run", "only", "1" * 64, 100),),
        ),
    ],
)
def test_atomic_success_validates_additions_before_opening_database(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: NotificationDeliveryRecord,
    dedup_records: tuple[NotificationDedupRecord, ...],
) -> None:
    repository = _repository(tmp_path / "runtime.sqlite3")
    if outcome.outcome == "SUCCESS" and dedup_records and dedup_records[0].run_id == "run":
        invalid = NotificationDedupRecord("run", "only", "invalid", 100)
        dedup_records = (invalid,)
    monkeypatch.setattr(
        repository,
        "_connect",
        lambda: (_ for _ in ()).throw(AssertionError("database opened before validation")),
    )

    with pytest.raises(ValueError):
        repository.store_successful_notification_delivery(outcome, dedup_records)


class _FaultingConnection(sqlite3.Connection):
    phase = ""

    def execute(self, sql, parameters=(), /):
        if self.phase == "first_update" and sql.startswith(
            "UPDATE runs SET notification_outcomes_json"
        ):
            raise OSError("PRIVATE_ATOMIC_FAILURE")
        cursor = super().execute(sql, parameters)
        if self.phase == "between_updates" and sql.startswith(
            "UPDATE runs SET notification_outcomes_json"
        ):
            raise OSError("PRIVATE_ATOMIC_FAILURE")
        if self.phase == "before_commit" and sql.startswith(
            "UPDATE runs SET notification_dedup_json"
        ):
            raise OSError("PRIVATE_ATOMIC_FAILURE")
        return cursor

    def commit(self) -> None:
        if self.phase in {"commit", "rollback_cleanup"}:
            raise OSError("PRIVATE_ATOMIC_FAILURE")
        super().commit()

    def rollback(self) -> None:
        super().rollback()
        if self.phase == "rollback_cleanup":
            raise OSError("PRIVATE_ROLLBACK_FAILURE")

    def close(self) -> None:
        super().close()
        if self.phase == "rollback_cleanup":
            raise OSError("PRIVATE_CLEANUP_FAILURE")


def _faulting_connection(database: Path, phase: str) -> _FaultingConnection:
    connection = sqlite3.connect(
        database, timeout=5, isolation_level=None, factory=_FaultingConnection
    )
    connection.row_factory = sqlite3.Row
    connection.phase = phase
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=FULL")
    return connection


def _seed_and_snapshot_evidence(
    repository: SQLiteRepository,
) -> tuple[RunRecord | None, tuple[ObservationRecord, ...], tuple[DecisionRecord, ...]]:
    observation = ObservationRecord(
        "run", "BTC-USDT-PERP", "signal", "PASS", 1, 1, "confirmed", {"value": "1"}
    )
    decision = DecisionRecord(
        "run", "BTC-USDT-PERP", "NO_TRADE", None, None, None, None, "signal", {}
    )
    repository.store_observations((observation,))
    repository.store_decisions((decision,))
    return (
        repository.load_run("run"),
        repository.load_observations("run"),
        repository.load_decisions("run"),
    )


@pytest.mark.parametrize(
    "phase", ["first_update", "between_updates", "before_commit", "commit", "rollback_cleanup"]
)
@pytest.mark.parametrize("event_count", [1, 2])
def test_sqlite_phase_failure_rolls_back_success_and_all_dedup_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    phase: str,
    event_count: int,
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    evidence_before = _seed_and_snapshot_evidence(repository)
    events = (_event("decision_found"), _event("no_decision"))[:event_count]
    destination = _destination(maximum=event_count)
    calls: list[tuple[str, tuple[str, ...]]] = []
    monkeypatch.setattr(repository, "_connect", lambda: _faulting_connection(database, phase))
    router = NotificationRouter(
        NotificationConfig(True, frozen_mapping({"only": destination})),
        adapter_factory=lambda destination_id, _destination: _Adapter(destination_id, calls, False),
        clock_seconds=lambda: 100,
        deduplication_store=repository,
    )

    with caplog.at_level("INFO", logger="trading_research.application.notifications"):
        receipt = router.deliver(events[0]) if event_count == 1 else router.deliver_all(events)

    restarted = SQLiteRepository(database)
    assert receipt.outcome == "ALL_FAILURE"
    assert receipt.receipts[0].reason_code == "DEDUPLICATION_STATE_UNAVAILABLE"
    assert restarted.load_notification_outcomes("run") == ()
    with sqlite3.connect(database) as connection:
        assert (
            connection.execute(
                "SELECT notification_dedup_json FROM runs WHERE run_id='run'"
            ).fetchone()[0]
            == "[]"
        )
    assert _seed_and_snapshot_evidence(restarted) == evidence_before
    assert "PRIVATE_" not in caplog.text

    retry = _router(restarted, {"only": destination}, calls, now=101)
    recovered = retry.deliver(events[0]) if event_count == 1 else retry.deliver_all(events)
    suppressed = _router(SQLiteRepository(database), {"only": destination}, calls, now=102)
    duplicate = (
        suppressed.deliver(events[0]) if event_count == 1 else suppressed.deliver_all(events)
    )
    assert recovered.outcome == "ALL_SUCCESS"
    assert duplicate.outcome == "ALL_SUCCESS"
    assert all(item.outcome == "DEDUPLICATED" for item in duplicate.receipts)


def test_atomic_persistence_failure_isolated_from_other_destination(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = tmp_path / "runtime.sqlite3"
    repository = _repository(database)
    destination = _destination(maximum=1)
    calls: list[tuple[str, tuple[str, ...]]] = []
    connection_count = 0

    def connect() -> sqlite3.Connection:
        nonlocal connection_count
        connection_count += 1
        phase = "first_update" if connection_count == 2 else ""
        return _faulting_connection(database, phase)

    monkeypatch.setattr(repository, "_connect", connect)
    first = _router(repository, {"bad": destination, "good": destination}, calls, now=100).deliver(
        _event("run_succeeded")
    )
    restarted = SQLiteRepository(database)
    second = _router(restarted, {"bad": destination, "good": destination}, calls, now=101).deliver(
        _event("run_succeeded")
    )

    assert first.outcome == "PARTIAL_FAILURE"
    assert [(item.destination_id, item.outcome) for item in first.receipts] == [
        ("bad", "FAILURE"),
        ("good", "SUCCESS"),
    ]
    assert [(item.destination_id, item.outcome) for item in second.receipts] == [
        ("bad", "SUCCESS"),
        ("good", "DEDUPLICATED"),
    ]


def test_atomic_success_retains_only_newest_hundred_outcomes(tmp_path: Path) -> None:
    repository = _repository(tmp_path / "runtime.sqlite3")
    repository.store_notification_outcomes(
        tuple(
            NotificationDeliveryRecord(
                "run", "only", "generic_webhook", attempted_at, 1, "SUCCESS", None, 204
            )
            for attempted_at in range(100)
        )
    )
    outcome, dedup_records = _successful_evidence(now=100)

    repository.store_successful_notification_delivery(outcome, dedup_records)

    retained = repository.load_notification_outcomes("run")
    assert len(retained) == 100
    assert [record.attempted_at_seconds for record in retained] == list(range(1, 101))
