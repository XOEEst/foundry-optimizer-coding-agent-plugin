from __future__ import annotations

import concurrent.futures
import hashlib
import importlib.util
import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

import typer
import yaml
from azure.ai.projects import AIProjectClient
from azure.identity import AzureCliCredential
from openai import BadRequestError

from foundry_opt.poc.foundry import (
    FoundryPocClient,
    PromptDefinition,
    PromptDraftReference,
    RouteFingerprint,
)


class LocalPromptError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TargetConfig:
    agent_dir: Path
    repository_root: Path
    base_commit: str
    project_endpoint: str
    agent_name: str
    model: str
    instructions_path: Path
    instructions: str
    train_path: Path
    validation_path: Path
    evaluator_type: str
    max_train_samples: int
    max_concurrency: int


@dataclass(frozen=True, slots=True)
class EvaluationRow:
    row_id: str
    query: str
    ground_truth: str


def register_local_prompt_commands(parent: typer.Typer) -> None:
    prompt_app = typer.Typer(no_args_is_help=True)
    parent.add_typer(prompt_app, name="prompt")

    @prompt_app.command("init")
    def init_command(
        agent_dir: Path = typer.Option(..., "--agent-dir"),
        agent_name: str = typer.Option(..., "--agent-name"),
        state: Path = typer.Option(..., "--state"),
        confirmation_fraction: float = typer.Option(0.2, "--confirmation-fraction"),
        seed: int = typer.Option(0, "--seed"),
    ) -> None:
        target = load_target_config(agent_dir, agent_name=agent_name)
        if not 0 < confirmation_fraction < 1:
            raise typer.BadParameter("confirmation-fraction must be between 0 and 1")
        credential = AzureCliCredential()
        project_client = AIProjectClient(
            endpoint=target.project_endpoint,
            credential=credential,
        )
        foundry = FoundryPocClient(target.project_endpoint, credential)
        try:
            route, baseline = preflight_prompt_target(foundry, target)
            train_rows = load_rows(target.train_path)
            search_rows, confirmation_rows = deterministic_partition(
                train_rows,
                confirmation_fraction=confirmation_fraction,
                seed=seed,
                max_search_samples=target.max_train_samples,
            )
            evaluator = load_ifbench_checker(target.repository_root)
            openai_client = project_client.get_openai_client()
            training = evaluate_rows(
                openai_client,
                agent_name=target.agent_name,
                version=route.latest_version or "",
                rows=search_rows,
                checker=evaluator,
                max_concurrency=target.max_concurrency,
                include_details=True,
            )
            confirmation = evaluate_rows(
                openai_client,
                agent_name=target.agent_name,
                version=route.latest_version or "",
                rows=confirmation_rows,
                checker=evaluator,
                max_concurrency=target.max_concurrency,
                include_details=False,
            )
            payload = {
                "schema_version": 1,
                "target": {
                    "agent_dir": str(target.agent_dir),
                    "repository_root": str(target.repository_root),
                    "base_commit": target.base_commit,
                    "project_endpoint": target.project_endpoint,
                    "agent_name": target.agent_name,
                    "model": target.model,
                    "instructions_path": str(target.instructions_path),
                    "instructions_sha256": _sha256_text(target.instructions),
                    "train_sha256": _sha256_file(target.train_path),
                    "validation_sha256": _sha256_file(target.validation_path),
                },
                "contract": {
                    "editable_paths": [
                        str(target.instructions_path.relative_to(target.repository_root))
                    ],
                    "mutation": "instructions_only",
                    "seed": seed,
                    "confirmation_fraction": confirmation_fraction,
                    "route_sha256": route.sha256,
                    "baseline_version": route.latest_version,
                    "baseline_definition_sha256": baseline.sha256,
                    "training_row_ids": [row.row_id for row in search_rows],
                    "confirmation_row_ids": [row.row_id for row in confirmation_rows],
                    "validation_row_ids": [
                        row.row_id for row in load_rows(target.validation_path)
                    ],
                    "max_candidates": _load_max_candidates(target.agent_dir),
                },
                "baseline": {
                    "training": training,
                    "confirmation": confirmation,
                },
                "candidates": {},
                "winner": None,
            }
            _write_json(state, payload)
            _echo_json(payload)
        finally:
            foundry.close()
            _close_if_supported(project_client)
            _close_if_supported(credential)

    @prompt_app.command("candidate")
    def candidate_command(
        state: Path = typer.Option(..., "--state"),
        candidate_id: str = typer.Option(..., "--candidate"),
        instructions: Path = typer.Option(..., "--instructions"),
    ) -> None:
        payload = _read_json(state)
        target = load_target_config(
            Path(_required_string(payload["target"], "agent_dir")),
            agent_name=_required_string(payload["target"], "agent_name"),
        )
        _assert_contract_unchanged(payload, target)
        candidates = _mapping(payload.get("candidates"), "candidates")
        if candidate_id in candidates:
            raise LocalPromptError(f"candidate {candidate_id!r} already exists")
        max_candidates = int(_mapping(payload["contract"], "contract")["max_candidates"])
        if len(candidates) >= max_candidates:
            raise LocalPromptError("candidate budget is exhausted")
        candidate_instructions = instructions.resolve().read_text(encoding="utf-8")
        if candidate_instructions == target.instructions:
            raise LocalPromptError("candidate instructions must differ from the baseline")

        credential = AzureCliCredential()
        project_client = AIProjectClient(
            endpoint=target.project_endpoint,
            credential=credential,
        )
        foundry = FoundryPocClient(target.project_endpoint, credential)
        reference: PromptDraftReference | None = None
        try:
            route, baseline = preflight_prompt_target(foundry, target)
            contract = _mapping(payload["contract"], "contract")
            if route.sha256 != _required_string(contract, "route_sha256"):
                raise LocalPromptError("Foundry route drifted from the frozen run contract")
            definition = PromptDefinition(
                model=baseline.model,
                instructions=candidate_instructions,
                payload=baseline.payload,
            )
            reference = foundry.create_prompt_draft(
                target.agent_name,
                definition,
                deadline_monotonic=time.monotonic() + 300,
            )
            reference = foundry.verify_prompt_draft(
                reference,
                deadline_monotonic=time.monotonic() + 300,
            )
            checker = load_ifbench_checker(target.repository_root)
            train_by_id = {row.row_id: row for row in load_rows(target.train_path)}
            training_rows = _select_rows(
                train_by_id,
                _string_list(contract, "training_row_ids"),
                "training",
            )
            openai_client = project_client.get_openai_client()
            training = evaluate_rows(
                openai_client,
                agent_name=target.agent_name,
                version=reference.version,
                rows=training_rows,
                checker=checker,
                max_concurrency=target.max_concurrency,
                include_details=True,
            )
            baseline_training = _score(payload["baseline"], "training")
            confirmation: dict[str, Any] | None = None
            if float(training["avgScore"]) > baseline_training:
                confirmation_rows = _select_rows(
                    train_by_id,
                    _string_list(contract, "confirmation_row_ids"),
                    "confirmation",
                )
                confirmation = evaluate_rows(
                    openai_client,
                    agent_name=target.agent_name,
                    version=reference.version,
                    rows=confirmation_rows,
                    checker=checker,
                    max_concurrency=target.max_concurrency,
                    include_details=False,
                )
            candidate = {
                "candidate_id": candidate_id,
                "definition_sha256": definition.sha256,
                "instructions": candidate_instructions,
                "instructions_sha256": _sha256_text(candidate_instructions),
                "training": training,
                "confirmation": confirmation,
            }
            candidates[candidate_id] = candidate
            payload["candidates"] = candidates
            _write_json(state, payload)
            _echo_json(candidate)
        finally:
            if reference is not None:
                foundry.delete_owned_prompt_version(
                    reference,
                    deadline_monotonic=time.monotonic() + 300,
                )
            foundry.close()
            _close_if_supported(project_client)
            _close_if_supported(credential)

    @prompt_app.command("validate")
    def validate_command(
        state: Path = typer.Option(..., "--state"),
        candidate_id: str = typer.Option(..., "--candidate"),
        apply: bool = typer.Option(False, "--apply"),
    ) -> None:
        payload = _read_json(state)
        target = load_target_config(
            Path(_required_string(payload["target"], "agent_dir")),
            agent_name=_required_string(payload["target"], "agent_name"),
        )
        _assert_contract_unchanged(payload, target)
        candidate = _mapping(
            _mapping(payload.get("candidates"), "candidates").get(candidate_id),
            f"candidate {candidate_id}",
        )
        confirmation = candidate.get("confirmation")
        if confirmation is None:
            raise LocalPromptError("candidate did not qualify for confirmation")
        if _score(candidate, "training") <= _score(payload["baseline"], "training"):
            raise LocalPromptError("candidate did not improve the training score")
        if _score(candidate, "confirmation") <= _score(
            payload["baseline"], "confirmation"
        ):
            raise LocalPromptError("candidate did not improve the confirmation score")

        credential = AzureCliCredential()
        project_client = AIProjectClient(
            endpoint=target.project_endpoint,
            credential=credential,
        )
        foundry = FoundryPocClient(target.project_endpoint, credential)
        reference: PromptDraftReference | None = None
        try:
            route, baseline = preflight_prompt_target(foundry, target)
            if route.sha256 != _required_string(payload["contract"], "route_sha256"):
                raise LocalPromptError("Foundry route drifted from the frozen run contract")
            definition = PromptDefinition(
                model=baseline.model,
                instructions=_required_string(candidate, "instructions"),
                payload=baseline.payload,
            )
            if definition.sha256 != _required_string(candidate, "definition_sha256"):
                raise LocalPromptError("candidate definition drifted after evaluation")
            reference = foundry.create_prompt_draft(
                target.agent_name,
                definition,
                deadline_monotonic=time.monotonic() + 300,
            )
            reference = foundry.verify_prompt_draft(
                reference,
                deadline_monotonic=time.monotonic() + 300,
            )
            validation_by_id = {
                row.row_id: row for row in load_rows(target.validation_path)
            }
            validation_rows = _select_rows(
                validation_by_id,
                _string_list(payload["contract"], "validation_row_ids"),
                "validation",
            )
            final = evaluate_rows(
                project_client.get_openai_client(),
                agent_name=target.agent_name,
                version=reference.version,
                rows=validation_rows,
                checker=load_ifbench_checker(target.repository_root),
                max_concurrency=target.max_concurrency,
                include_details=False,
            )
            candidate["validation"] = final
            payload["winner"] = candidate_id
            if apply:
                target.instructions_path.write_text(
                    _required_string(candidate, "instructions"),
                    encoding="utf-8",
                )
                payload["applied"] = True
            _write_json(state, payload)
            _echo_json(
                {
                    "candidate_id": candidate_id,
                    "validation": final,
                    "applied": apply,
                }
            )
        finally:
            if reference is not None:
                foundry.delete_owned_prompt_version(
                    reference,
                    deadline_monotonic=time.monotonic() + 300,
                )
            foundry.close()
            _close_if_supported(project_client)
            _close_if_supported(credential)


