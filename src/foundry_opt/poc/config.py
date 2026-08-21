from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Any, Final, Literal, Self, TypeVar
import math
from urllib.parse import urlsplit

import yaml
from pydantic import StringConstraints, ValidationError, field_validator, model_validator
from yaml.events import AliasEvent
from yaml.nodes import MappingNode

from foundry_opt.models import FrozenModel
from foundry_opt.optimize_job.safety import (
    UnsafeCheckpointContentError,
    assert_safe_persisted_document,
    assert_safe_persisted_string,
)
from foundry_opt.poc.issue import ISSUE_NAMED_CHECK_GUIDANCE
from foundry_opt.verification import VerificationCheckSpec, VerificationDatasetInput


Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
GitCommit = Annotated[
    str,
    StringConstraints(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$"),
]

MAX_DOCUMENT_BYTES: Final = 1024 * 1024
MAX_PATH_LENGTH: Final = 256
MAX_TEXT_LENGTH: Final = 512
_REPOSITORY_IDENTITY: Final = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_BUDGET: Final = re.compile(r"^baseline\+([1-9][0-9]*)$")
_PATH_CHARACTERS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
    "._-/"
)
_GLOB_META: Final = frozenset("*?[]{}!")
_GLOB_PATH_CHARACTERS: Final = _PATH_CHARACTERS | _GLOB_META


class POCConfigurationError(ValueError):
    """The optimize-job POC configuration is invalid or unsafe."""


class RepositoryPathError(POCConfigurationError):
    """A repository-relative path or pattern is unsafe."""


class IssueNarrowingError(POCConfigurationError):
    """An issue request widens repository policy."""


class IssueEvaluatorSyntaxError(POCConfigurationError):
    """An issue-supplied evaluator line is invalid."""


_TARGET_CHARS: Final = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:/-"
)
_EVALUATOR_LINE = re.compile(
    r"^(?P<evaluator_id>\S+?)(?:\s+weight=(?P<weight>[^\s]+))?$"
)
_REPO_AGENT_ID = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_BUILTIN_EVALUATOR_PREFIX = "azureai://built-in/evaluators/"
_VERSIONED_EVALUATOR = re.compile(
    r"^azureai://accounts/[^/]+/projects/[^/]+/evaluators/[^/]+/versions/[^/]+$"
)
_REGISTRY_EVALUATOR = re.compile(
    r"^azureml://registries/[^/]+/evaluators/[^/]+/versions/[^/]+$"
)


class _StrictYamlLoader(yaml.SafeLoader):
    def compose_node(self, parent: Any, index: Any) -> Any:
        event = self.peek_event()
        if isinstance(event, AliasEvent) or getattr(event, "anchor", None):
            raise POCConfigurationError(
                "YAML aliases and anchors are not allowed"
            )
        return super().compose_node(parent, index)

    def construct_mapping(
        self,
        node: MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            if key_node.tag == "tag:yaml.org,2002:merge":
                raise POCConfigurationError("YAML merge keys are not allowed")
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str):
                raise POCConfigurationError("YAML mapping keys must be strings")
            if key in mapping:
                raise POCConfigurationError(f"duplicate YAML field: {key!r}")
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


ModelT = TypeVar("ModelT", bound=FrozenModel)


def load_strict_yaml_mapping(
    document: str | bytes | Mapping[str, object],
    *,
    subject: str,
) -> dict[str, object]:
    if isinstance(document, Mapping):
        return dict(document)
    raw = document.encode("utf-8") if isinstance(document, str) else document
    if len(raw) > MAX_DOCUMENT_BYTES:
        raise POCConfigurationError(f"{subject} exceeds the size limit")
    try:
        payload = yaml.load(raw.decode("utf-8"), Loader=_StrictYamlLoader)
    except POCConfigurationError:
        raise
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        raise POCConfigurationError(
            f"{subject} is not valid UTF-8 YAML or JSON"
        ) from exc
    if not isinstance(payload, Mapping):
        raise POCConfigurationError(f"{subject} must be a mapping")
    return dict(payload)


def load_strict_yaml_file(path: Path | str, *, subject: str) -> dict[str, object]:
    target = Path(path)
    try:
        content = target.read_bytes()
    except OSError as exc:
        raise POCConfigurationError(f"{subject} could not be read: {target}") from exc
    return load_strict_yaml_mapping(content, subject=subject)


def validate_repository_relative_path(
    value: object,
    *,
    field: str = "repository path",
    allow_glob: bool = False,
) -> str:
    if not isinstance(value, str):
        raise RepositoryPathError(f"{field} must be a string")
    if value in {"", ".", ".."}:
        raise RepositoryPathError(f"{field} must not be empty or dot-like")
    try:
        assert_safe_persisted_string(value, field=field, limit=MAX_PATH_LENGTH)
    except UnsafeCheckpointContentError as exc:
        raise RepositoryPathError(str(exc)) from exc
    if any(character.isspace() for character in value):
        raise RepositoryPathError(f"{field} must not contain whitespace")
    if value.startswith("/") or value.endswith("/"):
        raise RepositoryPathError(f"{field} must be repository-relative: {value!r}")
    if "\\" in value or ":" in value:
        raise RepositoryPathError(
            f"{field} must use forward-slash repository-relative form: {value!r}"
        )
    allowed = _GLOB_PATH_CHARACTERS if allow_glob else _PATH_CHARACTERS
    if any(character not in allowed for character in value):
        raise RepositoryPathError(
            f"{field} contains unsupported characters: {value!r}"
        )
    segments = value.split("/")
    if any(segment in {"", ".", ".."} for segment in segments):
        raise RepositoryPathError(f"{field} contains an unsafe segment: {value!r}")
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if posix.is_absolute() or windows.is_absolute() or windows.drive:
        raise RepositoryPathError(f"{field} must be relative: {value!r}")
    if not allow_glob and any(
        character in _GLOB_META for character in value
    ):
        raise RepositoryPathError(f"{field} must not contain glob syntax: {value!r}")
    return posix.as_posix()


def validate_repository_relative_paths(
    values: object,
    *,
    field: str,
    allow_glob: bool = False,
    allow_empty: bool = False,
) -> tuple[str, ...]:
    if values is None and allow_empty:
        return ()
    if isinstance(values, (str, bytes)) or not isinstance(values, Sequence):
        raise RepositoryPathError(f"{field} must be a list")
    validated: list[str] = []
    seen: dict[str, str] = {}
    for index, value in enumerate(values):
        path = validate_repository_relative_path(
            value,
            field=f"{field}[{index}]",
            allow_glob=allow_glob,
        )
        key = unicodedata.normalize("NFC", path).casefold()
        previous = seen.get(key)
        if previous is not None:
            raise RepositoryPathError(
                f"{field} contains a case-fold duplicate: {previous!r}, {path!r}"
            )
        seen[key] = path
        validated.append(path)
    if not validated and not allow_empty:
        raise RepositoryPathError(f"{field} must not be empty")
    return tuple(validated)


