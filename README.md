# smc-ict-engine

`smc-ict-engine` is a command-line research engine for deterministic evaluation of completed public-market candles. It does not trade.

The engine stores research receipts in SQLite. It has no web service, order path, broker credentials, or live-trading feature.

## Architecture

The package has these boundaries:

- `domain` contains immutable candle, observation, decision, and evidence values.
- `configuration` loads strict YAML and computes non-secret configuration hashes.
- `application` owns the indicator DAG, persistence transaction, notifications, process lock, and scheduler policy.
- `adapters` contain Binance USD-M, OKX swap, SQLite, and generic HTTPS webhook boundaries.
- `composition` selects closed registries and builds the shared `EngineRunner`.
- `cli` only parses commands and writes JSON receipts.

Manual and scheduled runs use the same `EngineRunner`. A process lock prevents concurrent runs. A restart marks interrupted `RUNNING` rows as failed with `PROCESS_RESTART`.

## Docker Compose operation

The Compose service owns the internal scheduler. Do not install host cron for this deployment.

The fixed database contract is:

- Host path: `${DATA_FOLDER}/smc_ict.db`
- Container path: `/data/smc_ict.db`
- Bind mount: `${DATA_FOLDER}:/data` (the only writable application bind)

Bootstrap the writable data directory and the one Discord secret before you start Compose. The
container runs as UID/GID `10001:10001`, so the host bind must be writable by that identity:

```bash
export DATA_FOLDER="/absolute/path/to/smc-ict-data"
sudo install -d -m 0750 -o 10001 -g 10001 "$DATA_FOLDER"
install -d -m 0700 secrets
umask 077
read -rsp 'Discord webhook URL: ' DISCORD_WEBHOOK_URL && printf '\n'
printf '%s' "$DISCORD_WEBHOOK_URL" > secrets/discord_webhook_url
unset DISCORD_WEBHOOK_URL
export SMC_ICT_GIT_COMMIT="$(git rev-parse HEAD)"
./scripts/preflight-data-folder.sh
./scripts/compose.sh config --quiet
./scripts/compose.sh build engine
./scripts/compose.sh up -d engine
```

The sample has one `discord_debug` destination for all five event types. It resolves only
`/run/secrets/discord_webhook_url`, mounted from `./secrets/discord_webhook_url`. Do not commit the
resolved endpoint, `.env`, `secrets/`, databases, backups, or logs. See `docs/operations.md` for the
copy-ready rotation, health, database, log, manual-run, and shutdown commands.

Read readiness and logs:

```sh
./scripts/compose.sh ps
uv run smc-ict database status --database "$DATA_FOLDER/smc_ict.db"
./scripts/compose.sh logs --follow engine
```

The Compose health command reads the scheduler readiness marker at `/data/scheduler.ready` and confirms that its process is alive. Scheduler `READY` means that configuration validation and restart recovery completed. It does not prove provider synchronization, a successful strategy run, or Discord delivery; use logs and persisted run receipts for those outcomes.

Stop the scheduler without killing its process:

```sh
./scripts/compose.sh stop --timeout 30 engine
./scripts/compose.sh down
```

The image sends `SIGTERM` to the CLI. The scheduler stops new fires, applies its bounded child termination and reconciliation path, and writes a `SHUTDOWN` receipt.

## Configuration

Validate all configured files before an operation:

```sh
uv sync --dev
uv run smc-ict validate \
  --strategy strategies/source-aligned-research.yaml \
  --market-data config/market-data.yaml \
  --schedule config/schedule.yaml \
  --notifications config/notifications.yaml
```

Validation checks YAML structure, types, provider IDs, schedule policy, notification references, and strategy dependencies. It does not resolve a notification endpoint. Endpoint resolution occurs only at the selected notification adapter boundary.

`config/market-data.yaml` selects OKX swap. `config/market-data.binance-usdm.yaml` remains an inactive Binance USD-M alternate. Keep the configured instrument IDs aligned with the selected provider symbols. Use one market-data file per run.

Bootstrap or inspect a local database:

```sh
uv run smc-ict database bootstrap --database "$DATA_FOLDER/smc_ict.db"
uv run smc-ict database status --database "$DATA_FOLDER/smc_ict.db"
```

The notifier dry test validates a bounded event payload without a delivery attempt:

```sh
uv run smc-ict notifier-test \
  --notifications config/notifications.yaml \
  --event run_succeeded \
  --run-id fixture-run \
  --strategy-id source-aligned-research \
  --payload '{"decision_count":0}'
```

