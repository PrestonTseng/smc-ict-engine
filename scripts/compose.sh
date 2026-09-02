#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ROOT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)

"$SCRIPT_DIR/preflight-data-folder.sh"
cd "$ROOT_DIR"
exec docker compose "$@"
