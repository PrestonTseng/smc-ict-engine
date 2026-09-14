# Operations

## Prepare the service and create the secret

The image runs as UID/GID `10001:10001`. Create the bind mount with that ownership, then read the
single Discord endpoint without placing it in the command line or shell history:

```bash
export DATA_FOLDER="/absolute/path/to/trading-research-data"
export CONFIG_FOLDER="$(pwd)/config"
sudo install -d -m 0750 -o 10001 -g 10001 "$DATA_FOLDER"
install -d -m 0700 secrets
mkdir -p backups
umask 077
read -rsp 'Discord webhook URL: ' DISCORD_WEBHOOK_URL && printf '\n'
printf '%s' "$DISCORD_WEBHOOK_URL" > secrets/discord_webhook_url
unset DISCORD_WEBHOOK_URL
uv run trading-research database bootstrap
uv run trading-research database status
docker compose config --quiet
docker compose build engine
docker compose up -d engine
```

`DATA_FOLDER` is the only writable application bind and must be exported and non-empty. Host
commands derive the database, lock, and scheduler health paths only from this normalized absolute
root. Runtime composition derives its configuration root only from the normalized absolute
`CONFIG_FOLDER`; operation-level path overrides are intentionally rejected. Compose supplies both
roots inside the container and requires the configured data, config, and strategy sources instead
of creating them.

Deployment and research provenance are separate. Record the Docker image ID or registry digest for
the deployed container; Compose intentionally uses the fixed local tag
`trading-research-engine:local`, which is only a mutable local name. Each process automatically
calculates a lowercase SHA-256 `code_hash` from the installed `trading_research` Python sources.
Run and backtest evidence uses that source hash and does not accept an operator-entered revision.

Rotate the secret atomically, then recreate the service so Compose remounts it:

```bash
umask 077
read -rsp 'Replacement Discord webhook URL: ' DISCORD_WEBHOOK_URL && printf '\n'
printf '%s' "$DISCORD_WEBHOOK_URL" > secrets/discord_webhook_url.new
unset DISCORD_WEBHOOK_URL
mv secrets/discord_webhook_url.new secrets/discord_webhook_url
docker compose up -d --force-recreate engine
```

## Read health and receipts

```sh
docker compose ps
docker compose logs --tail 100 engine
uv run trading-research database status
```

The checked-in cron fires at minutes `1,16,31,46`: four runs per hour, shortly after the four
15-minute boundaries. The status command reports persisted provider data through candle and run
totals. Scheduler `READY` proves only that configuration validation, recovery, and the scheduler
process are ready. It does not prove provider synchronization, strategy-run success, or Discord
delivery. Confirm provider synchronization, run success, and Discord delivery independently in the
service log and persisted run receipts.

## Run one manual receipt path

This uses the same image, configuration, data bind, and process lock as the scheduler. The manual
service has no Discord secret. This command does not load notification configuration or send
notifications. It can contact the configured provider. Use it only during an approved operator
window.

```sh
docker compose --profile manual run --rm manual run \
  --strategy /strategies/source-aligned-research.yaml \
  --trigger manual
```

The manual service supplies `DATA_FOLDER=/data` and `CONFIG_FOLDER=/config`; the run therefore
loads `/config/market-data.yaml` and uses the database and lock under `/data`.

## Do a notification dry test

Select an event enabled for at least one destination to receive its adapter preview. A filtered event
returns no matching destination or preview.

```sh
uv run trading-research notifier-test \
  --notifications config/notifications.yaml \
  --event run_failed \
  --run-id fixture-run \
  --strategy-id source-aligned-research \
  --payload '{"status":"FAILED","event_time_ms":1725000000123,"instrument_count":2,"error_category":"fixture_failure"}'
```

