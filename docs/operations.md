# Operations

## Prepare the service

```sh
mkdir -p data secrets backups
umask 077
printf '%s' 'https://your-approved-webhook.example/path' > secrets/discord_2_webhook_url
export DISCORD_1_WEBHOOK_URL='https://your-approved-webhook.example/path'
export SMC_ICT_GIT_COMMIT="$(git rev-parse HEAD)"
uv run smc-ict database bootstrap --database ./data/smc_ict.db
uv run smc-ict database status --database ./data/smc_ict.db
docker compose config -q
docker compose up -d --build
```

## Read health and receipts

```sh
docker compose ps
docker compose logs --tail 100 engine
docker compose exec engine smc-ict database status --database /data/smc_ict.db
```

The status command reports all persisted provider data through the candle and run totals. Run validation for each selected market-data authority before a provider change.

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
