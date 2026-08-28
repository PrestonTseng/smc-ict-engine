"""Conversions from immutable analysis evidence to existing persistence records."""

from __future__ import annotations

from collections.abc import Sequence

from smc_ict.application.ports.repository import (
    DecisionRecord,
    ObservationRecord,
    Repository,
)
from smc_ict.domain import Decision, Observation, hash_decision, hash_observation


def observation_record(run_id: str, observation: Observation) -> ObservationRecord:
    """Preserve formula provenance inside the exact schema-v1 payload column."""

    return ObservationRecord(
        run_id=run_id,
        instrument_id=observation.instrument_id,
        signal_id=observation.signal_id,
        status=observation.status,
        event_time_ms=observation.event_time_ms,
        known_time_ms=observation.known_time_ms,
        reason=observation.bounded_reason,
        payload={
            "event_type": observation.event_type,
            "direction": observation.direction,
            "state": observation.state,
            "timeframe": observation.timeframe,
            "dependency_ids": observation.dependency_ids,
            "parameter_hash": observation.parameter_hash,
            "source_manifest_ids": observation.source_manifest_ids,
            "payload_schema_version": observation.payload_schema_version,
            "level_text": observation.level_text,
            "lower_text": observation.lower_text,
            "upper_text": observation.upper_text,
            "observation_hash": hash_observation(observation),
            "payload": observation.payload,
        },
    )


def decision_record(run_id: str, decision: Decision) -> DecisionRecord:
    """Preserve a canonical decision hash inside the exact schema-v1 payload column."""

    return DecisionRecord(
        run_id=run_id,
        instrument_id=decision.instrument_id,
        decision_status=decision.status,
        direction=decision.direction,
        entry_text=decision.entry_text,
        stop_text=decision.stop_text,
        target_text=decision.target_text,
        first_failed_signal=decision.first_failed_signal,
        payload={"decision_hash": hash_decision(decision), "payload": decision.payload},
    )


def persist_evidence(
    repository: Repository,
    run_id: str,
    observations: Sequence[Observation],
    decisions: Sequence[Decision],
) -> None:
    """Persist deterministic analysis rows through the existing repository port."""

    observation_rows = tuple(
        observation_record(run_id, observation)
        for observation in sorted(
            observations, key=lambda item: (item.instrument_id, item.signal_id)
        )
    )
    decision_rows = tuple(
        decision_record(run_id, decision)
        for decision in sorted(decisions, key=lambda item: item.instrument_id)
    )
    repository.store_observations(observation_rows)
    repository.store_decisions(decision_rows)