def load_target_config(agent_dir: Path, *, agent_name: str) -> TargetConfig:
    resolved_agent_dir = agent_dir.resolve(strict=True)
    config_path = resolved_agent_dir / "agent.yaml"
    if not config_path.is_file():
        raise LocalPromptError(f"agent.yaml not found in {resolved_agent_dir}")
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config = _mapping(raw, "agent.yaml")
    model = _required_string(config, "model")
    instructions_name = str(config.get("instructions_file") or "instructions.md")
    instructions_path = (resolved_agent_dir / instructions_name).resolve(strict=True)
    if instructions_path.parent != resolved_agent_dir:
        raise LocalPromptError("instructions_file must resolve directly under agent_dir")
    eval_config = _mapping(config.get("eval"), "eval")
    datasets = _mapping(eval_config.get("dataset"), "eval.dataset")
    evaluator = _mapping(eval_config.get("evaluator"), "eval.evaluator")
    optimization = _mapping(eval_config.get("optimization"), "eval.optimization")
    faos = _mapping(optimization.get("faos"), "eval.optimization.faos")
    project_endpoint = _required_string(faos, "project_endpoint")
    if evaluator.get("type") != "ifbench":
        raise LocalPromptError("local prompt optimization currently requires evaluator.type=ifbench")
    repository_root = _git_root(resolved_agent_dir)
    train_path = (resolved_agent_dir / _required_string(datasets, "train")).resolve(
        strict=True
    )
    validation_path = (
        resolved_agent_dir / _required_string(datasets, "validation")
    ).resolve(strict=True)
    for path, label in ((train_path, "train"), (validation_path, "validation")):
        if resolved_agent_dir not in path.parents:
            raise LocalPromptError(f"{label} dataset must remain under agent_dir")
    return TargetConfig(
        agent_dir=resolved_agent_dir,
        repository_root=repository_root,
        base_commit=_git(repository_root, "rev-parse", "HEAD"),
        project_endpoint=project_endpoint,
        agent_name=agent_name,
        model=model,
        instructions_path=instructions_path,
        instructions=instructions_path.read_text(encoding="utf-8"),
        train_path=train_path,
        validation_path=validation_path,
        evaluator_type="ifbench",
        max_train_samples=_positive_int(
            optimization.get("max_train_samples", 60),
            "eval.optimization.max_train_samples",
        ),
        max_concurrency=_positive_int(
            optimization.get("max_concurrency", 10),
            "eval.optimization.max_concurrency",
        ),
    )


