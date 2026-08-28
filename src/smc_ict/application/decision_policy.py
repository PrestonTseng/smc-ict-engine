"""Generic ordered decision composition over configured observations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from smc_ict.configuration.models import StrategyConfig
from smc_ict.domain import Decision, Observation, hash_observation

from .graph import RunContext


@dataclass(frozen=True, slots=True)
class DecisionSignal:
    signal_id: str
    required: bool
    effect: str
    order: int

    def __post_init__(self) -> None:
        if self.effect not in {"REJECT", "LEVELS"}:
            raise ValueError("decision effect must be REJECT or LEVELS")


def configured_decision_signals(strategy: StrategyConfig) -> tuple[DecisionSignal, ...]:
    """Translate validated strategy order into generic decision-policy inputs."""

    return tuple(
        DecisionSignal(signal.id, signal.required, signal.effect, signal.order)
        for signal in strategy.signals
    )


class OrderedDecisionPlugin:
    """Compose evidence in explicit order without indicator or source conditionals."""

    plugin_id = "project.ordered_decision"

    def __init__(self, signals: tuple[DecisionSignal, ...]) -> None:
        self._signals = tuple(sorted(signals, key=lambda signal: (signal.order, signal.signal_id)))

    def decide(self, context: RunContext, observations: Mapping[str, Observation]) -> Decision:
        ordered_hashes: dict[str, str] = {}
        level_values: tuple[str, str, str, str] | None = None
        for signal in self._signals:
            observation = observations.get(signal.signal_id)
            if observation is not None:
                ordered_hashes[signal.signal_id] = hash_observation(observation)
            if not signal.required:
                continue
            if observation is None or observation.status == "UNAVAILABLE":
                return Decision(
                    instrument_id=context.instrument_id,
                    status="UNAVAILABLE",
                    direction=None,
                    entry_text=None,
                    stop_text=None,
                    target_text=None,
                    first_failed_signal=signal.signal_id,
                    payload={"observation_hashes": ordered_hashes},
                )
            if observation.status == "FAIL":
                return Decision(
                    instrument_id=context.instrument_id,
                    status="NO_TRADE",
                    direction=None,
                    entry_text=None,
                    stop_text=None,
                    target_text=None,
                    first_failed_signal=signal.signal_id,
                    payload={"observation_hashes": ordered_hashes},
                )
            if signal.effect == "LEVELS":
                values = tuple(
                    observation.payload.get(field)
                    for field in ("direction", "entry_text", "stop_text", "target_text")
                )
                if not all(type(value) is str for value in values):
                    return Decision(
                        instrument_id=context.instrument_id,
                        status="UNAVAILABLE",
                        direction=None,
                        entry_text=None,
                        stop_text=None,
                        target_text=None,
                        first_failed_signal=signal.signal_id,
                        payload={"observation_hashes": ordered_hashes},
                    )
                level_values = values  # type: ignore[assignment]
        if level_values is not None:
            direction, entry_text, stop_text, target_text = level_values
            return Decision(
                instrument_id=context.instrument_id,
                status="READY",
                direction=direction,
                entry_text=entry_text,
                stop_text=stop_text,
                target_text=target_text,
                first_failed_signal=None,
                payload={"observation_hashes": ordered_hashes},
            )
        return Decision(
            instrument_id=context.instrument_id,
            status="UNAVAILABLE",
            direction=None,
            entry_text=None,
            stop_text=None,
            target_text=None,
            first_failed_signal="decision.levels",
            payload={"observation_hashes": ordered_hashes},
        )
