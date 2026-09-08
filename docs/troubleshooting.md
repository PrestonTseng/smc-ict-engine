# Troubleshooting

## `DEFERRED_PLUGIN`

This error is expected for the source-aligned strategy. Do not enable the plugin without approved conformance evidence.

## `PROCESS_RESTART`

The scheduler found a stale `RUNNING` row after process death. It changed the row to `FAILED` with `PROCESS_RESTART`.

If the engine lock remains held, find the owner before you stop it:

```sh
lsof "$DATA_FOLDER/engine.lock"
docker compose ps
```

Do not delete an active lock file. The lock uses the inode, and file deletion can permit a second owner.

## `MAXIMUM_RUNTIME` or `SCHEDULER_SHUTDOWN`

The scheduler stopped a child process. It first sends termination. Then it kills and drains a child that does not stop.

Examine the JSON receipt and the durable run row. The run must have `FAILED` status and the same bounded reason.

## Notification warnings

A partial delivery error does not roll back research evidence. Examine the destination ID and bounded error category in the receipt or log.

Do not paste a webhook URL into a ticket. Correct the secret source, then run the dry test.

## Database errors

Stop the engine before a restore. Run these commands against the backup:

```sh
sqlite3 backups/smc_ict.db 'PRAGMA integrity_check;'
sqlite3 backups/smc_ict.db 'PRAGMA foreign_key_check;'
```

## `existing backtest result differs`

The deterministic backtest ID already exists but at least one artifact byte differs. The engine will not overwrite or repair it. Preserve the directory for investigation, compare every file with `manifest.json`, and rerun only after restoring the original immutable result or selecting a legitimately changed scenario that produces a new ID.

## `snapshot candle range is incomplete`

The offline snapshot does not contain an exact continuous required range, or a candle identity does not match the globally configured provider/instrument. Rerun `market-data sync-range` for the scenario period plus strategy warm-up and investigate any source-conflict or provider failure. No partial report is published for a snapshot candle range failure.

## Backtest lock or interrupted publication

`backtest cannot overlap the active writer` means the scheduler or another operation owns `DATA_FOLDER/engine.lock`. Stop the scheduler through its normal shutdown path; never delete an active lock file. Temporary `.backtest-*` snapshot directories and dot-prefixed report staging directories are removed on handled failures. A visible `<backtest-id>` directory is either complete and byte-verified or treated as an immutable conflict.
