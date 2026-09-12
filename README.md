# Trading Research Engine

Trading Research Engine is a command-line research engine for deterministic evaluation of completed public-market candles. The distribution and image slug is `trading-research-engine`. It does not trade.

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

- Host path: `${DATA_FOLDER}/trading_research.db`
- Container path: `/data/trading_research.db`
- Bind mount: `${DATA_FOLDER}:/data` (the only writable application bind)

Bootstrap the writable data directory and the one Discord secret before you start Compose. The
container runs as UID/GID `10001:10001`, so the host bind must be writable by that identity:

```bash
export DATA_FOLDER="/absolute/path/to/trading-research-data"
sudo install -d -m 0750 -o 10001 -g 10001 "$DATA_FOLDER"
install -d -m 0700 secrets
umask 077
read -rsp 'Discord webhook URL: ' DISCORD_WEBHOOK_URL && printf '\n'
printf '%s' "$DISCORD_WEBHOOK_URL" > secrets/discord_webhook_url
unset DISCORD_WEBHOOK_URL
docker compose config --quiet
docker compose build engine
docker compose up -d engine
```

The sample has one `discord_debug` destination for all five event types. It resolves only
`/run/secrets/discord_webhook_url`, mounted from `./secrets/discord_webhook_url`. Do not commit the
resolved endpoint, `.env`, `secrets/`, databases, backups, or logs. See `docs/operations.md` for the
copy-ready rotation, health, database, log, manual-run, and shutdown commands.

Read readiness and logs:

```sh
docker compose ps
uv run trading-research database status
docker compose logs --follow engine
```

The Compose health command reads the scheduler readiness marker at `/data/scheduler.ready` and confirms that its process is alive. Scheduler `READY` means that configuration validation and restart recovery completed. It does not prove provider synchronization, a successful strategy run, or Discord delivery; use logs and persisted run receipts for those outcomes.

Stop the scheduler without killing its process:

```sh
docker compose stop --timeout 30 engine
docker compose down
```

The image sends `SIGTERM` to the CLI. The scheduler stops new fires, applies its bounded child termination and reconciliation path, and writes a `SHUTDOWN` receipt.

## Configuration

Validate all configured files before an operation:

```sh
uv sync --dev
uv run trading-research validate \
  --strategy strategies/source-aligned-research.yaml \
  --market-data config/market-data.yaml \
  --schedule config/schedule.yaml \
  --notifications config/notifications.yaml
```

Validation checks YAML structure, types, provider IDs, schedule policy, notification references, and strategy dependencies. It does not resolve a notification endpoint. Endpoint resolution occurs only at the selected notification adapter boundary.

`config/market-data.yaml` selects OKX swap. `config/market-data.okx-swap.yaml` is an identical,
explicitly named example. `config/market-data.binance-usdm.yaml` is an inactive Binance USD-M
alternate. Runtime commands read only `${CONFIG_FOLDER}/market-data.yaml`. Copy the selected example
to that path, and keep its instrument IDs aligned with the strategy.

Bootstrap or inspect a local database:

```sh
export DATA_FOLDER="$(pwd)/data"
export CONFIG_FOLDER="$(pwd)/config"
uv run trading-research database bootstrap
uv run trading-research database status
```

Host commands derive `trading_research.db`, `engine.lock`, and `scheduler.ready` only from
`DATA_FOLDER`; commands that compose runtime services derive their configuration root only from
`CONFIG_FOLDER`. Both roots must be normalized absolute paths. Operation-level path overrides are
intentionally rejected.

The notifier dry test validates a bounded event payload without a delivery attempt:

```sh
uv run trading-research notifier-test \
  --notifications config/notifications.yaml \
  --event run_succeeded \
  --run-id fixture-run \
  --strategy-id source-aligned-research \
  --payload '{"status":"SUCCEEDED","event_time_ms":1725000000123,"instrument_count":2,"decision_count":0}'
```

For a matching Discord destination, the dry-run response includes the exact native `discord_preview`
card while keeping `delivery_attempted` false. It does not resolve the webhook secret or contact an
endpoint. Lifecycle cards are sent separately; terminal setup/no-setup results batch only within one
run and closed-bar boundary.

Run a manual receipt path:

```sh
export DATA_FOLDER="$(pwd)/data"
export CONFIG_FOLDER="$(pwd)/config"
uv run trading-research run \
  --strategy strategies/source-aligned-research.yaml \
  --notifications config/notifications.yaml \
  --trigger manual
```

The run loads market-data configuration from `${CONFIG_FOLDER}/market-data.yaml`.

The checked-in source-aligned strategy executes seven Python plugins over completed candles. Warm-up gaps produce `UNAVAILABLE`; fully evaluable gates that are not satisfied produce `NO_TRADE`. A `READY` result remains research evidence, not an order instruction.

## Deterministic backtesting

Backtests use the same global `config/market-data.yaml`, canonical candle store, strategy DAG, and ordered decision policy as normal research runs. A scenario selects one exact strategy and owns only its UTC period, conservative execution assumptions, costs, and immutable-output policy.