This command validates the notification configuration, event filters, and scalar payload. When the
event matches a Discord destination, it also returns the exact native `discord_preview`: human status
title and text, Discord `<t:...>` time, canonical UTC embed timestamp, and shortened identifiers. It
does not resolve a secret or contact an endpoint. The fixture timestamp is illustrative; use the
truthful lifecycle event time or closed-bar time for the event being previewed.

## Disable a schedule

Stop the service before you change `config/schedule.yaml`. Set `schedule.enabled` to `false`. Then validate and restart the service.

```sh
docker compose stop --timeout 30 engine
uv run trading-research validate --strategy strategies/source-aligned-research.yaml --market-data config/market-data.yaml --schedule config/schedule.yaml --notifications config/notifications.yaml
docker compose up -d engine
```

## Back up and restore SQLite

Stop the service before a restore. Use SQLite online backup for a backup.

```sh
mkdir -p backups
sqlite3 "$DATA_FOLDER/trading_research.db" '.backup backups/trading_research.db'
sqlite3 backups/trading_research.db 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
docker compose stop --timeout 30 engine
cp backups/trading_research.db "$DATA_FOLDER/trading_research.db"
docker compose up -d engine
```

## Stop the service

```sh
docker compose stop --timeout 30 engine
docker compose down
```

The scheduler stops new fires. It terminates, kills, drains, and reconciles an active child within the bounded shutdown path.

## Run an immutable backtest

For host execution, set normalized absolute runtime roots. The command hashes its installed source
payload automatically:

```sh
export DATA_FOLDER="$(pwd)/data"
export CONFIG_FOLDER="$(pwd)/config"
uv run trading-research backtest backtests/source-aligned-research/one-year-baseline.yaml
```

For Compose, avoid overlapping a long historical fill with the scheduler. The manual service has no Discord environment or secret mount:

```sh
docker compose stop engine
docker compose --profile manual run --rm manual \
  market-data sync-range --start 2025-06-03T00:00:00Z --end 2026-08-31T23:59:00Z
docker compose --profile manual run --rm manual \
  backtest /backtests/source-aligned-research/one-year-baseline.yaml
docker compose start engine
```

Open `${DATA_FOLDER}/backtests/<backtest-id>/report.html` locally. Treat JSON and JSONL as the
canonical audit evidence. Do not edit an existing result. Change the scenario version or assumptions
to produce a new identity.

## Production schema version 2 cutover and rollback

Do not cut over while the scheduler is active. First stop it and create a verified online backup of
the version 1 database. Build the candidate image, record its immutable image ID (or registry digest
when using a registry), then let the candidate open the database once. Opening a valid version 1
database transactionally rebuilds `runs`, preserves legacy `git_commit` values and related evidence,
and sets schema version 2. No historical `code_hash` is invented.

```sh
docker compose stop --timeout 30 engine
sqlite3 "$DATA_FOLDER/trading_research.db" '.backup backups/trading_research.pre-v2.db'
sqlite3 backups/trading_research.pre-v2.db 'PRAGMA integrity_check; PRAGMA foreign_key_check; PRAGMA user_version;'
docker compose build engine
docker image inspect trading-research-engine:local --format '{{.Id}}'
docker compose --profile manual run --rm manual database status
sqlite3 "$DATA_FOLDER/trading_research.db" 'PRAGMA integrity_check; PRAGMA foreign_key_check; PRAGMA user_version;'
docker compose up -d engine
```

Confirm `PRAGMA user_version;` returns `2`, both integrity commands are clean, health is ready, and
new `runs` rows contain `code_hash` with `git_commit` null before accepting the cutover.

Rollback requires both the pre-cutover database backup and the previously recorded image ID or
digest. Stop the candidate before restoring; version 1 software must never open the version 2 file.

```sh
docker compose stop --timeout 30 engine
cp backups/trading_research.pre-v2.db "$DATA_FOLDER/trading_research.db"
# Restore/re-tag the previously recorded image ID or digest as trading-research-engine:local.
docker compose up -d engine
docker compose ps
```
