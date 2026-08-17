from __future__ import annotations

import difflib
import hashlib
import json
import os
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
    FingerprintRecord,
    ManagedFileEntry,
    SemanticPatchSpec,
    TemplatePayloadSpec,
)
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapPlanError
from foundry_opt.poc.config import load_strict_yaml_mapping, validate_repository_relative_path

LOCK_PATH = ".foundry-opt/bootstrap.lock.json"
PREIMAGE_DIR = ".foundry-opt/receipts"
SUPPORTED_YAML_PATH = ".github/copilot-setup-steps.yml"
SUPPORTED_YAML_STEP_IDS = frozenset({"foundry-opt-checkout", "foundry-opt-bootstrap"})
LEGACY_FETCH_MARKERS = ("FOUNDRY_OPT_SHARED_REPO_SSH_KEY", "git@github.com", "known_hosts")


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


def inventory_repository(root: Path, payloads: Sequence[TemplatePayloadSpec]) -> RepositoryInventory:
    lock = _load_lock(root)
    managed_by_path = {entry.path: entry for entry in lock.managed_files} if lock else {}
    entries = tuple(
        RepositoryInventoryEntry(
            path=payload.destination_path,
            exists=_target(root, payload.destination_path).exists(),
            sha256=_hash_if_exists(root, payload.destination_path),
            managed=managed_by_path.get(payload.destination_path),
        )
        for payload in payloads
    )
    return RepositoryInventory(entries=entries, lock=lock)


