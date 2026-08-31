from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path
from types import MappingProxyType

import pytest


def test_configured_dag_executes_dependencies_in_topological_order_with_immutable_context() -> None:
    from smc_ict.application.graph import ConfiguredNode, IndicatorGraph, RunContext
    from smc_ict.domain import Observation

    calls: list[tuple[str, tuple[str, ...]]] = []
    parameter_hash = ConfiguredNode("hash", "hash", "execution", (), {}, 1).parameter_hash

    class Plugin:
        def __init__(self, plugin_id: str) -> None:
            self.plugin_id = plugin_id

        def evaluate(self, context: RunContext, dependencies: object) -> Observation:
            dependency_map = dict(dependencies)  # type: ignore[arg-type]
            calls.append((self.plugin_id, tuple(dependency_map)))
            return Observation.available(
                signal_id=self.plugin_id,
                instrument_id=context.instrument_id,
                timeframe="5m",
                status="PASS",
                event_type="FIXTURE",
                direction=None,
                event_time_ms=context.evaluation_time_ms,
                known_time_ms=context.evaluation_time_ms,
                state="CONFIRMED",
                dependency_ids=tuple(dependency_map),
                parameter_hash=parameter_hash,
                source_manifest_ids=(),
                payload_schema_version=1,
                bounded_reason="fixture passed",
                payload={},
            )

    graph = IndicatorGraph(
        nodes=(
            ConfiguredNode("third", "third", "execution", ("second",), {}, 30),
            ConfiguredNode("first", "first", "execution", (), {}, 10),
            ConfiguredNode("second", "second", "execution", ("first",), {}, 20),
        ),
        factories={
            name: (lambda _parameters, name=name: Plugin(name))
            for name in ("first", "second", "third")
        },
    )
    context = RunContext(
        instrument_id="BTC-USDT-PERP",
        evaluation_time_ms=299_999,
        candles_by_role={"execution": ()},
    )

    observations = graph.execute(context)

    assert tuple(observations) == ("first", "second", "third")
    assert calls == [("first", ()), ("second", ("first",)), ("third", ("second",))]
    assert isinstance(context.candles_by_role, MappingProxyType)
    with pytest.raises(FrozenInstanceError):
        context.instrument_id = "ETH-USDT-PERP"


def test_configured_dag_rejects_unknown_missing_and_cyclic_dependencies() -> None:
    from smc_ict.application.graph import (
        ConfiguredNode,
        CyclicDependencyError,
        IndicatorGraph,
        MissingDependencyError,
        UnknownPluginError,
    )

    def node(instance_id: str, dependencies: tuple[str, ...] = ()) -> ConfiguredNode:
        return ConfiguredNode(instance_id, "fixture", "execution", dependencies, {}, 1)

    with pytest.raises(UnknownPluginError, match="unknown"):
        IndicatorGraph(nodes=(node("node"),), factories={})
    with pytest.raises(MissingDependencyError, match="missing"):
        IndicatorGraph(nodes=(node("node", ("missing",)),), factories={"fixture": object})
    with pytest.raises(CyclicDependencyError, match="cyclic"):
        IndicatorGraph(
            nodes=(node("first", ("second",)), node("second", ("first",))),
            factories={"fixture": object},
        )