Run a manual receipt path:

```sh
uv run smc-ict run \
  --strategy strategies/source-aligned-research.yaml \
  --market-data config/market-data.yaml \
  --notifications config/notifications.yaml \
  --database "$DATA_FOLDER/smc_ict.db" \
  --lock "$DATA_FOLDER/engine.lock" \
  --trigger manual
```

The checked-in source-aligned strategy executes seven Python plugins over completed candles. Warm-up gaps produce `UNAVAILABLE`; fully evaluable gates that are not satisfied produce `NO_TRADE`. A `READY` result remains research evidence, not an order instruction.

Start the scheduler outside Compose only for local diagnosis:

```sh
uv run smc-ict scheduler \
  --schedule config/schedule.yaml \
  --database "$DATA_FOLDER/smc_ict.db" \
  --lock "$DATA_FOLDER/engine.lock" \
  --config-root config
```

## Strategy DAG authoring

A strategy YAML file owns its name, version, instruments, history, roles, signal instances, parameters, dependencies, and order. The engine core does not select a strategy formula.

Give each role one completed-candle timeframe. Make every dependency explicit. Give each signal instance a unique ID and increasing order. A required failed gate produces `NO_TRADE`. Missing required evidence produces `UNAVAILABLE`.

See `docs/strategy-examples.md` for role and dependency guidance. The seven checked-in registrations have fixed role, timeframe, and dependency contracts; the strict loader rejects mismatches before execution.

## Notification destinations

Each enabled destination has an adapter, event filter, secret reference, timeout, retry policy, deduplication window, batching values, redaction names, and warning failure policy. The first queued terminal event opens the destination's `flush_seconds` deadline; the next event at or after that deadline flushes the queue before it is accepted. Reaching `maximum_events` flushes immediately, and process finalization flushes every remaining event without a timer thread.

The router delivers matching destinations in identifier order. A failed destination does not prevent another destination from receiving the event. Delivery occurs after the engine commits its run evidence. Successful destination-scoped deduplication identities survive manual and scheduled child processes in the existing SQLite `runs` table. The durable JSON contains only the destination ID, deduplication ID, and successful delivery time; receipts and durable state do not contain endpoint values or notification payloads.

For a partial failure, read the service log, then examine the destination identifier and bounded error category. Do not paste a resolved endpoint into a ticket or shell history. Correct the secret reference or remote service, then run the dry test and the next scheduled or manual receipt path.

## Recovery and backup

Stop the engine before a backup or restore. SQLite backups must use a consistent database state.

```sh
./scripts/compose.sh stop engine
mkdir -p backups
sqlite3 "$DATA_FOLDER/smc_ict.db" '.backup backups/smc_ict.db'
sqlite3 backups/smc_ict.db 'PRAGMA integrity_check;'
```

Restore only after you stop the service:

```sh
./scripts/compose.sh stop engine
cp backups/smc_ict.db "$DATA_FOLDER/smc_ict.db"
./scripts/compose.sh up -d engine
```

Upgrade and restart with the same bind mount:

```sh
./scripts/compose.sh pull
./scripts/compose.sh up -d --build
./scripts/compose.sh logs --tail 100 engine
```

If a process dies, start the service again. The advisory lock releases when the process dies. Scheduler startup marks stale `RUNNING` receipts as `FAILED` with `PROCESS_RESTART`. If the lock remains held, identify the process before you stop it:

```sh
lsof "$DATA_FOLDER/engine.lock"
./scripts/compose.sh ps
./scripts/compose.sh restart engine
```

## Source provenance and license boundary

`provenance/sources.yaml` records source metadata and hashes. `LICENSES/README.md` records the unresolved repository license gate. No Pine source is vendored in this repository.

The six source-derived registrations are original Python closed-bar translations of the pinned source revisions; the seventh is strategy-owned risk composition. This project makes no TradingView-execution-equivalence, profitability, commercial-license, or performance claim.

`docs/deployment-design.html` is a durable local deployment design reference. It contains no agent-local path and no source code from external indicators.

## Financial-risk boundary

This software is research-only. It does not submit orders, calculate position size, promise returns, or replace risk controls. A `READY` result is not a trading instruction.

Use an independent risk process before you act on market information. Do not use this engine as the sole input for a financial decision.

## Development

```sh
uv sync --dev
uv run pytest
uv run ruff format --check .
uv run ruff check .
uv run mypy
uv build
```
