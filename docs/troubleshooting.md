# Troubleshooting

## `DEFERRED_PLUGIN`

This error is expected for the source-aligned strategy. Do not enable the plugin without approved conformance evidence.

## `PROCESS_RESTART`

The scheduler found a stale `RUNNING` row after process death. It changed the row to `FAILED` with `PROCESS_RESTART`.

If the engine lock remains held, find the owner before you stop it:

```sh
lsof "$DATA_FOLDER/engine.lock"
./scripts/compose.sh ps
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
