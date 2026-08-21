from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from foundry_opt.packaging.foundry_bootstrap_release import (
    ARCHIVE_FILES,
    ARCHIVE_ROOT,
    ARTIFACT_NAME,
    CHECKSUM_MANIFEST_FILENAME,
    FoundryBootstrapReleaseError,
    SKILL_LOCK_FILENAME,
    SKILL_LOCK_TEMPLATE,
    ZIP_FILENAME,
    build_foundry_bootstrap_skill,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP_SOURCE = REPOSITORY_ROOT / "plugins" / "foundry-bootstrap"


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _release_fixture(tmp_path: Path) -> tuple[Path, str, str]:
    repository = tmp_path / "checkout"
    shutil.copytree(BOOTSTRAP_SOURCE, repository / "plugins" / "foundry-bootstrap")
    shutil.copyfile(REPOSITORY_ROOT / "uv.lock", repository / "uv.lock")
    (repository / "plugins" / "foundry-bootstrap" / ".env").write_text(
        "SHOULD_NOT_SHIP=1\n",
        encoding="utf-8",
    )
    (repository / "plugins" / "foundry-bootstrap" / "__pycache__").mkdir()
    (repository / "plugins" / "foundry-bootstrap" / "__pycache__" / "cache.pyc").write_bytes(
        b"cache"
    )
    subprocess.run(
        ["git", "-C", str(repository), "init", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.name", "Skill Test"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "config", "user.email", "skill@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "remote", "add", "origin", "git@github.com:example/foundry-opt.git"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "add", "."],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repository), "commit", "--quiet", "-m", "fixture"],
        check=True,
    )
    sha = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    lock_sha = _sha256_file(repository / "uv.lock")
    return repository, sha, lock_sha


def test_build_release_creates_expected_files_checksums_and_install_shape(
    tmp_path: Path,
) -> None:
    repository, sha, lock_sha = _release_fixture(tmp_path)
    result = build_foundry_bootstrap_skill(repository, dist_root=tmp_path / "dist")

    assert result.package_directory == tmp_path / "dist" / ARTIFACT_NAME
    assert result.zip_path == tmp_path / "dist" / ZIP_FILENAME
    assert result.manifest_path == tmp_path / "dist" / CHECKSUM_MANIFEST_FILENAME
    assert result.skill_lock_path == result.package_directory / ARCHIVE_ROOT / SKILL_LOCK_FILENAME
    assert result.files == ARCHIVE_FILES
    assert not (repository / "plugins" / "foundry-bootstrap" / SKILL_LOCK_FILENAME).exists()

    skill_lock = json.loads(result.skill_lock_path.read_text(encoding="utf-8"))
    assert skill_lock == {
        "package_path": ".",
        "runtime_commit": sha,
        "runtime_repository": "https://github.com/example/foundry-opt.git",
        "schema_version": 1,
        "uv_lock_sha256": lock_sha,
    }

    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert manifest == {
        "artifact_path": ZIP_FILENAME,
        "artifact_sha256": result.zip_sha256,
        "artifact_size_bytes": result.zip_size_bytes,
        "package_directory": ARTIFACT_NAME,
        "package_files": list(ARCHIVE_FILES),
        "package_root": ARCHIVE_ROOT,
        "package_tree_sha256": result.package_tree_sha256,
        "schema_version": 1,
        "skill_lock": {
            **skill_lock,
            "path": f"{ARTIFACT_NAME}/{ARCHIVE_ROOT}/{SKILL_LOCK_FILENAME}",
            "sha256": _sha256_file(result.skill_lock_path),
        },
    }

    with zipfile.ZipFile(result.zip_path) as archive:
        infos = archive.infolist()
        assert [info.filename for info in infos] == list(ARCHIVE_FILES)
        assert all(info.date_time == (1980, 1, 1, 0, 0, 0) for info in infos)
        assert all(info.create_system == 3 for info in infos)
        assert all((info.external_attr >> 16) & 0o777 == 0o644 for info in infos)
        assert json.loads(archive.read(f"{ARCHIVE_ROOT}/{SKILL_LOCK_FILENAME}").decode("utf-8")) == skill_lock

    archive_names = set(result.files)
    assert all(name.startswith(f"{ARCHIVE_ROOT}/") for name in archive_names)
    assert not any(
        marker in name
        for name in archive_names
        for marker in (
            ".env",
            "__pycache__",
            ".pyc",
            ".git",
            "skill.lock.template.json",
            "templates/",
        )
    )
    assert f"{ARCHIVE_ROOT}/scripts/bootstrap.py" in archive_names
    assert f"{ARCHIVE_ROOT}/references/owner-flow.md" in archive_names


