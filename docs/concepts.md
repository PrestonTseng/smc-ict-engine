# Concepts

`smc-ict-engine` is a research-only command-line engine. It reads completed market candles and writes deterministic research evidence.

The engine does not submit orders. It has no broker account, position-size, web-service, or live-trading boundary.

A run has one strategy, one market-data configuration, and an optional notification configuration. Manual and scheduled runs use the same `EngineRunner`.

Each strategy defines a directed acyclic graph (DAG). A graph node uses one configured role and timeframe. Required failed evidence produces `NO_TRADE`. Missing evidence produces `UNAVAILABLE`.

A successful run commits candles, observations, decisions, and the run receipt in SQLite. Notification delivery occurs after this commit. A notification error cannot change committed research evidence.

The production composition root registers exactly seven Python plugins. Six are closed-bar translations tied to pinned first-party source revisions; `project.risk_levels` is deterministic strategy-owned composition. No Pine runtime, chart object, or provider-specific branch participates in evaluation.
