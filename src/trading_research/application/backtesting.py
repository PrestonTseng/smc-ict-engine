"""Deterministic offline point-in-time replay over canonical candle snapshots."""

from __future__ import annotations

import json
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Protocol

from trading_research.application.decision_policy import (
    OrderedDecisionPlugin,
    configured_decision_signals,
)
from trading_research.application.graph import (
    IndicatorFactory,
    IndicatorGraph,
    RunContext,
    configured_nodes,
)
from trading_research.application.resampling import DerivedCandle, resample_roles
from trading_research.configuration.models import BacktestPeriod, StrategyConfig
from trading_research.domain import (
    ClosedCandle,
    Decision,
    Observation,
    hash_candles,
    hash_decision,
    hash_observation,
)
from trading_research.domain.backtesting import (
    PipelineStep,
    PipelineTrace,
    ReplayEvaluation,
    ReplayResult,
)


@dataclass(frozen=True, slots=True)
class RequiredRange:
    start_open_ms: int
    end_open_ms: int

    def canonical_dict(self) -> dict[str, int]:
        return {"start_open_ms": self.start_open_ms, "end_open_ms": self.end_open_ms}


class RequiredRangeResolver:
    @staticmethod
    def resolve(period: BacktestPeriod, *, history_minutes: int) -> RequiredRange:
        if type(history_minutes) is not int or history_minutes < 1:
            raise ValueError("history minutes must be a positive integer")
        start = period.start_ms - history_minutes * 60_000
        if start < 0:
            raise ValueError("configured warm-up precedes the provider epoch")
        return RequiredRange(start, period.end_ms)