def test_plugin_instances_and_nested_parameters_are_isolated_per_execution() -> None:
    from smc_ict.application.graph import ConfiguredNode, IndicatorGraph, RunContext
    from smc_ict.domain import Observation

    seen: list[tuple[int, object]] = []
    parameter_hash = ConfiguredNode(
        "hash", "hash", "execution", (), {"nested": ["original"]}, 1
    ).parameter_hash

    class StatefulPlugin:
        def __init__(self, parameters: object) -> None:
            self.parameters = parameters
            self.counter = 0

        def evaluate(self, context: RunContext, _dependencies: object) -> Observation:
            self.counter += 1
            seen.append((self.counter, self.parameters))
            return Observation.available(
                signal_id="instance",
                instrument_id=context.instrument_id,
                timeframe="5m",
                status="PASS",
                event_type="FIXTURE",
                direction=None,
                event_time_ms=0,
                known_time_ms=0,
                state="CONFIRMED",
                dependency_ids=(),
                parameter_hash=parameter_hash,
                source_manifest_ids=(),
                payload_schema_version=1,
                bounded_reason="fixture passed",
                payload={},
            )

    mutable = {"nested": ["original"]}
    graph = IndicatorGraph(
        nodes=(ConfiguredNode("instance", "fixture", "execution", (), mutable, 1),),
        factories={"fixture": StatefulPlugin},
    )
    mutable["nested"].append("mutated")
    context = RunContext("BTC-USDT-PERP", 0, {"execution": ()})

    graph.execute(context)
    graph.execute(context)

    assert [counter for counter, _ in seen] == [1, 1]
    assert [dict(parameters)["nested"] for _, parameters in seen] == [("original",), ("original",)]


def test_graph_rejects_observation_that_does_not_match_node_evidence() -> None:
    from smc_ict.application.graph import ConfiguredNode, IndicatorGraph, RunContext
    from smc_ict.domain import Observation

    class MismatchedPlugin:
        def evaluate(self, context: RunContext, _dependencies: object) -> Observation:
            return Observation.available(
                signal_id="instance",
                instrument_id="ETH-USDT-PERP",
                timeframe="5m",
                status="PASS",
                event_type="FIXTURE",
                direction=None,
                event_time_ms=0,
                known_time_ms=0,
                state="CONFIRMED",
                dependency_ids=("undeclared",),
                parameter_hash="a" * 64,
                source_manifest_ids=(),
                payload_schema_version=1,
                bounded_reason="fixture passed",
                payload={},
            )

    graph = IndicatorGraph(
        nodes=(ConfiguredNode("instance", "fixture", "execution", (), {}, 1),),
        factories={"fixture": lambda _parameters: MismatchedPlugin()},
    )

    with pytest.raises(ValueError, match="instrument"):
        graph.execute(RunContext("BTC-USDT-PERP", 0, {"execution": ()}))


def test_graph_rejects_observation_timeframe_that_does_not_match_configured_role() -> None:
    from smc_ict.application.graph import ConfiguredNode, IndicatorGraph, RunContext
    from smc_ict.domain import Observation

    node = ConfiguredNode("instance", "fixture", "regime", (), {}, 1, timeframe="1h")

    class Plugin:
        def evaluate(self, context: RunContext, _dependencies: object) -> Observation:
            return Observation.available(
                signal_id="instance",
                instrument_id=context.instrument_id,
                timeframe="5m",
                status="PASS",
                event_type="FIXTURE",
                direction=None,
                event_time_ms=0,
                known_time_ms=0,
                state="CONFIRMED",
                dependency_ids=(),
                parameter_hash=node.parameter_hash,
                source_manifest_ids=(),
                payload_schema_version=1,
                bounded_reason="fixture passed",
                payload={},
            )

    graph = IndicatorGraph(nodes=(node,), factories={"fixture": lambda _parameters: Plugin()})

    with pytest.raises(ValueError, match="timeframe"):
        graph.execute(RunContext("BTC-USDT-PERP", 0, {"regime": ()}))


def test_graph_rejects_plugin_role_that_does_not_match_configured_node() -> None:
    from smc_ict.application.graph import ConfiguredNode, IndicatorGraph, RunContext

    class ContextPlugin:
        role = "context"

        def evaluate(self, context: object, dependencies: object) -> object:
            return (context, dependencies)

    graph = IndicatorGraph(
        nodes=(ConfiguredNode("instance", "fixture", "execution", (), {}, 1, timeframe="15m"),),
        factories={"fixture": lambda _parameters: ContextPlugin()},
    )

    with pytest.raises(ValueError, match="role"):
        graph.execute(RunContext("BTC-USDT-PERP", 0, {"execution": ()}))


