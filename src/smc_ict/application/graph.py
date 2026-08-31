"""Generic configured indicator DAG with deterministic execution."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from hashlib import sha256
from types import MappingProxyType

from smc_ict.application.ports.plugins import IndicatorPlugin
from smc_ict.configuration.models import StrategyConfig
from smc_ict.domain import InstrumentId, Observation, Timeframe
from smc_ict.domain.evidence_values import canonical_evidence, freeze_evidence


def _freeze(value: object) -> object:
    return freeze_evidence(value)


def _canonical_data(value: object) -> object:
    return canonical_evidence(value)


def _canonical_hash(domain: bytes, value: object) -> str:
    encoded = json.dumps(
        _canonical_data(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(domain + encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ConfiguredNode:
    instance_id: str
    plugin_id: str
    role: str
    depends_on: tuple[str, ...]
    parameters: Mapping[str, object]
    order: int
    timeframe: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "parameters", _freeze(self.parameters))

    def canonical_dict(self) -> dict[str, object]:
        return {
            "instance_id": self.instance_id,
            "plugin_id": self.plugin_id,
            "role": self.role,
            "depends_on": list(self.depends_on),
            "parameters": _canonical_data(self.parameters),
            "order": self.order,
            "timeframe": self.timeframe,
        }

    @property
    def parameter_hash(self) -> str:
        return _canonical_hash(b"indicator-parameters-v1\0", self.parameters)


@dataclass(frozen=True, slots=True)
class RunContext:
    instrument_id: str
    evaluation_time_ms: int
    candles_by_role: Mapping[str, tuple[object, ...]]
    timeframes_by_role: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", str(InstrumentId(self.instrument_id)))
        if type(self.evaluation_time_ms) is not int or self.evaluation_time_ms < 0:
            raise ValueError("evaluation time must be a non-negative integer")
        object.__setattr__(
            self,
            "candles_by_role",
            MappingProxyType(
                {role: tuple(candles) for role, candles in self.candles_by_role.items()}
            ),
        )
        object.__setattr__(
            self,
            "timeframes_by_role",
            MappingProxyType(
                {
                    role: str(Timeframe(timeframe))
                    for role, timeframe in self.timeframes_by_role.items()
                }
            ),
        )


IndicatorFactory = Callable[[Mapping[str, object]], IndicatorPlugin]


class GraphConfigurationError(ValueError):
    """The configured indicator graph cannot be executed safely."""


class UnknownPluginError(GraphConfigurationError):
    """A node names a plugin outside the supplied closed registry."""


class MissingDependencyError(GraphConfigurationError):
    """A node names an instance that is absent from the graph."""


class CyclicDependencyError(GraphConfigurationError):
    """Configured dependencies contain a cycle."""


def configured_nodes(strategy: StrategyConfig) -> tuple[ConfiguredNode, ...]:
    """Translate validated strategy signals into provider-neutral graph nodes."""

    return tuple(
        ConfiguredNode(
            instance_id=signal.id,
            plugin_id=signal.id,
            role=signal.role,
            depends_on=signal.depends_on,
            parameters=signal.parameters,
            order=signal.order,
            timeframe=strategy.roles[signal.role],
        )
        for signal in strategy.signals
    )


class IndicatorGraph:
    def __init__(
        self,
        *,
        nodes: tuple[ConfiguredNode, ...],
        factories: Mapping[str, IndicatorFactory],
    ) -> None:
        self._nodes = nodes
        self._factories = MappingProxyType(dict(factories))
        self._ordered = self._topological_order()

    @property
    def ordered_instance_ids(self) -> tuple[str, ...]:
        return tuple(node.instance_id for node in self._ordered)

    @property
    def graph_hash(self) -> str:
        return _canonical_hash(
            b"indicator-graph-v1\0", [node.canonical_dict() for node in self._ordered]
        )

    def _topological_order(self) -> tuple[ConfiguredNode, ...]:
        by_id = {node.instance_id: node for node in self._nodes}
        if len(by_id) != len(self._nodes):
            raise ValueError("duplicate configured instance ID")
        missing_factories = sorted(
            {node.plugin_id for node in self._nodes} - self._factories.keys()
        )
        if missing_factories:
            raise UnknownPluginError(f"unknown plugin IDs: {missing_factories}")
        missing_dependencies = sorted(
            {
                dependency
                for node in self._nodes
                for dependency in node.depends_on
                if dependency not in by_id
            }
        )
        if missing_dependencies:
            raise MissingDependencyError(f"missing dependency IDs: {missing_dependencies}")
        remaining = dict(by_id)
        completed: set[str] = set()
        ordered: list[ConfiguredNode] = []
        while remaining:
            ready = sorted(
                (node for node in remaining.values() if set(node.depends_on).issubset(completed)),
                key=lambda node: (node.order, node.instance_id),
            )
            if not ready:
                raise CyclicDependencyError("configured indicator graph has cyclic dependencies")
            for node in ready:
                ordered.append(node)
                completed.add(node.instance_id)
                del remaining[node.instance_id]
        return tuple(ordered)

    def execute(self, context: RunContext) -> Mapping[str, Observation]:
        observations: dict[str, Observation] = {}
        for node in self._ordered:
            if node.role not in context.candles_by_role:
                raise ValueError(f"missing candle role {node.role!r}")
            plugin = self._factories[node.plugin_id](node.parameters)
            plugin_role = getattr(plugin, "role", node.role)
            if plugin_role != node.role:
                raise ValueError("plugin role does not match configured node")
            dependency_values = MappingProxyType(
                {dependency: observations[dependency] for dependency in node.depends_on}
            )
            observation = plugin.evaluate(context, dependency_values)
            if not isinstance(observation, Observation):
                raise TypeError("indicator plugin must return Observation")
            if observation.signal_id != node.instance_id:
                raise ValueError("plugin observation signal ID does not match configured instance")
            if observation.instrument_id != context.instrument_id:
                raise ValueError("plugin observation instrument does not match run context")
            if node.timeframe is not None and observation.timeframe != node.timeframe:
                raise ValueError("plugin observation timeframe does not match configured role")
            if observation.dependency_ids != node.depends_on:
                raise ValueError("plugin observation dependencies do not match configured node")
            if observation.parameter_hash != node.parameter_hash:
                raise ValueError("plugin observation parameter hash does not match configured node")
            if (
                observation.known_time_ms is not None
                and observation.known_time_ms > context.evaluation_time_ms
            ):
                raise ValueError("plugin observation knowledge time exceeds evaluation time")
            observations[node.instance_id] = observation
        return MappingProxyType(observations)
