"""Exact receipt and scheduler-outcome allowlists shared across process boundaries."""

from __future__ import annotations

RUN_RECEIPT_STATUSES = frozenset(
    {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS", "FAILED", "OVERLAP_SKIPPED"}
)
RUN_RECEIPT_SUCCESS_STATUSES = frozenset({"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"})
RUN_RECEIPT_TRIGGERS = frozenset({"manual", "scheduled"})
SCHEDULER_FAILURE_OUTCOMES = frozenset(
    {"FAILED", "OVERLAP_SKIPPED", "MAXIMUM_RUNTIME", "SCHEDULER_SHUTDOWN", "PROCESS_RESTART"}
)


def is_canonical_run_id(value: object) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )
