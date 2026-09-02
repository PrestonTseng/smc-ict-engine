# Operations

## Prepare the service and create the secret

The image runs as UID/GID `10001:10001`. Create the bind mount with that ownership, then read the
single Discord endpoint without placing it in the command line or shell history:

```bash
sudo install -d -m 0750 -o 10001 -g 10001 data
install -d -m 0700 secrets
mkdir -p backups
umask 077
read -rsp 'Discord webhook URL: ' DISCORD_WEBHOOK_URL && printf '\n'
printf '%s' "$DISCORD_WEBHOOK_URL" > secrets/discord_webhook_url
unset DISCORD_WEBHOOK_URL
export SMC_ICT_GIT_COMMIT="$(git rev-parse HEAD)"
export DATA_FOLDER="${DATA_FOLDER:-./data}"
uv run smc-ict database bootstrap --database ./data/smc_ict.db
uv run smc-ict database status --database ./data/smc_ict.db
docker compose config --quiet
docker compose build engine
docker compose up -d engine
```

`DATA_FOLDER` is the only writable application bind and must already exist as a real directory.
Compose rejects a missing configured data, config, or strategy source rather than creating it. Do
not use a symlink for `DATA_FOLDER`; use the real directory that owns the SQLite state.

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
docker compose exec engine smc-ict database status --database /data/smc_ict.db
```

The checked-in cron fires at minutes `1,16,31,46`: four runs per hour, shortly after the four
15-minute boundaries. The status command reports persisted provider data through candle and run
totals. Scheduler `READY` proves only that configuration validation, recovery, and the scheduler
process are ready. It does not prove provider synchronization, strategy-run success, or Discord
delivery. Confirm provider synchronization, run success, and Discord delivery independently in the
service log and persisted run receipts.

## Run one manual receipt path

This uses the same image, configuration, data bind, process lock, and Discord secret as the
scheduler. It can contact the configured provider and destination; use it only during an approved
operator window.

```sh
docker compose --profile manual run --rm manual run \
  --strategy /strategies/source-aligned-research.yaml \
  --market-data /config/market-data.yaml \
  --notifications /config/notifications.yaml \
  --database /data/smc_ict.db \
  --lock /data/engine.lock \
  --trigger manual
```

## Do a notification dry test

```sh
uv run smc-ict notifier-test \
  --notifications config/notifications.yaml \
  --event run_succeeded \
  --run-id fixture-run \
  --strategy-id source-aligned-research \
  --payload '{"decision_count":0}'
```

This command validates the notification configuration, event filters, and scalar payload. It does not resolve a secret or contact an endpoint.

## Disable a schedule

Stop the service before you change `config/schedule.yaml`. Set `schedule.enabled` to `false`. Then validate and restart the service.

```sh
docker compose stop --timeout 30 engine
uv run smc-ict validate --strategy strategies/source-aligned-research.yaml --market-data config/market-data.yaml --schedule config/schedule.yaml --notifications config/notifications.yaml
docker compose up -d engine
```

## Back up and restore SQLite

Stop the service before a restore. Use SQLite online backup for a backup.

```sh
mkdir -p backups
sqlite3 ./data/smc_ict.db '.backup backups/smc_ict.db'
sqlite3 backups/smc_ict.db 'PRAGMA integrity_check; PRAGMA foreign_key_check;'
docker compose stop --timeout 30 engine
cp backups/smc_ict.db ./data/smc_ict.db
docker compose up -d engine
```

## Stop the service

```sh
docker compose stop --timeout 30 engine
docker compose down
```

The scheduler stops new fires. It terminates, kills, drains, and reconciles an active child within the bounded shutdown path.