def plan_repository(
    root: Path,
    *,
    operation_id: str,
    runtime_repository: str,
    runtime_commit: str,
    repository_identity: str,
    payloads: Sequence[TemplatePayloadSpec],
) -> BootstrapPlan:
    inventory = inventory_repository(root, payloads)
    _validate_destinations(root, payloads)
    actions: list[BootstrapAction] = []
    for payload in payloads:
        action_id = f"repository:{payload.template_id}:{payload.destination_path}"
        target = _target(root, payload.destination_path)
        current_text = _read_text(target)
        before_sha = _hash_if_exists(root, payload.destination_path)
        planned = _planned_content(payload, current_text, inventory.lock)
        diagnostics = [f"before:{before_sha or 'missing'}"]
        if planned.conflict_sibling_path is not None:
            diagnostics.append(f"conflict:{planned.conflict_sibling_path}")
        if planned.changed:
            diagnostics.append(planned.diff)
        actions.append(
            BootstrapAction(
                action_id=action_id,
                phase="repository",
                stage="planned",
                kind="repository-write",
                template_payload=payload,
                diagnostics=tuple(diagnostics),
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
    lock_before = _load_lock(root)
    _validate_repository_plan(root, plan)
    before = _fingerprints(root, plan)
    _bind_before_fingerprints(plan, before)
    preimage_path = _preimage_path(root, plan.operation_id)
    preimages: dict[str, str | None] = {}
    created: list[str] = []
    changed: list[str] = []
    skipped: list[str] = []
    updated_entries: dict[str, ManagedFileEntry] = {entry.path: entry for entry in lock_before.managed_files} if lock_before else {}
    try:
        for action in plan.actions:
            payload = action.template_payload
            if payload is None:
                skipped.append(action.action_id)
                continue
            target = _target(root, payload.destination_path)
            current_text = _read_text(target)
            planned = _planned_content(payload, current_text, lock_before)
            if planned.conflict_sibling_path is not None:
                sibling = _target(root, planned.conflict_sibling_path)
                preimages[planned.conflict_sibling_path] = _read_text(sibling)
                _atomic_write_text(sibling, planned.rendered_text)
                skipped.append(action.action_id)
                continue
            if not planned.changed:
                skipped.append(action.action_id)
                continue
            existing_entry = updated_entries.get(payload.destination_path)
            if current_text is not None and existing_entry is None:
                raise BootstrapApplyError(f"refusing to overwrite unowned file: {payload.destination_path}")
            if existing_entry is not None and _sha_text(current_text) != existing_entry.applied_sha256:
                raise BootstrapApplyError(f"owned file has drifted: {payload.destination_path}")
            preimages[payload.destination_path] = current_text
            _atomic_write_text(target, planned.rendered_text)
            updated_entries[payload.destination_path] = ManagedFileEntry(
                path=payload.destination_path,
                ownership_mode="owned",
                owner_scope="repository",
                template_id=payload.template_id,
                template_base_sha256=_sha_text(payload.rendered_template),
                applied_sha256=_sha_text(planned.rendered_text),
                semantic_patch_id=_semantic_patch_id(payload.semantic_patches),
            )
            (created if current_text is None else changed).append(action.action_id)
        preimages[LOCK_PATH] = _read_text(_target(root, LOCK_PATH))
        _persist_preimages(preimage_path, preimages)
        lock = BootstrapLock(
            engine="repository-engine",
            runtime_repository=plan.runtime_repository,
            channel="repository",
            runtime_commit=plan.runtime_commit,
            managed_files=tuple(sorted(updated_entries.values(), key=lambda item: item.path)),
            github_environments=() if lock_before is None else lock_before.github_environments,
            cloud_resources=() if lock_before is None else lock_before.cloud_resources,
            sidecar_paths=tuple(sorted(set((() if lock_before is None else lock_before.sidecar_paths) + (_repo_relative(preimage_path, root),)))),
            last_activation=ActivationOutcomeRecord(outcome="succeeded"),
        )
        _atomic_write_json(_target(root, LOCK_PATH), lock.model_dump(mode="json"))
    except Exception:
        if preimage_path.exists():
            preimage_path.unlink()
        for repo_path, original in reversed(tuple(preimages.items())):
            if repo_path == LOCK_PATH:
                continue
            target = _target(root, repo_path)
            if original is None:
                if target.exists():
                    target.unlink()
            else:
                _atomic_write_text(target, original)
        raise
    after = _fingerprints(root, plan)
    receipt = BootstrapReceipt.create(
        operation_id=plan.operation_id,
        runtime_repository=plan.runtime_repository,
        runtime_commit=plan.runtime_commit,
        repository_identity=plan.repository_identity,
        plan_hash=plan.plan_hash,
        before_fingerprints=before,
        after_fingerprints=after,
        created_actions=tuple(created),
        changed_actions=tuple(changed),
        skipped_actions=tuple(skipped),
    )
    return receipt, lock


def rollback_repository(root: Path, receipt: BootstrapReceipt) -> None:
    preimage_path = _preimage_path(root, receipt.operation_id)
    if not preimage_path.exists():
        raise BootstrapApplyError("rollback preimage receipt is unavailable")
    for fingerprint in receipt.after_fingerprints:
        if _hash_if_exists(root, fingerprint.label) != fingerprint.sha256:
            raise BootstrapApplyError(f"rollback refused because current hash changed: {fingerprint.label}")
    preimages = json.loads(preimage_path.read_text(encoding="utf-8"))
    for repo_path, original in reversed(list(preimages.items())):
        target = _target(root, repo_path)
        if original is None:
            if target.exists():
                target.unlink()
        else:
            _atomic_write_text(target, original)


def drift_status(root: Path, lock: BootstrapLock) -> tuple[str, ...]:
    statuses: list[str] = []
    for entry in lock.managed_files:
        current = _hash_if_exists(root, entry.path)
        if current is None:
            statuses.append(f"missing:{entry.path}")
        elif current != entry.applied_sha256:
            statuses.append(f"drifted:{entry.path}")
    return tuple(statuses)


def render_template_payload(payload: TemplatePayloadSpec, current_text: str | None = None) -> bytes:
    base_text = current_text if payload.destination_path == SUPPORTED_YAML_PATH and current_text is not None else payload.rendered_template
    rendered = base_text.encode("utf-8")
    if payload.destination_path == SUPPORTED_YAML_PATH:
        return _patch_reserved_yaml(base_text, payload.semantic_patches)
    for patch in payload.semantic_patches:
        rendered = _apply_text_patch(rendered, patch)
    return rendered


@dataclass(frozen=True)
class _PlannedContent:
    rendered_text: str
    changed: bool
    conflict_sibling_path: str | None
    diff: str


def _planned_content(payload: TemplatePayloadSpec, current_text: str | None, lock: BootstrapLock | None) -> _PlannedContent:
    rendered_text = render_template_payload(payload, current_text).decode("utf-8")
    if any(marker in rendered_text for marker in LEGACY_FETCH_MARKERS):
        raise BootstrapPlanError("legacy private SSH bootstrap fetch must be removed")
    entry = _managed_entry(lock, payload.destination_path)
    conflict_sibling = None
    if current_text is not None and entry is not None:
        current_hash = _sha_text(current_text)
        if current_hash != entry.applied_sha256:
            base_text = payload.rendered_template
            if payload.destination_path == SUPPORTED_YAML_PATH:
                base_text = current_text
                rendered_text = render_template_payload(payload, current_text).decode("utf-8")
            elif current_text != base_text:
                conflict_sibling = f"{payload.destination_path}.foundry-proposed"
    elif current_text is not None and entry is None:
        conflict_sibling = f"{payload.destination_path}.foundry-proposed"
    changed = current_text != rendered_text
    diff = _unified_diff(payload.destination_path, current_text or "", rendered_text) if changed else ""
    return _PlannedContent(rendered_text=rendered_text, changed=changed, conflict_sibling_path=conflict_sibling, diff=diff)


def _patch_reserved_yaml(base_text: str, patches: Sequence[SemanticPatchSpec]) -> bytes:
    payload = yaml.safe_load(base_text)
    if not isinstance(payload, Mapping):
        raise BootstrapPlanError("unsupported or ambiguous copilot-setup-steps.yml shape")
    jobs = payload.get("jobs")
    if not isinstance(jobs, Mapping) or len(jobs) != 1:
        raise BootstrapPlanError("unsupported or ambiguous copilot-setup-steps.yml shape")
    job = next(iter(jobs.values()))
    if not isinstance(job, Mapping):
        raise BootstrapPlanError("unsupported or ambiguous copilot-setup-steps.yml shape")
    steps = job.get("steps")
    if not isinstance(steps, list):
        raise BootstrapPlanError("unsupported or ambiguous copilot-setup-steps.yml shape")
    positions = {
        step.get("id"): index
        for index, step in enumerate(steps)
        if isinstance(step, Mapping) and isinstance(step.get("id"), str)
    }
    for patch in patches:
        replacement = _parse_yaml_block(patch.replacement_text)
        match = _parse_yaml_block(patch.match_text)
        step_id = (match or replacement).get("id")
        if step_id not in SUPPORTED_YAML_STEP_IDS:
            raise BootstrapPlanError("unrecognized managed copilot step id")
        if patch.operation == "delete":
            if step_id not in positions:
                raise BootstrapPlanError("managed copilot step id not found")
            del steps[positions[step_id]]
        elif step_id in positions:
            steps[positions[step_id]] = replacement or match
        elif patch.operation == "insert_after":
            steps.append(replacement or match)
        else:
            raise BootstrapPlanError("unsupported or ambiguous copilot-setup-steps.yml shape")
        positions = {
            step.get("id"): index
            for index, step in enumerate(steps)
            if isinstance(step, Mapping) and isinstance(step.get("id"), str)
        }
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False).encode("utf-8")


def _parse_yaml_block(text: str | None) -> dict[str, object]:
    if text is None or not text.strip():
        return {}
    document = yaml.safe_load(text)
    if not isinstance(document, Mapping):
        raise BootstrapPlanError("managed YAML patch block must be a mapping")
    return dict(document)


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
    expected = canonical_sha256(
        BootstrapPlanPayload.model_validate(plan.model_dump(mode="python", exclude={"plan_hash"})).model_dump(mode="json")
    )
    if expected != plan.plan_hash:
        raise BootstrapPlanError("stale plan fingerprint")
    _validate_destinations(root, [action.template_payload for action in plan.actions if action.template_payload is not None])


def _bind_before_fingerprints(plan: BootstrapPlan, current: tuple[FingerprintRecord, ...]) -> None:
    actual = {item.label: item.sha256 for item in current}
    for action in plan.actions:
        payload = action.template_payload
        if payload is None:
            continue
        before_items = [item for item in action.diagnostics if item.startswith("before:")]
        if len(before_items) != 1:
            raise BootstrapApplyError("plan missing before fingerprint binding")
        expected = before_items[0][7:]
        observed = actual.get(payload.destination_path, "missing")
        if observed != expected:
            raise BootstrapApplyError(f"filesystem drift detected since plan: {payload.destination_path}")


def _validate_destinations(root: Path, payloads: Sequence[TemplatePayloadSpec]) -> None:
    seen: dict[str, str] = {}
    root_resolved = root.resolve()
    for payload in payloads:
        path = validate_repository_relative_path(payload.destination_path, field="destination_path")
        key = path.casefold()
        if key in seen and seen[key] != path:
            raise BootstrapPlanError(f"case-fold path conflict: {seen[key]!r} and {path!r}")
        seen[key] = path
        candidate = _target(root, path).resolve(strict=False)
        if not candidate.is_relative_to(root_resolved):
            raise BootstrapPlanError(f"path escapes repository: {path}")


def _load_lock(root: Path) -> BootstrapLock | None:
    target = _target(root, LOCK_PATH)
    if not target.exists():
        return None
    return BootstrapLock.from_document(target.read_text(encoding="utf-8"))


def _managed_entry(lock: BootstrapLock | None, path: str) -> ManagedFileEntry | None:
    if lock is None:
        return None
    return next((entry for entry in lock.managed_files if entry.path == path), None)


def _hash_if_exists(root: Path, path: str) -> str | None:
    text = _read_text(_target(root, path))
    return None if text is None else _sha_text(text)


def _sha_text(text: str | None) -> str:
    return hashlib.sha256((text or "").replace("\r\n", "\n").encode("utf-8")).hexdigest()


def _target(root: Path, path: str) -> Path:
    return root / Path(PurePosixPath(path))


def _read_text(path: Path) -> str | None:
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _fingerprints(root: Path, plan: BootstrapPlan) -> tuple[FingerprintRecord, ...]:
    entries: list[FingerprintRecord] = []
    for action in plan.actions:
        payload = action.template_payload
        if payload is None:
            continue
        sha = _hash_if_exists(root, payload.destination_path)
        if sha is not None:
            entries.append(FingerprintRecord(label=payload.destination_path, sha256=sha))
    return tuple(entries)


def _persist_preimages(path: Path, preimages: Mapping[str, str | None]) -> None:
    _atomic_write_json(path, preimages)


def _preimage_path(root: Path, operation_id: str) -> Path:
    return _target(root, f"{PREIMAGE_DIR}/{operation_id}.json")


def _repo_relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _atomic_write_text(path: Path, text: str) -> None:
    _atomic_write_bytes(path, text.encode("utf-8"))


def _atomic_write_json(path: Path, payload: object) -> None:
    _atomic_write_bytes(path, json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("ascii"))


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.foundry-tmp")
    temp.write_bytes(data)
    os.replace(temp, path)


def _unified_diff(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            lineterm="",
        )
    )


def _semantic_patch_id(patches: Sequence[SemanticPatchSpec]) -> str | None:
    return None if not patches else canonical_sha256([patch.model_dump(mode="json") for patch in patches])
