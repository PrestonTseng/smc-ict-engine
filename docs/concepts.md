# Concepts

`smc-ict-engine` is a research-only command-line engine. It reads completed market candles and writes deterministic research evidence.

The engine does not submit orders. It has no broker account, position-size, web-service, or live-trading boundary.

A run has one strategy, one market-data configuration, and an optional notification configuration. Manual and scheduled runs use the same `EngineRunner`.

Each strategy defines a directed acyclic graph (DAG). A graph node uses one configured role and timeframe. Required failed evidence produces `NO_TRADE`. Missing evidence produces `UNAVAILABLE`.

A successful run commits candles, observations, decisions, and the run receipt in SQLite. Notification delivery occurs after this commit. A notification error cannot change committed research evidence.

Source-derived formulas remain deferred. A `DEFERRED_PLUGIN` error is a safety result, not a trading signal.