def test_graph_hash_is_canonical_and_changes_only_with_configured_instances() -> None:
    from smc_ict.application.graph import ConfiguredNode, IndicatorGraph

    def factory(_parameters: object) -> object:
        return object()

    first = ConfiguredNode("first", "fixture", "regime", (), {"threshold": "1.50"}, 10)
    second = ConfiguredNode("second", "fixture", "execution", ("first",), {}, 20)

    original = IndicatorGraph(nodes=(first, second), factories={"fixture": factory})
    reordered = IndicatorGraph(nodes=(second, first), factories={"fixture": factory})
    changed = IndicatorGraph(
        nodes=(
            ConfiguredNode("first", "fixture", "regime", (), {"threshold": "1.5"}, 10),
            second,
        ),
        factories={"fixture": factory},
    )
    deleted = IndicatorGraph(nodes=(first,), factories={"fixture": factory})

    assert original.graph_hash == reordered.graph_hash
    assert original.ordered_instance_ids == ("first", "second")
    assert original.graph_hash != changed.graph_hash
    assert original.graph_hash != deleted.graph_hash
    assert deleted.ordered_instance_ids == ("first",)
    with pytest.raises(TypeError, match="canonical JSON"):
        ConfiguredNode("float", "fixture", "execution", (), {"threshold": 1.5}, 30)


def test_observation_hash_is_deterministic_for_decimal_text_and_knowledge_time() -> None:
    from dataclasses import replace

    from smc_ict.domain import DecimalText, Observation, hash_observation

    base = Observation.available(
        signal_id="fixture.signal",
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status="PASS",
        event_type="FIXTURE",
        direction="BULLISH",
        event_time_ms=100,
        known_time_ms=200,
        state="CONFIRMED",
        dependency_ids=("fixture.parent",),
        parameter_hash="a" * 64,
        source_manifest_ids=(),
        payload_schema_version=1,
        bounded_reason="fixture passed",
        payload={"z": 1, "level_text": str(DecimalText("1.500"))},
    )
    reordered = replace(base, payload={"level_text": "1.5", "z": 1})

    assert hash_observation(base) == hash_observation(reordered)
    assert len(hash_observation(base)) == 64
    assert hash_observation(base) != hash_observation(replace(base, known_time_ms=201))


def test_indicator_and_decision_plugin_contracts_are_runtime_checkable() -> None:
    from smc_ict.application.ports import DecisionPlugin, IndicatorPlugin

    class Plugin:
        plugin_id = "fixture"

        def evaluate(self, context: object, dependencies: object) -> object:
            return (context, dependencies)

        def decide(self, context: object, observations: object) -> object:
            return (context, observations)

    assert isinstance(Plugin(), IndicatorPlugin)
    assert isinstance(Plugin(), DecisionPlugin)


def test_ordered_decision_policy_stops_at_first_required_unavailable_observation() -> None:
    from smc_ict.application.decision_policy import DecisionSignal, OrderedDecisionPlugin
    from smc_ict.application.graph import RunContext
    from smc_ict.domain import Observation, hash_decision

    unavailable = Observation.available(
        signal_id="first",
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status="UNAVAILABLE",
        event_type=None,
        direction=None,
        event_time_ms=None,
        known_time_ms=None,
        state="UNAVAILABLE",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=(),
        payload_schema_version=1,
        bounded_reason="complete input is unavailable",
        payload={},
    )
    policy = OrderedDecisionPlugin(
        (
            DecisionSignal("levels", required=True, effect="LEVELS", order=20),
            DecisionSignal("first", required=True, effect="REJECT", order=10),
        )
    )

    decision = policy.decide(
        RunContext("BTC-USDT-PERP", 300_000, {"execution": ()}), {"first": unavailable}
    )

    assert decision.status == "UNAVAILABLE"
    assert decision.first_failed_signal == "first"
    assert decision.direction is None
    assert len(hash_decision(decision)) == 64


