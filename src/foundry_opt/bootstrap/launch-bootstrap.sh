#!/usr/bin/env bash
set -euo pipefail
repo="${1:?repository required}"
pin="${2:-}"
ref="${3:-main}"
work_root="${4:-.}"
runtime="${5:-python -m foundry_opt.cli}"
resolved="${pin:-$ref}"
sha="$(git ls-remote "$repo" "$resolved" | head -n1 | awk '{print $1}')"
test "${#sha}" -eq 40
archive_url="${repo%.git}/archive/${sha}.zip"
archive_path="${work_root}/foundry-opt-${sha}.zip"
curl -fsSL "$archive_url" -o "$archive_path"
extract_root="${work_root}/foundry-opt-${sha}"
python - <<'PY' "$archive_path" "$extract_root"
import sys, zipfile
zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])
PY
checkout="$(find "$extract_root" -mindepth 1 -maxdepth 1 -type d | head -n1)"
test -n "$checkout"
head_sha="$(git -C "$checkout" rev-parse HEAD)"
test "$head_sha" = "$sha"
lock_hash="$(python - <<'PY' "$checkout/uv.lock"
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
printf '{"sha":"%s","archive":"%s","uv_lock_sha256":"%s","runtime":"%s"}\n' "$sha" "$archive_url" "$lock_hash" "$runtime"
