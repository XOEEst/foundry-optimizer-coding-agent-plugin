from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Final, Literal, Self

from pydantic import StringConstraints, ValidationError, field_validator, model_validator

from foundry_opt.models import FrozenModel
from foundry_opt.optimize_job.safety import (
    UnsafeCheckpointContentError,
    assert_safe_persisted_document,
    assert_safe_persisted_string,
)
from foundry_opt.poc.config import (
    POCConfigurationError,
    SharedPin,
    validate_repository_relative_path,
)


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]

MAX_RECEIPT_BYTES: Final = 16 * 1024
_COMMIT: Final = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
CANONICAL_OPTIMIZER_SKILL_PATH: Final = "plugins/foundry-agent-optimizer"
LEGACY_OPTIMIZER_SKILL_PATH: Final = (
    "src/foundry_opt/templates/skills/foundry-agent-optimizer"
)
_SKILL_PATH_ALIASES: Final = {
    LEGACY_OPTIMIZER_SKILL_PATH: CANONICAL_OPTIMIZER_SKILL_PATH,
}


class BootstrapError(POCConfigurationError):
    """The shared bootstrap layer is invalid or incomplete."""


class BootstrapVerificationError(BootstrapError):
    """The external shared checkout does not match its pin."""


class BootstrapReceiptError(BootstrapError):
    """The bootstrap receipt is missing, malformed, or tampered."""


def canonical_optimizer_skill_path(value: str, *, field: str = "skill_path") -> str:
    normalized = validate_repository_relative_path(value, field=field)
    return _SKILL_PATH_ALIASES.get(normalized, normalized)


def optimizer_skill_paths_match(left: str, right: str) -> bool:
    return canonical_optimizer_skill_path(left) == canonical_optimizer_skill_path(
        right
    )


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def _absolute_path_text(value: Path | str, field: str) -> str:
    raw = str(value)
    if not raw:
        raise ValueError(f"{field} must not be empty")
    resolved = Path(raw).resolve(strict=False)
    if not resolved.is_absolute():
        raise ValueError(f"{field} must be absolute")
    text = str(resolved)
    try:
        assert_safe_persisted_string(text, field=field, limit=1024)
    except UnsafeCheckpointContentError as exc:
        raise ValueError(str(exc)) from exc
    return text


def _validate_repository_url(value: object, field: str) -> str:
    return SharedPin.from_document(
        {
            "schema_version": 1,
            "repository_url": value,
            "commit": "0" * 40,
            "package_path": "pkg",
            "skill_path": "skill",
            "uv_lock_sha256": "0" * 64,
        }
    ).repository_url


def _validate_package_path(value: object, field: str) -> str:
    if value == ".":
        try:
            assert_safe_persisted_string(".", field=field, limit=256)
        except UnsafeCheckpointContentError as exc:
            raise ValueError(str(exc)) from exc
        return "."
    return validate_repository_relative_path(value, field=field)


def _bootstrap_receipt_payload(values: Mapping[str, object]) -> dict[str, object]:
    return {
        "repository": values["repository"],
        "commit": values["commit"],
        "package_path": values["package_path"],
        "skill_path": values["skill_path"],
        "lock_sha256": values["lock_sha256"],
        "checkout_root": values["checkout_root"],
    }


def _bootstrap_receipt_sha256(values: Mapping[str, object]) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(_bootstrap_receipt_payload(values))
    ).hexdigest()


class BootstrapReceipt(FrozenModel):
    repository: str
    commit: GitCommit
    package_path: str
    skill_path: str
    lock_sha256: Sha256
    checkout_root: str
    receipt_sha256: Sha256

    @classmethod
    def create(
        cls,
        *,
        repository: str,
        commit: str,
        package_path: str,
        skill_path: str,
        lock_sha256: str,
        checkout_root: str,
    ) -> BootstrapReceipt:
        payload: dict[str, object] = {
            "repository": repository,
            "commit": commit,
            "package_path": package_path,
            "skill_path": skill_path,
            "lock_sha256": lock_sha256,
            "checkout_root": checkout_root,
        }
        payload["receipt_sha256"] = _bootstrap_receipt_sha256(payload)
        return cls.model_validate(payload)

    @field_validator("repository")
    @classmethod
    def validate_repository(cls, value: str) -> str:
        return _validate_repository_url(value, "repository")

    @field_validator("package_path")
    @classmethod
    def validate_package_path(cls, value: str) -> str:
        return _validate_package_path(value, "package_path")

    @field_validator("skill_path")
    @classmethod
    def validate_skill_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="skill_path")

    @field_validator("checkout_root")
    @classmethod
    def validate_checkout_root(cls, value: str) -> str:
        return _absolute_path_text(value, "checkout_root")

    @model_validator(mode="after")
    def validate_receipt(self) -> Self:
        expected = _bootstrap_receipt_sha256(self.model_dump(mode="json"))
        if self.receipt_sha256 != expected:
            raise ValueError("receipt_sha256 does not match the receipt payload")
        assert_safe_persisted_document(self.model_dump(mode="json"))
        return self


