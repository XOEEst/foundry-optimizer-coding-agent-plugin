from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

import yaml

from foundry_opt.bootstrap.canonical import canonical_sha256
from foundry_opt.bootstrap.contracts import (
    ActivationOutcomeRecord,
    BootstrapAction,
    BootstrapLock,
    BootstrapPlan,
    BootstrapPlanPayload,
    BootstrapReceipt,
    BootstrapSidecar,
    FingerprintRecord,
    ManagedFileEntry,
    SemanticPatchSpec,
    TemplatePayloadSpec,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError, BootstrapPlanError
from foundry_opt.poc.config import validate_repository_relative_path

LOCK_PATH = ".foundry-opt/bootstrap.lock.json"
JOURNAL_DIR = ".foundry-opt/journal"
RECEIPT_DIR = ".foundry-opt/receipts"
SUPPORTED_YAML_PATH = ".github/workflows/copilot-setup-steps.yml"
SUPPORTED_YAML_STEP_IDS = frozenset({"foundry-opt-checkout", "foundry-opt-bootstrap"})
SUPPORTED_YAML_LEGACY_STEP_NAMES = {
    "foundry-opt-checkout": frozenset(
        {
            "Fetch exact v1-capable shared revision",
            "Fetch the exact shared revision",
        }
    ),
    "foundry-opt-bootstrap": frozenset(
        {
            "Install the exact shared CLI and skill",
            "Install the frozen shared environment and skill",
        }
    ),
}
SUPPORTED_YAML_LEGACY_WORKFLOW_STEP_NAMES = (
    "Check out the agent repository",
    "Canonicalize the repository origin",
    "Set up Python",
    "Set up uv",
    "Record trusted state paths",
    "Detect trusted optimize job context",
    "Fetch the exact shared revision",
    "Install the frozen shared environment and skill",
    "Verify bootstrap receipt and target configuration",
    "Launch the minimal GitHub issue broker",
    "Validate the complete setup contract",
)
SUPPORTED_YAML_LEGACY_WORKFLOW_MARKERS = (
    b'pin=".github/foundry-opt.lock.yml"',
    b"--pin .github/foundry-opt.lock.yml",
    b"foundry-opt validate-config",
)
LEGACY_FETCH_MARKERS = ("FOUNDRY_OPT_SHARED_REPO_SSH_KEY", "git@github.com", "known_hosts")
_OPERATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass(frozen=True)
class RepositoryInventoryEntry:
    path: str
    exists: bool
    sha256: str | None
    managed: ManagedFileEntry | None


@dataclass(frozen=True)
class RepositoryInventory:
    entries: tuple[RepositoryInventoryEntry, ...]
    lock: BootstrapLock | None


@dataclass(frozen=True)
class _Disposition:
    mode: str
    rendered_bytes: bytes | None
    conflict_sibling_path: str | None
    diff: str
    target_hash: str | None
    sibling_hash: str | None


def inventory_repository(root: Path, payloads: Sequence[TemplatePayloadSpec]) -> RepositoryInventory:
    lock = _load_lock(root)
    managed = {} if lock is None else {entry.path: entry for entry in lock.managed_files}
    return RepositoryInventory(
        entries=tuple(
            RepositoryInventoryEntry(
                path=payload.destination_path,
                exists=_target(root, payload.destination_path).exists(),
                sha256=_hash_if_exists(root, payload.destination_path),
                managed=managed.get(payload.destination_path),
            )
            for payload in payloads
        ),
        lock=lock,
    )


def plan_repository(
    root: Path,
    *,
    operation_id: str,
    runtime_repository: str,
    runtime_commit: str,
    repository_identity: str,
    payloads: Sequence[TemplatePayloadSpec],
) -> BootstrapPlan:
    _validate_operation_id(operation_id)
    inventory = inventory_repository(root, payloads)
    _validate_destinations(root, operation_id, payloads)
    lock_hash = _hash_if_exists(root, LOCK_PATH) or "missing"
    actions: list[BootstrapAction] = []
    for payload in payloads:
        current_bytes = _read_bytes(_target(root, payload.destination_path))
        disposition = _planned_disposition(root, payload, current_bytes, inventory.lock)
        diagnostics = tuple(
            item
            for item in (
                f"target:{disposition.target_hash or 'missing'}",
                f"lock:{lock_hash}",
                f"sibling:{disposition.sibling_hash or 'missing'}",
                f"mode:{disposition.mode}",
                f"conflict:{disposition.conflict_sibling_path}" if disposition.conflict_sibling_path else "",
                disposition.diff,
            )
            if item
        )
        actions.append(
            BootstrapAction(
                action_id=f"repository:{payload.template_id}:{payload.destination_path}",
                phase="repository",
                stage="planned",
                kind="repository-write",
                template_payload=payload,
                diagnostics=diagnostics,
            )
        )
    return BootstrapPlan.create(
        operation_id=operation_id,
        runtime_repository=runtime_repository,
        runtime_commit=runtime_commit,
        repository_identity=repository_identity,
        actions=tuple(actions),
    )


def apply_repository(root: Path, plan: BootstrapPlan) -> tuple[BootstrapReceipt, BootstrapLock]:
    _validate_operation_id(plan.operation_id)
    _validate_repository_plan(root, plan)
    lock_before = _load_lock(root)
    _enforce_plan_bindings(root, plan)
    planned: list[tuple[BootstrapAction, TemplatePayloadSpec, _Disposition, bytes | None]] = []
    adopted: list[str] = []
    changed: list[str] = []
    created: list[str] = []
    skipped: list[str] = []
    managed_entries = {} if lock_before is None else {entry.path: entry for entry in lock_before.managed_files}
    for action in plan.actions:
        payload = action.template_payload
        if payload is None:
            skipped.append(action.action_id)
            continue
        current_bytes = _read_bytes(_target(root, payload.destination_path))
        disposition = _planned_disposition(root, payload, current_bytes, lock_before)
        if disposition.mode == "skip":
            skipped.append(action.action_id)
            continue
        if disposition.mode == "adopt-identical":
            adopted.append(action.action_id)
        planned.append((action, payload, disposition, current_bytes))
    resulting_entries = dict(managed_entries)
    for action, payload, disposition, current_bytes in planned:
        if disposition.mode in {"write", "adopt-identical"}:
            resulting_entries[payload.destination_path] = ManagedFileEntry(
                path=payload.destination_path,
                ownership_mode="owned",
                owner_scope="repository",
                template_id=payload.template_id,
                template_base_sha256=_hash_bytes(payload.rendered_template.encode("utf-8")) or _missing_hash(),
                applied_sha256=_hash_bytes(current_bytes if disposition.mode == "adopt-identical" else disposition.rendered_bytes) or _missing_hash(),
                semantic_patch_id=_semantic_patch_id(payload.semantic_patches),
            )
    next_lock = _build_lock(lock_before, plan, tuple(sorted(resulting_entries.values(), key=lambda item: item.path)))
    unchanged_lock = _lock_equal(lock_before, next_lock)
    if not planned and (lock_before is None or unchanged_lock):
        receipt = BootstrapReceipt.create(
            operation_id=plan.operation_id,
            runtime_repository=plan.runtime_repository,
            runtime_commit=plan.runtime_commit,
            repository_identity=plan.repository_identity,
            plan_hash=plan.plan_hash,
            before_fingerprints=_fingerprints(root, plan, include_system=False),
            after_fingerprints=_fingerprints(root, plan, include_system=False),
            adopted_actions=tuple(adopted),
            skipped_actions=tuple(skipped),
        )
        return receipt, lock_before or next_lock
    journal_entries = _prepare_journal_entries(root, plan, planned, lock_before)
    journal_path = _journal_path(root, plan.operation_id)
    receipt_path = _receipt_path(root, plan.operation_id)
    _write_journal(journal_path, plan.operation_id, journal_entries, receipt_path, applied=())
    try:
        for action, payload, disposition, current_bytes in planned:
            if disposition.mode == "conflict":
                assert disposition.conflict_sibling_path is not None
                _atomic_write_bytes(_target(root, disposition.conflict_sibling_path), disposition.rendered_bytes or b"")
                skipped.append(action.action_id)
            elif disposition.mode == "write":
                _atomic_write_bytes(_target(root, payload.destination_path), disposition.rendered_bytes or b"")
                if current_bytes is None:
                    created.append(action.action_id)
                else:
                    changed.append(action.action_id)
            _write_journal(journal_path, plan.operation_id, journal_entries, receipt_path, applied=tuple(sorted({*json.loads(journal_path.read_text(encoding="utf-8"))["applied"], payload.destination_path})))
        if not unchanged_lock:
            _atomic_write_json(_target(root, LOCK_PATH), next_lock.model_dump(mode="json"))
        _atomic_write_json(receipt_path, _serialize_preimages(journal_entries))
        _write_journal(journal_path, plan.operation_id, journal_entries, receipt_path, applied=tuple(entry.path for entry in journal_entries), state="completed")
    except Exception:
        # Keep the prepared journal so an interrupted or partially applied
        # operation can be recovered explicitly and safely.
        raise
    receipt = BootstrapReceipt.create(
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        repository_identity=plan.repository_identity,
        plan_hash=plan.plan_hash,
        before_fingerprints=_fingerprints(root, plan, include_system=False),
        after_fingerprints=_fingerprints(root, plan, include_system=True),
        created_actions=tuple(created),
        adopted_actions=tuple(adopted),
        changed_actions=tuple(changed),
        skipped_actions=tuple(skipped),
    )
    return receipt, next_lock


def rollback_repository(root: Path, receipt: BootstrapReceipt) -> None:
    receipt_path = _receipt_path(root, receipt.operation_id)
    payload = _read_json_file(receipt_path)
    current = {fingerprint.label: fingerprint.sha256 for fingerprint in receipt.after_fingerprints}
    lock_preimage = payload.get(LOCK_PATH)
    for repo_path, encoded in payload.items():
        if repo_path == LOCK_PATH:
            continue
        observed = _hash_if_exists(root, repo_path)
        expected = current.get(repo_path, "missing")
        if observed != expected:
            raise BootstrapApplyError(f"rollback refused because current hash changed: {repo_path}")
    for repo_path, encoded in payload.items():
        if repo_path == LOCK_PATH:
            continue
        original = _decode_preimage(encoded)
        target = _target(root, repo_path)
        if original is None:
            if target.exists():
                target.unlink()
        else:
            _atomic_write_bytes(target, original)
    _rollback_managed_lock(root, payload, lock_preimage)


def _rollback_managed_lock(
    root: Path,
    preimages: Mapping[str, object],
    encoded_preimage: object,
) -> None:
    current = _load_lock(root)
    original_bytes = _decode_preimage(encoded_preimage)
    original = (
        None
        if original_bytes is None
        else BootstrapLock.model_validate(
            json.loads(original_bytes.decode("utf-8"))
        )
    )
    touched_paths = {
        path
        for path in preimages
        if not path.startswith(".foundry-opt/")
        and not path.endswith(".foundry-proposed")
    }
    current_entries = (
        {} if current is None else {entry.path: entry for entry in current.managed_files}
    )
    original_entries = (
        {} if original is None else {entry.path: entry for entry in original.managed_files}
    )
    for path in touched_paths:
        current_entries.pop(path, None)
        if path in original_entries:
            current_entries[path] = original_entries[path]
    lock_path = _target(root, LOCK_PATH)
    if current is None:
        if original is not None:
            _atomic_write_json(lock_path, original.model_dump(mode="json"))
        return
    merged = current.model_copy(
        update={
            "managed_files": tuple(
                sorted(current_entries.values(), key=lambda entry: entry.path)
            )
        }
    )
    if (
        original is None
        and not merged.managed_files
        and not merged.github_environments
        and not merged.cloud_resources
        and not merged.sidecar_paths
    ):
        if lock_path.exists():
            lock_path.unlink()
        return
    _atomic_write_json(lock_path, merged.model_dump(mode="json"))


def recover_repository_journal(root: Path, operation_id: str) -> None:
    _validate_operation_id(operation_id)
    _recover_journal(root, operation_id)


def drift_status(root: Path, lock: BootstrapLock) -> tuple[str, ...]:
    result: list[str] = []
    for entry in lock.managed_files:
        current = _hash_if_exists(root, entry.path)
        if current is None:
            result.append(f"missing:{entry.path}")
        elif current != entry.applied_sha256:
            result.append(f"drifted:{entry.path}")
    return tuple(result)


def render_template_payload(payload: TemplatePayloadSpec, current_bytes: bytes | None = None) -> bytes:
    if payload.destination_path == SUPPORTED_YAML_PATH:
        if (
            current_bytes is not None
            and _is_recognized_legacy_setup_workflow(current_bytes)
        ):
            rendered = payload.rendered_template.encode("utf-8")
            _validate_workflow_yaml(rendered)
            return rendered
        source = current_bytes if current_bytes is not None else payload.rendered_template.encode("utf-8")
        return _patch_reserved_workflow(source, payload.semantic_patches)
    rendered = payload.rendered_template.encode("utf-8")
    if payload.template_id == "sidecar" and current_bytes is not None:
        preserved = _preserve_enriched_sidecar(current_bytes, rendered)
        if preserved is not None:
            return preserved
    for patch in payload.semantic_patches:
        rendered = _apply_text_patch(rendered, patch)
    return rendered


def _preserve_enriched_sidecar(current_bytes: bytes, rendered: bytes) -> bytes | None:
    try:
        current = BootstrapSidecar.from_document(current_bytes.decode("utf-8"))
        desired = BootstrapSidecar.from_document(rendered.decode("utf-8"))
    except (UnicodeDecodeError, BootstrapConfigError):
        return None
    if current.static_fingerprint() != desired.static_fingerprint():
        return None
    if current.verification.bundle is None and current.verification.lineage is None:
        return None
    return current_bytes


def _planned_disposition(root: Path, payload: TemplatePayloadSpec, current_bytes: bytes | None, lock: BootstrapLock | None) -> _Disposition:
    rendered = render_template_payload(payload, current_bytes)
    if any(marker.encode("utf-8") in rendered for marker in LEGACY_FETCH_MARKERS):
        raise BootstrapPlanError("legacy private SSH bootstrap fetch must be removed")
    entry = _managed_entry(lock, payload.destination_path)
    sibling_path = f"{payload.destination_path}.foundry-proposed"
    sibling_hash = _hash_if_exists(root, sibling_path)
    normalized_equal = _hash_bytes(current_bytes) == _hash_bytes(rendered) and current_bytes is not None
    recognized_legacy_conversion = (
        payload.destination_path == SUPPORTED_YAML_PATH
        and current_bytes is not None
        and _is_recognized_legacy_setup_workflow(current_bytes)
    )
    if entry is None:
        if current_bytes is None:
            mode = "write"
        elif normalized_equal:
            mode = "skip"
        elif recognized_legacy_conversion:
            mode = "write"
        else:
            mode = "conflict"
        diff = "" if current_bytes is None and mode == "write" else _unified_diff(payload.destination_path, current_bytes or b"", rendered)
        return _Disposition(mode=mode, rendered_bytes=rendered, conflict_sibling_path=sibling_path if mode == "conflict" else None, diff=diff, target_hash=_hash_bytes(current_bytes), sibling_hash=sibling_hash)
    if normalized_equal:
        return _Disposition(mode="skip", rendered_bytes=None, conflict_sibling_path=None, diff="", target_hash=_hash_bytes(current_bytes), sibling_hash=sibling_hash)
    if _hash_bytes(current_bytes) != entry.applied_sha256 and payload.destination_path != SUPPORTED_YAML_PATH:
        return _Disposition(mode="conflict", rendered_bytes=rendered, conflict_sibling_path=sibling_path, diff=_unified_diff(payload.destination_path, current_bytes or b"", rendered), target_hash=_hash_bytes(current_bytes), sibling_hash=sibling_hash)
    return _Disposition(mode="write", rendered_bytes=rendered, conflict_sibling_path=None, diff=_unified_diff(payload.destination_path, current_bytes or b"", rendered), target_hash=_hash_bytes(current_bytes), sibling_hash=sibling_hash)


def _patch_reserved_workflow(base_bytes: bytes, patches: Sequence[SemanticPatchSpec]) -> bytes:
    _validate_workflow_yaml(base_bytes)
    content = bytearray(base_bytes)
    for patch in patches:
        blocks = _scan_step_blocks(bytes(content))
        step_id = _extract_step_id(patch)
        if step_id not in SUPPORTED_YAML_STEP_IDS:
            raise BootstrapPlanError("unrecognized managed copilot step id")
        existing = _find_managed_step_block(
            blocks,
            step_id,
            allow_legacy_name=patch.operation == "replace",
        )
        if patch.operation == "replace":
            if existing is None or patch.replacement_text is None:
                raise BootstrapPlanError(
                    "managed workflow replace requires a reserved step id or supported legacy step name"
                )
            replacement = _indent_block(
                existing["indent"],
                patch.replacement_text,
                _line_ending(bytes(content)),
            )
            content[existing["start"] : existing["end"]] = replacement.encode("utf-8")
        elif patch.operation == "delete":
            if existing is None:
                raise BootstrapPlanError("managed workflow delete requires existing step")
            content[existing["start"] : existing["end"]] = b""
        elif patch.operation in {"insert_before", "insert_after"}:
            anchor = _find_anchor_block(blocks, patch.match_text)
            block = _indent_block(
                anchor["indent"],
                patch.replacement_text,
                _line_ending(bytes(content)),
            )
            index = anchor["start"] if patch.operation == "insert_before" else anchor["end"]
            content[index:index] = block.encode("utf-8")
        else:
            raise BootstrapPlanError(f"unsupported semantic patch operation: {patch.operation}")
        _validate_workflow_yaml(bytes(content))
    return bytes(content)


def _validate_workflow_yaml(document: bytes) -> None:
    try:
        parsed = yaml.safe_load(document)
    except yaml.YAMLError as exc:
        raise BootstrapPlanError("patched workflow is not valid YAML") from exc
    if not isinstance(parsed, Mapping):
        raise BootstrapPlanError("workflow must remain a YAML mapping")
    jobs = parsed.get("jobs")
    if not isinstance(jobs, Mapping) or len(jobs) != 1:
        raise BootstrapPlanError("unsupported or ambiguous workflow shape")
    job = next(iter(jobs.values()))
    if not isinstance(job, Mapping) or not isinstance(job.get("steps"), list):
        raise BootstrapPlanError("unsupported or ambiguous workflow shape")


def _scan_step_blocks(document: bytes) -> list[dict[str, int | str]]:
    lines = document.decode("utf-8").splitlines(keepends=True)
    steps_indent: int | None = None
    steps_line = 0
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if stripped.startswith("steps:"):
            steps_indent = len(line) - len(stripped)
            steps_line = index + 1
            break
    if steps_indent is None:
        raise BootstrapPlanError("workflow does not contain steps")
    blocks: list[dict[str, int | str]] = []
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    index = steps_line
    while index < len(lines):
        line = lines[index]
        stripped = line.lstrip()
        indent = len(line) - len(stripped)
        if stripped and not stripped.startswith("#") and indent <= steps_indent:
            break
        if stripped.startswith("- "):
            start = index
            item_indent = indent
            index += 1
            while index < len(lines):
                nxt = lines[index]
                nxt_stripped = nxt.lstrip()
                nxt_indent = len(nxt) - len(nxt_stripped)
                if nxt_stripped.startswith("- ") and nxt_indent == item_indent:
                    break
                if nxt_stripped and nxt_indent <= steps_indent:
                    break
                index += 1
            block_text = "".join(lines[start:index])
            step_id = _extract_block_id(block_text)
            step_name = _extract_block_name(block_text)
            blocks.append(
                {
                    "id": step_id,
                    "name": step_name,
                    "start": offsets[start],
                    "end": offsets[index],
                    "indent": item_indent,
                    "text": block_text,
                }
            )
            continue
        index += 1
    return blocks


def _extract_block_id(block_text: str) -> str:
    for line in block_text.splitlines():
        stripped = line.lstrip()
        candidate = stripped[2:] if stripped.startswith("- ") else stripped
        if candidate.startswith("id:"):
            return candidate[3:].strip()
    return ""


def _extract_block_name(block_text: str) -> str:
    for line in block_text.splitlines():
        stripped = line.lstrip()
        candidate = stripped[2:] if stripped.startswith("- ") else stripped
        if candidate.startswith("name:"):
            return candidate[5:].strip()
    return ""


def _is_recognized_legacy_setup_workflow(document: bytes) -> bool:
    try:
        _validate_workflow_yaml(document)
        step_names = tuple(str(block["name"]) for block in _scan_step_blocks(document))
    except BootstrapPlanError:
        return False
    return (
        step_names == SUPPORTED_YAML_LEGACY_WORKFLOW_STEP_NAMES
        and all(marker in document for marker in SUPPORTED_YAML_LEGACY_WORKFLOW_MARKERS)
    )


def _find_managed_step_block(
    blocks: Sequence[dict[str, int | str]],
    step_id: str,
    *,
    allow_legacy_name: bool,
) -> dict[str, int | str] | None:
    id_matches = [block for block in blocks if block["id"] == step_id]
    if len(id_matches) > 1:
        raise BootstrapPlanError("managed workflow step id must match exactly one step")
    legacy_matches = (
        [
            block
            for block in blocks
            if block["name"] in SUPPORTED_YAML_LEGACY_STEP_NAMES[step_id]
        ]
        if allow_legacy_name
        else []
    )
    if id_matches:
        selected = id_matches[0]
        if any(block["start"] != selected["start"] for block in legacy_matches):
            raise BootstrapPlanError(
                "managed workflow contains both reserved and legacy bootstrap steps"
            )
        return selected
    if len(legacy_matches) > 1:
        raise BootstrapPlanError(
            "supported legacy workflow step name must match exactly one step"
        )
    return legacy_matches[0] if legacy_matches else None


def _extract_step_id(patch: SemanticPatchSpec) -> str:
    text = patch.replacement_text if patch.replacement_text is not None else patch.match_text
    for line in text.replace("\r\n", "\n").splitlines():
        stripped = line.strip()
        if stripped.startswith("id:"):
            return stripped[3:].strip()
    raise BootstrapPlanError("managed workflow patch must declare an id")


def _find_anchor_block(blocks: Sequence[dict[str, int | str]], match_text: str) -> dict[str, int | str]:
    normalized = match_text.replace("\r\n", "\n")
    matches = [
        block
        for block in blocks
        if normalized in str(block["text"]).replace("\r\n", "\n")
        or normalized.strip() == str(block["id"])
    ]
    if len(matches) != 1:
        raise BootstrapPlanError(
            "managed workflow insert anchor must match exactly one step block"
        )
    return matches[0]


def _indent_block(
    indent: int | str,
    replacement_text: str | None,
    line_ending: str,
) -> str:
    if replacement_text is None:
        raise BootstrapPlanError("managed workflow insert requires replacement_text")
    spaces = " " * int(indent)
    lines = replacement_text.replace("\r\n", "\n").splitlines()
    if not lines:
        raise BootstrapPlanError("managed workflow replacement must not be empty")
    first = lines[0].strip()
    if first.startswith("- "):
        first = first[2:].lstrip()
    remaining = lines[1:]
    nonempty_indents = [
        len(line) - len(line.lstrip())
        for line in remaining
        if line.strip()
    ]
    base_indent = min(nonempty_indents) if nonempty_indents else 0
    rendered = [f"{spaces}- {first}{line_ending}"]
    for line in remaining:
        if not line.strip():
            rendered.append(line_ending)
            continue
        relative = line[base_indent:]
        rendered.append(f"{spaces}  {relative}{line_ending}")
    return "".join(rendered)


def _line_ending(document: bytes) -> str:
    return "\r\n" if b"\r\n" in document else "\n"


def _apply_text_patch(base: bytes, patch: SemanticPatchSpec) -> bytes:
    text = base.decode("utf-8")
    if text.count(patch.match_text) != 1:
        raise BootstrapPlanError(f"semantic patch {patch.operation} must match exactly once")
    if patch.operation == "replace":
        if patch.replacement_text is None:
            raise BootstrapPlanError("semantic patch replace requires replacement_text")
        return text.replace(patch.match_text, patch.replacement_text, 1).encode("utf-8")
    if patch.operation == "delete":
        return text.replace(patch.match_text, "", 1).encode("utf-8")
    if patch.operation == "insert_before":
        if patch.replacement_text is None:
            raise BootstrapPlanError("semantic patch insert_before requires replacement_text")
        return text.replace(patch.match_text, f"{patch.replacement_text}{patch.match_text}", 1).encode("utf-8")
    if patch.operation == "insert_after":
        if patch.replacement_text is None:
            raise BootstrapPlanError("semantic patch insert_after requires replacement_text")
        return text.replace(patch.match_text, f"{patch.match_text}{patch.replacement_text}", 1).encode("utf-8")
    raise BootstrapPlanError(f"unsupported semantic patch operation: {patch.operation}")


def _validate_repository_plan(root: Path, plan: BootstrapPlan) -> None:
    if not plan.actions:
        raise BootstrapPlanError("repository plan must contain actions")
    expected = canonical_sha256(BootstrapPlanPayload.model_validate(plan.model_dump(mode="python", exclude={"plan_hash"})).model_dump(mode="json"))
    if expected != plan.plan_hash:
        raise BootstrapPlanError("stale plan fingerprint")
    _validate_destinations(root, plan.operation_id, [action.template_payload for action in plan.actions if action.template_payload is not None])


def _enforce_plan_bindings(root: Path, plan: BootstrapPlan) -> None:
    lock_hash = _hash_if_exists(root, LOCK_PATH) or "missing"
    lock = _load_lock(root)
    for action in plan.actions:
        payload = action.template_payload
        if payload is None:
            continue
        current_bytes = _read_bytes(_target(root, payload.destination_path))
        disposition = _planned_disposition(root, payload, current_bytes, lock)
        binding = _binding_map(action.diagnostics)
        if binding.get("target") != (disposition.target_hash or "missing"):
            raise BootstrapApplyError(f"filesystem drift detected since plan: {payload.destination_path}")
        if binding.get("sibling") != (disposition.sibling_hash or "missing"):
            raise BootstrapApplyError(f"proposed sibling drift detected since plan: {payload.destination_path}")
        if binding.get("lock") != lock_hash:
            raise BootstrapApplyError("bootstrap lock changed since plan")
        if binding.get("mode") != disposition.mode:
            raise BootstrapApplyError(f"planned disposition drifted: {payload.destination_path}")


def _binding_map(diagnostics: Sequence[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for item in diagnostics:
        if ":" not in item:
            continue
        key, value = item.split(":", 1)
        if key in {"target", "lock", "sibling", "mode"}:
            result[key] = value
    return result


def _validate_operation_id(operation_id: str) -> None:
    if _OPERATION_ID_RE.fullmatch(operation_id) is None:
        raise BootstrapPlanError("operation_id must be a safe bounded identifier")


def _validate_destinations(root: Path, operation_id: str, payloads: Sequence[TemplatePayloadSpec]) -> None:
    seen: dict[str, str] = {}
    reserved = {
        LOCK_PATH.casefold(),
        _repo_relative(_journal_path(root, operation_id), root).casefold(),
        _repo_relative(_receipt_path(root, operation_id), root).casefold(),
    }
    for payload in payloads:
        path = validate_repository_relative_path(payload.destination_path, field="destination_path")
        for candidate in (path, f"{path}.foundry-proposed"):
            folded = candidate.casefold()
            if folded in seen or folded in reserved:
                raise BootstrapPlanError(f"destination collision: {candidate}")
            seen[folded] = candidate
            _target(root, candidate)


def _build_lock(lock_before: BootstrapLock | None, plan: BootstrapPlan, managed_files: tuple[ManagedFileEntry, ...]) -> BootstrapLock:
    previous = lock_before
    sidecars = () if previous is None else previous.sidecar_paths
    receipt_rel = _repo_relative(_receipt_path(Path.cwd() if False else Path(plan.operation_id) if False else Path("."), plan.operation_id), Path("."))  # unreachable sentinel
    del receipt_rel
    return BootstrapLock(
        engine="repository-engine",
        runtime_repository=plan.runtime_repository,
        channel="repository",
        runtime_commit=plan.runtime_commit,
        managed_files=managed_files,
        github_environments=() if previous is None else previous.github_environments,
        cloud_resources=() if previous is None else previous.cloud_resources,
        sidecar_paths=sidecars,
        last_activation=ActivationOutcomeRecord(outcome="succeeded"),
    )


def _lock_equal(left: BootstrapLock | None, right: BootstrapLock) -> bool:
    return left is not None and left.model_dump(mode="json") == right.model_dump(mode="json")


@dataclass(frozen=True)
class _JournalEntry:
    path: str
    before: bytes | None


def _prepare_journal_entries(
    root: Path,
    plan: BootstrapPlan,
    planned: Sequence[tuple[BootstrapAction, TemplatePayloadSpec, _Disposition, bytes | None]],
    lock_before: BootstrapLock | None,
) -> tuple[_JournalEntry, ...]:
    entries: dict[str, _JournalEntry] = {
        LOCK_PATH: _JournalEntry(LOCK_PATH, _read_bytes(_target(root, LOCK_PATH))),
        _repo_relative(_journal_path(root, plan.operation_id), root): _JournalEntry(_repo_relative(_journal_path(root, plan.operation_id), root), None),
        _repo_relative(_receipt_path(root, plan.operation_id), root): _JournalEntry(_repo_relative(_receipt_path(root, plan.operation_id), root), None),
    }
    for _, payload, disposition, current_bytes in planned:
        entries[payload.destination_path] = _JournalEntry(payload.destination_path, current_bytes)
        if disposition.conflict_sibling_path is not None:
            entries[disposition.conflict_sibling_path] = _JournalEntry(disposition.conflict_sibling_path, _read_bytes(_target(root, disposition.conflict_sibling_path)))
    return tuple(entries.values())


def _write_journal(path: Path, operation_id: str, entries: Sequence[_JournalEntry], receipt_path: Path, *, applied: Sequence[str], state: str = "prepared") -> None:
    payload = {
        "operation_id": operation_id,
        "state": state,
        "receipt_path": path.parent.parent.joinpath("receipts", f"{operation_id}.json").relative_to(path.parents[2]).as_posix(),
        "applied": list(applied),
        "entries": {entry.path: _encode_preimage(entry.before) for entry in entries},
    }
    _atomic_write_json(path, payload, fsync=True)


def _recover_journal(root: Path, operation_id: str) -> None:
    journal_path = _journal_path(root, operation_id)
    if not journal_path.exists():
        return
    payload = _read_json_file(journal_path)
    entries = payload.get("entries")
    if not isinstance(entries, Mapping):
        raise BootstrapApplyError("journal is invalid")
    for repo_path, encoded in reversed(list(entries.items())):
        target = _target(root, str(repo_path))
        original = _decode_preimage(encoded)
        if original is None:
            if target.exists():
                target.unlink()
        else:
            _atomic_write_bytes(target, original)
    journal_path.unlink(missing_ok=True)


def _receipt_path(root: Path, operation_id: str) -> Path:
    return _target(root, f"{RECEIPT_DIR}/{operation_id}.json")


def _journal_path(root: Path, operation_id: str) -> Path:
    return _target(root, f"{JOURNAL_DIR}/{operation_id}.json")


def _serialize_preimages(entries: Sequence[_JournalEntry]) -> Mapping[str, object]:
    return {entry.path: _encode_preimage(entry.before) for entry in entries}


def _encode_preimage(data: bytes | None) -> Mapping[str, str] | None:
    return None if data is None else {"hex": data.hex()}


def _decode_preimage(value: object) -> bytes | None:
    if value is None:
        return None
    if not isinstance(value, Mapping) or not isinstance(value.get("hex"), str):
        raise BootstrapApplyError("encoded preimage is invalid")
    return bytes.fromhex(str(value["hex"]))


def _load_lock(root: Path) -> BootstrapLock | None:
    target = _target(root, LOCK_PATH)
    if not target.exists():
        return None
    payload = _read_json_file(target)
    return BootstrapLock.from_document(payload)


def _read_json_file(path: Path) -> Mapping[str, object]:
    if path.is_symlink():
        raise BootstrapApplyError(f"symlinks are not supported: {path}")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BootstrapApplyError("bootstrap lock must be strict JSON") from exc
    if not isinstance(payload, Mapping):
        raise BootstrapApplyError("JSON document must be an object")
    return dict(payload)


def _managed_entry(lock: BootstrapLock | None, path: str) -> ManagedFileEntry | None:
    return None if lock is None else next((entry for entry in lock.managed_files if entry.path == path), None)


def _target(root: Path, path: str) -> Path:
    target = root / Path(PurePosixPath(path))
    resolved_root = root.resolve()
    resolved_parent = target.parent.resolve(strict=False)
    if not resolved_parent.is_relative_to(resolved_root):
        raise BootstrapApplyError(f"path escapes repository: {path}")
    if target.exists() and target.is_symlink():
        raise BootstrapApplyError(f"symlinks are not supported: {path}")
    return target


def _read_bytes(path: Path) -> bytes | None:
    if not path.exists():
        return None
    mode = path.stat(follow_symlinks=False).st_mode
    if not stat.S_ISREG(mode):
        raise BootstrapApplyError(f"special files are not supported: {path}")
    return path.read_bytes()


def _hash_if_exists(root: Path, path: str) -> str | None:
    return _hash_bytes(_read_bytes(_target(root, path)))


def _hash_bytes(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data.replace(b"\r\n", b"\n")).hexdigest()


def _missing_hash() -> str:
    return "0" * 64


def _fingerprints(root: Path, plan: BootstrapPlan, *, include_system: bool) -> tuple[FingerprintRecord, ...]:
    items: list[FingerprintRecord] = []
    for action in plan.actions:
        payload = action.template_payload
        if payload is None:
            continue
        items.append(FingerprintRecord(label=payload.destination_path, sha256=_hash_if_exists(root, payload.destination_path) or _missing_hash()))
        sibling_path = f"{payload.destination_path}.foundry-proposed"
        sibling_hash = _hash_if_exists(root, sibling_path)
        if sibling_hash is not None:
            items.append(FingerprintRecord(label=sibling_path, sha256=sibling_hash))
    if include_system:
        for system_path in (LOCK_PATH, _repo_relative(_journal_path(root, plan.operation_id), root), _repo_relative(_receipt_path(root, plan.operation_id), root)):
            hash_value = _hash_if_exists(root, system_path)
            if hash_value is not None:
                items.append(FingerprintRecord(label=system_path, sha256=hash_value))
    return tuple(items)


def _repo_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = True) -> None:
    """Atomically replace `path` with `data` (public helper for post-activation writes)."""

    _atomic_write_bytes(path, data, fsync=fsync)


def _atomic_write_json(path: Path, payload: object, *, fsync: bool = False) -> None:
    _atomic_write_bytes(path, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"), fsync=fsync)


def _atomic_write_bytes(path: Path, data: bytes, *, fsync: bool = False) -> None:
    _ensure_parent(path)
    fd, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temp_path = Path(temp_name)
        if temp_path.parent.resolve() != path.parent.resolve():
            raise BootstrapApplyError("temporary file escaped target directory")
        if temp_path.is_symlink():
            raise BootstrapApplyError("temporary file must not be a symlink")
        os.replace(temp_path, path)
        if path.is_symlink():
            raise BootstrapApplyError("target file must not be a symlink")
        if fsync:
            _fsync_directory(path.parent)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise BootstrapApplyError(f"symlink parent is not supported: {path.parent}")


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _unified_diff(path: str, before: bytes, after: bytes) -> str:
    return "".join(
        difflib.unified_diff(
            before.decode("utf-8").splitlines(keepends=True),
            after.decode("utf-8").splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


def _semantic_patch_id(patches: Sequence[SemanticPatchSpec]) -> str | None:
    return None if not patches else canonical_sha256([patch.model_dump(mode="json") for patch in patches])
