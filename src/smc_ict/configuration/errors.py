"""Fail-closed configuration errors."""


class StrictConfigurationError(ValueError):
    """A configuration value or YAML construct violates the strict contract."""


class DeferredPluginError(StrictConfigurationError):
    """A strategy selected plugins that have no approved conformance vectors."""

    def __init__(self, plugin_ids: tuple[str, ...]) -> None:
        self.plugin_ids = plugin_ids
        super().__init__("DEFERRED_PLUGIN: " + ", ".join(plugin_ids))