def test_build_release_is_byte_deterministic_for_same_checkout(tmp_path: Path) -> None:
    repository, _, _ = _release_fixture(tmp_path)
    first = build_foundry_bootstrap_skill(repository, dist_root=tmp_path / "first")

    os.utime(
        repository / "plugins" / "foundry-bootstrap" / "SKILL.md",
        (2_000_000_000, 2_000_000_000),
    )
    os.chmod(
        repository / "plugins" / "foundry-bootstrap" / "SKILL.md",
        stat.S_IREAD | stat.S_IWRITE,
    )
    second = build_foundry_bootstrap_skill(repository, dist_root=tmp_path / "second")

    assert first.zip_sha256 == second.zip_sha256
    assert first.package_tree_sha256 == second.package_tree_sha256
    assert first.files == second.files
    assert first.zip_path.read_bytes() == second.zip_path.read_bytes()
    assert first.manifest_path.read_bytes() == second.manifest_path.read_bytes()
    assert first.skill_lock_path.read_bytes() == second.skill_lock_path.read_bytes()


def test_build_release_rejects_unresolved_placeholders_and_source_exact_lock(
    tmp_path: Path,
) -> None:
    repository, _, _ = _release_fixture(tmp_path)

    with pytest.raises(
        FoundryBootstrapReleaseError,
        match="runtime_repository placeholder was not resolved",
    ):
        build_foundry_bootstrap_skill(
            repository,
            dist_root=tmp_path / "placeholder",
            runtime_repository=SKILL_LOCK_TEMPLATE["runtime_repository"],
        )

    (repository / "plugins" / "foundry-bootstrap" / SKILL_LOCK_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": 1,
                "runtime_repository": "https://github.com/example/foundry-opt.git",
                "runtime_commit": "a" * 40,
                "uv_lock_sha256": "b" * 64,
                "package_path": ".",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(
        FoundryBootstrapReleaseError,
        match="must not be checked in; build output belongs in dist only",
    ):
        build_foundry_bootstrap_skill(repository, dist_root=tmp_path / "exact-lock")


def test_script_command_builds_artifacts_from_checked_out_repo(tmp_path: Path) -> None:
    repository, sha, lock_sha = _release_fixture(tmp_path)
    dist_root = tmp_path / "artifacts"

    completed = subprocess.run(
        [
            sys.executable,
            str(REPOSITORY_ROOT / "tools" / "build_foundry_bootstrap_skill.py"),
            "--repository-root",
            str(repository),
            "--dist-root",
            str(dist_root),
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    assert completed.returncode == 0, completed.stderr
    assert f"zip={dist_root / ZIP_FILENAME}" in completed.stdout
    manifest = json.loads((dist_root / CHECKSUM_MANIFEST_FILENAME).read_text(encoding="utf-8"))
    assert manifest["artifact_sha256"] == _sha256_file(dist_root / ZIP_FILENAME)
    assert manifest["skill_lock"]["runtime_commit"] == sha
    assert manifest["skill_lock"]["runtime_repository"] == "https://github.com/example/foundry-opt.git"
    assert manifest["skill_lock"]["uv_lock_sha256"] == lock_sha
