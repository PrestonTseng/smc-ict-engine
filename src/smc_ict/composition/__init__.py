"""Composition roots are the only location that owns concrete registries."""

from .registries import (
    ClosedRegistry,
    CompositionRoot,
    RegistryEntry,
    UnavailableComponentError,
    UnknownComponentError,
    build_market_provider,
    foundation_composition_root,
    indicator_composition_root,
    market_data_composition_root,
)

__all__ = [
    "ClosedRegistry",
    "CompositionRoot",
    "RegistryEntry",
    "UnavailableComponentError",
    "UnknownComponentError",
    "build_market_provider",
    "foundation_composition_root",
    "indicator_composition_root",
    "market_data_composition_root",
]