def _validate_package_path(value: object, *, field: str) -> str:
    if value == ".":
        try:
            assert_safe_persisted_string(".", field=field, limit=MAX_PATH_LENGTH)
        except UnsafeCheckpointContentError as exc:
            raise RepositoryPathError(str(exc)) from exc
        return "."
    return validate_repository_relative_path(value, field=field)


def _compact_text(value: object, field: str, *, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field} must not be empty")
    try:
        assert_safe_persisted_string(value, field=field, limit=limit)
    except UnsafeCheckpointContentError as exc:
        raise ValueError(str(exc)) from exc
    if any(character.isspace() for character in value):
        raise ValueError(f"{field} must not contain whitespace")
    return value


def _free_text(value: object, field: str, *, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string")
    if not value or not value.strip():
        raise ValueError(f"{field} must not be empty")
    try:
        assert_safe_persisted_string(value, field=field, limit=limit)
    except UnsafeCheckpointContentError as exc:
        raise ValueError(str(exc)) from exc
    return value


def _string_sequence(
    value: object,
    field: str,
    *,
    compact: bool,
    allow_empty: bool = False,
    limit: int = MAX_TEXT_LENGTH,
) -> tuple[str, ...]:
    if value is None and allow_empty:
        return ()
    items: Sequence[object]
    if isinstance(value, str):
        stripped = value.strip()
        if allow_empty and not stripped:
            return ()
        if "\n" in value:
            items = tuple(
                line.strip() for line in value.splitlines() if line.strip()
            )
        else:
            items = (stripped,)
    elif isinstance(value, (bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    else:
        items = value
    validated: list[str] = []
    seen: dict[str, str] = {}
    for index, item in enumerate(items):
        text = (
            _compact_text(item, f"{field}[{index}]", limit=limit)
            if compact
            else _free_text(item, f"{field}[{index}]", limit=limit)
        )
        key = unicodedata.normalize("NFC", text).casefold()
        previous = seen.get(key)
        if previous is not None:
            raise ValueError(
                f"{field} contains a case-fold duplicate: {previous!r}, {text!r}"
            )
        seen[key] = text
        validated.append(text)
    if not validated and not allow_empty:
        raise ValueError(f"{field} must not be empty")
    return tuple(validated)


def _sequence_items(value: object, field: str) -> tuple[object, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return ()
        if "\n" in value:
            return tuple(line.strip() for line in value.splitlines() if line.strip())
        return (stripped,)
    if isinstance(value, (bytes, bytearray)) or not isinstance(value, Sequence):
        raise ValueError(f"{field} must be a list")
    return tuple(value)


def _strict_int(value: object, field: str) -> int:
    if type(value) is not int:
        raise ValueError(f"{field} must be an integer")
    return value


def _strict_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{field} must be a boolean")
    return value


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a number")
    number = float(value)
    if number != number or number in {float("inf"), float("-inf")}:
        raise ValueError(f"{field} must be finite")
    return number


def _validate_https_url(
    value: object,
    *,
    field: str,
    github_repository: bool = False,
) -> str:
    text = _compact_text(value, field)
    parsed = urlsplit(text)
    if parsed.scheme != "https":
        raise ValueError(f"{field} must use HTTPS")
    if (
        not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{field} must be a plain HTTPS URL")
    if github_repository:
        if parsed.hostname != "github.com" or parsed.port is not None:
            raise ValueError(f"{field} must point at github.com")
        segments = [segment for segment in parsed.path.split("/") if segment]
        if len(segments) != 2:
            raise ValueError(f"{field} must identify one GitHub repository")
        repository = segments[1][:-4] if segments[1].endswith(".git") else segments[1]
        if (
            _REPOSITORY_IDENTITY.fullmatch(f"{segments[0]}/{repository}")
            is None
        ):
            raise ValueError(f"{field} must identify one GitHub repository")
    return text


def _validate_repository_identity(value: object, field: str) -> str:
    text = _compact_text(value, field, limit=255)
    if _REPOSITORY_IDENTITY.fullmatch(text) is None:
        raise ValueError(f"{field} must use owner/name form")
    return text


def _validate_resource_id(value: object, field: str) -> str:
    text = _compact_text(value, field)
    if not text.startswith("/subscriptions/"):
        raise ValueError(f"{field} must be an ARM resource ID")
    return text


def _require_mapping(value: object, field: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise POCConfigurationError(f"{field} must be a mapping")
    return value


def _require_sequence(value: object, field: str) -> Sequence[object]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list")
    return value


def _format_validation_error(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors(include_url=False):
        location = ".".join(str(part) for part in item["loc"]) or "<root>"
        parts.append(f"{location}: {item['msg']}")
    return "; ".join(parts)


def _validate_model(
    model: type[ModelT],
    payload: Mapping[str, object],
    *,
    subject: str,
) -> ModelT:
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise POCConfigurationError(
            f"{subject} is invalid: {_format_validation_error(exc)}"
        ) from exc


def _require_schema_version(payload: Mapping[str, object], *, subject: str) -> int:
    value = payload.get("schema_version")
    if type(value) is not int or value != 1:
        raise POCConfigurationError(f"{subject} must declare schema_version: 1")
    return value


def _normalize_guardrails(value: object, field: str) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        items: list[dict[str, object]] = []
        for raw_name, raw_spec in value.items():
            name = _compact_text(raw_name, f"{field} key", limit=128)
            spec = dict(_require_mapping(raw_spec, f"{field}.{name}"))
            if spec.get("required") is False:
                continue
            spec.pop("required", None)
            spec["metric"] = name
            items.append(spec)
        return items
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list or mapping")
    return [dict(_require_mapping(item, f"{field} item")) for item in value]


def _normalize_runtime_payload(value: object, field: str) -> dict[str, object]:
    runtime = dict(_require_mapping(value, field))
    if "protocol_name" in runtime and "protocol_version" in runtime:
        return runtime
    protocol = _require_mapping(runtime.get("protocol"), f"{field}.protocol")
    container = _require_mapping(runtime.get("container"), f"{field}.container")
    resources = _require_mapping(
        container.get("resources"),
        f"{field}.container.resources",
    )
    return {
        "kind": runtime.get("kind"),
        "runtime": runtime.get("runtime"),
        "entry_point": runtime.get("entry_point"),
        "dependency_resolution": runtime.get("dependency_resolution"),
        "protocol_name": protocol.get("name"),
        "protocol_version": protocol.get("version"),
        "cpu": resources.get("cpu"),
        "memory": resources.get("memory"),
        "model_environment_variable": runtime.get("model_environment_variable"),
    }


def _normalize_capabilities(value: object, field: str) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        items: list[dict[str, object]] = []
        for raw_name, raw_enabled in value.items():
            items.append(
                {
                    "name": _compact_text(raw_name, f"{field} key", limit=128),
                    "enabled": raw_enabled,
                }
            )
        return items
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list or mapping")
    return [dict(_require_mapping(item, f"{field} item")) for item in value]


def _normalize_model_deployments(
    value: object,
    field: str,
) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        items: list[dict[str, object]] = []
        for raw_alias, raw_spec in value.items():
            alias = _compact_text(raw_alias, f"{field} key", limit=128)
            spec = dict(_require_mapping(raw_spec, f"{field}.{alias}"))
            model = _require_mapping(spec.get("model"), f"{field}.{alias}.model")
            items.append(
                {
                    "alias": alias,
                    "deployment_name": spec.get("deployment_name"),
                    "model_format": model.get("format"),
                    "model_name": model.get("name"),
                    "model_version": model.get("version"),
                    "required_capabilities": _normalize_capabilities(
                        spec.get("required_capabilities"),
                        f"{field}.{alias}.required_capabilities",
                    ),
                }
            )
        return items
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list or mapping")
    return [dict(_require_mapping(item, f"{field} item")) for item in value]


def _normalize_subjects(value: object, field: str) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        items: list[dict[str, object]] = []
        for raw_name, raw_spec in value.items():
            item = dict(_require_mapping(raw_spec, f"{field}.{raw_name}"))
            item["name"] = _compact_text(raw_name, f"{field} key", limit=128)
            items.append(item)
        return items
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list or mapping")
    return [dict(_require_mapping(item, f"{field} item")) for item in value]


def _normalize_principals(value: object, field: str) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        items: list[dict[str, object]] = []
        for raw_role, raw_spec in value.items():
            item = dict(_require_mapping(raw_spec, f"{field}.{raw_role}"))
            item["role"] = _compact_text(raw_role, f"{field} key", limit=128)
            if "subjects" in item:
                item["subjects"] = _normalize_subjects(
                    item["subjects"],
                    f"{field}.{raw_role}.subjects",
                )
            items.append(item)
        return items
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list or mapping")
    return [dict(_require_mapping(item, f"{field} item")) for item in value]


def _normalize_workflow_variables(
    value: object,
    field: str,
) -> list[dict[str, object]]:
    if isinstance(value, Mapping):
        items: list[dict[str, object]] = []
        for raw_alias, raw_spec in value.items():
            item = dict(_require_mapping(raw_spec, f"{field}.{raw_alias}"))
            item["alias"] = _compact_text(raw_alias, f"{field} key", limit=128)
            items.append(item)
        return items
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise POCConfigurationError(f"{field} must be a list or mapping")
    return [dict(_require_mapping(item, f"{field} item")) for item in value]


def _normalize_oidc_payload(value: object, field: str) -> dict[str, object]:
    oidc = dict(_require_mapping(value, field))
    oidc["workflow_variables"] = _normalize_workflow_variables(
        oidc.get("workflow_variables", ()),
        f"{field}.workflow_variables",
    )
    oidc["principals"] = _normalize_principals(
        oidc.get("principals", ()),
        f"{field}.principals",
    )
    return oidc


def _normalize_evaluation_contract(
    value: object,
    *,
    field: str,
    name: str,
) -> dict[str, object]:
    evaluation = dict(_require_mapping(value, field))
    return {
        "name": name,
        "split": evaluation.get("split"),
        "resolved_evaluation_id": evaluation.get("resolved_evaluation_id"),
        "dataset_id": evaluation.get("dataset_id"),
        "custom_evaluator_ids": evaluation.get("custom_evaluator_ids", ()),
    }


def _infer_source_root(editable_paths: Sequence[object]) -> str:
    validated = validate_repository_relative_paths(
        editable_paths,
        field="editable_paths",
        allow_glob=True,
    )
    for path in validated:
        root = path.split("/", 1)[0]
        if root != "tests":
            return root
    raise POCConfigurationError("editable_paths do not identify a source root")


class SharedPin(FrozenModel):
    schema_version: Literal[1] = 1
    repository_url: str
    commit: GitCommit
    package_path: str
    skill_path: str
    uv_lock_sha256: Sha256

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        return _validate_https_url(
            value,
            field="repository_url",
            github_repository=True,
        )

    @field_validator("package_path")
    @classmethod
    def validate_package_path(cls, value: str) -> str:
        return _validate_package_path(value, field="package_path")

    @field_validator("skill_path")
    @classmethod
    def validate_skill_path(cls, value: str) -> str:
        return validate_repository_relative_path(value, field="skill_path")

    @model_validator(mode="after")
    def validate_safe_document(self) -> Self:
        assert_safe_persisted_document(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        payload = load_strict_yaml_mapping(document, subject="shared pin")
        if "repository" in payload and "repository_url" not in payload:
            payload["repository_url"] = payload.pop("repository")
        return _validate_model(cls, payload, subject="shared pin")


class DecisionRules(FrozenModel):
    minimum_aggregate_delta: float
    focused_cases_required: bool
    max_regressions: int

    @field_validator("minimum_aggregate_delta", mode="before")
    @classmethod
    def validate_delta(cls, value: object) -> float:
        number = _finite_float(value, "minimum_aggregate_delta")
        if number <= 0:
            raise ValueError("minimum_aggregate_delta must be greater than zero")
        return number

    @field_validator("focused_cases_required", mode="before")
    @classmethod
    def validate_focused_cases(cls, value: object) -> bool:
        return _strict_bool(value, "focused_cases_required")

    @field_validator("max_regressions", mode="before")
    @classmethod
    def validate_regressions(cls, value: object) -> int:
        count = _strict_int(value, "max_regressions")
        if count < 0:
            raise ValueError("max_regressions must be non-negative")
        return count


class GuardrailSpec(FrozenModel):
    metric: str
    required_pass_rate: float

    @field_validator("metric")
    @classmethod
    def validate_metric(cls, value: str) -> str:
        return _compact_text(value, "metric", limit=128)

    @field_validator("required_pass_rate", mode="before")
    @classmethod
    def validate_pass_rate(cls, value: object) -> float:
        number = _finite_float(value, "required_pass_rate")
        if number < 0 or number > 1:
            raise ValueError("required_pass_rate must be between 0 and 1")
        return number


class RepositoryPolicy(FrozenModel):
    schema_version: Literal[1] = 1
    source_root: str
    editable_paths: tuple[str, ...]
    min_candidates: int
    max_candidates: int
    baseline_model: str
    allowed_models: tuple[str, ...]
    primary_metric: str
    decision_rules: DecisionRules
    hard_guardrails: tuple[GuardrailSpec, ...]
    metadata_path: str

    @field_validator("source_root", "metadata_path")
    @classmethod
    def validate_path_fields(cls, value: str, info: Any) -> str:
        return validate_repository_relative_path(value, field=info.field_name)

    @field_validator("editable_paths")
    @classmethod
    def validate_editable_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return validate_repository_relative_paths(
            value,
            field="editable_paths",
            allow_glob=True,
        )

    @field_validator("min_candidates", "max_candidates", mode="before")
    @classmethod
    def validate_candidate_counts(cls, value: object, info: Any) -> int:
        count = _strict_int(value, info.field_name)
        if count < 1:
            raise ValueError(f"{info.field_name} must be at least one")
        return count

    @field_validator("baseline_model", "primary_metric")
    @classmethod
    def validate_policy_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=128)

    @field_validator("allowed_models")
    @classmethod
    def validate_allowed_models(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _string_sequence(
            value,
            "allowed_models",
            compact=True,
            limit=128,
        )

    @field_validator("hard_guardrails")
    @classmethod
    def validate_guardrails(
        cls,
        value: tuple[GuardrailSpec, ...],
    ) -> tuple[GuardrailSpec, ...]:
        if not value:
            raise ValueError("hard_guardrails must not be empty")
        return value

    @model_validator(mode="after")
    def validate_policy(self) -> Self:
        if self.min_candidates > self.max_candidates:
            raise ValueError("min_candidates cannot exceed max_candidates")
        guardrail_names = [guardrail.metric.casefold() for guardrail in self.hard_guardrails]
        if len(guardrail_names) != len(set(guardrail_names)):
            raise ValueError("hard_guardrails must be unique by metric")
        assert_safe_persisted_document(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_document(
        cls,
        document: str | bytes | Mapping[str, object],
        *,
        metadata_document: str | bytes | Mapping[str, object] | None = None,
    ) -> Self:
        payload = load_strict_yaml_mapping(document, subject="repository policy")
        metadata_payload = (
            load_strict_yaml_mapping(metadata_document, subject="agent metadata")
            if metadata_document is not None
            else None
        )
        normalized = _normalize_repository_policy_payload(
            payload,
            metadata_payload=metadata_payload,
        )
        return _validate_model(cls, normalized, subject="repository policy")


def _normalize_repository_policy_payload(
    payload: Mapping[str, object],
    *,
    metadata_payload: Mapping[str, object] | None,
) -> dict[str, object]:
    schema_version = _require_schema_version(payload, subject="repository policy")
    if "source_root" in payload:
        normalized = dict(payload)
        normalized["schema_version"] = schema_version
        normalized["hard_guardrails"] = _normalize_guardrails(
            normalized.get("hard_guardrails", ()),
            "hard_guardrails",
        )
        return normalized
    if "repository_scope" in payload and "optimize_job" in payload:
        optimize_job = _require_mapping(payload.get("optimize_job"), "optimize_job")
        repository_scope = _require_mapping(
            payload.get("repository_scope"),
            "repository_scope",
        )
        primary_metric = _require_mapping(
            optimize_job.get("primary_metric"),
            "optimize_job.primary_metric",
        )
        return {
            "schema_version": schema_version,
            "source_root": _infer_source_root(
                _require_sequence(
                    repository_scope.get("allowed_editable_paths"),
                    "repository_scope.allowed_editable_paths",
                )
            ),
            "editable_paths": repository_scope.get("allowed_editable_paths"),
            "min_candidates": 1,
            "max_candidates": optimize_job.get("maximum_candidates"),
            "baseline_model": optimize_job.get("baseline_model"),
            "allowed_models": optimize_job.get("candidate_model_allowlist"),
            "primary_metric": primary_metric.get("name"),
            "decision_rules": {
                "minimum_aggregate_delta": primary_metric.get("minimum_improvement"),
                "focused_cases_required": False,
                "max_regressions": 0,
            },
            "hard_guardrails": _normalize_guardrails(
                optimize_job.get("hard_guardrails", ()),
                "optimize_job.hard_guardrails",
            ),
            "metadata_path": ".foundry/agent-metadata.yaml",
        }
    if "models" in payload and metadata_payload is not None:
        _require_schema_version(metadata_payload, subject="agent metadata")
        models = _require_mapping(payload.get("models"), "models")
        evaluation = _require_mapping(payload.get("evaluation"), "evaluation")
        hard_guardrail = _require_mapping(
            evaluation.get("hard_guardrail"),
            "evaluation.hard_guardrail",
        )
        delivery = _require_mapping(payload.get("delivery"), "delivery")
        repository_scope = _require_mapping(
            metadata_payload.get("repository_scope"),
            "agent metadata.repository_scope",
        )
        allowed_editable_paths = _require_sequence(
            repository_scope.get("allowed_editable_paths"),
            "agent metadata.repository_scope.allowed_editable_paths",
        )
        return {
            "schema_version": schema_version,
            "source_root": _infer_source_root(allowed_editable_paths),
            "editable_paths": allowed_editable_paths,
            "min_candidates": 1,
            "max_candidates": delivery.get("maximum_candidates"),
            "baseline_model": models.get("baseline"),
            "allowed_models": models.get("candidates"),
            "primary_metric": evaluation.get("primary_metric"),
            "decision_rules": {
                "minimum_aggregate_delta": evaluation.get("minimum_improvement"),
                "focused_cases_required": False,
                "max_regressions": 0,
            },
            "hard_guardrails": [
                {
                    "metric": hard_guardrail.get("metric"),
                    "required_pass_rate": hard_guardrail.get("required_pass_rate"),
                }
            ],
            "metadata_path": ".foundry/agent-metadata.yaml",
        }
    raise POCConfigurationError(
        "repository policy must be generic, agent-metadata style, or paired "
        "with agent metadata"
    )


class CapabilityRequirement(FrozenModel):
    name: str
    enabled: bool

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _compact_text(value, "name", limit=128)

    @field_validator("enabled", mode="before")
    @classmethod
    def validate_enabled(cls, value: object) -> bool:
        return _strict_bool(value, "enabled")


class ModelDeploymentContract(FrozenModel):
    alias: str
    deployment_name: str
    model_format: str
    model_name: str
    model_version: str
    required_capabilities: tuple[CapabilityRequirement, ...]

    @field_validator(
        "alias",
        "deployment_name",
        "model_format",
        "model_name",
        "model_version",
    )
    @classmethod
    def validate_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=256)

    @field_validator("required_capabilities")
    @classmethod
    def validate_capabilities(
        cls,
        value: tuple[CapabilityRequirement, ...],
    ) -> tuple[CapabilityRequirement, ...]:
        if not value:
            raise ValueError("required_capabilities must not be empty")
        names = [capability.name.casefold() for capability in value]
        if len(names) != len(set(names)):
            raise ValueError("required_capabilities must be unique by name")
        return value


class EvaluationContract(FrozenModel):
    name: str
    split: Literal["development", "validating"]
    resolved_evaluation_id: str
    dataset_id: str
    custom_evaluator_ids: tuple[str, ...] = ()

    @field_validator("name", "resolved_evaluation_id", "dataset_id")
    @classmethod
    def validate_evaluation_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=MAX_TEXT_LENGTH)

    @field_validator("custom_evaluator_ids")
    @classmethod
    def validate_custom_evaluators(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return _string_sequence(
            value,
            "custom_evaluator_ids",
            compact=True,
            allow_empty=True,
            limit=MAX_TEXT_LENGTH,
        )


class WorkflowVariableContract(FrozenModel):
    alias: str
    name: str
    value: str
    scope: Literal["repository", "environment"]
    environment: str | None = None

    @field_validator("alias", "name", "value")
    @classmethod
    def validate_variable_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=256)

    @field_validator("environment")
    @classmethod
    def validate_environment(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _compact_text(value, "environment", limit=128)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if self.scope == "environment" and self.environment is None:
            raise ValueError("environment scope requires an environment name")
        if self.scope == "repository" and self.environment is not None:
            raise ValueError("repository scope cannot name an environment")
        return self


class OidcSubjectContract(FrozenModel):
    name: str
    subject: str
    event_name: str | None = None
    ref: str | None = None
    ref_type: str | None = None
    environment: str | None = None

    @field_validator("name", "subject", "event_name", "ref", "ref_type", "environment")
    @classmethod
    def validate_subject_tokens(
        cls,
        value: str | None,
        info: Any,
    ) -> str | None:
        if value is None:
            return None
        return _compact_text(value, info.field_name, limit=MAX_TEXT_LENGTH)


class OidcPrincipalContract(FrozenModel):
    role: str
    client_id: str
    client_id_variable: str
    environment: str | None = None
    subject: str | None = None
    direct_oidc_subject: str | None = None
    subjects: tuple[OidcSubjectContract, ...] = ()

    @field_validator("role", "client_id", "client_id_variable", "environment", "subject", "direct_oidc_subject")
    @classmethod
    def validate_principal_tokens(
        cls,
        value: str | None,
        info: Any,
    ) -> str | None:
        if value is None:
            return None
        return _compact_text(value, info.field_name, limit=MAX_TEXT_LENGTH)

    @model_validator(mode="after")
    def validate_principal(self) -> Self:
        if (
            self.subject is None
            and self.direct_oidc_subject is None
            and not self.subjects
        ):
            raise ValueError("OIDC principal must declare at least one subject")
        names = [subject.name.casefold() for subject in self.subjects]
        if len(names) != len(set(names)):
            raise ValueError("OIDC principal subjects must be unique by name")
        return self


class OidcSettings(FrozenModel):
    issuer: str
    audience: str
    tenant_id: str
    subscription_id: str
    repository_id_claim: str
    workflow_variables: tuple[WorkflowVariableContract, ...]
    principals: tuple[OidcPrincipalContract, ...]

    @field_validator("issuer")
    @classmethod
    def validate_issuer(cls, value: str) -> str:
        return _validate_https_url(value, field="issuer")

    @field_validator("audience", "tenant_id", "subscription_id", "repository_id_claim")
    @classmethod
    def validate_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=256)

    @model_validator(mode="after")
    def validate_oidc(self) -> Self:
        if not self.workflow_variables:
            raise ValueError("workflow_variables must not be empty")
        if not self.principals:
            raise ValueError("principals must not be empty")
        aliases = [item.alias.casefold() for item in self.workflow_variables]
        if len(aliases) != len(set(aliases)):
            raise ValueError("workflow_variables must be unique by alias")
        roles = [item.role.casefold() for item in self.principals]
        if len(roles) != len(set(roles)):
            raise ValueError("principals must be unique by role")
        return self


class HostedRuntimeContract(FrozenModel):
    kind: Literal["hosted"]
    runtime: str
    entry_point: tuple[str, ...]
    dependency_resolution: str
    protocol_name: str
    protocol_version: str
    cpu: str
    memory: str
    model_environment_variable: str

    @field_validator(
        "runtime",
        "dependency_resolution",
        "protocol_name",
        "protocol_version",
        "memory",
        "model_environment_variable",
    )
    @classmethod
    def validate_runtime_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=256)

    @field_validator("cpu", mode="before")
    @classmethod
    def validate_cpu(cls, value: object) -> str:
        if isinstance(value, bool):
            raise ValueError("cpu must be text or a number")
        if isinstance(value, (int, float)):
            value = str(value)
        return _compact_text(value, "cpu", limit=64)

    @field_validator("entry_point")
    @classmethod
    def validate_entry_point(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _string_sequence(
            value,
            "entry_point",
            compact=True,
            limit=128,
        )


class AgentMetadata(FrozenModel):
    schema_version: Literal[1] = 1
    repository_identity: str
    repository_id: int
    default_branch: str
    project_endpoint: str
    foundry_account_resource_id: str
    agent_name: str
    authentication_method: Literal["oidc"] = "oidc"
    static_credentials_allowed: Literal[False] = False
    hosted_runtime: HostedRuntimeContract
    oidc: OidcSettings
    model_deployments: tuple[ModelDeploymentContract, ...]
    development_evaluation: EvaluationContract
    validating_evaluation: EvaluationContract

    @field_validator("repository_identity")
    @classmethod
    def validate_repository_identity(cls, value: str) -> str:
        return _validate_repository_identity(value, "repository_identity")

    @field_validator("repository_id", mode="before")
    @classmethod
    def validate_repository_id(cls, value: object) -> int:
        repository_id = _strict_int(value, "repository_id")
        if repository_id <= 0:
            raise ValueError("repository_id must be positive")
        return repository_id

    @field_validator("default_branch", "agent_name")
    @classmethod
    def validate_agent_tokens(cls, value: str, info: Any) -> str:
        return _compact_text(value, info.field_name, limit=128)

    @field_validator("project_endpoint")
    @classmethod
    def validate_project_endpoint(cls, value: str) -> str:
        return _validate_https_url(value, field="project_endpoint")

    @field_validator("foundry_account_resource_id")
    @classmethod
    def validate_account_resource_id(cls, value: str) -> str:
        return _validate_resource_id(value, "foundry_account_resource_id")

    @field_validator("model_deployments")
    @classmethod
    def validate_model_deployments(
        cls,
        value: tuple[ModelDeploymentContract, ...],
    ) -> tuple[ModelDeploymentContract, ...]:
        if not value:
            raise ValueError("model_deployments must not be empty")
        aliases = [item.alias.casefold() for item in value]
        if len(aliases) != len(set(aliases)):
            raise ValueError("model_deployments must be unique by alias")
        deployments = [item.deployment_name.casefold() for item in value]
        if len(deployments) != len(set(deployments)):
            raise ValueError("model_deployments must be unique by deployment_name")
        return value

    @model_validator(mode="after")
    def validate_metadata(self) -> Self:
        if self.oidc.repository_id_claim != str(self.repository_id):
            raise ValueError("oidc.repository_id_claim must match repository_id")
        aliases = {item.alias for item in self.oidc.workflow_variables}
        for principal in self.oidc.principals:
            if principal.client_id_variable not in aliases:
                raise ValueError(
                    "every OIDC principal must reference a declared workflow variable"
                )
        if self.development_evaluation.split != "development":
            raise ValueError("development_evaluation must use split=development")
        if self.validating_evaluation.split != "validating":
            raise ValueError("validating_evaluation must use split=validating")
        assert_safe_persisted_document(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        payload = load_strict_yaml_mapping(document, subject="agent metadata")
        normalized = _normalize_agent_metadata_payload(payload)
        return _validate_model(cls, normalized, subject="agent metadata")


def _normalize_agent_metadata_payload(payload: Mapping[str, object]) -> dict[str, object]:
    schema_version = _require_schema_version(payload, subject="agent metadata")
    if "repository_identity" in payload:
        normalized = dict(payload)
        normalized["schema_version"] = schema_version
        if "deployment" in normalized and "hosted_runtime" not in normalized:
            normalized["hosted_runtime"] = normalized.pop("deployment")
        if "hosted_runtime" in normalized:
            normalized["hosted_runtime"] = _normalize_runtime_payload(
                normalized["hosted_runtime"],
                "hosted_runtime",
            )
        if "oidc" in normalized:
            normalized["oidc"] = _normalize_oidc_payload(normalized["oidc"], "oidc")
        if "model_deployments" in normalized:
            normalized["model_deployments"] = _normalize_model_deployments(
                normalized["model_deployments"],
                "model_deployments",
            )
        return normalized
    repository = _require_mapping(payload.get("repository"), "repository")
    foundry = _require_mapping(payload.get("foundry"), "foundry")
    authentication = _require_mapping(
        payload.get("authentication"),
        "authentication",
    )
    identity_contracts = _require_mapping(
        payload.get("identity_contracts"),
        "identity_contracts",
    )
    azure_oidc = _require_mapping(
        identity_contracts.get("azure_oidc"),
        "identity_contracts.azure_oidc",
    )
    optimize_job = _require_mapping(payload.get("optimize_job"), "optimize_job")
    development_name = _compact_text(
        optimize_job.get("development_evaluation_definition"),
        "optimize_job.development_evaluation_definition",
        limit=128,
    )
    validating_name = _compact_text(
        optimize_job.get("validating_evaluation_definition"),
        "optimize_job.validating_evaluation_definition",
        limit=128,
    )
    evaluation = _require_mapping(payload.get("evaluation"), "evaluation")
    definitions = _require_mapping(
        evaluation.get("definitions"),
        "evaluation.definitions",
    )
    return {
        "schema_version": schema_version,
        "repository_identity": repository.get("identity"),
        "repository_id": repository.get("id"),
        "default_branch": repository.get("default_branch"),
        "project_endpoint": foundry.get("project_endpoint"),
        "foundry_account_resource_id": foundry.get("account_resource_id"),
        "agent_name": foundry.get("proposed_agent"),
        "authentication_method": authentication.get("method"),
        "static_credentials_allowed": authentication.get("static_credentials_allowed"),
        "hosted_runtime": _normalize_runtime_payload(
            payload.get("deployment"),
            "deployment",
        ),
        "oidc": _normalize_oidc_payload(
            azure_oidc,
            "identity_contracts.azure_oidc",
        ),
        "model_deployments": _normalize_model_deployments(
            payload.get("model_deployment_contracts"),
            "model_deployment_contracts",
        ),
        "development_evaluation": _normalize_evaluation_contract(
            definitions.get(development_name),
            field=f"evaluation.definitions.{development_name}",
            name=development_name,
        ),
        "validating_evaluation": _normalize_evaluation_contract(
            definitions.get(validating_name),
            field=f"evaluation.definitions.{validating_name}",
            name=validating_name,
        ),
    }


class OptimizeIssueRequest(FrozenModel):
    repo_agent_id: str | None = None
    explicit_target: str | None = None
    goal: str
    primary_metric: str | None = None
    observed_failures: tuple[str, ...]
    constraints: tuple[str, ...] = ()
    candidate_budget: int
    model_subset: tuple[str, ...] | None = None
    editable_scope_subset: tuple[str, ...] | None = None
    issue_evaluators: tuple["IssueEvaluatorEntry", ...] | None = None
    verification_dataset: VerificationDatasetInput | None = None
    verification_checks: tuple[VerificationCheckSpec, ...] | None = None
    acknowledge_no_evidence: bool = False

    @field_validator("goal")
    @classmethod
    def validate_goal(cls, value: str) -> str:
        return _free_text(value, "goal", limit=MAX_TEXT_LENGTH)

    @field_validator("primary_metric")
    @classmethod
    def validate_primary_metric(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _compact_text(value, "primary_metric", limit=128)

    @field_validator("repo_agent_id")
    @classmethod
    def validate_repo_agent_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _compact_text(value, "repo_agent_id", limit=128)
        if _REPO_AGENT_ID.fullmatch(normalized) is None:
            raise ValueError("repo_agent_id must be a stable repoAgentId token")
        return normalized

    @field_validator("explicit_target")
    @classmethod
    def validate_explicit_target(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = _free_text(value, "explicit_target", limit=512)
        if any(character not in _TARGET_CHARS for character in normalized):
            raise ValueError("explicit_target contains unsupported characters")
        return normalized

    @field_validator("observed_failures", mode="before")
    @classmethod
    def validate_failures(cls, value: object) -> tuple[str, ...]:
        return _string_sequence(
            value,
            "observed_failures",
            compact=False,
            limit=MAX_TEXT_LENGTH,
        )

    @field_validator("constraints", mode="before")
    @classmethod
    def validate_constraints(cls, value: object) -> tuple[str, ...]:
        return _string_sequence(
            value,
            "constraints",
            compact=False,
            allow_empty=True,
            limit=MAX_TEXT_LENGTH,
        )

    @field_validator("candidate_budget", mode="before")
    @classmethod
    def validate_budget(cls, value: object) -> int:
        if type(value) is int:
            budget = value
        elif isinstance(value, str):
            match = _BUDGET.fullmatch(value.strip())
            if match is None:
                raise ValueError("candidate_budget must be an integer or baseline+N")
            budget = int(match.group(1))
        else:
            raise ValueError("candidate_budget must be an integer or baseline+N")
        if budget < 1:
            raise ValueError("candidate_budget must be at least one")
        return budget

    @field_validator("model_subset", mode="before")
    @classmethod
    def validate_model_subset(
        cls,
        value: object,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if text.endswith(" only"):
                value = (text[:-5].strip(),)
            elif " and " in text:
                value = tuple(
                    part.strip() for part in text.split(" and ") if part.strip()
                )
            else:
                value = (text,)
        return _string_sequence(
            value,
            "model_subset",
            compact=True,
            limit=128,
        )

    @field_validator("editable_scope_subset", mode="before")
    @classmethod
    def validate_editable_scope_subset(
        cls,
        value: object,
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        return validate_repository_relative_paths(
            value,
            field="editable_scope_subset",
            allow_glob=True,
        )

    @field_validator("issue_evaluators", mode="before")
    @classmethod
    def validate_issue_evaluators(
        cls,
        value: object,
    ) -> tuple["IssueEvaluatorEntry", ...] | None:
        if value is None:
            return None
        items = _sequence_items(value, "issue_evaluators")
        if not items:
            return None
        parsed: list[IssueEvaluatorEntry] = []
        seen: set[str] = set()
        for index, item in enumerate(items):
            try:
                if isinstance(item, IssueEvaluatorEntry):
                    entry = item
                elif isinstance(item, str):
                    entry = IssueEvaluatorEntry.parse_line(item)
                elif isinstance(item, Mapping):
                    entry = IssueEvaluatorEntry.model_validate(item)
                else:
                    raise ValueError(
                        f"issue_evaluators[{index}] must be a string or object"
                    )
            except (IssueEvaluatorSyntaxError, ValidationError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            key = entry.evaluator_id.casefold()
            if key in seen:
                raise ValueError("issue_evaluators must not contain duplicate evaluator IDs")
            seen.add(key)
            parsed.append(entry)
        return tuple(parsed)

    @field_validator("verification_dataset", mode="before")
    @classmethod
    def validate_verification_dataset(
        cls,
        value: object,
    ) -> VerificationDatasetInput | None:
        if value is None:
            return None
        if isinstance(value, str) and not value.strip():
            return None
        if isinstance(value, VerificationDatasetInput):
            return value
        if isinstance(value, Mapping):
            payload = dict(value)
            if "dataset_id_or_uri" not in payload:
                for alias in ("dataset", "dataset_id", "value"):
                    if alias in payload:
                        payload["dataset_id_or_uri"] = payload.pop(alias)
                        break
            return VerificationDatasetInput.model_validate(payload)
        return VerificationDatasetInput(dataset_id_or_uri=value)

    @field_validator("verification_checks", mode="before")
    @classmethod
    def validate_verification_checks(
        cls,
        value: object,
    ) -> tuple[VerificationCheckSpec, ...] | None:
        if value is None:
            return None
        items = _sequence_items(value, "verification_checks")
        if not items:
            return None
        parsed: list[VerificationCheckSpec] = []
        seen: set[tuple[str, str]] = set()
        for index, item in enumerate(items):
            try:
                if isinstance(item, VerificationCheckSpec):
                    check = item
                elif isinstance(item, str):
                    check = VerificationCheckSpec.parse_line(item)
                elif isinstance(item, Mapping):
                    check = VerificationCheckSpec.model_validate(item)
                else:
                    raise ValueError(
                        f"verification_checks[{index}] must be a string or object"
                    )
            except (ValidationError, ValueError) as exc:
                raise ValueError(str(exc)) from exc
            if check.kind == "check":
                raise ValueError(ISSUE_NAMED_CHECK_GUIDANCE)
            if check.casefold_key in seen:
                raise ValueError("verification_checks must not contain duplicates")
            seen.add(check.casefold_key)
            parsed.append(check)
        return tuple(parsed)

    @field_validator("acknowledge_no_evidence", mode="before")
    @classmethod
    def validate_acknowledge_no_evidence(cls, value: object) -> bool:
        if value is None:
            return False
        if type(value) is bool:
            return value
        if isinstance(value, str):
            normalized = value.strip().casefold()
            if not normalized:
                return False
            if normalized in {"acknowledge", "acknowledged", "true", "yes"}:
                return True
        raise ValueError(
            "acknowledge_no_evidence must be a boolean or the word acknowledge"
        )

    @model_validator(mode="after")
    def validate_safe_document(self) -> Self:
        if self.primary_metric is not None and not self.issue_evaluators:
            raise ValueError(
                "primary_metric requires at least one issue evaluator"
            )
        if self.repo_agent_id is None and self.explicit_target is None:
            object.__setattr__(self, "repo_agent_id", "default")
        if (self.repo_agent_id is None) == (self.explicit_target is None):
            raise ValueError("exactly one of repo_agent_id or explicit_target is required")
        assert_safe_persisted_document(self.model_dump(mode="json"))
        return self

    @classmethod
    def from_document(cls, document: str | bytes | Mapping[str, object]) -> Self:
        payload = load_strict_yaml_mapping(document, subject="optimize issue request")
        normalized = dict(payload)
        if "candidate_models" in normalized:
            if "model_subset" not in normalized:
                normalized["model_subset"] = normalized["candidate_models"]
            normalized.pop("candidate_models", None)
        if "editable_scope" in normalized:
            if "editable_scope_subset" not in normalized:
                normalized["editable_scope_subset"] = normalized["editable_scope"]
            normalized.pop("editable_scope", None)
        if "target" in normalized:
            target = normalized.pop("target")
            if isinstance(target, str):
                stripped = target.strip()
                if _REPO_AGENT_ID.fullmatch(stripped):
                    normalized.setdefault("repo_agent_id", stripped)
                else:
                    normalized.setdefault("explicit_target", stripped)
        if "issue_dataset" in normalized and "verification_dataset" not in normalized:
            normalized["verification_dataset"] = normalized.pop("issue_dataset")
        if "dataset_id" in normalized and "verification_dataset" not in normalized:
            normalized["verification_dataset"] = normalized.pop("dataset_id")
        if (
            "verification_commands" in normalized
            and "verification_checks" not in normalized
        ):
            normalized["verification_checks"] = normalized.pop("verification_commands")
        if (
            "no_evidence_acknowledged" in normalized
            and "acknowledge_no_evidence" not in normalized
        ):
            normalized["acknowledge_no_evidence"] = normalized.pop(
                "no_evidence_acknowledged"
            )
        return _validate_model(
            cls,
            normalized,
            subject="optimize issue request",
        )


class IssueEvaluatorEntry(FrozenModel):
    evaluator_id: str
    weight: float | None = None

    @field_validator("evaluator_id")
    @classmethod
    def validate_evaluator_id(cls, value: str) -> str:
        normalized = _compact_text(value, "evaluator_id", limit=256)
        if not (
            normalized.startswith(_BUILTIN_EVALUATOR_PREFIX)
            or _VERSIONED_EVALUATOR.fullmatch(normalized)
            or _REGISTRY_EVALUATOR.fullmatch(normalized)
        ):
            raise ValueError(
                "evaluator_id must be an exact built-in, registry, or versioned evaluator ID"
            )
        return normalized

    @field_validator("weight")
    @classmethod
    def validate_weight(cls, value: float | None) -> float | None:
        if value is None:
            return None
        if not math.isfinite(value) or value <= 0:
            raise ValueError("weight must be a positive finite number")
        return value

    @classmethod
    def parse_line(cls, value: str) -> "IssueEvaluatorEntry":
        match = _EVALUATOR_LINE.fullmatch(value.strip())
        if match is None:
            raise IssueEvaluatorSyntaxError("issue evaluator entry must use exact-id [weight=<positive>]")
        try:
            weight = None if match.group("weight") is None else float(match.group("weight"))
        except ValueError as exc:
            raise IssueEvaluatorSyntaxError("issue evaluator entry weight must be numeric") from exc
        try:
            return cls(evaluator_id=match.group("evaluator_id"), weight=weight)
        except Exception as exc:
            raise IssueEvaluatorSyntaxError(str(exc)) from exc


def apply_issue_request(
    policy: RepositoryPolicy,
    issue: OptimizeIssueRequest,
) -> RepositoryPolicy:
    if not isinstance(policy, RepositoryPolicy):
        raise TypeError("policy must be a RepositoryPolicy")
    if not isinstance(issue, OptimizeIssueRequest):
        raise TypeError("issue must be an OptimizeIssueRequest")
    if issue.candidate_budget < policy.min_candidates:
        raise IssueNarrowingError("candidate_budget is smaller than policy minimum")
    if issue.candidate_budget > policy.max_candidates:
        raise IssueNarrowingError("candidate_budget exceeds policy maximum")
    if policy.decision_rules.focused_cases_required and not issue.observed_failures:
        raise IssueNarrowingError("focused_cases_required needs observed failures")

    allowed_models = policy.allowed_models
    if issue.model_subset is not None:
        allowed_map = {
            unicodedata.normalize("NFC", model).casefold(): model
            for model in policy.allowed_models
        }
        requested = {
            unicodedata.normalize("NFC", model).casefold()
            for model in issue.model_subset
        }
        if not requested <= set(allowed_map):
            raise IssueNarrowingError("model_subset widens allowed_models")
        allowed_models = tuple(
            model
            for model in policy.allowed_models
            if unicodedata.normalize("NFC", model).casefold() in requested
        )

    editable_paths = policy.editable_paths
    if issue.editable_scope_subset is not None:
        allowed_map = {
            unicodedata.normalize("NFC", path).casefold(): path
            for path in policy.editable_paths
        }
        requested = {
            unicodedata.normalize("NFC", path).casefold()
            for path in issue.editable_scope_subset
        }
        if not requested <= set(allowed_map):
            raise IssueNarrowingError(
                "editable_scope_subset widens editable_paths"
            )
        editable_paths = tuple(
            path
            for path in policy.editable_paths
            if unicodedata.normalize("NFC", path).casefold() in requested
        )

    payload = policy.model_dump(mode="json")
    payload["min_candidates"] = issue.candidate_budget
    payload["max_candidates"] = issue.candidate_budget
    payload["allowed_models"] = allowed_models
    payload["editable_paths"] = editable_paths
    if issue.primary_metric is not None:
        payload["primary_metric"] = issue.primary_metric
    return _validate_model(
        RepositoryPolicy,
        payload,
        subject="narrowed repository policy",
    )


def load_repository_policy(
    path: Path | str,
    *,
    metadata_path: Path | str | None = None,
) -> RepositoryPolicy:
    policy_document = load_strict_yaml_file(path, subject="repository policy")
    metadata_document = (
        load_strict_yaml_file(metadata_path, subject="agent metadata")
        if metadata_path is not None
        else None
    )
    return RepositoryPolicy.from_document(
        policy_document,
        metadata_document=metadata_document,
    )


def load_agent_metadata(path: Path | str) -> AgentMetadata:
    document = load_strict_yaml_file(path, subject="agent metadata")
    return AgentMetadata.from_document(document)


def load_optimize_issue_request(path: Path | str) -> OptimizeIssueRequest:
    document = load_strict_yaml_file(path, subject="optimize issue request")
    return OptimizeIssueRequest.from_document(document)


__all__ = [
    "AgentMetadata",
    "DecisionRules",
    "EvaluationContract",
    "GuardrailSpec",
    "HostedRuntimeContract",
    "IssueNarrowingError",
    "ModelDeploymentContract",
    "OidcPrincipalContract",
    "OidcSettings",
    "OidcSubjectContract",
    "OptimizeIssueRequest",
    "POCConfigurationError",
    "RepositoryPathError",
    "RepositoryPolicy",
    "SharedPin",
    "WorkflowVariableContract",
    "apply_issue_request",
    "load_agent_metadata",
    "load_optimize_issue_request",
    "load_repository_policy",
    "load_strict_yaml_file",
    "load_strict_yaml_mapping",
    "validate_repository_relative_path",
    "validate_repository_relative_paths",
]