def preflight_prompt_target(
    client: FoundryPocClient,
    target: TargetConfig,
) -> tuple[RouteFingerprint, PromptDefinition]:
    deadline = time.monotonic() + 120
    route = client.route_fingerprint(
        target.agent_name,
        deadline_monotonic=deadline,
    )
    if route.latest_version is None or not route.latest_version.isdigit():
        raise LocalPromptError("target agent has no active regular baseline version")
    definition, status, _ = client.get_prompt_version(
        target.agent_name,
        route.latest_version,
        deadline_monotonic=deadline,
    )
    if (status or "").lower() != "active":
        raise LocalPromptError("target baseline version is not active")
    if definition.model != target.model:
        raise LocalPromptError("local model does not match the deployed baseline")
    if definition.instructions != target.instructions:
        raise LocalPromptError("local instructions do not match the deployed baseline")
    return route, definition


def deterministic_partition(
    rows: Sequence[EvaluationRow],
    *,
    confirmation_fraction: float,
    seed: int,
    max_search_samples: int,
) -> tuple[list[EvaluationRow], list[EvaluationRow]]:
    if not rows:
        raise LocalPromptError("training dataset is empty")
    ranked = sorted(
        rows,
        key=lambda row: hashlib.sha256(
            f"{seed}\0{row.row_id}\0{row.query}".encode("utf-8")
        ).digest(),
    )
    confirmation_count = max(1, round(len(ranked) * confirmation_fraction))
    if confirmation_count >= len(ranked):
        raise LocalPromptError("confirmation partition leaves no training rows")
    confirmation = ranked[:confirmation_count]
    search = ranked[confirmation_count : confirmation_count + max_search_samples]
    if not search:
        raise LocalPromptError("training partition is empty")
    return search, confirmation


