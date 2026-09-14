# Architecture

The package has provider-neutral domain and application layers. Concrete market, notification, and SQLite adapters stay outside the core.

`configuration` loads strict YAML and computes redacted deterministic hashes. `composition` selects closed registries and builds runtime services. `cli` parses commands and emits JSON receipts.

The `EngineRunner` acquires the shared process lock. It loads all authorities before market I/O. Then it stores a `RUNNING` receipt and emits `run_started`.

The engine synchronizes completed one-minute candles. It resamples only complete higher-timeframe windows. The configured DAG creates observations and decisions.

SQLite schema version 2 has exactly five tables: `candles_1m`, `sync_state`, `runs`, `observations`, and `decisions`. A successful run commits its evidence atomically. New run rows identify the automatically hashed installed Python payload with `code_hash`. Migrated version 1 rows retain their original `git_commit` and never receive a manufactured hash. The `runs` table also holds validated, redacted notification deduplication JSON so suppression survives child-process boundaries without a sixth table or sidecar store.

The scheduler owns explicit child processes. Startup recovery uses the same process lock. Shutdown stops new fires before it stops and reconciles an active child.

Notification routing is sequential and deterministic by destination ID. Each destination has isolated construction, filtering, batching, retries, and receipts.

Backtesting is a separate read-only evidence flow. It runs the shared historical-readiness service first, then creates a short-lived SQLite snapshot while holding the existing writer lock. After the snapshot is complete, point-in-time replay, conservative execution simulation, metrics, and rendering use only the immutable snapshot and make no provider requests.

The report adapter stages all machine-readable files and the self-contained HTML in a sibling directory, syncs them, and exposes the complete result with one atomic rename. An existing identity is reusable only when every artifact byte matches. Backtests do not add or mutate production tables: SQLite remains exactly `candles_1m`, `sync_state`, `runs`, `observations`, and `decisions`.