def test_ordered_decision_policy_rejects_on_first_required_failure() -> None:
    from dataclasses import replace

    from smc_ict.application.decision_policy import DecisionSignal, OrderedDecisionPlugin
    from smc_ict.application.graph import RunContext
    from smc_ict.domain import Observation

    failed = Observation.available(
        signal_id="rejector",
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status="FAIL",
        event_type=None,
        direction=None,
        event_time_ms=None,
        known_time_ms=None,
        state="REJECTED",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=(),
        payload_schema_version=1,
        bounded_reason="condition did not pass",
        payload={},
    )
    later = replace(failed, signal_id="later", status="UNAVAILABLE")
    policy = OrderedDecisionPlugin(
        (
            DecisionSignal("later", True, "LEVELS", 20),
            DecisionSignal("rejector", True, "REJECT", 10),
        )
    )

    decision = policy.decide(
        RunContext("BTC-USDT-PERP", 300_000, {"execution": ()}),
        {"rejector": failed, "later": later},
    )

    assert decision.status == "NO_TRADE"
    assert decision.first_failed_signal == "rejector"
    assert tuple(decision.payload["observation_hashes"]) == ("rejector",)


def test_ordered_decision_policy_uses_configured_level_output_without_inventing_values() -> None:
    from dataclasses import replace

    from smc_ict.application.decision_policy import DecisionSignal, OrderedDecisionPlugin
    from smc_ict.application.graph import RunContext
    from smc_ict.domain import Observation

    passed = Observation.available(
        signal_id="gate",
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status="PASS",
        event_type="FIXTURE",
        direction="LONG",
        event_time_ms=100,
        known_time_ms=200,
        state="CONFIRMED",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=(),
        payload_schema_version=1,
        bounded_reason="condition passed",
        payload={},
    )
    levels = replace(
        passed,
        signal_id="levels",
        payload={
            "direction": "LONG",
            "entry_text": "101.50",
            "stop_text": "99.25",
            "target_text": "106",
        },
    )
    policy = OrderedDecisionPlugin(
        (
            DecisionSignal("gate", True, "REJECT", 10),
            DecisionSignal("levels", True, "LEVELS", 20),
        )
    )

    decision = policy.decide(
        RunContext("BTC-USDT-PERP", 300_000, {"execution": ()}),
        {"levels": levels, "gate": passed},
    )

    assert decision.status == "READY"
    assert (decision.direction, decision.entry_text, decision.stop_text, decision.target_text) == (
        "LONG",
        "101.5",
        "99.25",
        "106",
    )
    assert decision.first_failed_signal is None


def test_all_seven_source_aligned_modules_are_registered_as_real_factories() -> None:
    from smc_ict.application.ports import IndicatorPlugin
    from smc_ict.composition import indicator_composition_root
    from smc_ict.configuration import IMPLEMENTED_PLUGIN_IDS, load_strategy

    root = indicator_composition_root()

    assert root.plugins.ids == tuple(sorted(IMPLEMENTED_PLUGIN_IDS))
    assert len(root.plugins.ids) == 7
    for plugin_id in root.plugins.ids:
        parameters = next(
            signal.parameters
            for signal in load_strategy(
                Path(__file__).parents[1] / "strategies/source-aligned-research.yaml"
            ).signals
            if signal.id == plugin_id
        )
        assert isinstance(root.plugins.resolve(plugin_id)(parameters), IndicatorPlugin)


def test_first_party_provenance_records_preserve_attribution_and_publication_boundary() -> None:
    manifest = (Path(__file__).parents[1] / "provenance/sources.yaml").read_text(encoding="utf-8")

    assert "https://tw.tradingview.com/script/CnB3fSph-" in manifest
    assert "https://tw.tradingview.com/script/ib4uqBJx-" in manifest
    assert manifest.count("CC BY-NC-SA 4.0") == 2
    assert manifest.count("copyright: LuxAlgo") == 2
    assert manifest.count("facade: https://pine-facade.tradingview.com/") == 2
    assert "source_line_count: 848" in manifest
    assert "source_line_count: 1144" in manifest
    assert "pine_source:" not in manifest


