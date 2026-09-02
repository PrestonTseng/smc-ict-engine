#!/usr/bin/env bash
set -euo pipefail

die() {
  printf 'DATA_FOLDER preflight failed: %s\n' "$1" >&2
  exit 2
}

if [[ -z "${DATA_FOLDER:-}" ]]; then
  die "DATA_FOLDER must be set and non-empty"
fi
if [[ "$DATA_FOLDER" != /* ]]; then
  die "DATA_FOLDER must be an absolute path"
fi

candidate=${DATA_FOLDER%/}
if [[ -z "$candidate" ]]; then
  candidate=/
fi
while true; do
  if [[ -L "$candidate" ]]; then
    die "DATA_FOLDER must not be a symlink or have a symlink ancestor: $candidate"
  fi
  if [[ "$candidate" == / ]]; then
    break
  fi
  candidate=${candidate%/*}
  if [[ -z "$candidate" ]]; then
    candidate=/
  fi
done

if [[ ! -e "$DATA_FOLDER" ]]; then
  die "DATA_FOLDER does not exist"
fi
if [[ ! -d "$DATA_FOLDER" ]]; then
  die "DATA_FOLDER is not a directory"
fi
