"""Immutable replay evidence contracts for research-only backtests."""

from __future__ import annotations

from dataclasses import dataclass

from .decisions import Decision


@dataclass(frozen=True, slots=True)
class PipelineStep:
    kind: str
    step_id: str
    state: str
    reason: str
    dependency_hashes: tuple[tuple[str, str], ...]
    output_hash: str | None

    def __post_init__(self) -> None:
        if self.kind not in {"INDICATOR", "DECISION_GATE"}:
            raise ValueError("unknown pipeline step kind")
        allowed = (
            {"PASS", "FAIL", "UNAVAILABLE"}
            if self.kind == "INDICATOR"
            else {"PASS", "REJECT", "UNAVAILABLE", "SKIPPED_AFTER_REJECTION"}
        )
        if self.state not in allowed:
            raise ValueError("unknown pipeline step state")
        if not self.reason:
            raise ValueError("pipeline step reason must not be empty")

    def canonical_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "step_id": self.step_id,
            "state": self.state,
            "reason": self.reason,
            "dependency_hashes": [list(item) for item in self.dependency_hashes],
            "output_hash": self.output_hash,
        }


@dataclass(frozen=True, slots=True)
class PipelineTrace:
    instrument_id: str
    evaluation_time_ms: int
    steps: tuple[PipelineStep, ...]
    first_rejection: str | None
    decision_hash: str

    def canonical_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "evaluation_time_ms": self.evaluation_time_ms,
            "steps": [step.canonical_dict() for step in self.steps],
            "first_rejection": self.first_rejection,
            "decision_hash": self.decision_hash,
        }


@dataclass(frozen=True, slots=True)
class ReplayEvaluation:
    instrument_id: str
    evaluation_time_ms: int
    decision: Decision
    decision_hash: str
    trace: PipelineTrace

    def canonical_dict(self) -> dict[str, object]:
        return {
            "instrument_id": self.instrument_id,
            "evaluation_time_ms": self.evaluation_time_ms,
            "decision": self.decision.canonical_dict(),
            "decision_hash": self.decision_hash,
            "trace": self.trace.canonical_dict(),
        }


@dataclass(frozen=True, slots=True)
class ReplayResult:
    evaluations: tuple[ReplayEvaluation, ...]
    candle_count: int
    data_hash: str

    def canonical_dict(self) -> dict[str, object]:
        return {
            "evaluations": [evaluation.canonical_dict() for evaluation in self.evaluations],
            "candle_count": self.candle_count,
            "data_hash": self.data_hash,
        }
