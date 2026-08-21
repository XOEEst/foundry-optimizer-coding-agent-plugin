#!/usr/bin/env bash
set -euo pipefail

fail() {
  echo "$1" >&2
  exit 1
}

is_hex() {
  local value="$1"
  local length="$2"
  [[ "$value" =~ ^[0-9a-f]{$length}$ ]]
}

validate_package_path() {
  local value="$1"
  [[ -n "$value" ]] || fail "package_path is required"
  if [[ "$value" == "." ]]; then
    printf '.\n'
    return
  fi
  [[ "$value" != /* ]] || fail "package_path must stay relative to the runtime checkout"
  [[ "$value" != *\\* ]] || fail "package_path must use repository-relative '/' separators"
  IFS=/ read -r -a segments <<<"$value"
  [[ "${#segments[@]}" -gt 0 ]] || fail "package_path must stay relative to the runtime checkout"
  local segment
  for segment in "${segments[@]}"; do
    [[ -n "$segment" && "$segment" != "." && "$segment" != ".." ]] || fail "package_path must stay relative to the runtime checkout"
  done
  printf '%s\n' "$value"
}

python_executable="$(command -v python3 || command -v python || true)"
[[ -n "$python_executable" ]] || fail "python3 or python is required"

load_skill_lock() {
  local lock_path="$1"
  local resolved_output
  resolved_output="$("$python_executable" - "$lock_path" <<'PY'
import json
import pathlib
import re
import sys

path = pathlib.Path(sys.argv[1])
document = json.loads(path.read_text(encoding="utf-8"))
if not isinstance(document, dict):
    raise SystemExit("skill lock must contain a JSON object")
required_fields = (
    "runtime_repository",
    "runtime_commit",
    "uv_lock_sha256",
    "package_path",
)
for field in required_fields:
    value = document.get(field)
    if not isinstance(value, str) or not value:
        raise SystemExit(f"skill lock is missing {field}")
runtime_commit = document["runtime_commit"].lower()
lock_sha = document["uv_lock_sha256"].lower()
if not re.fullmatch(r"[0-9a-f]{40}", runtime_commit):
    raise SystemExit("skill lock runtime_commit must be a full 40 character SHA")
if not re.fullmatch(r"[0-9a-f]{64}", lock_sha):
    raise SystemExit("skill lock uv_lock_sha256 must be a 64 character SHA-256")
print(document["runtime_repository"])
print(runtime_commit)
print(lock_sha)
print(document["package_path"])
PY
)"
  mapfile -t resolved_contract <<<"$resolved_output"
  repository="${resolved_contract[0]}"
  sha="${resolved_contract[1]}"
  expected_lock="${resolved_contract[2]}"
  package_path="$(validate_package_path "${resolved_contract[3]}")"
}

repository=""
expected_lock=""
pin=""
ref="main"
work_root="."
package_path="."
skill_lock=""

if (( $# > 0 )) && [[ "$1" == "--skill-lock" ]]; then
  shift
  (( $# > 0 )) || fail "--skill-lock requires a path"
  skill_lock="$1"
  shift
  while (( $# > 0 )); do
    case "$1" in
      --work-root)
        shift
        (( $# > 0 )) || fail "--work-root requires a path"
        work_root="$1"
        shift
        ;;
      --)
        shift
        break
        ;;
      *)
        fail "Unexpected option '$1' after --skill-lock"
        ;;
    esac
  done
  load_skill_lock "$skill_lock"
else
  repository="${1:?repository required}"
  shift
  if (( $# > 0 )); then expected_lock="$1"; shift; fi
  if (( $# > 0 )); then pin="$1"; shift; fi
  if (( $# > 0 )); then ref="$1"; shift; else ref="main"; fi
  if (( $# > 0 )); then work_root="$1"; shift; else work_root="."; fi
  [[ -n "$expected_lock" ]] || fail "An exact uv.lock SHA-256 is required"
  [[ -n "$pin" ]] || fail "An exact runtime commit is required; floating refs like '$ref' are not allowed for privileged use"
  pin="${pin,,}"
  expected_lock="${expected_lock,,}"
  is_hex "$pin" 40 || fail "Pin must be a full 40 character SHA"
  is_hex "$expected_lock" 64 || fail "Expected uv.lock SHA-256 must be a 64 character SHA-256"
  package_path="$(validate_package_path "$package_path")"
  sha="$pin"
fi

mkdir -p "$work_root"
work_root="$(cd -- "$work_root" && pwd -P)"
extract_root="$work_root/foundry-opt-$sha"
if [[ -e "$extract_root" ]]; then
  [[ ! -L "$extract_root" ]] || fail "Refusing to delete symlinked checkout path"
  [[ "$(basename -- "$extract_root")" == "foundry-opt-$sha" ]] || fail "Refusing to delete unexpected checkout path"
  [[ "$(cd -- "$(dirname -- "$extract_root")" && pwd -P)" == "$work_root" ]] || fail "Refusing to delete checkout outside the requested work root"
  rm -rf -- "$extract_root"
fi
mkdir -p "$extract_root"
git -C "$extract_root" init >/dev/null
git -C "$extract_root" remote add origin "$repository"
git -C "$extract_root" fetch --depth 1 origin "$sha" >/dev/null
git -C "$extract_root" checkout --detach "$sha" >/dev/null
head_sha="$(git -C "$extract_root" rev-parse HEAD)"
[[ "$head_sha" == "$sha" ]] || fail "Commit verification failed"

if [[ "$package_path" == "." ]]; then
  project_root="$extract_root"
else
  project_root="$extract_root/$package_path"
fi
[[ -d "$project_root" ]] || fail "package_path does not exist in the runtime checkout"
lock_path="$project_root/uv.lock"
[[ -f "$lock_path" ]] || fail "uv.lock missing"
lock_hash="$("$python_executable" - "$lock_path" <<'PY'
import hashlib
import pathlib
import sys

print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())
PY
)"
[[ "$expected_lock" == "$lock_hash" ]] || fail "uv.lock hash mismatch"

export FOUNDRY_OPT_RUNTIME_REPOSITORY="$repository"
export FOUNDRY_OPT_RUNTIME_COMMIT="$sha"
export FOUNDRY_OPT_RUNTIME_LOCK_SHA256="$lock_hash"
export FOUNDRY_OPT_RUNTIME_PACKAGE_PATH="$package_path"

uv sync --frozen --project "$project_root" >/dev/null
if [[ "${FOUNDRY_BOOTSTRAP_EMIT_RUNTIME_PYTHON:-}" == "1" ]]; then
  uv run --no-sync --project "$project_root" python -c 'import sys; print(sys.executable)'
  exit $?
fi
exec uv run --no-sync --project "$project_root" foundry-opt "$@"