class ExternalCheckoutPlan(FrozenModel):
    repository_url: str
    commit: GitCommit
    checkout_root: str

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        return _validate_repository_url(value, "repository_url")

    @field_validator("checkout_root")
    @classmethod
    def validate_checkout_root(cls, value: str) -> str:
        return _absolute_path_text(value, "checkout_root")


class FrozenDependencyInstallPlan(FrozenModel):
    manager: Literal["uv"] = "uv"
    frozen: Literal[True] = True
    package_path: str
    lock_path: str
    lock_sha256: Sha256

    @field_validator("package_path")
    @classmethod
    def validate_package_path(cls, value: str) -> str:
        return _validate_package_path(value, "package_path")

    @field_validator("lock_path")
    @classmethod
    def validate_lock_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="lock_path")


class UserSkillInstallPlan(FrozenModel):
    scope: Literal["user"] = "user"
    skill_path: str

    @field_validator("skill_path")
    @classmethod
    def validate_skill_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="skill_path")


class BootstrapPlan(FrozenModel):
    checkout: ExternalCheckoutPlan
    dependency_install: FrozenDependencyInstallPlan
    skill_install: UserSkillInstallPlan
    receipt_path: str

    @field_validator("receipt_path")
    @classmethod
    def validate_receipt_path(cls, value: str) -> str:
        return _absolute_path_text(value, "receipt_path")

    @model_validator(mode="after")
    def validate_plan(self) -> Self:
        assert_safe_persisted_document(self.model_dump(mode="json"))
        return self


def load_shared_pin(path: Path | str) -> SharedPin:
    target = Path(path)
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise BootstrapError(f"shared pin could not be read: {target}") from exc
    try:
        return SharedPin.from_document(content)
    except POCConfigurationError as exc:
        raise BootstrapError(str(exc)) from exc


