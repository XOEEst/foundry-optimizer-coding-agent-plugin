from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from foundry_opt.packaging.deterministic_zip import (
    DeterministicZipBuilder,
    DeterministicZipResult,
    verify_deterministic_zip,
)
from foundry_opt.poc.config import validate_repository_relative_path


ARTIFACT_NAME = "foundry-bootstrap-skill"
ARCHIVE_ROOT = "foundry-bootstrap"
SKILL_LOCK_FILENAME = "skill.lock.json"
CHECKSUM_MANIFEST_FILENAME = f"{ARTIFACT_NAME}.checksums.json"
ZIP_FILENAME = f"{ARTIFACT_NAME}.zip"
SKILL_LOCK_TEMPLATE = {
    "schema_version": "__SCHEMA_VERSION__",
    "runtime_repository": "__RUNTIME_REPOSITORY__",
    "runtime_commit": "__RUNTIME_COMMIT__",
    "uv_lock_sha256": "__UV_LOCK_SHA256__",
    "package_path": "__PACKAGE_PATH__",
}
ARCHIVE_FILES = (
    "foundry-bootstrap/SKILL.md",
    "foundry-bootstrap/scripts/install-runtime.ps1",
    "foundry-bootstrap/scripts/install-runtime.sh",
    "foundry-bootstrap/skill.lock.json",
)
_GIT_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_HEX_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PLACEHOLDER = re.compile(r"^__[A-Z0-9_]+__$")


class FoundryBootstrapReleaseError(ValueError):
    """The bootstrap skill release contract is invalid."""


@dataclass(frozen=True, slots=True)
class RuntimeProvenance:
    runtime_repository: str
    runtime_commit: str
    uv_lock_sha256: str
    package_path: str = "."
    schema_version: int = 1

    def to_skill_lock_document(self) -> dict[str, object]:
        return {
            "package_path": self.package_path,
            "runtime_commit": self.runtime_commit,
            "runtime_repository": self.runtime_repository,
            "schema_version": self.schema_version,
            "uv_lock_sha256": self.uv_lock_sha256,
        }


@dataclass(frozen=True, slots=True)
class FoundryBootstrapReleaseResult:
    dist_root: Path
    package_directory: Path
    zip_path: Path
    manifest_path: Path
    skill_lock_path: Path
    runtime: RuntimeProvenance
    package_tree_sha256: str
    zip_sha256: str
    zip_size_bytes: int
    files: tuple[str, ...]