def load_rows(path: Path) -> list[EvaluationRow]:
    rows: list[EvaluationRow] = []
    seen: set[str] = set()
    for line_number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines(),
        start=1,
    ):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as error:
            raise LocalPromptError(
                f"{path}:{line_number}: invalid JSON"
            ) from error
        item = _mapping(raw, f"{path}:{line_number}")
        row_id = str(item.get("id") or f"row-{line_number}")
        if row_id in seen:
            raise LocalPromptError(f"{path}: duplicate row id {row_id!r}")
        seen.add(row_id)
        rows.append(
            EvaluationRow(
                row_id=row_id,
                query=_required_string(item, "query"),
                ground_truth=_required_string(item, "ground_truth"),
            )
        )
    if not rows:
        raise LocalPromptError(f"{path}: dataset is empty")
    return rows


def load_ifbench_checker(repository_root: Path) -> Callable[..., Sequence[bool]]:
    checker_path = repository_root / "src" / "optarena" / "evaluators" / "_ifbench_checks.py"
    if not checker_path.is_file():
        raise LocalPromptError(f"IFBench checker not found: {checker_path}")
    spec = importlib.util.spec_from_file_location(
        "_foundry_opt_ifbench_checks",
        checker_path,
    )
    if spec is None or spec.loader is None:
        raise LocalPromptError("could not load the IFBench checker")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    checker = getattr(module, "evaluate_constraints", None)
    if not callable(checker):
        raise LocalPromptError("IFBench checker does not define evaluate_constraints")
    return checker


