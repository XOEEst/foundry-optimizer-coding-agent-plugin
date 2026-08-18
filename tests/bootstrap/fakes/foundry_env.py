"""Offline Foundry SDK fakes for the staged onboarding machine (no live calls).

These fakes implement the exact seams the adapter uses: dataset registration and case index,
generation jobs and pollers, evaluator versions with rubric structure, OpenAI-compatible Evals
definitions and runs with per-criterion measurements, and agent draft cleanup.
"""

from __future__ import annotations

import atexit
from collections.abc import Mapping, Sequence
import hashlib
import io
from pathlib import Path
import shutil
import tempfile
import zipfile

import httpx
import openai
from azure.core.exceptions import ResourceNotFoundError

from foundry_opt.bootstrap.providers.foundry import AgentPackage, FoundryAdapter
from foundry_opt.packaging import build_deterministic_zip

PROJECT_ENDPOINT = "https://example.services.ai.azure.com/api/projects/example"
RUBRIC_JOB_ID = "foundry-evalgen-rubric-000000000000000000000001"
DATASET_JOB_ID = "foundry-datagen-synthetic-000000000000000000000001"
TRACE_JOB_ID = "foundry-datagen-trace-000000000000000000000001"
LEGACY_AGGREGATE_SAFETY_ID = "azureai://built-in/evaluators/content_safety"
SOURCE_DATASET_URI = "https://example.blob.core.windows.net/eval/generated.jsonl"

# Shapes observed in a live dev project catalog: individual built-in safety evaluators from the
# shared registry, with immutable versioned ids. There is no aggregate `content_safety` entry.
SAFETY_CATALOG_VERSION = "3"
SAFETY_EVALUATOR_NAMES = (
    "violence",
    "sexual",
    "self_harm",
    "hate_unfairness",
    "indirect_attack",
    "protected_material",
)
NON_SAFETY_BUILTIN_NAMES = (
    "coherence",
    "fluency",
    "groundedness",
    "relevance",
    "retrieval",
)


def registry_evaluator_id(name: str, version: str = SAFETY_CATALOG_VERSION) -> str:
    return f"azureml://registries/azureml/evaluators/builtin.{name}/versions/{version}"


def onboarding_definition_criteria(
    *,
    safety_names: Sequence[str] = SAFETY_EVALUATOR_NAMES,
    objective_name: str = "quality-eval",
    objective_version: str = "2",
    model_deployment: str = "baseline-model",
    safety_version: str = SAFETY_CATALOG_VERSION,
) -> list[dict[str, object]]:
    """The exact criteria the onboarding machine binds, for adoption-signature fixtures."""

    mapping = {"query": "{{item.query}}", "response": "{{sample.output_text}}"}
    criteria: list[dict[str, object]] = [
        {
            "type": "azure_ai_evaluator",
            "name": objective_name,
            "evaluator_name": objective_name,
            "evaluator_version": objective_version,
            "data_mapping": dict(mapping),
            "initialization_parameters": {"deployment_name": model_deployment},
        }
    ]
    for name in safety_names:
        criteria.append(
            {
                "type": "azure_ai_evaluator",
                "name": f"builtin.{name}",
                "evaluator_name": f"builtin.{name}",
                "evaluator_version": safety_version,
                "data_mapping": dict(mapping),
            }
        )
    return criteria


def builtin_catalog(
    *,
    safety_names: Sequence[str] = SAFETY_EVALUATOR_NAMES,
    include_aggregate: bool = False,
    version: str = SAFETY_CATALOG_VERSION,
) -> list["SdkValue"]:
    """Build a catalog entry list matching the live project shapes."""

    items = [
        SdkValue(
            {
                "name": f"builtin.{name}",
                "version": version,
                "id": registry_evaluator_id(name, version),
                "evaluator_type": "builtin",
            }
        )
        for name in safety_names
    ]
    items.extend(
        SdkValue(
            {
                "name": f"builtin.{name}",
                "version": version,
                "id": registry_evaluator_id(name, version),
                "evaluator_type": "builtin",
            }
        )
        for name in NON_SAFETY_BUILTIN_NAMES
    )
    if include_aggregate:
        items.append(
            SdkValue(
                {
                    "name": "content_safety",
                    "version": "1",
                    "id": LEGACY_AGGREGATE_SAFETY_ID,
                    "evaluator_type": "builtin",
                }
            )
        )
    return items