```sh
export DATA_FOLDER="$(pwd)/data"
export CONFIG_FOLDER="$(pwd)/config"
uv run trading-research backtest backtests/source-aligned-research/one-year-baseline.yaml
```

V1 accepts only `touch_limit` entries. A pending entry expires after the configured number of
execution bars. The simulator permits only one pending or open trade per instrument.

V1 also fixes these settings:

- `intrabar_conflict: stop_first` closes at the stop when one candle touches both exit levels.
- `allow_same_minute_target: false` prevents a target exit during the entry minute. A stop can still close the trade during that minute.
- `existing_result: fail` prevents replacement or repair of an existing result. An identical result is verified and reused. A different result causes an error.

The command synchronizes the requested period plus strategy warm-up under the shared writer lock, creates a short-lived SQLite online snapshot, releases the lock, and performs replay, simulation, and reporting offline. It publishes `${DATA_FOLDER}/backtests/<backtest-id>/` only after all six artifacts are complete:

- `manifest.json` version 2 binds the automatic installed-source `code_hash`, immutable input identities, and the byte size and SHA-256 of every payload artifact.
- `decisions.jsonl` and `pipeline-traces.jsonl` preserve every ordered evaluation, pass/reject/unavailable reason, and first rejection.
- `trades.jsonl` contains normalized, one-unit simulated outcomes without account sizing.
- `summary.json` contains overall, instrument, direction, disposition, and unavailable-reason metrics.
- `report.html` embeds each canonical evaluation as a deterministic directly addressable gzip record, plus a compact chunked filter index, so each page decompresses at most its 25 selected evaluations with no CDN or network dependency. Browsers without the standard `DecompressionStream` gzip primitive show an explicit compatibility failure instead of partial evidence.

An identical rerun verifies and reuses byte-identical output. If any existing artifact differs, the command fails without overwriting it. A failure before publication leaves no partial result directory and never writes backtest rows to the five production tables.

Version 1 report directories remain immutable historical evidence. Their manifests continue to
identify the recorded Git commit; they are never rewritten or assigned a `code_hash`. Newly
published version 2 manifests use only the automatically calculated source hash.

The offline report benchmark and Chromium probe do not access a provider. The benchmark creates
synthetic replay-shaped evaluations. It tests report publication and browser limits, not strategy
results, execution results, or provider performance.

```sh
uv sync --locked --all-groups
uv run --locked playwright install chromium
benchmark_root="$(mktemp -d)"
UV_NO_NETWORK=1 uv run --locked python scripts/benchmark_backtest_report.py \
  --output-root "$benchmark_root/reports" \
  --result-json "$benchmark_root/generation.json"
report="$(printf '%s\n' "$benchmark_root"/reports/*/report.html)"
UV_NO_NETWORK=1 uv run --locked python scripts/probe_backtest_report.py \
  "$report" \
  --result-json "$benchmark_root/browser.json"
```

The benchmark defaults to 210,240 synthetic evaluations across two instruments at five-minute
intervals. Each evaluation has 14 ordered trace steps. The JSON separates fixture generation from
report publication. The recorded time and memory values are measurements, not an SLA.

On a disposable hosted Linux worker, install Chromium and its system packages with the same locked
client that CI uses:

```sh
uv run --locked playwright install --with-deps chromium
```

This hosted-worker command can change system packages. For an existing local Chromium binary, omit
the browser install and pass `--chromium-executable /absolute/path/to/chromium` to the probe.

For Compose, stop the scheduled writer during a long historical fill and use the manual profile, which does not mount or resolve notification secrets:

```sh
docker compose stop engine
docker compose --profile manual run --rm manual \
  backtest /backtests/source-aligned-research/one-year-baseline.yaml
docker compose start engine
```

Start the scheduler outside Compose only for local diagnosis:

```sh
export DATA_FOLDER="$(pwd)/data"
export CONFIG_FOLDER="$(pwd)/config"
uv run trading-research scheduler \
  --schedule config/schedule.yaml
```

While that scheduler is running, read its readiness marker from another shell with the same
`DATA_FOLDER`:

```sh
uv run trading-research scheduler-health
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
docker compose stop engine
mkdir -p backups
sqlite3 "$DATA_FOLDER/trading_research.db" '.backup backups/trading_research.db'
sqlite3 backups/trading_research.db 'PRAGMA integrity_check;'
```

Restore only after you stop the service:

```sh
docker compose stop engine
cp backups/trading_research.db "$DATA_FOLDER/trading_research.db"
docker compose up -d engine
```

Upgrade and restart with the same bind mount:

```sh
docker compose pull
docker compose up -d --build
docker compose logs --tail 100 engine
```

If a process dies, start the service again. The advisory lock releases when the process dies. Scheduler startup marks stale `RUNNING` receipts as `FAILED` with `PROCESS_RESTART`. If the lock remains held, identify the process before you stop it:

```sh
lsof "$DATA_FOLDER/engine.lock"
docker compose ps
docker compose restart engine
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