def test_strategy_configuration_wires_graph_and_decision_order_without_source_branches() -> None:
    from smc_ict.application.decision_policy import configured_decision_signals
    from smc_ict.application.graph import configured_nodes
    from smc_ict.configuration import load_strategy

    strategy = load_strategy(Path(__file__).parents[1] / "strategies/source-aligned-research.yaml")

    nodes = configured_nodes(strategy)
    decision_signals = configured_decision_signals(strategy)

    assert tuple(node.instance_id for node in nodes) == tuple(
        signal.id for signal in strategy.signals
    )
    assert tuple(node.depends_on for node in nodes) == tuple(
        signal.depends_on for signal in strategy.signals
    )
    assert tuple(signal.signal_id for signal in decision_signals) == tuple(
        signal.id for signal in strategy.signals
    )


def test_active_strategy_has_exact_multitimeframe_composition_and_runs_all_factories() -> None:
    from smc_ict.application.graph import IndicatorGraph, RunContext, configured_nodes
    from smc_ict.application.resampling import DerivedCandle
    from smc_ict.composition import indicator_composition_root
    from smc_ict.configuration import load_strategy
    from smc_ict.domain import Timeframe, hash_observation

    strategy = load_strategy(Path(__file__).parents[1] / "strategies/source-aligned-research.yaml")
    assert strategy.history_minutes == 90 * 24 * 60
    assert dict(strategy.roles) == {"regime": "4h", "context": "1h", "execution": "15m"}
    assert tuple(signal.canonical_dict() for signal in strategy.signals) == (
        {
            "id": "smc.swing_structure",
            "role": "regime",
            "depends_on": [],
            "parameters": {"swing_length": 50, "show_labels": True},
            "required": True,
            "effect": "REJECT",
            "order": 10,
        },
        {
            "id": "smc.equal_high_low",
            "role": "context",
            "depends_on": ["smc.swing_structure"],
            "parameters": {"confirmation_bars": 3, "threshold_atr_fraction": "0.1"},
            "required": True,
            "effect": "REJECT",
            "order": 20,
        },
        {
            "id": "smc.order_block",
            "role": "context",
            "depends_on": ["smc.swing_structure"],
            "parameters": {
                "scope": "swing",
                "volatility_filter": "atr",
                "mitigation_source": "close",
                "maximum_blocks": 5,
            },
            "required": True,
            "effect": "REJECT",
            "order": 30,
        },
        {
            "id": "ict.clustered_liquidity",
            "role": "execution",
            "depends_on": ["smc.equal_high_low"],
            "parameters": {
                "pivot_width": 5,
                "minimum_pivots": 3,
                "margin_atr_fraction": "0.4",
            },
            "required": True,
            "effect": "REJECT",
            "order": 40,
        },
        {
            "id": "ict.market_structure",
            "role": "execution",
            "depends_on": ["ict.clustered_liquidity"],
            "parameters": {"pivot_width": 5, "emit_mss": True, "emit_bos": True},
            "required": True,
            "effect": "REJECT",
            "order": 50,
        },
        {
            "id": "ict.fair_value_gap",
            "role": "execution",
            "depends_on": ["ict.market_structure"],
            "parameters": {
                "kind": "ordinary",
                "require_displacement": True,
                "displacement_length": 20,
                "mitigation": "full_traversal",
            },
            "required": True,
            "effect": "REJECT",
            "order": 60,
        },
        {
            "id": "project.risk_levels",
            "role": "execution",
            "depends_on": ["ict.clustered_liquidity", "ict.fair_value_gap"],
            "parameters": {"minimum_reward_risk": "2"},
            "required": True,
            "effect": "LEVELS",
            "order": 70,
        },
    )

    def role_candles(interval: str, count: int) -> tuple[DerivedCandle, ...]:
        duration = Timeframe(interval).duration_minutes * 60_000
        return tuple(
            DerivedCandle(
                "BTC-USDT-PERP",
                interval,
                index * duration,
                (index + 1) * duration - 1,
                "100",
                "101",
                "99",
                "100",
                "1",
                "1",
            )
            for index in range(count)
        )

    roles = {
        "regime": role_candles("4h", 60),
        "context": role_candles("1h", 220),
        "execution": role_candles("15m", 30),
    }
    root = indicator_composition_root()
    factories = {plugin_id: root.plugins.resolve(plugin_id) for plugin_id in root.plugins.ids}
    graph = IndicatorGraph(  # type: ignore[arg-type]
        nodes=configured_nodes(strategy), factories=factories
    )
    context = RunContext(
        "BTC-USDT-PERP",
        roles["regime"][-1].close_time_ms,
        roles,
        strategy.roles,
    )

    first = graph.execute(context)
    second = graph.execute(context)

    assert tuple(first) == tuple(signal.id for signal in strategy.signals)
    assert tuple(item.timeframe for item in first.values()) == (
        "4h",
        "1h",
        "1h",
        "15m",
        "15m",
        "15m",
        "15m",
    )
    assert tuple(map(hash_observation, first.values())) == tuple(
        map(hash_observation, second.values())
    )