def evaluate_rows(
    openai_client: object,
    *,
    agent_name: str,
    version: str,
    rows: Sequence[EvaluationRow],
    checker: Callable[..., Sequence[bool]],
    max_concurrency: int,
    include_details: bool,
) -> dict[str, Any]:
    if not rows:
        raise LocalPromptError("evaluation split is empty")

    def evaluate_one(row: EvaluationRow) -> dict[str, Any]:
        response_text, invocation_error = _invoke_with_retries(
            openai_client,
            agent_name=agent_name,
            version=version,
            query=row.query,
        )
        ground_truth = _mapping(
            json.loads(row.ground_truth),
            f"ground_truth for {row.row_id}",
        )
        instruction_ids = _string_list(ground_truth, "instruction_id_list")
        kwargs_list = ground_truth.get("kwargs")
        if not isinstance(kwargs_list, list):
            raise LocalPromptError(
                f"ground_truth for {row.row_id} has invalid kwargs"
            )
        results = list(
            checker(
                response_text,
                instruction_ids,
                kwargs_list,
                prompt=row.query,
            )
        )
        if len(results) != len(instruction_ids):
            raise LocalPromptError(
                f"IFBench checker returned an incomplete result for {row.row_id}"
            )
        score = sum(bool(result) for result in results) / len(results) if results else 0.0
        failed = [
            instruction_id
            for instruction_id, passed in zip(instruction_ids, results, strict=True)
            if not passed
        ]
        return {
            "row_id": row.row_id,
            "score": score,
            "passed": score >= 0.6,
            "failed_constraints": failed,
            "response": response_text,
            "invocation_error": invocation_error,
        }

    results: list[dict[str, Any] | None] = [None] * len(rows)
    with concurrent.futures.ThreadPoolExecutor(
        max_workers=min(max_concurrency, len(rows))
    ) as executor:
        futures = {
            executor.submit(evaluate_one, row): index
            for index, row in enumerate(rows)
        }
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except Exception as error:
                raise LocalPromptError(
                    f"evaluation failed for row {rows[index].row_id}: {error}"
                ) from error
    completed = [result for result in results if result is not None]
    if len(completed) != len(rows):
        raise LocalPromptError("evaluation returned an incomplete task set")
    payload: dict[str, Any] = {
        "avgScore": sum(float(result["score"]) for result in completed)
        / len(completed),
        "total": len(completed),
        "passed": sum(bool(result["passed"]) for result in completed),
        "failed": sum(not bool(result["passed"]) for result in completed),
        "content_filtered": sum(
            result["invocation_error"] == "content_filter" for result in completed
        ),
    }
    if include_details:
        payload["rows"] = completed
    return payload


