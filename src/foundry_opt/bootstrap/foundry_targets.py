from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
import re
from typing import Protocol
from urllib.parse import quote, urlparse

import yaml
from azure.identity import AzureCliCredential, ChainedTokenCredential, DefaultAzureCredential

from foundry_opt.bootstrap.contracts import BootstrapSidecar, FoundryTargetSource, ReviewedFoundryTarget
from foundry_opt.bootstrap.errors import BootstrapApplyError, BootstrapConfigError
from foundry_opt.bootstrap.owner_review import ResourceLink, ResourceLinksReview
from foundry_opt.bootstrap.providers.foundry import FoundryAdapter
from foundry_opt.bootstrap.runner import (
    BootstrapFoundryTargetRecord,
    BootstrapQuestion,
    BootstrapStageOutcome,
)

_PROJECT_ENDPOINT_RE = re.compile(r"^https://[^\s]+/api/projects/[^\s/]+/?$")
_AGENT_NAME_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?$")
_ACCOUNT_RESOURCE_ID_RE = re.compile(
    r"^/subscriptions/[^/]+/resourceGroups/[^/]+/providers/"
    r"Microsoft\.CognitiveServices/accounts/[^/]+$",
    re.IGNORECASE,
)
_SOURCE_LABELS: Mapping[str, str] = {
    "existing_profile": "existing v2 profile",
    "agent_metadata": ".foundry/agent-metadata*.yaml",
    "azure_yaml": "azure.yaml",
    "azd_environment": "azd environment values",
    "binding_evidence": "reviewed binding evidence",
    "owner_answer": "owner answer",
}


def _validate_project_endpoint(value: str) -> str:
    if _PROJECT_ENDPOINT_RE.fullmatch(value) is None:
        raise BootstrapConfigError("project_endpoint must be an HTTPS Foundry project endpoint")
    return value


def _validate_agent_name(value: str) -> str:
    if _AGENT_NAME_RE.fullmatch(value) is None:
        raise BootstrapConfigError("agent_name must be a safe Foundry agent identifier")
    return value


def _validate_account_resource_id(
    value: str,
    *,
    project_endpoint: str | None = None,
) -> str:
    normalized = value.rstrip("/")
    if _ACCOUNT_RESOURCE_ID_RE.fullmatch(normalized) is None:
        raise BootstrapConfigError(
            "account_resource_id must identify a Microsoft.CognitiveServices account"
        )
    if project_endpoint is not None:
        hostname = urlparse(project_endpoint).hostname or ""
        endpoint_account = hostname.split(".", 1)[0]
        resource_account = normalized.rsplit("/", 1)[-1]
        if (
            endpoint_account
            and endpoint_account.casefold() != resource_account.casefold()
        ):
            raise BootstrapConfigError(
                "account_resource_id account must match the Foundry project endpoint"
            )
    return normalized


def normalized_foundry_target_key(
    project_endpoint: str,
    agent_name: str,
) -> tuple[str, str]:
    return project_endpoint.rstrip("/").casefold(), agent_name.casefold()


def _human_source(source: FoundryTargetSource | None) -> str:
    if source is None:
        return "unknown source"
    return _SOURCE_LABELS.get(source, source.replace("_", " "))


@dataclass(frozen=True, slots=True)
class _FieldValue:
    value: str
    source: FoundryTargetSource
    detail: str


@dataclass(frozen=True, slots=True)
class _TargetSeed:
    detail: str
    project_endpoint: _FieldValue | None = None
    agent_name: _FieldValue | None = None
    account_resource_id: _FieldValue | None = None
    expected_version: str | None = None


@dataclass(frozen=True, slots=True)
class _PendingQuestion:
    repo_agent_id: str
    root: str
    missing_fields: tuple[str, ...]
    project_endpoint: _FieldValue | None
    agent_name: _FieldValue | None
    account_resource_id: _FieldValue | None
    blocked_detail: str | None = None


@dataclass(frozen=True, slots=True)
class _LocalTargetContext:
    repo_agent_id: str
    root: str
    source_root: str
    package_root: str
    source_fingerprint: str
    package_fingerprint: str
    project_endpoint: _FieldValue | None
    agent_name: _FieldValue | None
    account_resource_id: _FieldValue | None
    expected_version: str | None
    blocked_detail: str | None

    @property
    def missing_fields(self) -> tuple[str, ...]:
        missing: list[str] = []
        if self.project_endpoint is None:
            missing.append("project_endpoint")
        if self.agent_name is None:
            missing.append("agent_name")
        if self.account_resource_id is None:
            missing.append("account_resource_id")
        return tuple(missing)


@dataclass(frozen=True, slots=True)
class FoundryProjectInventory:
    project_endpoint: str
    agent_latest_versions: Mapping[str, str | None]


class FoundryTargetInventoryAdapterProtocol(Protocol):
    def inspect_project(self, project_endpoint: str) -> FoundryProjectInventory: ...

    def observe_agent(
        self,
        project_endpoint: str,
        *,
        agent_name: str,
        agent_version: str,
        source_root: str,
        package_root: str,
    ) -> Mapping[str, object]: ...


def build_local_user_credential() -> ChainedTokenCredential:
    return ChainedTokenCredential(
        AzureCliCredential(),
        DefaultAzureCredential(exclude_interactive_browser_credential=True),
    )


class DefaultFoundryTargetInventoryAdapter(FoundryTargetInventoryAdapterProtocol):
    def __init__(self, *, credential: object | None = None) -> None:
        self._credential = credential or build_local_user_credential()
        self._clients: dict[str, FoundryAdapter] = {}

    def _client_for(self, project_endpoint: str) -> FoundryAdapter:
        client = self._clients.get(project_endpoint)
        if client is None:
            client = FoundryAdapter(project_endpoint, self._credential)
            self._clients[project_endpoint] = client
        return client

    def inspect_project(self, project_endpoint: str) -> FoundryProjectInventory:
        agents = self._client_for(project_endpoint).inventory_agents()
        latest_versions = {
            str(item.get("name") or "").casefold(): (
                str(item.get("latest_version"))
                if item.get("latest_version") not in (None, "")
                else None
            )
            for item in agents
            if item.get("name")
        }
        return FoundryProjectInventory(
            project_endpoint=project_endpoint,
            agent_latest_versions=latest_versions,
        )

    def observe_agent(
        self,
        project_endpoint: str,
        *,
        agent_name: str,
        agent_version: str,
        source_root: str,
        package_root: str,
    ) -> Mapping[str, object]:
        return self._client_for(project_endpoint).observe_agent_binding(
            agent_name=agent_name,
            agent_version=agent_version,
            source_root=source_root,
            package_root=package_root,
        )