def _no_deadline() -> None:
    return None


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
        + b"\n"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _run_git(repository_root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown git failure"
        raise FoundryBootstrapReleaseError(f"git {' '.join(arguments)} failed: {detail}")
    return completed.stdout.strip()


def _normalize_repository_url(value: str) -> str:
    raw = value.strip()
    ssh_match = re.fullmatch(
        r"git@github\.com:(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+?)(?:\.git)?",
        raw,
    )
    if ssh_match is not None:
        owner = ssh_match.group("owner")
        repo = ssh_match.group("repo")
        return f"https://github.com/{owner}/{repo}.git"

    parsed = urlsplit(raw)
    if parsed.hostname != "github.com":
        raise FoundryBootstrapReleaseError(
            "runtime repository must resolve to a public github.com URL"
        )
    segments = [segment for segment in parsed.path.split("/") if segment]
    if len(segments) != 2:
        raise FoundryBootstrapReleaseError(
            "runtime repository must identify exactly one GitHub repository"
        )
    owner, repo = segments
    repo = repo[:-4] if repo.endswith(".git") else repo
    if not owner or not repo:
        raise FoundryBootstrapReleaseError("runtime repository path is incomplete")
    return f"https://github.com/{owner}/{repo}.git"


def _validate_runtime_provenance(
    *,
    runtime_repository: str,
    runtime_commit: str,
    uv_lock_sha256: str,
    package_path: str,
    schema_version: int,
) -> RuntimeProvenance:
    if _PLACEHOLDER.fullmatch(runtime_repository.strip()) is not None:
        raise FoundryBootstrapReleaseError("runtime_repository placeholder was not resolved")
    normalized_repository = _normalize_repository_url(runtime_repository)
    normalized_commit = runtime_commit.strip().lower()
    if _PLACEHOLDER.fullmatch(normalized_commit) is not None:
        raise FoundryBootstrapReleaseError("runtime_commit placeholder was not resolved")
    if _PLACEHOLDER.fullmatch(uv_lock_sha256.strip()) is not None:
        raise FoundryBootstrapReleaseError("uv_lock_sha256 placeholder was not resolved")
    if _PLACEHOLDER.fullmatch(package_path.strip()) is not None:
        raise FoundryBootstrapReleaseError("package_path placeholder was not resolved")
    if schema_version != 1:
        raise FoundryBootstrapReleaseError("schema_version must be 1")
    if _GIT_COMMIT.fullmatch(normalized_commit) is None:
        raise FoundryBootstrapReleaseError("runtime_commit must be a full 40 character SHA")
    normalized_lock = uv_lock_sha256.strip().lower()
    if _HEX_SHA256.fullmatch(normalized_lock) is None:
        raise FoundryBootstrapReleaseError("uv_lock_sha256 must be a 64 character SHA-256")
    normalized_package_path = (
        "."
        if package_path == "."
        else validate_repository_relative_path(package_path, field="package_path")
    )
    return RuntimeProvenance(
        runtime_repository=normalized_repository,
        runtime_commit=normalized_commit,
        uv_lock_sha256=normalized_lock,
        package_path=normalized_package_path,
        schema_version=schema_version,
    )


def _resolve_repository_root(repository_root: Path | str) -> Path:
    resolved = Path(repository_root).resolve(strict=True)
    if not resolved.is_dir():
        raise FoundryBootstrapReleaseError("repository_root must be a directory")
    return resolved


def _resolve_dist_root(dist_root: Path | str) -> Path:
    resolved = Path(dist_root).resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise FoundryBootstrapReleaseError("dist_root must be a directory")
    return resolved


def _delete_output(path: Path) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        raise FoundryBootstrapReleaseError(f"refusing to replace symlinked output: {path}")
    if path.is_dir():
        shutil.rmtree(path)
        return
    if not path.is_file():
        raise FoundryBootstrapReleaseError(f"output path is not a regular file: {path}")
    path.unlink()


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


def _load_skill_lock_template(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FoundryBootstrapReleaseError(f"could not read skill lock template: {path}") from exc
    except json.JSONDecodeError as exc:
        raise FoundryBootstrapReleaseError("skill lock template is not valid JSON") from exc
    if payload != SKILL_LOCK_TEMPLATE:
        raise FoundryBootstrapReleaseError(
            "skill lock template must remain the checked-in placeholder contract"
        )
    return payload


def _copy_required_source_file(source_root: Path, destination_root: Path, relative_path: str) -> None:
    source = source_root / relative_path
    destination = destination_root / ARCHIVE_ROOT / relative_path
    if not source.is_file():
        raise FoundryBootstrapReleaseError(f"missing bootstrap skill file: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)


def infer_runtime_provenance(
    repository_root: Path | str,
    *,
    runtime_repository: str | None = None,
    runtime_commit: str | None = None,
    package_path: str = ".",
    schema_version: int = 1,
) -> RuntimeProvenance:
    resolved_root = _resolve_repository_root(repository_root)
    resolved_package_path = (
        "."
        if package_path == "."
        else validate_repository_relative_path(package_path, field="package_path")
    )
    resolved_repository = runtime_repository or _run_git(
        resolved_root,
        "config",
        "--get",
        "remote.origin.url",
    )
    resolved_commit = runtime_commit or _run_git(
        resolved_root,
        "rev-parse",
        "HEAD",
    )
    lock_root = resolved_root if resolved_package_path == "." else resolved_root / resolved_package_path
    lock_path = lock_root / "uv.lock"
    if not lock_path.is_file():
        raise FoundryBootstrapReleaseError(f"uv.lock is missing: {lock_path}")
    return _validate_runtime_provenance(
        runtime_repository=resolved_repository,
        runtime_commit=resolved_commit,
        uv_lock_sha256=_sha256_file(lock_path),
        package_path=resolved_package_path,
        schema_version=schema_version,
    )


def _build_manifest(
    *,
    zip_result: DeterministicZipResult,
    skill_lock_path: Path,
    runtime: RuntimeProvenance,
) -> dict[str, object]:
    return {
        "artifact_path": ZIP_FILENAME,
        "artifact_sha256": zip_result.zip_sha256,
        "artifact_size_bytes": zip_result.size_bytes,
        "package_directory": ARTIFACT_NAME,
        "package_files": list(zip_result.files),
        "package_root": ARCHIVE_ROOT,
        "package_tree_sha256": zip_result.tree_sha256,
        "schema_version": 1,
        "skill_lock": {
            **runtime.to_skill_lock_document(),
            "path": f"{ARTIFACT_NAME}/{ARCHIVE_ROOT}/{SKILL_LOCK_FILENAME}",
            "sha256": _sha256_file(skill_lock_path),
        },
    }


def build_foundry_bootstrap_skill(
    repository_root: Path | str,
    *,
    dist_root: Path | str | None = None,
    runtime_repository: str | None = None,
    runtime_commit: str | None = None,
    package_path: str = ".",
    schema_version: int = 1,
) -> FoundryBootstrapReleaseResult:
    resolved_root = _resolve_repository_root(repository_root)
    resolved_dist_root = _resolve_dist_root(
        resolved_root / "dist" if dist_root is None else dist_root
    )
    source_root = resolved_root / "plugins" / "foundry-bootstrap"
    if not source_root.is_dir():
        raise FoundryBootstrapReleaseError(f"missing bootstrap plugin root: {source_root}")
    if (source_root / SKILL_LOCK_FILENAME).exists():
        raise FoundryBootstrapReleaseError(
            "plugins/foundry-bootstrap/skill.lock.json must not be checked in; build output belongs in dist only"
        )
    _load_skill_lock_template(source_root / "skill.lock.template.json")
    runtime = infer_runtime_provenance(
        resolved_root,
        runtime_repository=runtime_repository,
        runtime_commit=runtime_commit,
        package_path=package_path,
        schema_version=schema_version,
    )

    package_directory = resolved_dist_root / ARTIFACT_NAME
    skill_lock_path = package_directory / ARCHIVE_ROOT / SKILL_LOCK_FILENAME
    zip_path = resolved_dist_root / ZIP_FILENAME
    manifest_path = resolved_dist_root / CHECKSUM_MANIFEST_FILENAME

    resolved_dist_root.mkdir(parents=True, exist_ok=True)
    _delete_output(package_directory)
    _delete_output(zip_path)
    _delete_output(manifest_path)

    for relative_path in (
        "SKILL.md",
        "scripts/install-runtime.ps1",
        "scripts/install-runtime.sh",
    ):
        _copy_required_source_file(source_root, package_directory, relative_path)

    skill_lock_bytes = _canonical_json_bytes(runtime.to_skill_lock_document())
    _write_bytes(skill_lock_path, skill_lock_bytes)

    builder = DeterministicZipBuilder(includes=ARCHIVE_FILES)
    zip_result = builder.build(
        package_directory,
        zip_path,
        check_deadline=_no_deadline,
    )
    verified = verify_deterministic_zip(
        zip_path,
        check_deadline=_no_deadline,
    )
    if (
        verified.files != zip_result.files
        or verified.tree_sha256 != zip_result.tree_sha256
        or verified.zip_sha256 != zip_result.zip_sha256
    ):
        raise FoundryBootstrapReleaseError("built bootstrap skill ZIP did not verify deterministically")

    manifest = _build_manifest(
        zip_result=zip_result,
        skill_lock_path=skill_lock_path,
        runtime=runtime,
    )
    _write_bytes(manifest_path, _canonical_json_bytes(manifest))
    return FoundryBootstrapReleaseResult(
        dist_root=resolved_dist_root,
        package_directory=package_directory,
        zip_path=zip_path,
        manifest_path=manifest_path,
        skill_lock_path=skill_lock_path,
        runtime=runtime,
        package_tree_sha256=zip_result.tree_sha256,
        zip_sha256=zip_result.zip_sha256,
        zip_size_bytes=zip_result.size_bytes,
        files=zip_result.files,
    )


__all__ = [
    "ARCHIVE_FILES",
    "ARCHIVE_ROOT",
    "ARTIFACT_NAME",
    "CHECKSUM_MANIFEST_FILENAME",
    "FoundryBootstrapReleaseError",
    "FoundryBootstrapReleaseResult",
    "RuntimeProvenance",
    "SKILL_LOCK_FILENAME",
    "SKILL_LOCK_TEMPLATE",
    "ZIP_FILENAME",
    "build_foundry_bootstrap_skill",
    "infer_runtime_provenance",
]