@dataclass(frozen=True, slots=True)
class BacktestIdentity:
    scenario_hash: str
    strategy_hash: str
    market_data_hash: str
    candle_data_hash: str
    git_commit: str
    period: BacktestPeriod
    required_range: RequiredRange
    backtest_id: str

    @classmethod
    def create(
        cls,
        *,
        scenario_hash: str,
        strategy_hash: str,
        market_data_hash: str,
        candle_data_hash: str,
        git_commit: str,
        period: BacktestPeriod,
        required_range: RequiredRange,
    ) -> BacktestIdentity:
        hashes = (scenario_hash, strategy_hash, market_data_hash, candle_data_hash)
        if any(
            len(value) != 64 or any(char not in "0123456789abcdef" for char in value)
            for value in hashes
        ):
            raise ValueError("identity inputs must contain lowercase SHA-256 hashes")
        if len(git_commit) != 40 or any(char not in "0123456789abcdef" for char in git_commit):
            raise ValueError("Git commit must be a lowercase 40-character hash")
        payload = {
            "scenario_hash": scenario_hash,
            "strategy_hash": strategy_hash,
            "market_data_hash": market_data_hash,
            "candle_data_hash": candle_data_hash,
            "git_commit": git_commit,
            "period": period.canonical_dict(),
            "required_range": required_range.canonical_dict(),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        identity = sha256(b"backtest-v1\0" + encoded).hexdigest()
        return cls(
            scenario_hash,
            strategy_hash,
            market_data_hash,
            candle_data_hash,
            git_commit,
            period,
            required_range,
            identity,
        )


class CandleRangeSource(Protocol):
    def load_candles(
        self,
        provider_id: str,
        market_type: str,
        instrument_id: str,
        start_open_ms: int,
        end_open_ms: int,
    ) -> tuple[ClosedCandle, ...]: ...


class PointInTimeReplay:
    """Evaluate the production graph at closed execution-timeframe boundaries."""

    def __init__(
        self,
        *,
        strategy: StrategyConfig,
        provider_id: str,
        market_type: str,
        candle_source: CandleRangeSource,
        plugin_factories: Mapping[str, IndicatorFactory],
    ) -> None:
        if "execution" not in strategy.roles:
            raise ValueError("strategy requires an execution role")
        self._strategy = strategy
        self._provider_id = provider_id
        self._market_type = market_type
        self._candle_source = candle_source
        self._plugin_factories = MappingProxyType(dict(plugin_factories))

    def run(self, period: BacktestPeriod) -> ReplayResult:
        required = RequiredRangeResolver.resolve(
            period, history_minutes=self._strategy.history_minutes
        )
        roles_by_instrument: dict[str, Mapping[str, tuple[DerivedCandle, ...]]] = {}
        all_candles: list[ClosedCandle] = []
        for instrument_id in self._strategy.instruments:
            candles = self._candle_source.load_candles(
                self._provider_id,
                self._market_type,
                instrument_id,
                required.start_open_ms,
                required.end_open_ms,
            )
            self._validate_candles(candles, instrument_id, required)
            roles_by_instrument[instrument_id] = resample_roles(candles, self._strategy.roles)
            all_candles.extend(candles)

        first_instrument = self._strategy.instruments[0]
        schedule = tuple(
            bar.close_time_ms
            for bar in roles_by_instrument[first_instrument]["execution"]
            if bar.open_time_ms >= period.start_ms and bar.close_time_ms <= period.end_ms + 59_999
        )
        graph = IndicatorGraph(
            nodes=configured_nodes(self._strategy), factories=self._plugin_factories
        )
        policy = OrderedDecisionPlugin(configured_decision_signals(self._strategy))
        evaluations: list[ReplayEvaluation] = []
        for evaluation_time_ms in schedule:
            for instrument_id in self._strategy.instruments:
                context = RunContext(
                    instrument_id,
                    evaluation_time_ms,
                    self._prefixes(roles_by_instrument[instrument_id], evaluation_time_ms),
                )
                observations = graph.execute(context)
                decision = policy.decide(context, observations)
                decision_hash = hash_decision(decision)
                trace = self._trace(context, observations, decision, decision_hash)
                evaluations.append(
                    ReplayEvaluation(
                        instrument_id,
                        evaluation_time_ms,
                        decision,
                        decision_hash,
                        trace,
                    )
                )
        return ReplayResult(tuple(evaluations), len(all_candles), hash_candles(all_candles))

    def _prefixes(
        self,
        roles: Mapping[str, tuple[DerivedCandle, ...]],
        evaluation_time_ms: int,
    ) -> Mapping[str, tuple[DerivedCandle, ...]]:
        earliest_close = evaluation_time_ms - self._strategy.history_minutes * 60_000
        result: dict[str, tuple[DerivedCandle, ...]] = {}
        for role, candles in roles.items():
            closes = tuple(candle.close_time_ms for candle in candles)
            start = bisect_right(closes, earliest_close)
            end = bisect_right(closes, evaluation_time_ms)
            result[role] = candles[start:end]
        return MappingProxyType(result)

    def _trace(
        self,
        context: RunContext,
        observations: Mapping[str, Observation],
        decision: Decision,
        decision_hash: str,
    ) -> PipelineTrace:
        observation_hashes = {
            signal_id: hash_observation(observation)
            for signal_id, observation in observations.items()
        }
        steps: list[PipelineStep] = []
        for signal_id, observation in observations.items():
            dependencies = tuple(
                (dependency, observation_hashes[dependency])
                for dependency in observation.dependency_ids
            )
            steps.append(
                PipelineStep(
                    "INDICATOR",
                    signal_id,
                    observation.status,
                    observation.bounded_reason,
                    dependencies,
                    observation_hashes[signal_id],
                )
            )
        rejected = False
        for signal in sorted(
            configured_decision_signals(self._strategy),
            key=lambda item: (item.order, item.signal_id),
        ):
            output_hash = observation_hashes.get(signal.signal_id)
            dependency_hashes = () if output_hash is None else ((signal.signal_id, output_hash),)
            if rejected:
                state, reason, output_hash = (
                    "SKIPPED_AFTER_REJECTION",
                    "SKIPPED_AFTER_REJECTION",
                    None,
                )
                dependency_hashes = ()
            elif signal.signal_id == decision.first_failed_signal:
                unavailable = decision.status == "UNAVAILABLE"
                state = "UNAVAILABLE" if unavailable else "REJECT"
                reason = "REQUIRED_SIGNAL_UNAVAILABLE" if unavailable else "REQUIRED_SIGNAL_FAILED"
                rejected = True
            else:
                state, reason = "PASS", "SIGNAL_ACCEPTED"
            steps.append(
                PipelineStep(
                    "DECISION_GATE",
                    signal.signal_id,
                    state,
                    reason,
                    dependency_hashes,
                    output_hash,
                )
            )
        return PipelineTrace(
            context.instrument_id,
            context.evaluation_time_ms,
            tuple(steps),
            decision.first_failed_signal,
            decision_hash,
        )

    def _validate_candles(
        self,
        candles: tuple[ClosedCandle, ...],
        instrument_id: str,
        required: RequiredRange,
    ) -> None:
        expected_count = (required.end_open_ms - required.start_open_ms) // 60_000 + 1
        if len(candles) != expected_count:
            raise ValueError("snapshot candle range is incomplete")
        for index, candle in enumerate(candles):
            if (
                candle.provider_id != self._provider_id
                or candle.market_type != self._market_type
                or candle.instrument_id != instrument_id
                or candle.open_time_ms != required.start_open_ms + index * 60_000
            ):
                raise ValueError("snapshot candle range has a gap or identity mismatch")
