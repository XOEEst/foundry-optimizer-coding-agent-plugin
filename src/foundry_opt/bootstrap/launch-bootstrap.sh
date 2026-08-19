#!/usr/bin/env bash
set -euo pipefail
repo="${1:?repository required}"
shift
if (( $# > 0 )); then expected_lock="$1"; shift; else expected_lock=""; fi
if (( $# > 0 )); then pin="$1"; shift; else pin=""; fi
if (( $# > 0 )); then ref="$1"; shift; else ref="main"; fi
if (( $# > 0 )); then work_root="$1"; shift; else work_root="."; fi
if [[ -n "$pin" && "$pin" =~ ^[0-9a-f]{40}$ ]]; then
  sha="$pin"
else
  query="${pin:-refs/heads/$ref}"
  sha="$(git ls-remote "$repo" "$query" | head -n1 | awk '{print $1}')"
fi
test "${#sha}" -eq 40
if [[ -n "$pin" && -z "$expected_lock" ]]; then
  echo "an explicit pin requires the expected uv.lock SHA-256" >&2
  exit 1
fi
extract_root="${work_root}/foundry-opt-${sha}"
rm -rf "$extract_root"
mkdir -p "$extract_root"
git -C "$extract_root" init >/dev/null
git -C "$extract_root" remote add origin "$repo"
git -C "$extract_root" fetch --depth 1 origin "$sha" >/dev/null
git -C "$extract_root" checkout --detach "$sha" >/dev/null
head_sha="$(git -C "$extract_root" rev-parse HEAD)"
test "$head_sha" = "$sha"
python_executable="$(command -v python3 || command -v python || true)"
if [[ -z "$python_executable" ]]; then
  echo "python3 or python is required" >&2
  exit 1
fi
lock_hash="$("$python_executable" - <<'PY' "$extract_root/uv.lock"
import hashlib, pathlib, sys
print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
if [[ -n "$expected_lock" && "$expected_lock" != "$lock_hash" ]]; then
  echo "uv.lock hash mismatch" >&2
  exit 1
fi
uv sync --frozen --project "$extract_root" >/dev/null
export FOUNDRY_OPT_RUNTIME_REPOSITORY="$repo"
export FOUNDRY_OPT_RUNTIME_COMMIT="$sha"
export FOUNDRY_OPT_RUNTIME_LOCK_SHA256="$lock_hash"
exec uv run --no-sync --project "$extract_root" foundry-opt "$@"