def _git_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for key in (
        "GIT_DIR",
        "GIT_WORK_TREE",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_CONFIG",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(key, None)
    environment["GIT_TERMINAL_PROMPT"] = "0"
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_NO_REPLACE_OBJECTS"] = "1"
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    return environment


def _resolve_checkout_root(checkout_root: Path | str) -> Path:
    try:
        resolved = Path(checkout_root).resolve(strict=True)
    except OSError as exc:
        raise BootstrapVerificationError(
            f"checkout root could not be resolved: {checkout_root}"
        ) from exc
    if not resolved.is_dir():
        raise BootstrapVerificationError("checkout root must be a directory")
    return resolved


def _resolve_plan_target(
    checkout_root: Path,
    relative_path: str,
    *,
    field: str,
    allow_root: bool = False,
) -> Path:
    if allow_root:
        safe_relative = _validate_package_path(relative_path, field)
        if safe_relative == ".":
            return checkout_root
    else:
        safe_relative = validate_repository_relative_path(relative_path, field=field)
    resolved = (checkout_root / safe_relative).resolve(strict=False)
    if not resolved.is_relative_to(checkout_root):
        raise BootstrapError(f"{field} escapes the checkout root")
    return resolved


def _resolve_checkout_target(
    checkout_root: Path,
    relative_path: str,
    *,
    field: str,
    file_required: bool = False,
    allow_root: bool = False,
) -> Path:
    if allow_root:
        safe_relative = _validate_package_path(relative_path, field)
        if safe_relative == ".":
            return checkout_root
    else:
        safe_relative = validate_repository_relative_path(relative_path, field=field)
    try:
        resolved = (checkout_root / safe_relative).resolve(strict=True)
    except OSError as exc:
        raise BootstrapVerificationError(
            f"{field} could not be resolved under the checkout root"
        ) from exc
    if not resolved.is_relative_to(checkout_root):
        raise BootstrapVerificationError(f"{field} escapes the checkout root")
    if file_required and not resolved.is_file():
        raise BootstrapVerificationError(f"{field} must be a file")
    if not file_required and not (resolved.is_dir() or resolved.is_file()):
        raise BootstrapVerificationError(f"{field} must be a file or directory")
    return resolved


def _git_head(checkout_root: Path) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", str(checkout_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=30,
            env=_git_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise BootstrapVerificationError("Git HEAD resolution timed out") from exc
    except OSError as exc:
        raise BootstrapVerificationError("Git could not be executed") from exc
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise BootstrapVerificationError(
            f"Git HEAD could not be resolved: {detail or 'unknown failure'}"
        )
    head = completed.stdout.strip()
    if _COMMIT.fullmatch(head) is None:
        raise BootstrapVerificationError("Git returned an invalid HEAD")
    return head


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
    except OSError as exc:
        raise BootstrapVerificationError(f"could not read {path}") from exc
    return digest.hexdigest()


def verify_shared_checkout(
    pin: SharedPin,
    checkout_root: Path | str,
) -> BootstrapReceipt:
    if not isinstance(pin, SharedPin):
        raise TypeError("pin must be a SharedPin")
    resolved_root = _resolve_checkout_root(checkout_root)
    head = _git_head(resolved_root)
    if head != pin.commit:
        raise BootstrapVerificationError(
            f"shared checkout HEAD {head} does not match pinned commit {pin.commit}"
        )
    _resolve_checkout_target(
        resolved_root,
        pin.package_path,
        field="package_path",
        allow_root=True,
    )
    _resolve_checkout_target(
        resolved_root,
        canonical_optimizer_skill_path(pin.skill_path),
        field="skill_path",
    )
    lock_path = _resolve_checkout_target(
        resolved_root,
        "uv.lock",
        field="uv.lock",
        file_required=True,
    )
    lock_sha256 = _sha256_file(lock_path)
    if lock_sha256 != pin.uv_lock_sha256:
        raise BootstrapVerificationError("uv.lock digest does not match the pin")
    return BootstrapReceipt.create(
        repository=pin.repository_url,
        commit=head,
        package_path=pin.package_path,
        skill_path=pin.skill_path,
        lock_sha256=lock_sha256,
        checkout_root=str(resolved_root),
    )


def _reject_duplicate_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise BootstrapReceiptError(f"duplicate bootstrap receipt field: {key!r}")
        payload[key] = value
    return payload


def read_bootstrap_receipt(path: Path | str) -> BootstrapReceipt:
    target = Path(path)
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise BootstrapReceiptError(
            f"bootstrap receipt could not be read: {target}"
        ) from exc
    if len(content) > MAX_RECEIPT_BYTES:
        raise BootstrapReceiptError("bootstrap receipt exceeds the size limit")
    try:
        payload = json.loads(
            content.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_pairs,
        )
    except BootstrapReceiptError:
        raise
    except UnicodeDecodeError as exc:
        raise BootstrapReceiptError("bootstrap receipt is not UTF-8 JSON") from exc
    except json.JSONDecodeError as exc:
        raise BootstrapReceiptError("bootstrap receipt is not valid JSON") from exc
    if not isinstance(payload, Mapping):
        raise BootstrapReceiptError("bootstrap receipt must be a JSON object")
    try:
        return BootstrapReceipt.model_validate(payload)
    except ValidationError as exc:
        raise BootstrapReceiptError("bootstrap receipt is invalid") from exc


def _write_atomic_bytes(path: Path, content: bytes) -> None:
    temporary = path.parent / f".{path.name}.{os.getpid():x}.tmp"
    try:
        if temporary.exists():
            temporary.unlink()
        with temporary.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as exc:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        raise BootstrapReceiptError(f"bootstrap receipt could not be written: {path}") from exc


def write_bootstrap_receipt(path: Path | str, receipt: BootstrapReceipt) -> None:
    if not isinstance(receipt, BootstrapReceipt):
        raise TypeError("receipt must be a BootstrapReceipt")
    target = Path(path)
    if target.exists():
        existing = read_bootstrap_receipt(target)
        if existing == receipt:
            return
        raise BootstrapReceiptError(
            "bootstrap receipt already exists with different content"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    content = _canonical_json_bytes(receipt.model_dump(mode="json")) + b"\n"
    _write_atomic_bytes(target, content)


def build_bootstrap_plan(
    pin: SharedPin,
    *,
    checkout_root: Path | str,
    receipt_path: Path | str,
) -> BootstrapPlan:
    if not isinstance(pin, SharedPin):
        raise TypeError("pin must be a SharedPin")
    resolved_checkout_root = Path(
        _absolute_path_text(checkout_root, "checkout_root")
    )
    _resolve_plan_target(
        resolved_checkout_root,
        pin.package_path,
        field="package_path",
        allow_root=True,
    )
    _resolve_plan_target(
        resolved_checkout_root,
        canonical_optimizer_skill_path(pin.skill_path),
        field="skill_path",
    )
    _resolve_plan_target(
        resolved_checkout_root,
        "uv.lock",
        field="lock_path",
    )
    return BootstrapPlan(
        checkout=ExternalCheckoutPlan(
            repository_url=pin.repository_url,
            commit=pin.commit,
            checkout_root=str(resolved_checkout_root),
        ),
        dependency_install=FrozenDependencyInstallPlan(
            package_path=pin.package_path,
            lock_path="uv.lock",
            lock_sha256=pin.uv_lock_sha256,
        ),
        skill_install=UserSkillInstallPlan(
            skill_path=canonical_optimizer_skill_path(pin.skill_path)
        ),
        receipt_path=_absolute_path_text(receipt_path, "receipt_path"),
    )


__all__ = [
    "BootstrapError",
    "BootstrapPlan",
    "BootstrapReceipt",
    "BootstrapReceiptError",
    "BootstrapVerificationError",
    "CANONICAL_OPTIMIZER_SKILL_PATH",
    "ExternalCheckoutPlan",
    "FrozenDependencyInstallPlan",
    "LEGACY_OPTIMIZER_SKILL_PATH",
    "UserSkillInstallPlan",
    "build_bootstrap_plan",
    "canonical_optimizer_skill_path",
    "load_shared_pin",
    "optimizer_skill_paths_match",
    "read_bootstrap_receipt",
    "verify_shared_checkout",
    "write_bootstrap_receipt",
]
