"""Provider-neutral plugin contracts; no indicator formulas live here."""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from smc_ict.domain import Decision, Observation

if TYPE_CHECKING:
    from smc_ict.application.graph import RunContext


@runtime_checkable
class IndicatorPlugin(Protocol):
    """A deterministic plugin that sees candles and declared dependency outputs."""

    plugin_id: str

    def evaluate(
        self, context: RunContext, dependencies: Mapping[str, Observation]
    ) -> Observation: ...


@runtime_checkable
class DecisionPlugin(Protocol):
    """Compose configured observations without reading hidden indicator state."""

    plugin_id: str

    def decide(self, context: RunContext, observations: Mapping[str, Observation]) -> Decision: ...