VALID_RUBRIC = {
    "dimensions": [
        {
            "name": "task-completion",
            "weight": 1.0,
            "required_inputs": ["reference"],
            "scalar_range": {"min": 0.0, "max": 1.0},
            "threshold": 0.6,
        }
    ]
}
MALFORMED_RUBRIC = {"dimensions": []}


def _not_found() -> openai.NotFoundError:
    response = httpx.Response(404, request=httpx.Request("GET", "https://example.test"))
    return openai.NotFoundError("not found", response=response, body=None)


class Credential:
    def get_token(self, *scopes: str, **kwargs: object) -> str:
        del scopes, kwargs
        return "token"


class SdkValue:
    def __init__(self, payload: Mapping[str, object]) -> None:
        self._payload = dict(payload)

    def as_dict(self) -> Mapping[str, object]:
        return dict(self._payload)


class SdkObject(SdkValue):
    """SDK response model shape: attribute access plus `as_dict`."""

    def __getattr__(self, item: str) -> object:
        try:
            return self._payload[item]
        except KeyError:
            raise AttributeError(item) from None


class Poller:
    def __init__(self, result: object) -> None:
        self._result = result

    def continuation_token(self) -> str:
        return "ct-1"

    def polling_method(self) -> object:
        return None

    def done(self) -> bool:
        return True

    def result(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return self._result


class Datasets:
    """Dataset registry plus the stable case-index seam."""

    def __init__(self, gets: dict[tuple[str, str], SdkValue], case_index: dict[tuple[str, str], list[dict[str, str]]]) -> None:
        self.gets = gets
        self.case_index = case_index
        self.create_calls: list[tuple[str, str, object]] = []
        self.delete_calls: list[tuple[str, str]] = []

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return list(self.gets.values())

    def list_versions(self, name: str, **kwargs: object) -> list[object]:
        del kwargs
        return [value for (item_name, _), value in self.gets.items() if item_name == name]

    def get(self, name: str, version: str, **kwargs: object) -> object:
        del kwargs
        if (name, version) not in self.gets:
            raise ResourceNotFoundError(message="not found")
        return self.gets[(name, version)]

    def get_case_index(self, name: str, version: str) -> list[dict[str, str]]:
        if (name, version) not in self.case_index:
            raise ResourceNotFoundError(message="no case index")
        return [dict(item) for item in self.case_index[(name, version)]]

    def create_or_update(self, name: str, version: str, dataset_version: object, **kwargs: object) -> object:
        del kwargs
        self.create_calls.append((name, version, dataset_version))
        value = SdkValue(
            {
                "name": name,
                "version": version,
                "id": f"azureai://accounts/example/projects/example/data/{name}/versions/{version}",
                "type": getattr(dataset_version, "type", None),
                "dataUri": getattr(dataset_version, "data_uri", None),
                "tags": getattr(dataset_version, "tags", None) or {},
            }
        )
        self.gets[(name, version)] = value
        return value

    def delete(self, name: str, version: str, **kwargs: object) -> None:
        del kwargs
        self.delete_calls.append((name, version))
        self.gets.pop((name, version), None)


class DatasetJobs:
    def __init__(self, *, generated_samples: int, output_name: str, output_version: str, datasets: Datasets, case_index: list[dict[str, str]]) -> None:
        self.generated_samples = generated_samples
        self.output_name = output_name
        self.output_version = output_version
        self.datasets = datasets
        self.case_index = case_index
        self.create_calls: list[tuple[object, str | None, str | None]] = []

    def _job(self) -> SdkValue:
        if self.generated_samples > 0:
            self.datasets.gets[(self.output_name, self.output_version)] = SdkValue(
                {
                    "name": self.output_name,
                    "version": self.output_version,
                    "id": f"azureai://accounts/example/projects/example/data/{self.output_name}/versions/{self.output_version}",
                    "type": "uri_file",
                    "dataUri": SOURCE_DATASET_URI,
                }
            )
            self.datasets.case_index[(self.output_name, self.output_version)] = list(self.case_index)
        return SdkValue(
            {
                "id": DATASET_JOB_ID,
                "status": "succeeded",
                "generated_samples": self.generated_samples,
                "outputs": [{"type": "dataset", "name": self.output_name, "version": self.output_version}],
            }
        )

    def begin_create_generation_job(self, job: object, *, operation_id: str | None = None, continuation_token: str | None = None, **kwargs: object) -> Poller:
        del kwargs
        self.create_calls.append((job, operation_id, continuation_token))
        return Poller(self._job())

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


class EvaluatorJobs:
    def __init__(self, *, builtin: list[SdkValue], versions: dict[tuple[str, str], SdkValue], generated: tuple[str, str] | None, rubric: Mapping[str, object] | None) -> None:
        self.builtin = builtin
        self.versions = versions
        self.generated = generated
        self.rubric = rubric
        self.create_calls: list[tuple[object, str | None, str | None]] = []
        self.delete_version_calls: list[tuple[str, str]] = []

    def _job(self) -> SdkValue:
        if self.generated is not None:
            name, version = self.generated
            self.versions[(name, version)] = SdkValue(
                {
                    "name": name,
                    "version": version,
                    "id": f"azureai://accounts/example/projects/example/evaluators/{name}/versions/{version}",
                    "evaluator_type": "custom",
                    "generation_job_id": "evaluatorgen-fake-1",
                    "metadata": {"operation_id": RUBRIC_JOB_ID},
                    "definition": dict(self.rubric or VALID_RUBRIC),
                }
            )
        name, version = self.generated or ("quality-eval", "2")
        return SdkValue(
            {
                "id": "evaluatorgen-fake-1",
                "status": "succeeded",
                "result": {
                    "id": f"azureai://accounts/example/projects/example/evaluators/{name}/versions/{version}",
                    "name": name,
                    "version": version,
                    "display_name": name,
                },
            }
        )

    def begin_create_generation_job(self, job: object, *, operation_id: str | None = None, continuation_token: str | None = None, **kwargs: object) -> Poller:
        del kwargs
        self.create_calls.append((job, operation_id, continuation_token))
        return Poller(self._job())

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return [*self.builtin, *self.versions.values()]

    def list_versions(self, name: str, **kwargs: object) -> list[object]:
        del kwargs
        return [value for (item_name, _), value in self.versions.items() if item_name == name]

    def get_version(self, name: str, version: str, **kwargs: object) -> object:
        del kwargs
        if (name, version) not in self.versions:
            raise ResourceNotFoundError(message="not found")
        return self.versions[(name, version)]

    def delete_version(self, name: str, version: str, **kwargs: object) -> None:
        del kwargs
        self.delete_version_calls.append((name, version))
        self.versions.pop((name, version), None)


class Beta:
    def __init__(self, datasets: DatasetJobs, evaluators: EvaluatorJobs) -> None:
        self.datasets = datasets
        self.evaluators = evaluators


class Agents:
    def __init__(self, *, code_archive: bytes | None = None, content_hash: str | None = None, versions: Mapping[tuple[str, str], Mapping[str, object]] | None = None, existing_drafts: Sequence[tuple[str, str]] = ()) -> None:
        self.delete_version_calls: list[tuple[str, str]] = []
        self.download_calls: list[tuple[str, str | None]] = []
        self.create_from_code_calls: list[dict[str, object]] = []
        self._code_archive = code_archive
        self._content_hash = content_hash
        self._versions = dict(versions or {})
        self.created: dict[tuple[str, str], dict[str, object]] = {
            (name, version): {"name": name, "version": version, "status": "active", "code_configuration": {}}
            for name, version in existing_drafts
        }

    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return []

    def list_versions(self, agent_name: str, **kwargs: object) -> list[object]:
        del agent_name, kwargs
        return []

    def create_version_from_code(self, agent_name: str, *, definition: object, code, code_zip_sha256: str | None = None, description: str | None = None, metadata: Mapping[str, str] | None = None, **kwargs: object) -> object:
        del kwargs
        payload = code.read()
        digest = hashlib.sha256(payload).hexdigest()
        version = str(len([key for key in self.created if key[0] == agent_name]) + 1)
        self.create_from_code_calls.append(
            {
                "agent_name": agent_name,
                "definition": definition,
                "code_zip_sha256": code_zip_sha256,
                "observed_zip_sha256": digest,
                "metadata": dict(metadata or {}),
                "size_bytes": len(payload),
                "code_name": getattr(code, "name", None),
            }
        )
        record = {
            "name": agent_name,
            "version": version,
            "status": "active",
            "code_configuration": {"content_hash": code_zip_sha256 or digest},
            "metadata": dict(metadata or {}),
        }
        self.created[(agent_name, version)] = record
        return SdkObject(record)

    def get_version(self, agent_name: str, agent_version: str, **kwargs: object) -> object:
        del kwargs
        override = self._versions.get((agent_name, agent_version))
        if override is not None:
            return SdkObject(dict(override))
        created = self.created.get((agent_name, agent_version))
        if created is not None:
            return SdkObject(dict(created))
        if self._code_archive is None:
            raise ResourceNotFoundError("agent version not found")
        code_configuration: dict[str, object] = {}
        if self._content_hash is not None:
            code_configuration["content_hash"] = self._content_hash
        return SdkObject({"name": agent_name, "version": agent_version, "status": "active", "code_configuration": code_configuration})

    def download_code(self, agent_name: str, *, agent_version: str | None = None, **kwargs: object):
        del kwargs
        self.download_calls.append((agent_name, agent_version))
        if self._code_archive is None:
            raise ResourceNotFoundError("agent code not found")
        chunk = 4096
        return iter([self._code_archive[index : index + chunk] for index in range(0, len(self._code_archive), chunk)])

    def delete_version(self, agent_name: str, agent_version: str, **kwargs: object) -> None:
        del kwargs
        self.delete_version_calls.append((agent_name, agent_version))
        self.created.pop((agent_name, agent_version), None)


class Connections:
    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


class Deployments:
    def list(self, **kwargs: object) -> list[object]:
        del kwargs
        return []


class EvalObject:
    def __init__(self, eval_id: str, name: str, *, data_source_config: Mapping[str, object] | None = None, testing_criteria: Sequence[Mapping[str, object]] | None = None) -> None:
        self.id = eval_id
        self.name = name
        self.data_source_config = dict(data_source_config) if data_source_config is not None else None
        self.testing_criteria = [SdkObject(dict(item)) for item in (testing_criteria or ())]


class RunObject:
    """Shaped like `RunCreateResponse`/`RunRetrieveResponse`: the synthetic generation run
    echoes its data source back, and the output dataset id lands in
    `data_source.item_generation_params.output_dataset_id`."""

    def __init__(
        self,
        run_id: str,
        status: str,
        measurements: Sequence[Mapping[str, object]],
        *,
        data_source: Mapping[str, object] | None = None,
        eval_id: str | None = None,
        name: str | None = None,
    ) -> None:
        self.id = run_id
        self.status = status
        self.eval_id = eval_id
        self.name = name
        self.per_testing_criteria_results = [dict(item) for item in measurements]
        self.data_source = SdkObject(dict(data_source)) if data_source is not None else None


class OutputItems:
    """`client.evals.runs.output_items` — one item per generated/evaluated sample."""

    def __init__(self, counts: dict[str, int]) -> None:
        self.counts = counts
        self.list_calls: list[tuple[str, str]] = []

    def list(self, run_id: str, *, eval_id: str, **kwargs: object) -> list[Mapping[str, object]]:
        del kwargs
        self.list_calls.append((run_id, eval_id))
        return [{"id": f"{run_id}-item-{index}", "status": "pass"} for index in range(self.counts.get(run_id, 0))]


class Runs:
    def __init__(
        self,
        measurements: dict[str, list[Mapping[str, object]]],
        *,
        synthetic_dataset_id: str | None = None,
        synthetic_generated_samples: int | None = None,
    ) -> None:
        self.measurements = measurements
        self.create_calls: list[tuple[str, Mapping[str, object]]] = []
        self.delete_calls: list[tuple[str, str]] = []
        self.items: dict[str, RunObject] = {}
        self._next = 0
        self.fail_create = False
        self.status = "completed"
        self.synthetic_dataset_id = synthetic_dataset_id
        self.synthetic_generated_samples = synthetic_generated_samples
        self.output_items = OutputItems({})
        self.on_synthetic_create = None

    def create(self, eval_id: str, *, data_source: Mapping[str, object], name: str | None = None) -> RunObject:
        if self.fail_create:
            raise RuntimeError("run submission failed")
        self.create_calls.append((eval_id, data_source))
        self._next += 1
        source_type = str(data_source.get("type") or "")
        if source_type == "azure_ai_synthetic_data_gen_preview":
            if callable(self.on_synthetic_create):
                self.on_synthetic_create()
            params = dict(data_source.get("item_generation_params") or {})
            params["output_dataset_id"] = self.synthetic_dataset_id
            run = RunObject(f"run-{self._next}", self.status, [], data_source={**data_source, "item_generation_params": params}, eval_id=eval_id, name=name)
            if self.synthetic_generated_samples is not None:
                self.output_items.counts[run.id] = self.synthetic_generated_samples
        else:
            phase = "development" if str(name or "").startswith("development") else "validating"
            run = RunObject(f"run-{self._next}", self.status, self.measurements.get(phase, []), data_source=data_source, eval_id=eval_id, name=name)
        self.items[run.id] = run
        return run

    def retrieve(self, run_id: str, *, eval_id: str) -> RunObject:
        del eval_id
        if run_id not in self.items:
            raise _not_found()
        return self.items[run_id]

    def list(self, *, eval_id: str, **kwargs: object) -> list[RunObject]:
        del kwargs
        return [item for item in self.items.values() if item.eval_id == eval_id]

    def delete(self, run_id: str, *, eval_id: str) -> None:
        self.delete_calls.append((eval_id, run_id))
        self.items.pop(run_id, None)


class Evals:
    def __init__(self, existing: list[EvalObject], runs: Runs) -> None:
        self.items = {item.id: item for item in existing}
        self.create_calls: list[Mapping[str, object]] = []
        self.delete_calls: list[str] = []
        self.runs = runs
        self._next = 0

    def list(self) -> list[EvalObject]:
        return list(self.items.values())

    def create(self, *, data_source_config: Mapping[str, object], testing_criteria: list[Mapping[str, object]], name: str) -> EvalObject:
        self.create_calls.append({"data_source_config": data_source_config, "testing_criteria": testing_criteria, "name": name})
        self._next += 1
        created = EvalObject(f"eval_{self._next}", name, data_source_config=data_source_config, testing_criteria=testing_criteria)
        self.items[created.id] = created
        return created

    def retrieve(self, eval_id: str) -> EvalObject:
        if eval_id not in self.items:
            raise _not_found()
        return self.items[eval_id]

    def delete(self, eval_id: str) -> None:
        self.delete_calls.append(eval_id)
        self.items.pop(eval_id, None)


class OpenAIClient:
    def __init__(self, evals: Evals) -> None:
        self.evals = evals


class Client:
    def __init__(self, *, beta: Beta, datasets: Datasets, agents: Agents, openai_client: OpenAIClient) -> None:
        self.beta = beta
        self.datasets = datasets
        self.agents = agents
        self.connections = Connections()
        self.deployments = Deployments()
        self.project = None
        self._openai_client = openai_client

    def get_openai_client(self) -> OpenAIClient:
        return self._openai_client


def _rows(count: int, *, prefix: str = "case") -> list[dict[str, str]]:
    return [{"row_id": f"{prefix}-{index:03d}", "group_id": f"group-{index:03d}"} for index in range(1, count + 1)]


def _measurements(
    *,
    quality_score: float,
    safety_pass_rate: float,
    safety_entries: Sequence[tuple[str, str, str]],
    quality_pass_rate: float = 1.0,
    errored: int = 0,
    degraded_safety_name: str | None = None,
) -> list[Mapping[str, object]]:
    """Build per-criterion measurements; `safety_entries` are (short name, catalog name, version)."""

    total = 10
    measurements: list[Mapping[str, object]] = [
        {
            # Criterion names are the evaluator names bound by the azure_ai_evaluator graders.
            "testing_criteria": "quality-eval",
            "passed": round(quality_pass_rate * total),
            "failed": total - round(quality_pass_rate * total),
            "errored": errored,
            "score": quality_score,
        }
    ]
    for short_name, catalog_name, _version in safety_entries:
        rate = safety_pass_rate if degraded_safety_name in (None, short_name) else 1.0
        passed = round(rate * total)
        measurements.append(
            {
                "testing_criteria": catalog_name,
                "passed": passed,
                "failed": total - passed,
                "errored": 0,
            }
        )
    return measurements


def build_fake_adapter(
    *,
    reuse: bool = False,
    generated_samples: int = 30,
    definitions_exist: bool = False,
    quality_score: float = 0.8,
    safety_pass_rate: float = 1.0,
    degraded_safety_name: str | None = None,
    safety_names: Sequence[str] = SAFETY_EVALUATOR_NAMES,
    catalog_safety_names: Sequence[str] | None = None,
    include_aggregate_safety: bool = False,
    rubric: Mapping[str, object] | None = None,
    split_writer_available: bool = True,
    fail_run_create: bool = False,
    errored_cases: int = 0,
    code_archive: bytes | None = None,
    code_content_hash: str | None = None,
    agent_versions: Mapping[tuple[str, str], Mapping[str, object]] | None = None,
    existing_definition_criteria: Sequence[Mapping[str, object]] | None = None,
    existing_definition_config: Mapping[str, object] | None = None,
    existing_drafts: Sequence[tuple[str, str]] = (),
    agent_packages: Mapping[str, "AgentPackage"] | None = None,
) -> tuple[FoundryAdapter, dict[str, object]]:
    """Build a fully offline adapter wired for the staged onboarding machine."""

    gets: dict[tuple[str, str], SdkValue] = {}
    case_index: dict[tuple[str, str], list[dict[str, str]]] = {}
    if reuse:
        for name, count in (("dev-set", 20), ("val-set", 10)):
            gets[(name, "1")] = SdkValue(
                {
                    "name": name,
                    "version": "1",
                    "id": f"azureai://accounts/example/projects/example/data/{name}/versions/1",
                    "type": "uri_file",
                    "dataUri": f"https://example.blob.core.windows.net/eval/{name}.jsonl",
                }
            )
            case_index[(name, "1")] = _rows(count, prefix="dev" if name == "dev-set" else "val")
    datasets = Datasets(gets, case_index)
    if not reuse:
        # The synthetic agent run returns an immutable `output_dataset_id`; the service-side
        # dataset is registered here so the adapter can resolve and split it.
        gets[("generated-set", "1")] = SdkValue(
            {
                "name": "generated-set",
                "version": "1",
                "id": "azureai://accounts/example/projects/example/data/generated-set/versions/1",
                "type": "uri_file",
                "dataUri": SOURCE_DATASET_URI,
                "tags": {"data_generation_job_id": "datagen-fake-1"},
            }
        )
        case_index[("generated-set", "1")] = _rows(generated_samples)
    dataset_jobs = DatasetJobs(
        generated_samples=generated_samples,
        output_name="generated-set",
        output_version="1",
        datasets=datasets,
        case_index=_rows(generated_samples),
    )
    builtin = builtin_catalog(
        safety_names=catalog_safety_names if catalog_safety_names is not None else safety_names,
        include_aggregate=include_aggregate_safety,
    )
    versions: dict[tuple[str, str], SdkValue] = {}
    if reuse:
        versions[("quality-eval", "2")] = SdkValue(
            {
                "name": "quality-eval",
                "version": "2",
                "id": "azureai://accounts/example/projects/example/evaluators/quality-eval/versions/2",
                "evaluator_type": "custom",
                "rubric": dict(VALID_RUBRIC),
            }
        )
    evaluator_jobs = EvaluatorJobs(
        builtin=builtin,
        versions=versions,
        generated=None if reuse else ("quality-eval", "2"),
        rubric=rubric or VALID_RUBRIC,
    )
    measured_entries = (
        [("content_safety", "content_safety", "1")]
        if include_aggregate_safety
        else [(name, f"builtin.{name}", SAFETY_CATALOG_VERSION) for name in safety_names]
    )
    generated_dataset_name = "generated-set"
    runs = Runs(
        {
            "development": _measurements(
                quality_score=quality_score,
                safety_pass_rate=safety_pass_rate,
                safety_entries=measured_entries,
                errored=errored_cases,
                degraded_safety_name=degraded_safety_name,
            ),
            "validating": _measurements(
                quality_score=quality_score,
                safety_pass_rate=1.0,
                safety_entries=measured_entries,
            ),
        },
        synthetic_dataset_id=(
            f"azureai://accounts/example/projects/example/data/{generated_dataset_name}/versions/1"
        ),
        synthetic_generated_samples=generated_samples,
    )
    if not reuse:
        def _restore_synthetic_dataset() -> None:
            gets[("generated-set", "1")] = SdkValue(
                {
                    "name": "generated-set",
                    "version": "1",
                    "id": "azureai://accounts/example/projects/example/data/generated-set/versions/1",
                    "type": "uri_file",
                    "dataUri": SOURCE_DATASET_URI,
                    "tags": {"data_generation_job_id": "datagen-fake-1"},
                }
            )
            case_index[("generated-set", "1")] = _rows(generated_samples)

        runs.on_synthetic_create = _restore_synthetic_dataset
    runs.fail_create = fail_run_create
    if definitions_exist:
        criteria = existing_definition_criteria if existing_definition_criteria is not None else onboarding_definition_criteria(safety_names=safety_names)
        config = existing_definition_config if existing_definition_config is not None else {"type": "azure_ai_source", "scenario": "synthetic_data_gen_preview"}
        existing_definitions = [
            EvalObject("eval_development", "dev-def", data_source_config=config, testing_criteria=criteria),
            EvalObject("eval_validating", "val-def", data_source_config=config, testing_criteria=criteria),
        ]
    else:
        existing_definitions = []
    evals = Evals(existing_definitions, runs)
    agents = Agents(code_archive=code_archive, content_hash=code_content_hash, versions=agent_versions, existing_drafts=existing_drafts)
    client = Client(
        beta=Beta(dataset_jobs, evaluator_jobs),
        datasets=datasets,
        agents=agents,
        openai_client=OpenAIClient(evals),
    )

    def _split_writer(*, source_data_uri: str, role: str, case_ids: Sequence[str], dataset_name: str, dataset_version: str) -> str:
        del source_data_uri, case_ids
        return f"https://example.blob.core.windows.net/eval/{dataset_name}-{dataset_version}-{role}.jsonl"

    adapter = FoundryAdapter(
        PROJECT_ENDPOINT,
        Credential(),
        client=client,
        split_writer=_split_writer if split_writer_available else None,
        sleep=lambda _seconds: None,
    )
    default_package = fake_agent_package()
    adapter.set_agent_packages(_DefaultingPackages(default_package, **{key: value for key, value in (agent_packages or {}).items()}))
    return adapter, {
        "datasets": datasets,
        "dataset_jobs": dataset_jobs,
        "evaluator_jobs": evaluator_jobs,
        "evals": evals,
        "runs": runs,
        "package": default_package,
        "agents": agents,
        "client": client,
        "split_writer": _split_writer,
    }


def fake_credential() -> Credential:
    return Credential()


_PACKAGE_CACHE: dict[str, "AgentPackage"] = {}


def fake_agent_package(repo_agent_id: str = "app", *, marker: str = "print('agent')\n") -> "AgentPackage":
    """Build a real deterministic archive for the owned-draft creation path."""

    key = f"{repo_agent_id}-{hashlib.sha256(marker.encode('utf-8')).hexdigest()[:12]}"
    cached = _PACKAGE_CACHE.get(key)
    if cached is not None:
        return cached
    root = Path(_package_root()) / key
    source = root / "src"
    source.mkdir(parents=True, exist_ok=True)
    (source / "main.py").write_text(marker, encoding="utf-8")
    result = build_deterministic_zip(source, root / "package.zip", includes=("**/*",), excludes=(), check_deadline=lambda: None)
    package = AgentPackage(
        repo_agent_id=repo_agent_id,
        zip_path=str(result.zip_path),
        zip_sha256=result.zip_sha256,
        tree_sha256=result.tree_sha256,
        file_count=len(result.entries),
        size_bytes=result.size_bytes,
    )
    _PACKAGE_CACHE[key] = package
    return package


def _package_root() -> str:
    global _PACKAGE_ROOT
    if _PACKAGE_ROOT is None:
        _PACKAGE_ROOT = tempfile.mkdtemp(prefix="foundry-opt-fake-packages-")
        atexit.register(shutil.rmtree, _PACKAGE_ROOT, True)
    return _PACKAGE_ROOT


_PACKAGE_ROOT: str | None = None


class _DefaultingPackages(dict):
    """Test seam: every agent id resolves to a package unless one is registered."""

    def __init__(self, default: "AgentPackage", **entries: "AgentPackage") -> None:
        super().__init__(entries)
        self._default = default

    def get(self, key, default=None):  # type: ignore[override]
        return super().get(key, self._default)


def build_code_archive(root: Path, *, extra: Mapping[str, bytes] | None = None) -> bytes:
    """Zip a local directory the way a deployed agent version's code archive is shaped.

    Entry names are relative to `root`, exactly as the service returns them, so the adapter
    can re-root them under the discovered source/package root.
    """

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as bundle:
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            bundle.writestr(path.relative_to(root).as_posix(), path.read_bytes())
        for name, payload in (extra or {}).items():
            bundle.writestr(name, payload)
    return buffer.getvalue()


__all__ = [
    "DATASET_JOB_ID",
    "LEGACY_AGGREGATE_SAFETY_ID",
    "MALFORMED_RUBRIC",
    "PROJECT_ENDPOINT",
    "RUBRIC_JOB_ID",
    "SAFETY_CATALOG_VERSION",
    "SAFETY_EVALUATOR_NAMES",
    "TRACE_JOB_ID",
    "VALID_RUBRIC",
    "build_code_archive",
    "build_fake_adapter",
    "builtin_catalog",
    "fake_credential",
    "onboarding_definition_criteria",
    "registry_evaluator_id",
]