class DefaultFoundryTargetResolutionHandler:
    def __init__(
        self,
        *,
        foundry_inventory: FoundryTargetInventoryAdapterProtocol | None = None,
        binding_evidence_by_root: Mapping[str, Mapping[str, object]] | None = None,
        binding_evidence_by_agent: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self._foundry_inventory = foundry_inventory or DefaultFoundryTargetInventoryAdapter()
        self._binding_evidence_by_root = {
            str(key).casefold(): dict(value)
            for key, value in (binding_evidence_by_root or {}).items()
        }
        self._binding_evidence_by_agent = {
            str(key).casefold(): dict(value)
            for key, value in (binding_evidence_by_agent or {}).items()
        }

    def prepare(
        self,
        *,
        operation,
    ) -> BootstrapStageOutcome:
        records = self._prepare_records(operation)
        if self._next_pending_question(operation, records) is not None:
            return BootstrapStageOutcome(
                stage="foundry_target_resolution",
                note="Resolve the remaining reviewed Foundry target inputs.",
                foundry_targets=records,
            )
        blocked = [
            item.repo_agent_id
            for item in records
            if item.reviewed_target.state == "blocked"
        ]
        note = "Reviewed Foundry targets were resolved."
        if blocked:
            note = (
                "Reviewed Foundry targets were recorded, but deployment readiness stays false "
                f"for blocked targets: {', '.join(sorted(blocked, key=str.casefold))}."
            )
        return BootstrapStageOutcome(
            stage=(
                "verification_policy"
                if operation.registration_intents
                else "register_enable"
            ),
            note=note,
            foundry_targets=records,
        )

    def persisted_answer_value(
        self,
        *,
        operation,
        answer: object,
    ) -> Mapping[str, str]:
        question = self._require_pending_question(operation, operation.foundry_targets)
        if question.blocked_detail is not None:
            if isinstance(answer, str):
                if answer.strip().casefold() == "retry":
                    payload: dict[str, str] = {}
                elif len(question.missing_fields) == 1:
                    payload = {question.missing_fields[0]: answer}
                else:
                    raise BootstrapApplyError(
                        "blocked Foundry target correction requires named fields"
                    )
            elif isinstance(answer, Mapping):
                payload = {str(key): str(value) for key, value in answer.items()}
            else:
                raise BootstrapApplyError(
                    "blocked Foundry target answers must retry or provide corrected fields"
                )
            retry = payload.pop("retry", None)
            if retry is not None and retry.casefold() not in {"true", "yes", "1"}:
                raise BootstrapApplyError("Foundry target retry must be true")
            if retry is not None and payload:
                raise BootstrapApplyError(
                    "Foundry target retry cannot be combined with corrected fields"
                )
            unsupported = set(payload) - {
                "project_endpoint",
                "agent_name",
                "account_resource_id",
            }
            if unsupported:
                raise BootstrapApplyError(
                    "unsupported Foundry target field: "
                    + ", ".join(sorted(unsupported))
                )
            merged = {
                key: value
                for key, value in (
                    (
                        "project_endpoint",
                        (
                            question.project_endpoint.value
                            if question.project_endpoint is not None
                            else None
                        ),
                    ),
                    (
                        "agent_name",
                        (
                            question.agent_name.value
                            if question.agent_name is not None
                            else None
                        ),
                    ),
                    (
                        "account_resource_id",
                        (
                            question.account_resource_id.value
                            if question.account_resource_id is not None
                            else None
                        ),
                    ),
                )
                if value is not None
            }
            merged.update(payload)
            missing = {
                "project_endpoint",
                "agent_name",
                "account_resource_id",
            } - set(merged)
            if missing:
                raise BootstrapApplyError(
                    "blocked Foundry target correction must provide: "
                    + ", ".join(sorted(missing))
                )
            project_endpoint = _validate_project_endpoint(
                merged["project_endpoint"]
            )
            return {
                "project_endpoint": project_endpoint,
                "agent_name": _validate_agent_name(merged["agent_name"]),
                "account_resource_id": _validate_account_resource_id(
                    merged["account_resource_id"],
                    project_endpoint=project_endpoint,
                ),
            }
        if isinstance(answer, str):
            if len(question.missing_fields) != 1:
                raise BootstrapApplyError(
                    "Foundry target question requires named fields when multiple "
                    "values are unresolved"
                )
            payload = {question.missing_fields[0]: answer}
        elif isinstance(answer, Mapping):
            payload = {str(key): str(value) for key, value in answer.items()}
        else:
            raise BootstrapApplyError("foundry target answer must be a mapping or string")
        expected = set(question.missing_fields)
        actual = set(payload)
        allowed = {"project_endpoint", "agent_name", "account_resource_id"}
        if not expected.issubset(actual) or not actual.issubset(allowed):
            raise BootstrapApplyError(
                "foundry target answer must include these fields: "
                + ", ".join(sorted(expected))
            )
        normalized: dict[str, str] = {}
        project_endpoint = (
            question.project_endpoint.value
            if question.project_endpoint is not None
            else None
        )
        if "project_endpoint" in payload:
            project_endpoint = _validate_project_endpoint(
                payload["project_endpoint"]
            )
            normalized["project_endpoint"] = project_endpoint
        if "agent_name" in payload:
            normalized["agent_name"] = _validate_agent_name(
                payload["agent_name"]
            )
        if "account_resource_id" in payload:
            normalized["account_resource_id"] = _validate_account_resource_id(
                payload["account_resource_id"],
                project_endpoint=project_endpoint,
            )
        return normalized

    def build_question(
        self,
        *,
        operation,
        question_id: str,
    ) -> BootstrapQuestion | None:
        pending = self._next_pending_question(operation, operation.foundry_targets)
        if pending is None:
            return None
        if pending.blocked_detail is not None:
            lines = [
                f"The Foundry target for `{pending.repo_agent_id}` is blocked.",
                f"- Detail: {pending.blocked_detail}",
            ]
        else:
            lines = [
                f"Provide the unresolved Foundry target fields for `{pending.repo_agent_id}`."
            ]
        if pending.project_endpoint is not None:
            lines.append(
                f"- project_endpoint: `{pending.project_endpoint.value}` "
                f"(from {_human_source(pending.project_endpoint.source)})"
            )
        if pending.agent_name is not None:
            lines.append(
                f"- agent_name: `{pending.agent_name.value}` "
                f"(from {_human_source(pending.agent_name.source)})"
            )
        if pending.account_resource_id is not None:
            lines.append("- Azure account: resolved")
        if pending.blocked_detail is not None:
            if pending.missing_fields:
                lines.append(
                    "- Still needed: "
                    + ", ".join(
                        f"`{field}`" for field in pending.missing_fields
                    )
                )
            if pending.blocked_detail.startswith("owner input recorded;"):
                lines.append("- Provide the remaining field through the skill bridge.")
            else:
                lines.append(
                    "- Retry after correcting Foundry access, or provide corrected "
                    "target fields."
                )
        else:
            owner_fields = tuple(
                field
                for field in pending.missing_fields
                if field != "account_resource_id"
            )
            if owner_fields:
                lines.append(
                    "- Search the repository first; ask the owner only if these remain "
                    "unresolved: "
                    + ", ".join(f"`{field}`" for field in owner_fields)
                )
            if "account_resource_id" in pending.missing_fields:
                lines.extend(
                    (
                        "- Azure account lookup is still needed for this endpoint.",
                        "- Use the current Azure login and coding-agent Azure tools to "
                        "resolve the exact Microsoft.CognitiveServices account.",
                        "- Ask the owner to correct or choose Azure access only if the "
                        "tool lookup returns no unique account.",
                    )
                )
            else:
                lines.append(
                    "- Provide the requested value or values through the skill bridge."
                )
        return BootstrapQuestion(
            question_id=question_id,
            kind="foundry_target",
            title=f"Resolve the Foundry target for {pending.repo_agent_id}",
            details_markdown="\n".join(lines),
            required_fields=pending.missing_fields,
        )

    def render_owner_markdown(
        self,
        *,
        operation,
    ) -> str | None:
        selected = {
            item.casefold(): item
            for item in self._target_agent_ids(operation)
        }
        if not selected:
            return None
        records = {
            item.repo_agent_id.casefold(): item
            for item in operation.foundry_targets
        }
        pending = self._next_pending_question(operation, operation.foundry_targets)
        lines = ["## Foundry targets"]
        for key in sorted(selected, key=str.casefold):
            repo_agent_id = selected[key]
            record = records.get(key)
            if record is not None:
                target = record.reviewed_target
                lines.append(f"- {repo_agent_id}: {target.state}")
                if target.project_endpoint is not None and target.project_endpoint_source is not None:
                    lines.append(
                        f"  - Endpoint: {target.project_endpoint} ({_human_source(target.project_endpoint_source)})"
                    )
                if target.agent_name is not None and target.agent_name_source is not None:
                    lines.append(
                        f"  - Agent name: {target.agent_name} ({_human_source(target.agent_name_source)})"
                    )
                if target.latest_agent_version is not None:
                    lines.append(f"  - Latest immutable version: {target.latest_agent_version}")
                lines.append(
                    f"  - Deployment ready: {'yes' if target.deployment_ready else 'no'}"
                )
                if target.detail:
                    lines.append(f"  - Detail: {target.detail}")
                continue
            if pending is not None and pending.repo_agent_id.casefold() == key:
                lines.append(f"- {repo_agent_id}: unresolved")
                if pending.project_endpoint is not None:
                    lines.append(
                        f"  - Endpoint: {pending.project_endpoint.value} "
                        f"({_human_source(pending.project_endpoint.source)})"
                    )
                if pending.agent_name is not None:
                    lines.append(
                        f"  - Agent name: {pending.agent_name.value} "
                        f"({_human_source(pending.agent_name.source)})"
                    )
                lines.append(
                    "  - Still needed: "
                    + ", ".join(
                        (
                            "Azure account lookup"
                            if field == "account_resource_id"
                            else field
                        )
                        for field in sorted(pending.missing_fields)
                    )
                )
            else:
                lines.append(f"- {repo_agent_id}: waiting for earlier target resolution")
        return "\n".join(lines)

    def build_resource_links(
        self,
        *,
        operation,
    ) -> ResourceLinksReview | None:
        foundry: list[ResourceLink] = []
        azure: list[ResourceLink] = []
        for record in operation.foundry_targets:
            target = record.reviewed_target
            if target.project_endpoint is not None:
                foundry.append(
                    ResourceLink(
                        label=f"{record.repo_agent_id} project",
                        target=target.project_endpoint,
                        url=target.project_endpoint,
                    )
                )
            if (
                target.project_endpoint is not None
                and target.agent_name is not None
                and target.latest_agent_version is not None
            ):
                foundry.append(
                    ResourceLink(
                        label=f"{record.repo_agent_id} agent version",
                        target=f"{target.agent_name}:{target.latest_agent_version}",
                        url=(
                            f"{target.project_endpoint.rstrip('/')}/agents/"
                            f"{quote(target.agent_name, safe='')}/versions/"
                            f"{quote(target.latest_agent_version, safe='')}"
                        ),
                    )
                )
            if target.account_resource_id is not None:
                azure.append(
                    ResourceLink(
                        label=f"{record.repo_agent_id} account",
                        target=target.account_resource_id,
                        url=f"https://resources.azure.com{target.account_resource_id}",
                    )
                )
        if not foundry and not azure:
            return None
        return ResourceLinksReview(azure=tuple(azure), foundry=tuple(foundry))

    def handle_answer(
        self,
        *,
        operation,
        answer: object,
    ) -> BootstrapStageOutcome:
        normalized = self.persisted_answer_value(operation=operation, answer=answer)
        pending = self._require_pending_question(operation, operation.foundry_targets)
        records = self._prepare_records(
            operation,
            owner_overrides={pending.repo_agent_id.casefold(): normalized},
        )
        if not any(
            item.repo_agent_id.casefold() == pending.repo_agent_id.casefold()
            for item in records
        ):
            context = self._local_context(
                operation,
                repo_agent_id=pending.repo_agent_id,
                owner_overrides=normalized,
            )
            if context is None:
                raise BootstrapApplyError(
                    "selected agent is missing from persisted discovery facts"
                )
            records = tuple(
                sorted(
                    (
                        *records,
                        self._blocked_record(
                            context,
                            detail=(
                                "owner input recorded; still required: "
                                + ", ".join(context.missing_fields)
                            ),
                        ),
                    ),
                    key=lambda item: item.repo_agent_id.casefold(),
                )
            )
        next_pending = self._next_pending_question(operation, records)
        if next_pending is not None:
            return BootstrapStageOutcome(
                stage="foundry_target_resolution",
                note=f"Recorded the reviewed Foundry target for {pending.repo_agent_id}.",
                foundry_targets=records,
            )
        blocked = [
            item.repo_agent_id
            for item in records
            if item.reviewed_target.state == "blocked"
        ]
        note = "Recorded all reviewed Foundry targets."
        if blocked:
            note = (
                "Recorded all reviewed Foundry targets, but deployment readiness stays false "
                f"for blocked targets: {', '.join(sorted(blocked, key=str.casefold))}."
            )
        return BootstrapStageOutcome(
            stage=(
                "verification_policy"
                if operation.registration_intents
                else "register_enable"
            ),
            note=note,
            foundry_targets=records,
        )

    def _prepare_records(
        self,
        operation,
        *,
        owner_overrides: Mapping[str, Mapping[str, str]] | None = None,
    ) -> tuple[BootstrapFoundryTargetRecord, ...]:
        overrides = {str(key).casefold(): dict(value) for key, value in (owner_overrides or {}).items()}
        existing = {
            item.repo_agent_id.casefold(): item
            for item in operation.foundry_targets
            if item.repo_agent_id.casefold() not in overrides
        }
        records: list[BootstrapFoundryTargetRecord] = list(existing.values())
        targets_by_key: dict[tuple[str, str], str] = {}
        for record in records:
            target = record.reviewed_target
            if target.project_endpoint is None or target.agent_name is None:
                continue
            key = normalized_foundry_target_key(
                target.project_endpoint,
                target.agent_name,
            )
            previous = targets_by_key.get(key)
            if previous is not None and previous.casefold() != record.repo_agent_id.casefold():
                raise BootstrapApplyError(
                    "duplicate Foundry target resolved for different repo agents: "
                    f"{previous} and {record.repo_agent_id}"
                )
            targets_by_key[key] = record.repo_agent_id
        selected = {
            item.casefold(): item
            for item in self._target_agent_ids(operation)
        }
        for key in sorted(selected, key=str.casefold):
            if key in existing:
                continue
            context = self._local_context(
                operation,
                repo_agent_id=selected[key],
                owner_overrides=overrides.get(key),
            )
            if context is None:
                records.append(
                    BootstrapFoundryTargetRecord(
                        repo_agent_id=selected[key],
                        root=selected[key],
                        reviewed_target=ReviewedFoundryTarget(
                            state="blocked",
                            detail="selected agent is missing from persisted discovery facts",
                        ),
                    )
                )
                continue
            if context.blocked_detail is not None:
                records.append(self._blocked_record(context, detail=context.blocked_detail))
                continue
            if context.missing_fields:
                continue
            assert context.project_endpoint is not None
            assert context.agent_name is not None
            assert context.account_resource_id is not None
            target_key = normalized_foundry_target_key(
                context.project_endpoint.value,
                context.agent_name.value,
            )
            previous = targets_by_key.get(target_key)
            if previous is not None and previous.casefold() != context.repo_agent_id.casefold():
                raise BootstrapApplyError(
                    "duplicate Foundry target resolved for different repo agents: "
                    f"{previous} and {context.repo_agent_id}"
                )
            targets_by_key[target_key] = context.repo_agent_id
            records.append(self._classify_target(context))
        filtered = [
            item
            for item in records
            if item.repo_agent_id.casefold() in selected
        ]
        return tuple(sorted(filtered, key=lambda item: item.repo_agent_id.casefold()))

    def _next_pending_question(
        self,
        operation,
        records: Sequence[BootstrapFoundryTargetRecord],
    ) -> _PendingQuestion | None:
        by_id = {
            item.repo_agent_id.casefold(): item
            for item in records
        }
        for repo_agent_id in sorted(
            self._target_agent_ids(operation),
            key=str.casefold,
        ):
            record = by_id.get(repo_agent_id.casefold())
            if record is not None and record.reviewed_target.state != "blocked":
                continue
            if record is not None:
                target = record.reviewed_target
                return _PendingQuestion(
                    repo_agent_id=record.repo_agent_id,
                    root=record.root,
                    missing_fields=tuple(
                        field
                        for field, value in (
                            ("project_endpoint", target.project_endpoint),
                            ("agent_name", target.agent_name),
                            ("account_resource_id", target.account_resource_id),
                        )
                        if value is None
                    ),
                    project_endpoint=(
                        _FieldValue(
                            target.project_endpoint,
                            target.project_endpoint_source,
                            "blocked target",
                        )
                        if (
                            target.project_endpoint is not None
                            and target.project_endpoint_source is not None
                        )
                        else None
                    ),
                    agent_name=(
                        _FieldValue(
                            target.agent_name,
                            target.agent_name_source,
                            "blocked target",
                        )
                        if (
                            target.agent_name is not None
                            and target.agent_name_source is not None
                        )
                        else None
                    ),
                    account_resource_id=(
                        _FieldValue(
                            target.account_resource_id,
                            "owner_answer",
                            "blocked target",
                        )
                        if target.account_resource_id is not None
                        else None
                    ),
                    blocked_detail=target.detail,
                )
            context = self._local_context(
                operation,
                repo_agent_id=repo_agent_id,
            )
            if context is None or context.blocked_detail is not None:
                continue
            if context.missing_fields:
                return _PendingQuestion(
                    repo_agent_id=context.repo_agent_id,
                    root=context.root,
                    missing_fields=context.missing_fields,
                    project_endpoint=context.project_endpoint,
                    agent_name=context.agent_name,
                    account_resource_id=context.account_resource_id,
                )
        return None

    @staticmethod
    def _target_agent_ids(operation) -> tuple[str, ...]:
        if operation.registration_intents:
            return tuple(
                item.repo_agent_id
                for item in operation.registration_intents
                if item.intent == "register_enabled"
            )
        return tuple(operation.selection_plan.selected_agent_ids)

    def _require_pending_question(
        self,
        operation,
        records: Sequence[BootstrapFoundryTargetRecord],
    ) -> _PendingQuestion:
        pending = self._next_pending_question(operation, records)
        if pending is None:
            raise BootstrapApplyError("there is no unresolved Foundry target question")
        return pending

    def _classify_target(self, context: _LocalTargetContext) -> BootstrapFoundryTargetRecord:
        assert context.project_endpoint is not None
        assert context.agent_name is not None
        assert context.account_resource_id is not None
        account_resource_id = context.account_resource_id.value
        try:
            inventory = self._foundry_inventory.inspect_project(context.project_endpoint.value)
        except Exception as exc:
            return self._blocked_record(
                context,
                detail=f"project inventory failed: {str(exc).strip() or type(exc).__name__}",
            )
        latest_version = inventory.agent_latest_versions.get(context.agent_name.value.casefold())
        if latest_version in (None, ""):
            if context.agent_name.value.casefold() in inventory.agent_latest_versions:
                return BootstrapFoundryTargetRecord(
                    repo_agent_id=context.repo_agent_id,
                    root=context.root,
                    reviewed_target=ReviewedFoundryTarget(
                        state="existing_unknown",
                        project_endpoint=context.project_endpoint.value,
                        project_endpoint_source=context.project_endpoint.source,
                        agent_name=context.agent_name.value,
                        agent_name_source=context.agent_name.source,
                        account_resource_id=account_resource_id,
                        deployment_ready=False,
                        detail="project access succeeded, but the latest immutable version is unavailable",
                    ),
                )
            return BootstrapFoundryTargetRecord(
                repo_agent_id=context.repo_agent_id,
                root=context.root,
                reviewed_target=ReviewedFoundryTarget(
                    state="new_target",
                    project_endpoint=context.project_endpoint.value,
                    project_endpoint_source=context.project_endpoint.source,
                    agent_name=context.agent_name.value,
                    agent_name_source=context.agent_name.source,
                    account_resource_id=account_resource_id,
                    deployment_ready=True,
                    detail="project access succeeded and the agent name is not yet present",
                ),
            )
        expected_version = context.expected_version
        if expected_version is not None and expected_version != latest_version:
            return BootstrapFoundryTargetRecord(
                repo_agent_id=context.repo_agent_id,
                root=context.root,
                reviewed_target=ReviewedFoundryTarget(
                    state="existing_diverged",
                    project_endpoint=context.project_endpoint.value,
                    project_endpoint_source=context.project_endpoint.source,
                    agent_name=context.agent_name.value,
                    agent_name_source=context.agent_name.source,
                    account_resource_id=account_resource_id,
                    latest_agent_version=latest_version,
                    deployment_ready=True,
                    detail=(
                        f"latest immutable version is {latest_version}, "
                        f"but reviewed/local evidence expects {expected_version}"
                    ),
                ),
            )
        observed_state = self._observe_existing_target(
            context,
            latest_version=latest_version,
        )
        if observed_state is not None:
            state, detail, deployment_ready = observed_state
            return BootstrapFoundryTargetRecord(
                repo_agent_id=context.repo_agent_id,
                root=context.root,
                reviewed_target=ReviewedFoundryTarget(
                    state=state,
                    project_endpoint=context.project_endpoint.value,
                    project_endpoint_source=context.project_endpoint.source,
                    agent_name=context.agent_name.value,
                    agent_name_source=context.agent_name.source,
                    account_resource_id=account_resource_id,
                    latest_agent_version=latest_version,
                    deployment_ready=deployment_ready,
                    detail=detail,
                ),
            )
        return BootstrapFoundryTargetRecord(
            repo_agent_id=context.repo_agent_id,
            root=context.root,
            reviewed_target=ReviewedFoundryTarget(
                state="existing_unknown",
                project_endpoint=context.project_endpoint.value,
                project_endpoint_source=context.project_endpoint.source,
                agent_name=context.agent_name.value,
                agent_name_source=context.agent_name.source,
                account_resource_id=account_resource_id,
                latest_agent_version=latest_version,
                deployment_ready=False,
                detail="project access succeeded, but local alignment could not be proven",
            ),
        )

    def _observe_existing_target(
        self,
        context: _LocalTargetContext,
        *,
        latest_version: str,
    ) -> tuple[str, str, bool] | None:
        assert context.project_endpoint is not None
        assert context.agent_name is not None
        try:
            observation = self._foundry_inventory.observe_agent(
                context.project_endpoint.value,
                agent_name=context.agent_name.value,
                agent_version=latest_version,
                source_root=context.source_root,
                package_root=context.package_root,
            )
        except Exception:
            observation = None
        if observation is not None:
            mismatches: list[str] = []
            if str(observation.get("source_fingerprint") or "") != context.source_fingerprint:
                mismatches.append("source-fingerprint")
            if str(observation.get("package_fingerprint") or "") != context.package_fingerprint:
                mismatches.append("package-fingerprint")
            if not mismatches:
                return (
                    "existing_aligned",
                    f"latest immutable version {latest_version} matches local fingerprints",
                    True,
                )
            return (
                "existing_diverged",
                f"latest immutable version {latest_version} diverges from local {', '.join(mismatches)}",
                True,
            )
        binding = self._binding_evidence_for(context.repo_agent_id, context.root)
        if binding is None:
            return None
        binding_endpoint = binding.get("project_endpoint")
        binding_name = binding.get("agent_name")
        binding_version = binding.get("agent_version") or binding.get("expected_version") or binding.get("version")
        if (
            binding_endpoint != context.project_endpoint.value
            or binding_name != context.agent_name.value
        ):
            return (
                "existing_diverged",
                "reviewed binding evidence points at a different endpoint or agent name",
                False,
            )
        if isinstance(binding_version, str) and binding_version and binding_version != latest_version:
            return (
                "existing_diverged",
                f"reviewed binding evidence observed version {binding_version}, but latest immutable version is {latest_version}",
                False,
            )
        source_fingerprint = binding.get("source_fingerprint")
        package_fingerprint = binding.get("package_fingerprint")
        if isinstance(source_fingerprint, str) and isinstance(package_fingerprint, str):
            mismatches: list[str] = []
            if source_fingerprint != context.source_fingerprint:
                mismatches.append("source-fingerprint")
            if package_fingerprint != context.package_fingerprint:
                mismatches.append("package-fingerprint")
            if not mismatches:
                return (
                    "existing_aligned",
                    f"reviewed binding evidence for {latest_version} matches local fingerprints",
                    True,
                )
            return (
                "existing_diverged",
                f"reviewed binding evidence for {latest_version} diverges from local {', '.join(mismatches)}",
                False,
            )
        return None

    def _local_context(
        self,
        operation,
        *,
        repo_agent_id: str,
        owner_overrides: Mapping[str, str] | None = None,
    ) -> _LocalTargetContext | None:
        discovered = next(
            (
                item
                for item in operation.selection_plan.discovered_agents
                if item.repo_agent_id.casefold() == repo_agent_id.casefold()
            ),
            None,
        )
        if discovered is None:
            return None
        repository_root = Path(operation.repository_binding.repository_root)
        root_path = repository_root if discovered.root == "." else repository_root / discovered.root
        seeds: list[_TargetSeed] = []
        blocked_detail: str | None = None
        owner_seed, owner_blocked = self._seed_from_owner_answer(
            owner_overrides or {}
        )
        if owner_blocked is not None:
            blocked_detail = owner_blocked
        for seed in (
            self._seed_from_existing_profile(
                repository_root,
                root_path=root_path,
                config_path=discovered.config_path,
                repo_agent_id=discovered.repo_agent_id,
            ),
            self._seed_from_agent_metadata(root_path),
            self._seed_from_azure_values(repository_root, selected_count=len(operation.selection_plan.selected_agent_ids)),
            self._seed_from_binding_evidence(discovered.repo_agent_id, discovered.root),
        ):
            candidate, blocked = seed
            if blocked is not None:
                blocked_detail = blocked
                break
            if candidate is not None:
                seeds.append(candidate)
        if owner_seed is not None:
            seeds.insert(0, owner_seed)
        project_endpoint = next((item.project_endpoint for item in seeds if item.project_endpoint is not None), None)
        agent_name = next((item.agent_name for item in seeds if item.agent_name is not None), None)
        account_resource_id = next(
            (
                item.account_resource_id
                for item in seeds
                if item.account_resource_id is not None
            ),
            None,
        )
        if project_endpoint is not None and account_resource_id is not None:
            try:
                _validate_account_resource_id(
                    account_resource_id.value,
                    project_endpoint=project_endpoint.value,
                )
            except BootstrapConfigError:
                account_resource_id = None
        expected_version = next((item.expected_version for item in seeds if item.expected_version), None)
        if (
            owner_seed is not None
            and owner_seed.project_endpoint is not None
            and owner_seed.agent_name is not None
        ):
            blocked_detail = None
        return _LocalTargetContext(
            repo_agent_id=discovered.repo_agent_id,
            root=discovered.root,
            source_root=discovered.source_root,
            package_root=discovered.package_root,
            source_fingerprint=discovered.source_fingerprint,
            package_fingerprint=discovered.package_fingerprint,
            project_endpoint=project_endpoint,
            agent_name=agent_name,
            account_resource_id=account_resource_id,
            expected_version=expected_version,
            blocked_detail=blocked_detail,
        )

    def _seed_from_existing_profile(
        self,
        repository_root: Path,
        *,
        root_path: Path,
        config_path: str | None,
        repo_agent_id: str,
    ) -> tuple[_TargetSeed | None, str | None]:
        path = (
            repository_root.joinpath(*PurePosixPath(config_path).parts)
            if (
                config_path is not None
                and PurePosixPath(config_path).name == "foundry-opt.yaml"
            )
            else root_path / ".foundry" / "foundry-opt.yaml"
        )
        if not path.exists():
            return None, None
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if not isinstance(raw, Mapping):
                raise BootstrapConfigError("existing v2 profile must contain a mapping")
            if raw.get("schema_version") != 2:
                return None, None
            document = BootstrapSidecar.from_document(raw)
        except Exception as exc:
            return None, f"{path.relative_to(repository_root).as_posix()} is not a valid existing v2 profile: {exc}"
        if document.repo_agent_id != repo_agent_id:
            return None, (
                f"{path.relative_to(repository_root).as_posix()} repo_agent_id "
                f"does not match {repo_agent_id!r}"
            )
        detail = path.relative_to(repository_root).as_posix()
        project = document.foundry_project
        return (
            _TargetSeed(
                detail=detail,
                project_endpoint=_FieldValue(
                    value=project.project_endpoint,
                    source="existing_profile",
                    detail=detail,
                ),
                agent_name=_FieldValue(
                    value=project.agent_name,
                    source="existing_profile",
                    detail=detail,
                ),
                account_resource_id=_FieldValue(
                    value=project.account_resource_id,
                    source="existing_profile",
                    detail=detail,
                ),
                expected_version=project.expected_version,
            ),
            None,
        )

    def _seed_from_agent_metadata(self, root_path: Path) -> tuple[_TargetSeed | None, str | None]:
        metadata_dir = root_path / ".foundry"
        if not metadata_dir.is_dir():
            return None, None
        files = sorted(
            path
            for path in metadata_dir.iterdir()
            if path.is_file()
            and path.suffix.casefold() in {".yaml", ".yml"}
            and path.name.casefold().startswith("agent-metadata")
        )
        if not files:
            return None, None
        endpoints: set[str] = set()
        names: set[str] = set()
        account_resource_ids: set[str] = set()
        versions: set[str] = set()
        for path in files:
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
                if not isinstance(payload, Mapping):
                    raise BootstrapConfigError("agent metadata must contain a mapping")
            except Exception as exc:
                return None, f"{path.relative_to(root_path).as_posix()} is invalid: {exc}"
            endpoint = payload.get("project_endpoint")
            if endpoint is not None:
                try:
                    endpoints.add(_validate_project_endpoint(str(endpoint)))
                except Exception as exc:
                    return None, f"{path.relative_to(root_path).as_posix()} has an invalid project_endpoint: {exc}"
            name = payload.get("agent_name")
            if name is not None:
                try:
                    names.add(_validate_agent_name(str(name)))
                except Exception as exc:
                    return None, f"{path.relative_to(root_path).as_posix()} has an invalid agent_name: {exc}"
            account_resource_id = payload.get(
                "foundry_account_resource_id",
                payload.get("account_resource_id"),
            )
            if account_resource_id is not None:
                try:
                    account_resource_ids.add(
                        _validate_account_resource_id(
                            str(account_resource_id),
                            project_endpoint=str(endpoint) if endpoint is not None else None,
                        )
                    )
                except Exception as exc:
                    return None, (
                        f"{path.relative_to(root_path).as_posix()} has an invalid "
                        f"foundry_account_resource_id: {exc}"
                    )
            version = payload.get("expected_version")
            if version is not None:
                versions.add(str(version))
        if len(endpoints) > 1:
            return None, "agent metadata declares conflicting project_endpoint values"
        if len(names) > 1:
            return None, "agent metadata declares conflicting agent_name values"
        if len(account_resource_ids) > 1:
            return None, "agent metadata declares conflicting foundry_account_resource_id values"
        if len(versions) > 1:
            return None, "agent metadata declares conflicting expected_version values"
        detail = ", ".join(path.relative_to(root_path).as_posix() for path in files)
        return (
            _TargetSeed(
                detail=detail,
                project_endpoint=(
                    _FieldValue(next(iter(endpoints)), "agent_metadata", detail)
                    if endpoints
                    else None
                ),
                agent_name=(
                    _FieldValue(next(iter(names)), "agent_metadata", detail)
                    if names
                    else None
                ),
                account_resource_id=(
                    _FieldValue(
                        next(iter(account_resource_ids)),
                        "agent_metadata",
                        detail,
                    )
                    if account_resource_ids
                    else None
                ),
                expected_version=next(iter(versions)) if versions else None,
            ),
            None,
        )

    def _seed_from_azure_values(
        self,
        repository_root: Path,
        *,
        selected_count: int,
    ) -> tuple[_TargetSeed | None, str | None]:
        if selected_count != 1:
            return None, None
        endpoint_values: set[str] = set()
        name_values: set[str] = set()
        account_resource_id_values: set[str] = set()
        azure_yaml = repository_root / "azure.yaml"
        if azure_yaml.is_file():
            try:
                payload = yaml.safe_load(azure_yaml.read_text(encoding="utf-8")) or {}
                if isinstance(payload, Mapping):
                    self._collect_named_values(
                        payload,
                        endpoint_values,
                        name_values,
                        account_resource_id_values,
                    )
            except Exception:
                return None, None
        azure_dir = repository_root / ".azure"
        if azure_dir.is_dir():
            for path in sorted(azure_dir.rglob("*.env")):
                if not path.is_file():
                    continue
                try:
                    for line in path.read_text(encoding="utf-8").splitlines():
                        if "=" not in line:
                            continue
                        key, value = line.split("=", 1)
                        if key == "AZURE_AI_PROJECT_ENDPOINT":
                            endpoint_values.add(value.strip())
                        elif key == "AZURE_AI_AGENT_NAME":
                            name_values.add(value.strip())
                        elif key in {
                            "AZURE_AI_FOUNDRY_ACCOUNT_RESOURCE_ID",
                            "AZURE_FOUNDRY_ACCOUNT_RESOURCE_ID",
                        }:
                            account_resource_id_values.add(value.strip())
                except OSError:
                    return None, None
        endpoint = None
        agent_name = None
        account_resource_id = None
        if len(endpoint_values) == 1:
            try:
                endpoint = _validate_project_endpoint(next(iter(endpoint_values)))
            except Exception:
                endpoint = None
        if len(name_values) == 1:
            try:
                agent_name = _validate_agent_name(next(iter(name_values)))
            except Exception:
                agent_name = None
        if len(account_resource_id_values) == 1:
            try:
                account_resource_id = _validate_account_resource_id(
                    next(iter(account_resource_id_values)),
                    project_endpoint=endpoint,
                )
            except Exception:
                account_resource_id = None
        if endpoint is None and agent_name is None and account_resource_id is None:
            return None, None
        detail = "azure.yaml/azd values"
        return (
            _TargetSeed(
                detail=detail,
                project_endpoint=(
                    _FieldValue(endpoint, "azure_yaml", detail)
                    if endpoint is not None
                    else None
                ),
                agent_name=(
                    _FieldValue(agent_name, "azd_environment", detail)
                    if agent_name is not None
                    else None
                ),
                account_resource_id=(
                    _FieldValue(
                        account_resource_id,
                        "azd_environment",
                        detail,
                    )
                    if account_resource_id is not None
                    else None
                ),
            ),
            None,
        )

    def _seed_from_binding_evidence(
        self,
        repo_agent_id: str,
        root: str,
    ) -> tuple[_TargetSeed | None, str | None]:
        payload = self._binding_evidence_for(repo_agent_id, root)
        if payload is None:
            return None, None
        detail = "reviewed binding evidence"
        endpoint = payload.get("project_endpoint")
        agent_name = payload.get("agent_name")
        account_resource_id = payload.get(
            "account_resource_id",
            payload.get("foundry_account_resource_id"),
        )
        version = payload.get("agent_version") or payload.get("expected_version") or payload.get("version")
        try:
            normalized_endpoint = (
                _validate_project_endpoint(str(endpoint))
                if endpoint is not None
                else None
            )
        except Exception as exc:
            return None, f"reviewed binding evidence has an invalid project_endpoint: {exc}"
        try:
            normalized_name = (
                _validate_agent_name(str(agent_name))
                if agent_name is not None
                else None
            )
        except Exception as exc:
            return None, f"reviewed binding evidence has an invalid agent_name: {exc}"
        try:
            normalized_account_resource_id = (
                _validate_account_resource_id(
                    str(account_resource_id),
                    project_endpoint=normalized_endpoint,
                )
                if account_resource_id is not None
                else None
            )
        except Exception as exc:
            return None, (
                "reviewed binding evidence has an invalid account_resource_id: "
                f"{exc}"
            )
        return (
            _TargetSeed(
                detail=detail,
                project_endpoint=(
                    _FieldValue(normalized_endpoint, "binding_evidence", detail)
                    if normalized_endpoint is not None
                    else None
                ),
                agent_name=(
                    _FieldValue(normalized_name, "binding_evidence", detail)
                    if normalized_name is not None
                    else None
                ),
                account_resource_id=(
                    _FieldValue(
                        normalized_account_resource_id,
                        "binding_evidence",
                        detail,
                    )
                    if normalized_account_resource_id is not None
                    else None
                ),
                expected_version=str(version) if version is not None else None,
            ),
            None,
        )

    @staticmethod
    def _seed_from_owner_answer(
        owner_values: Mapping[str, str],
    ) -> tuple[_TargetSeed | None, str | None]:
        if not owner_values:
            return None, None
        detail = "owner answer"
        endpoint = owner_values.get("project_endpoint")
        agent_name = owner_values.get("agent_name")
        account_resource_id = owner_values.get("account_resource_id")
        return (
            _TargetSeed(
                detail=detail,
                project_endpoint=(
                    _FieldValue(endpoint, "owner_answer", detail)
                    if endpoint is not None
                    else None
                ),
                agent_name=(
                    _FieldValue(agent_name, "owner_answer", detail)
                    if agent_name is not None
                    else None
                ),
                account_resource_id=(
                    _FieldValue(
                        account_resource_id,
                        "owner_answer",
                        "coding-agent Azure lookup",
                    )
                    if account_resource_id is not None
                    else None
                ),
            ),
            None,
        )

    def _binding_evidence_for(
        self,
        repo_agent_id: str,
        root: str,
    ) -> Mapping[str, object] | None:
        by_agent = self._binding_evidence_by_agent.get(repo_agent_id.casefold())
        if by_agent is not None:
            return by_agent
        return self._binding_evidence_by_root.get(root.casefold())

    @staticmethod
    def _collect_named_values(
        payload: Mapping[str, object],
        endpoint_values: set[str],
        name_values: set[str],
        account_resource_id_values: set[str],
    ) -> None:
        for key, value in payload.items():
            lowered = str(key).casefold()
            if isinstance(value, Mapping):
                DefaultFoundryTargetResolutionHandler._collect_named_values(
                    value,
                    endpoint_values,
                    name_values,
                    account_resource_id_values,
                )
                continue
            if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
                for item in value:
                    if isinstance(item, Mapping):
                        DefaultFoundryTargetResolutionHandler._collect_named_values(
                            item,
                            endpoint_values,
                            name_values,
                            account_resource_id_values,
                        )
                continue
            if value is None:
                continue
            text = str(value).strip()
            if not text:
                continue
            if lowered in {"project_endpoint", "azure_ai_project_endpoint"}:
                endpoint_values.add(text)
            elif lowered in {"agent_name", "azure_ai_agent_name"}:
                name_values.add(text)
            elif lowered in {
                "foundry_account_resource_id",
                "azure_ai_foundry_account_resource_id",
                "azure_foundry_account_resource_id",
            }:
                account_resource_id_values.add(text)

    @staticmethod
    def _blocked_record(
        context: _LocalTargetContext,
        *,
        detail: str,
    ) -> BootstrapFoundryTargetRecord:
        return BootstrapFoundryTargetRecord(
            repo_agent_id=context.repo_agent_id,
            root=context.root,
            reviewed_target=ReviewedFoundryTarget(
                state="blocked",
                project_endpoint=(
                    context.project_endpoint.value
                    if context.project_endpoint is not None
                    else None
                ),
                project_endpoint_source=(
                    context.project_endpoint.source
                    if context.project_endpoint is not None
                    else None
                ),
                agent_name=(
                    context.agent_name.value
                    if context.agent_name is not None
                    else None
                ),
                agent_name_source=(
                    context.agent_name.source
                    if context.agent_name is not None
                    else None
                ),
                account_resource_id=(
                    context.account_resource_id.value
                    if context.account_resource_id is not None
                    else None
                ),
                deployment_ready=False,
                detail=detail,
            ),
        )


__all__ = [
    "DefaultFoundryTargetInventoryAdapter",
    "DefaultFoundryTargetResolutionHandler",
    "FoundryProjectInventory",
    "FoundryTargetInventoryAdapterProtocol",
    "normalized_foundry_target_key",
    "build_local_user_credential",
]
