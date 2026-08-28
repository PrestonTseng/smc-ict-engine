"""Closed component registries owned by the composition boundary."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from smc_ict.adapters.market_data.binance_usdm import BinanceUsdmProvider
from smc_ict.adapters.market_data.okx_swap import OkxSwapProvider
from smc_ict.adapters.persistence.sqlite import SQLiteRepository
from smc_ict.application.ports import KlineProvider
from smc_ict.configuration import DEFERRED_PLUGIN_IDS
from smc_ict.configuration.models import MarketDataConfig

Factory = Callable[..., object]


class UnknownComponentError(LookupError):
    """A configured ID is outside a registry's closed set."""


class UnavailableComponentError(RuntimeError):
    """A known component is intentionally unavailable at this implementation phase."""


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    factory: Factory | None
    unavailable_reason: str | None


class ClosedRegistry:
    """Immutable ID-to-constructor map with no aliases or fallback branch."""

    def __init__(self, kind: str, entries: Mapping[str, RegistryEntry]) -> None:
        self._kind = kind
        self._entries = MappingProxyType(dict(entries))

    @property
    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._entries))

    def resolve(self, component_id: str) -> Factory:
        try:
            entry = self._entries[component_id]
        except KeyError as exc:
            raise UnknownComponentError(
                f"unknown {self._kind} ID {component_id!r}; allowed IDs are {self.ids}"
            ) from exc
        if entry.factory is None:
            reason = entry.unavailable_reason or "component is unavailable"
            raise UnavailableComponentError(
                f"{self._kind} {component_id!r} is unavailable: {reason}"
            )
        return entry.factory


@dataclass(frozen=True, slots=True)
class CompositionRoot:
    """Explicit seams through which later cards install concrete constructors."""

    providers: ClosedRegistry
    plugins: ClosedRegistry
    notifiers: ClosedRegistry
    repositories: ClosedRegistry


def _unavailable(ids: tuple[str, ...], reason: str) -> dict[str, RegistryEntry]:
    return {component_id: RegistryEntry(None, reason) for component_id in ids}


def foundation_composition_root() -> CompositionRoot:
    """Return the approved v1 IDs, all fail-closed until their implementation cards land."""
    return CompositionRoot(
        providers=ClosedRegistry(
            "provider",
            _unavailable(("binance_usdm", "okx_swap"), "adapter implementation is deferred"),
        ),
        plugins=ClosedRegistry(
            "plugin",
            _unavailable(
                tuple(DEFERRED_PLUGIN_IDS), "pinned source-conformance vectors are missing"
            ),
        ),
        notifiers=ClosedRegistry(
            "notifier", _unavailable(("generic_webhook",), "adapter implementation is deferred")
        ),
        repositories=ClosedRegistry(
            "repository", _unavailable(("sqlite",), "repository implementation is deferred")
        ),
    )


def market_data_composition_root() -> CompositionRoot:
    """Install approved read-only providers and exact schema-v1 persistence."""

    foundation = foundation_composition_root()
    return CompositionRoot(
        providers=ClosedRegistry(
            "provider",
            {
                "binance_usdm": RegistryEntry(BinanceUsdmProvider, None),
                "okx_swap": RegistryEntry(OkxSwapProvider, None),
            },
        ),
        plugins=foundation.plugins,
        notifiers=foundation.notifiers,
        repositories=ClosedRegistry(
            "repository", {"sqlite": RegistryEntry(SQLiteRepository, None)}
        ),
    )


def indicator_composition_root() -> CompositionRoot:
    """Keep all candidate modules unavailable until both source gates are satisfied."""

    root = market_data_composition_root()
    reason = (
        "provenance is recorded but conformance is required; pinned TradingView "
        "expected-output vectors are missing"
    )
    return CompositionRoot(
        providers=root.providers,
        plugins=ClosedRegistry("plugin", _unavailable(tuple(DEFERRED_PLUGIN_IDS), reason)),
        notifiers=root.notifiers,
        repositories=root.repositories,
    )


def build_market_provider(config: MarketDataConfig, root: CompositionRoot) -> KlineProvider:
    """Select an adapter solely through the market-data configuration authority."""

    candidate = root.providers.resolve(config.provider)()
    if not isinstance(candidate, KlineProvider):
        raise TypeError("registered provider does not implement KlineProvider")
    return candidate