def _invoke_with_retries(
    openai_client: object,
    *,
    agent_name: str,
    version: str,
    query: str,
) -> tuple[str, str | None]:
    responses = getattr(openai_client, "responses", None)
    if responses is None:
        raise LocalPromptError("Foundry OpenAI responses client is unavailable")
    error: Exception | None = None
    for attempt in range(4):
        try:
            response = responses.create(
                input=query,
                extra_body={
                    "agent_reference": {
                        "type": "agent_reference",
                        "name": agent_name,
                        "version": version,
                    }
                },
            )
            output_text = getattr(response, "output_text", None)
            if not isinstance(output_text, str):
                raise LocalPromptError("Foundry response omitted output_text")
            return output_text, None
        except BadRequestError as exc:
            if exc.code == "content_filter":
                return "", "content_filter"
            raise
        except Exception as exc:
            error = exc
            if attempt == 3:
                break
            time.sleep(2**attempt)
    raise LocalPromptError("Foundry invocation failed after retries") from error


def _assert_contract_unchanged(payload: Mapping[str, Any], target: TargetConfig) -> None:
    expected = _mapping(payload.get("target"), "target")
    if target.base_commit != _required_string(expected, "base_commit"):
        raise LocalPromptError("repository HEAD drifted from the frozen run contract")
    if _sha256_text(target.instructions) != _required_string(
        expected, "instructions_sha256"
    ):
        raise LocalPromptError("baseline instructions changed after run initialization")
    if _sha256_file(target.train_path) != _required_string(expected, "train_sha256"):
        raise LocalPromptError("training dataset changed after run initialization")
    if _sha256_file(target.validation_path) != _required_string(
        expected, "validation_sha256"
    ):
        raise LocalPromptError("validation dataset changed after run initialization")


def _select_rows(
    rows_by_id: Mapping[str, EvaluationRow],
    row_ids: Sequence[str],
    split: str,
) -> list[EvaluationRow]:
    missing = [row_id for row_id in row_ids if row_id not in rows_by_id]
    if missing:
        raise LocalPromptError(f"{split} rows changed after run initialization")
    return [rows_by_id[row_id] for row_id in row_ids]


def _load_max_candidates(agent_dir: Path) -> int:
    config = _mapping(
        yaml.safe_load((agent_dir / "agent.yaml").read_text(encoding="utf-8")),
        "agent.yaml",
    )
    eval_config = _mapping(config.get("eval"), "eval")
    optimization = _mapping(eval_config.get("optimization"), "eval.optimization")
    faos = _mapping(optimization.get("faos"), "eval.optimization.faos")
    return _positive_int(faos.get("max_candidates", 5), "faos.max_candidates")


def _git_root(path: Path) -> Path:
    return Path(_git(path, "rev-parse", "--show-toplevel")).resolve(strict=True)


def _git(repository: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return completed.stdout.strip()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _mapping(value: object, subject: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LocalPromptError(f"{subject} must be an object")
    return dict(value)


def _required_string(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw:
        raise LocalPromptError(f"{key} must be a nonempty string")
    return raw


def _string_list(value: Mapping[str, Any], key: str) -> list[str]:
    raw = value.get(key)
    if not isinstance(raw, list) or any(
        not isinstance(item, str) or not item for item in raw
    ):
        raise LocalPromptError(f"{key} must be a list of nonempty strings")
    return list(raw)


def _positive_int(value: object, subject: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise LocalPromptError(f"{subject} must be a positive integer")
    return value


def _score(value: object, split: str) -> float:
    parent = _mapping(value, "score parent")
    summary = _mapping(parent.get(split), split)
    score = summary.get("avgScore")
    if isinstance(score, bool) or not isinstance(score, (int, float)):
        raise LocalPromptError(f"{split}.avgScore must be numeric")
    return float(score)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise LocalPromptError(f"run state not found: {path}")
    return _mapping(json.loads(path.read_text(encoding="utf-8")), "run state")


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    resolved = path.resolve(strict=False)
    resolved.parent.mkdir(parents=True, exist_ok=True)
    temporary = resolved.with_suffix(f"{resolved.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(resolved)


def _echo_json(payload: Mapping[str, Any]) -> None:
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


def _close_if_supported(value: object) -> None:
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()
