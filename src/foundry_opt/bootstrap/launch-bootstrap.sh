#!/usr/bin/env bash
set -euo pipefail

script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)"
repo_root="$(cd -- "$script_dir/../../.." && pwd -P)"
launcher="$repo_root/plugins/foundry-bootstrap/scripts/install-runtime.sh"

if [[ ! -f "$launcher" ]]; then
  echo "Canonical launcher not found at $launcher. This compatibility wrapper only works from a source checkout." >&2
  exit 1
fi

exec bash "$launcher" "$@"