def test_seven_node_graph_replay_is_byte_deterministic_end_to_end() -> None:
    import json

    from smc_ict.application.decision_policy import (
        OrderedDecisionPlugin,
        configured_decision_signals,
    )
    from smc_ict.application.graph import IndicatorGraph, RunContext, configured_nodes
    from smc_ict.application.resampling import DerivedCandle
    from smc_ict.composition import indicator_composition_root
    from smc_ict.configuration import load_strategy
    from smc_ict.domain import Timeframe, hash_decision, hash_observation

    strategy = load_strategy(Path(__file__).parents[1] / "strategies/source-aligned-research.yaml")
    root = indicator_composition_root()
    factories = {plugin_id: root.plugins.resolve(plugin_id) for plugin_id in root.plugins.ids}
    graph = IndicatorGraph(nodes=configured_nodes(strategy), factories=factories)  # type: ignore[arg-type]

    def role_candles(interval: str, count: int) -> tuple[DerivedCandle, ...]:
        duration = Timeframe(interval).duration_minutes * 60_000
        return tuple(
            DerivedCandle(
                "BTC-USDT-PERP",
                interval,
                index * duration,
                (index + 1) * duration - 1,
                "100",
                "101",
                "99",
                "100",
                "1",
                "1",
            )
            for index in range(count)
        )

    roles = {
        "regime": role_candles(strategy.roles["regime"], 60),
        "context": role_candles(strategy.roles["context"], 220),
        "execution": role_candles(strategy.roles["execution"], 30),
    }
    context = RunContext("BTC-USDT-PERP", roles["regime"][-1].close_time_ms, roles, strategy.roles)
    policy = OrderedDecisionPlugin(configured_decision_signals(strategy))

    first = graph.execute(context)
    second = graph.execute(context)
    first_decision = policy.decide(context, first)
    second_decision = policy.decide(context, second)
    first_bytes = json.dumps(
        {
            "observations": [hash_observation(value) for value in first.values()],
            "decision": hash_decision(first_decision),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    second_bytes = json.dumps(
        {
            "observations": [hash_observation(value) for value in second.values()],
            "decision": hash_decision(second_decision),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()

    assert tuple(first) == tuple(signal.id for signal in strategy.signals)
    assert first_bytes == second_bytes


def test_observation_and_decision_records_persist_idempotently_with_canonical_evidence(
    tmp_path: Path,
) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.evidence import persist_evidence
    from smc_ict.application.ports import RunRecord
    from smc_ict.domain import Decision, Observation, hash_decision, hash_observation

    repository = SQLiteRepository(tmp_path / "analysis.sqlite3")
    repository.store_run(
        RunRecord(
            run_id="run-1",
            status="RUNNING",
            started_at_ms=0,
            completed_at_ms=None,
            strategy_name="fixture",
            strategy_version="1",
            strategy_config_hash="a" * 64,
            provider_id="fixture",
            market_type="LINEAR_PERPETUAL",
            market_config_hash="b" * 64,
            git_commit="c" * 40,
            data_start_open_ms=0,
            data_end_close_ms=59_999,
            data_hash="d" * 64,
            error=None,
        )
    )
    observation = Observation.available(
        signal_id="fixture.signal",
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status="PASS",
        event_type="FIXTURE",
        direction="BULLISH",
        event_time_ms=100,
        known_time_ms=200,
        state="CONFIRMED",
        dependency_ids=("fixture.parent",),
        parameter_hash="e" * 64,
        source_manifest_ids=("fixture-source-v1",),
        payload_schema_version=1,
        bounded_reason="fixture passed",
        payload={"level_text": "1.5"},
    )
    decision = Decision(
        instrument_id="BTC-USDT-PERP",
        status="NO_TRADE",
        direction=None,
        entry_text=None,
        stop_text=None,
        target_text=None,
        first_failed_signal="fixture.signal",
        payload={"observation_hashes": {"fixture.signal": hash_observation(observation)}},
    )
    persist_evidence(repository, "run-1", (observation,), (decision,))
    persist_evidence(repository, "run-1", (observation,), (decision,))

    loaded_observation = repository.load_observations("run-1")[0]
    loaded_decision = repository.load_decisions("run-1")[0]
    assert loaded_observation.payload["parameter_hash"] == "e" * 64
    assert loaded_observation.payload["source_manifest_ids"] == ["fixture-source-v1"]
    assert loaded_observation.payload["observation_hash"] == hash_observation(observation)
    assert loaded_decision.payload["decision_hash"] == hash_decision(decision)


def test_evidence_payload_rejects_noncanonical_numbers() -> None:
    from smc_ict.domain import Decision, Observation

    with pytest.raises(TypeError, match="canonical JSON"):
        Observation.available(
            signal_id="fixture.signal",
            instrument_id="BTC-USDT-PERP",
            timeframe="5m",
            status="PASS",
            event_type=None,
            direction=None,
            event_time_ms=None,
            known_time_ms=None,
            state="CONFIRMED",
            dependency_ids=(),
            parameter_hash="a" * 64,
            source_manifest_ids=(),
            payload_schema_version=1,
            bounded_reason="fixture passed",
            payload={"not_decimal_text": 1.5},
        )

    with pytest.raises(TypeError, match="canonical JSON"):
        Decision(
            instrument_id="BTC-USDT-PERP",
            status="NO_TRADE",
            direction=None,
            entry_text=None,
            stop_text=None,
            target_text=None,
            first_failed_signal="fixture.signal",
            payload={"not_decimal_text": 1.5},
        )


def test_observation_canonicalizes_explicit_decimal_levels() -> None:
    from smc_ict.domain import Observation

    observation = Observation.available(
        signal_id="fixture.signal",
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status="PASS",
        event_type="FIXTURE",
        direction="BULLISH",
        event_time_ms=100,
        known_time_ms=200,
        state="CONFIRMED",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=(),
        payload_schema_version=1,
        bounded_reason="fixture passed",
        payload={},
        level_text="101.500",
        lower_text="99.250",
        upper_text="106.0",
    )

    assert (observation.level_text, observation.lower_text, observation.upper_text) == (
        "101.5",
        "99.25",
        "106",
    )


def test_core_pipeline_has_no_provider_or_source_platform_conditionals() -> None:
    application = Path(__file__).parents[1] / "src/smc_ict/application"
    forbidden = ("if provider ==", "if source ==", "tradingview", "binance", "okx")

    for path in application.rglob("*.py"):
        source = path.read_text(encoding="utf-8").lower()
        assert not any(term in source for term in forbidden), path


def test_strategy_examples_explain_logical_roles_and_risk_boundaries() -> None:
    examples = (Path(__file__).parents[1] / "docs/strategy-examples.md").read_text(encoding="utf-8")
    lowered = examples.lower()

    for phrase in (
        "logical role",
        "completed bars",
        "risk plugin",
        "not a profitability claim",
        "configuration owns",
    ):
        assert phrase in lowered
    assert "if provider" not in lowered
    assert "if source" not in lowered
