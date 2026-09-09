from __future__ import annotations

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry_opt.optimizer_adapters import (
    FoundryEvaluationAdapter,
    FoundryHostedTargetAdapter,
    FoundryHostedTargetConfig,
    FoundryPromptTargetAdapter,
    FoundryPromptTargetConfig,
    EvaluationTarget,
    OptimizerAdapterError,
)
from foundry_opt.poc.candidate import CandidateHashes, FinalizedCandidate
from foundry_opt.poc.foundry import (
    HostedDefinition,
    ImageHostedDraftReference,
    PromptDefinition,
    PromptDraftReference,
    RouteDriftError,
    RouteFingerprint,
)


HASH = f"sha256:{'0' * 64}"


def _canonical(value: object) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _route(*, digest: str = "1" * 64) -> RouteFingerprint:
    return RouteFingerprint(
        agent_name="agent-one",
        latest_version="7",
        selector=None,
        endpoint_configuration=None,
        sha256=digest,
    )


class _FakeTargetClient:
    def __init__(self, baseline: HostedDefinition) -> None:
        self.baseline = baseline
        self.route = _route()
        self.created_definition: HostedDefinition | None = None
        self.reference: ImageHostedDraftReference | None = None
        self.deleted = False
        self.drift = False

    def get_image_hosted_version(
        self,
        agent_name: str,
        version: str,
        *,
        deadline_monotonic: float,
    ) -> tuple[HostedDefinition, str, dict[str, str]]:
        assert agent_name == "agent-one"
        assert version == "7"
        return self.baseline, "active", {}

    def route_fingerprint(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        assert agent_name == "agent-one"
        return self.route

    def create_image_hosted_draft(
        self,
        agent_name: str,
        definition: HostedDefinition,
        *,
        deadline_monotonic: float,
        ownership_token: str,
    ) -> ImageHostedDraftReference:
        self.created_definition = definition
        self.reference = ImageHostedDraftReference(
            agent_name=agent_name,
            version="draft-one",
            ownership_token=ownership_token,
            definition_sha256=definition.sha256,
            route=self.route,
            definition=definition,
            status="creating",
        )
        return self.reference

    def verify_image_hosted_draft(
        self,
        reference: ImageHostedDraftReference,
        *,
        deadline_monotonic: float,
    ) -> ImageHostedDraftReference:
        if self.drift:
            raise RouteDriftError(
                "route drift",
                expected=reference.route,
                actual=_route(digest="2" * 64),
            )
        return replace(reference, status="active")

    def delete_owned_image_hosted_version(
        self,
        reference: ImageHostedDraftReference,
        *,
        deadline_monotonic: float,
    ) -> None:
        assert self.reference is not None
        assert reference.ownership_token == self.reference.ownership_token
        self.deleted = True


def _target_layout(
    tmp_path: Path,
) -> tuple[Path, Path, Path, HostedDefinition, str]:
    repository = tmp_path / "repository"
    agent_root = repository / "agent"
    agent_root.mkdir(parents=True)
    instruction = "Be concise.\n"
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Look up a value.",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    (agent_root / "instructions.txt").write_text(instruction, encoding="utf-8")
    (agent_root / "tools.py").write_text(
        "raise RuntimeError('must never execute')\n"
        f"TOOL_SCHEMAS = {tools!r}\n",
        encoding="utf-8",
    )
    config = {
        "model": "gpt-4.1",
        "instructions": instruction,
        "tool_schemas": tools,
        "frozen_runtime": {"handler": "agent.handlers:dispatch"},
    }
    definition = HostedDefinition(
        payload={
            "image": "registry.example/agent@sha256:" + "a" * 64,
            "cpu": "1",
            "memory": "2Gi",
            "container_protocol_versions": [
                {"protocol": "responses", "version": "1.0.0"}
            ],
            "environment_variables": {"AGENT_CONFIG": _canonical(config)},
        }
    )
    subprocess.run(["git", "-C", str(repository), "init"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
    }
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "baseline"],
        check=True,
        env=env,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    return repository, agent_root, tmp_path / "trusted", definition, commit


def _target_config() -> dict[str, object]:
    return {
        "endpoint_allowlist": ["https://foundry.example"],
        "project_endpoint": "https://foundry.example/api/projects/project-one",
        "agent_name": "agent-one",
        "baseline_version": "7",
        "baseline_commit": "a" * 40,
        "draft_mode": "image_definition",
        "credential_provider": "azure_cli",
        "instruction_file": "agent/instructions.txt",
        "tool_schema_file": "agent/tools.py",
        "tool_config_key": "tool_schemas",
        "config_environment_variable": "AGENT_CONFIG",
        "model": "gpt-4.1",
        "poll_interval_seconds": 0,
    }


def _candidate(repository: Path, agent_root: Path) -> FinalizedCandidate:
    workspace = repository.parent / "candidate"
    candidate_agent = workspace / "agent"
    candidate_agent.mkdir(parents=True)
    (candidate_agent / "instructions.txt").write_text(
        "Be concise and cite sources.\n",
        encoding="utf-8",
    )
    (candidate_agent / "tools.py").write_text(
        "raise RuntimeError('must never execute')\n"
        "TOOL_SCHEMAS = [{'type': 'function', 'function': {"
        "'name': 'lookup', 'description': 'Find a relevant value.', "
        "'parameters': {'type': 'object', 'properties': "
        "{'query': {'type': 'string'}}, 'required': ['query']}}}]\n",
        encoding="utf-8",
    )
    return FinalizedCandidate(
        candidate_id="candidate-one",
        model="coding-model",
        hypothesis="Improve bounded descriptions.",
        base_commit="a" * 40,
        origin_commit="a" * 40,
        candidate_commit="b" * 40,
        source_root="agent",
        workspace_path=workspace,
        changed_paths=("agent/instructions.txt", "agent/tools.py"),
        incremental_changed_paths=("agent/instructions.txt", "agent/tools.py"),
        hashes=CandidateHashes(
            patch_sha256="1" * 64,
            source_tree_sha256="2" * 64,
            source_zip_sha256="3" * 64,
        ),
        patch_path=workspace / "candidate.patch",
        source_zip_path=workspace / "source.zip",
    )


def test_endpoint_allowlist_rejects_unapproved_origin() -> None:
    config = _target_config()
    config["project_endpoint"] = "https://evil.example/api/projects/project-one"

    with pytest.raises(ValidationError, match="endpoint_allowlist"):
        FoundryHostedTargetConfig.model_validate(config)


def test_hosted_adapter_projects_only_instruction_and_descriptions_safely(
    tmp_path: Path,
) -> None:
    repository, agent_root, trusted, baseline, commit = _target_layout(tmp_path)
    client = _FakeTargetClient(baseline)
    config = _target_config()
    config["baseline_commit"] = commit
    adapter = FoundryHostedTargetAdapter(
        config,
        repository_root=repository,
        agent_root=agent_root,
        trusted_run_root=trusted,
        run_id="run-one",
        contract_hash=HASH,
        client=client,  # type: ignore[arg-type]
        monotonic=lambda: 1.0,
    )

    snapshot = adapter.inspect_target()
    handle = adapter.materialize_candidate(_candidate(repository, agent_root))

    assert snapshot.baseline_version == "7"
    assert handle.invoke_handle == "opaque:foundry-target:candidate-one"
    assert client.created_definition is not None
    baseline_payload = baseline.as_payload()
    candidate_payload = client.created_definition.as_payload()
    assert {
        key: value
        for key, value in candidate_payload.items()
        if key != "environment_variables"
    } == {
        key: value
        for key, value in baseline_payload.items()
        if key != "environment_variables"
    }
    candidate_config = json.loads(
        candidate_payload["environment_variables"]["AGENT_CONFIG"]  # type: ignore[index]
    )
    assert candidate_config["model"] == "gpt-4.1"
    assert candidate_config["frozen_runtime"] == {
        "handler": "agent.handlers:dispatch"
    }
    assert candidate_config["instructions"] == "Be concise and cite sources.\n"
    assert (
        candidate_config["tool_schemas"][0]["function"]["description"]
        == "Find a relevant value."
    )
    assert (
        candidate_config["tool_schemas"][0]["function"]["parameters"]
        == json.loads(
            baseline_payload["environment_variables"]["AGENT_CONFIG"]  # type: ignore[index]
        )["tool_schemas"][0]["function"]["parameters"]
    )


def test_hosted_adapter_lifecycle_detects_route_drift_and_cleans_owned_draft(
    tmp_path: Path,
) -> None:
    repository, agent_root, trusted, baseline, commit = _target_layout(tmp_path)
    client = _FakeTargetClient(baseline)
    config = _target_config()
    config["baseline_commit"] = commit
    adapter = FoundryHostedTargetAdapter(
        config,
        repository_root=repository,
        agent_root=agent_root,
        trusted_run_root=trusted,
        run_id="run-one",
        contract_hash=HASH,
        client=client,  # type: ignore[arg-type]
        monotonic=lambda: 1.0,
    )
    adapter.inspect_target()
    adapter.materialize_candidate(_candidate(repository, agent_root))
    client.drift = True

    with pytest.raises(RouteDriftError):
        adapter.verify_candidate("candidate-one")

    client.drift = False
    verified = adapter.verify_candidate("candidate-one")
    receipt = adapter.cleanup_candidate("candidate-one")
    assert verified.artifact_hash.startswith("sha256:")
    assert receipt.startswith("restricted:target-receipts/")
    assert client.deleted is True


class _FakePromptClient:
    def __init__(self, baseline: PromptDefinition) -> None:
        self.baseline = baseline
        self.route = _route()
        self.created_definition: PromptDefinition | None = None
        self.reference: PromptDraftReference | None = None
        self.deleted = False
        self.verify_drift = False
        self.create_drift = False

    def get_prompt_version(
        self,
        agent_name: str,
        version: str,
        *,
        deadline_monotonic: float,
    ) -> tuple[PromptDefinition, str, dict[str, str]]:
        assert agent_name == "agent-one"
        assert version in {"7", "draft-prompt"}
        definition = (
            self.baseline
            if version == "7"
            else self.reference.definition  # type: ignore[union-attr]
        )
        return definition, "active", {}

    def route_fingerprint(
        self,
        agent_name: str,
        *,
        deadline_monotonic: float,
    ) -> RouteFingerprint:
        assert agent_name == "agent-one"
        return self.route

    def create_prompt_draft(
        self,
        agent_name: str,
        definition: PromptDefinition,
        *,
        deadline_monotonic: float,
        ownership_token: str,
    ) -> PromptDraftReference:
        self.created_definition = definition
        route = _route(digest="2" * 64) if self.create_drift else self.route
        self.reference = PromptDraftReference(
            agent_name=agent_name,
            version="draft-prompt",
            ownership_token=ownership_token,
            definition_sha256=definition.sha256,
            route=route,
            definition=definition,
            status="creating",
        )
        return self.reference

    def verify_prompt_draft(
        self,
        reference: PromptDraftReference,
        *,
        deadline_monotonic: float,
    ) -> PromptDraftReference:
        if self.verify_drift:
            raise RouteDriftError(
                "route drift",
                expected=reference.route,
                actual=_route(digest="2" * 64),
            )
        return replace(reference, status="active")

    def delete_owned_prompt_version(
        self,
        reference: PromptDraftReference,
        *,
        deadline_monotonic: float,
    ) -> None:
        assert self.reference is not None
        assert reference.ownership_token == self.reference.ownership_token
        self.deleted = True


def _prompt_layout(
    tmp_path: Path,
) -> tuple[Path, Path, Path, PromptDefinition, str]:
    repository = tmp_path / "repository"
    agent_root = repository / "agent"
    agent_root.mkdir(parents=True)
    instruction = "Solve the problem carefully.\n"
    (agent_root / "instructions.txt").write_text(instruction, encoding="utf-8")
    subprocess.run(["git", "-C", str(repository), "init"], check=True)
    subprocess.run(["git", "-C", str(repository), "add", "."], check=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_AUTHOR_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "Test",
    }
    subprocess.run(
        ["git", "-C", str(repository), "commit", "-m", "baseline"],
        check=True,
        env=env,
    )
    commit = subprocess.run(
        ["git", "-C", str(repository), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    ).stdout.strip()
    definition = PromptDefinition(
        model="gpt-4.1",
        instructions=instruction,
        payload={
            "tools": [
                {
                    "type": "function",
                    "name": "calculator",
                    "parameters": {"type": "object"},
                }
            ],
            "temperature": 0,
            "metadata": {"frozen": True},
        },
    )
    return repository, agent_root, tmp_path / "trusted", definition, commit


def _prompt_config(commit: str) -> dict[str, object]:
    return {
        "endpoint_allowlist": ["https://foundry.example"],
        "project_endpoint": "https://foundry.example/api/projects/project-one",
        "agent_name": "agent-one",
        "baseline_version": "7",
        "baseline_commit": commit,
        "credential_provider": "azure_cli",
        "instruction_file": "agent/instructions.txt",
        "model": "gpt-4.1",
        "poll_interval_seconds": 0,
    }


def _prompt_candidate(
    repository: Path,
    *,
    extra_path: bool = False,
) -> FinalizedCandidate:
    workspace = repository.parent / (
        "candidate-extra" if extra_path else "candidate-prompt"
    )
    candidate_agent = workspace / "agent"
    candidate_agent.mkdir(parents=True, exist_ok=True)
    (candidate_agent / "instructions.txt").write_text(
        "Solve carefully and verify the final answer.\n",
        encoding="utf-8",
    )
    changed_paths = ["agent/instructions.txt"]
    if extra_path:
        (candidate_agent / "other.txt").write_text("drift\n", encoding="utf-8")
        changed_paths.append("agent/other.txt")
    return FinalizedCandidate(
        candidate_id="candidate-prompt",
        model="coding-model",
        hypothesis="Improve the instruction.",
        base_commit="a" * 40,
        origin_commit="a" * 40,
        candidate_commit="b" * 40,
        source_root="agent",
        workspace_path=workspace,
        changed_paths=tuple(changed_paths),
        incremental_changed_paths=tuple(changed_paths),
        hashes=CandidateHashes(
            patch_sha256="1" * 64,
            source_tree_sha256="2" * 64,
            source_zip_sha256="3" * 64,
        ),
        patch_path=workspace / "candidate.patch",
        source_zip_path=workspace / "source.zip",
    )


def test_prompt_endpoint_allowlist_and_baseline_alignment(tmp_path: Path) -> None:
    repository, agent_root, trusted, baseline, commit = _prompt_layout(tmp_path)
    invalid = _prompt_config(commit)
    invalid["project_endpoint"] = "https://evil.example/api/projects/project-one"
    with pytest.raises(ValidationError, match="endpoint_allowlist"):
        FoundryPromptTargetConfig.model_validate(invalid)

    for deployed, message in (
        (
            PromptDefinition(
                model="other-model",
                instructions=baseline.instructions,
                payload=baseline.payload,
            ),
            "frozen model",
        ),
        (
            PromptDefinition(
                model=baseline.model,
                instructions="different\n",
                payload=baseline.payload,
            ),
            "instruction_file",
        ),
    ):
        adapter = FoundryPromptTargetAdapter(
            _prompt_config(commit),
            repository_root=repository,
            agent_root=agent_root,
            trusted_run_root=trusted / deployed.sha256,
            run_id="run-one",
            contract_hash=HASH,
            client=_FakePromptClient(deployed),  # type: ignore[arg-type]
            monotonic=lambda: 1.0,
        )
        with pytest.raises(OptimizerAdapterError, match=message):
            adapter.inspect_target()


def test_prompt_adapter_projects_only_instruction_and_freezes_payload(
    tmp_path: Path,
) -> None:
    repository, agent_root, trusted, baseline, commit = _prompt_layout(tmp_path)
    client = _FakePromptClient(baseline)
    adapter = FoundryPromptTargetAdapter(
        _prompt_config(commit),
        repository_root=repository,
        agent_root=agent_root,
        trusted_run_root=trusted,
        run_id="run-one",
        contract_hash=HASH,
        client=client,  # type: ignore[arg-type]
        monotonic=lambda: 1.0,
    )

    snapshot = adapter.inspect_target()
    handle = adapter.materialize_candidate(_prompt_candidate(repository))

    assert snapshot.baseline_version == "7"
    assert handle.invoke_handle == "opaque:foundry-target:candidate-prompt"
    assert client.created_definition is not None
    assert client.created_definition.model == baseline.model
    assert client.created_definition.payload == baseline.payload
    assert client.created_definition.instructions == (
        "Solve carefully and verify the final answer.\n"
    )
    baseline_evidence = json.loads(
        (
            trusted
            / "restricted-evidence"
            / "targets"
            / "baseline.json"
        ).read_text(encoding="utf-8")
    )
    assert baseline_evidence["definition"] == json.loads(
        _canonical(baseline.as_payload())
    )
    receipts = [
        json.loads(path.read_text(encoding="utf-8"))
        for path in (trusted / "target-receipts").glob("*.json")
    ]
    assert {receipt["operation_type"] for receipt in receipts} == {
        "inspect",
        "materialize",
    }
    assert all(receipt["adapter_id"] == "foundry-prompt" for receipt in receipts)
    with pytest.raises(OptimizerAdapterError, match="exactly"):
        adapter.materialize_candidate(
            _prompt_candidate(repository, extra_path=True)
        )


def test_prompt_adapter_lifecycle_route_drift_cleanup_and_reference_reload(
    tmp_path: Path,
) -> None:
    repository, agent_root, trusted, baseline, commit = _prompt_layout(tmp_path)
    client = _FakePromptClient(baseline)
    config = _prompt_config(commit)
    adapter = FoundryPromptTargetAdapter(
        config,
        repository_root=repository,
        agent_root=agent_root,
        trusted_run_root=trusted,
        run_id="run-one",
        contract_hash=HASH,
        client=client,  # type: ignore[arg-type]
        monotonic=lambda: 1.0,
    )
    adapter.inspect_target()
    adapter.materialize_candidate(_prompt_candidate(repository))
    client.verify_drift = True
    with pytest.raises(RouteDriftError):
        adapter.verify_candidate("candidate-prompt")

    client.verify_drift = False
    adapter.verify_candidate("candidate-prompt")
    reloaded = FoundryPromptTargetAdapter(
        config,
        repository_root=repository,
        agent_root=agent_root,
        trusted_run_root=trusted,
        run_id="run-one",
        contract_hash=HASH,
        client=client,  # type: ignore[arg-type]
        monotonic=lambda: 1.0,
    )
    target = reloaded.candidate_target("candidate-prompt")
    receipt = reloaded.cleanup_candidate("candidate-prompt")

    assert target.agent_version == "draft-prompt"
    assert receipt.startswith("restricted:target-receipts/")
    assert client.deleted is True

    drift_client = _FakePromptClient(baseline)
    drift_adapter = FoundryPromptTargetAdapter(
        config,
        repository_root=repository,
        agent_root=agent_root,
        trusted_run_root=tmp_path / "drift-trusted",
        run_id="run-two",
        contract_hash=HASH,
        client=drift_client,  # type: ignore[arg-type]
        monotonic=lambda: 1.0,
    )
    drift_adapter.inspect_target()
    drift_client.create_drift = True
    with pytest.raises(RouteDriftError):
        drift_adapter.materialize_candidate(_prompt_candidate(repository))
    assert drift_client.deleted is True


class _Page:
    def __init__(self, data: list[object], next_page: _Page | None = None) -> None:
        self.data = data
        self._next = next_page
        self.next_calls = 0

    def has_next_page(self) -> bool:
        return self._next is not None

    def get_next_page(self) -> _Page:
        self.next_calls += 1
        assert self._next is not None
        return self._next


class _OutputItems:
    def __init__(self, page: _Page) -> None:
        self.page = page

    def list(self, run_id: str, *, eval_id: str, limit: int) -> _Page:
        assert run_id == "provider-run"
        assert eval_id == "eval-one"
        assert limit == 100
        return self.page


class _Runs:
    def __init__(self, page: _Page) -> None:
        self.output_items = _OutputItems(page)
        self.created: dict[str, object] | None = None

    def create(self, eval_id: str, **kwargs: object) -> object:
        assert eval_id == "eval-one"
        self.created = kwargs
        return {"id": "provider-run"}

    def retrieve(self, run_id: str, *, eval_id: str) -> object:
        return {
            "id": run_id,
            "eval_id": eval_id,
            "status": "completed",
            "report_url": "https://ai.azure.com/reports/provider-run?secret=hidden",
        }


class _Evals:
    def __init__(self, page: _Page) -> None:
        self.runs = _Runs(page)

    def retrieve(self, evaluation_id: str) -> object:
        assert evaluation_id == "eval-one"
        return {
            "id": evaluation_id,
            "testing_criteria": [{"name": "quality"}],
        }


class _OpenAI:
    def __init__(self, page: _Page) -> None:
        self.evals = _Evals(page)


def _output(task_id: int, score: float) -> dict[str, object]:
    return {
        "datasource_item_id": task_id,
        "results": [
            {
                "name": "quality",
                "score": score,
                "passed": score >= 0.5,
            }
        ],
    }


def test_evaluation_retrieves_all_pages_recomputes_score_and_hides_holdout_rows(
    tmp_path: Path,
) -> None:
    second = _Page([_output(2, 0.8)])
    first = _Page([_output(1, 0.2)], second)
    openai = _OpenAI(first)
    role_path = tmp_path / "confirmation.jsonl"
    role_path.write_text(
        '{"id":"hidden-1","query":"secret one"}\n'
        '{"id":"hidden-2","query":"secret two"}\n',
        encoding="utf-8",
    )
    adapter = FoundryEvaluationAdapter(
        {
            "endpoint_allowlist": ["https://foundry.example"],
            "project_endpoint": (
                "https://foundry.example/api/projects/project-one"
            ),
            "evaluation_id": "eval-one",
            "evaluator_references": ["quality"],
            "dataset_sources": {
                "search_pool": "opaque:search",
                "validation": "opaque:validation",
            },
            "data_mapping": {"query": "{{item.query}}"},
            "credential_provider": "azure_cli",
            "poll_interval_seconds": 0,
        },
        trusted_run_root=tmp_path / "trusted",
        run_id="run-one",
        contract_hash=HASH,
        objective={
            "evaluators": [
                {
                    "reference": "quality",
                    "weight": 1.0,
                    "normalization": {
                        "type": "linear",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                }
            ]
        },
        openai_client=openai,
        monotonic=lambda: 1.0,
        sleep=lambda _: None,
    )

    projection = adapter.evaluate(
        target=EvaluationTarget(
            subject_kind="candidate",
            subject_id="candidate-one",
            agent_name="agent-one",
            agent_version="draft-one",
        ),
        split_role="confirmation",
        split_fingerprint=f"sha256:{'4' * 64}",
        opaque_role_path=role_path,
        expected_count=2,
    )

    assert first.next_calls == 1
    assert projection.avgScore == pytest.approx(0.5)
    assert projection.total == 2
    assert projection.task_scores is None
    assert "secret" not in projection.model_dump_json()
    assert openai.evals.runs.created is not None
    assert openai.evals.runs.created["data_source"]["source"]["type"] == (
        "file_content"
    )
    evidence = next(
        (tmp_path / "trusted" / "restricted-evidence" / "evaluations").glob(
            "*.json"
        )
    ).read_text(encoding="utf-8")
    assert "hidden-1" not in evidence
    assert '"canonical_avgScore": 0.5' in evidence


def test_evaluation_rejects_incomplete_denominator(tmp_path: Path) -> None:
    role_path = tmp_path / "search.jsonl"
    role_path.write_text('{"id":"one","query":"one"}\n', encoding="utf-8")
    adapter = FoundryEvaluationAdapter(
        {
            "endpoint_allowlist": ["https://foundry.example"],
            "project_endpoint": (
                "https://foundry.example/api/projects/project-one"
            ),
            "evaluation_id": "eval-one",
            "evaluator_references": ["quality"],
            "dataset_sources": {
                "search_pool": "opaque:search",
                "validation": "opaque:validation",
            },
            "data_mapping": {"query": "{{item.query}}"},
            "credential_provider": "azure_cli",
        },
        trusted_run_root=tmp_path / "trusted",
        run_id="run-one",
        contract_hash=HASH,
        objective={
            "evaluators": [
                {
                    "reference": "quality",
                    "weight": 1.0,
                    "normalization": {
                        "type": "linear",
                        "minimum": 0.0,
                        "maximum": 1.0,
                    },
                }
            ]
        },
        openai_client=_OpenAI(_Page([])),
    )

    with pytest.raises(OptimizerAdapterError, match="denominator"):
        adapter.evaluate(
            target=EvaluationTarget(
                subject_kind="baseline",
                subject_id="baseline",
                agent_name="agent-one",
                agent_version="7",
            ),
            split_role="search",
            split_fingerprint=f"sha256:{'5' * 64}",
            opaque_role_path=role_path,
            expected_count=2,
        )
