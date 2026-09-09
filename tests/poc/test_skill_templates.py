from __future__ import annotations

import hashlib
from pathlib import Path

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SKILL_TEMPLATE_ROOT = (
    REPOSITORY_ROOT
    / "src"
    / "foundry_opt"
    / "templates"
    / "skills"
    / "foundry-agent-optimizer"
)
TENZING_ROOT = SKILL_TEMPLATE_ROOT / "references" / "tenzing"
TENZING_SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "tenzing.sha256"
TENZING_SNAPSHOT_PREFIX = ".github/skills/foundry-agent-optimizer/references/tenzing/"
SKILL_PATH = SKILL_TEMPLATE_ROOT / "SKILL.md"
ADAPTER_PATH = SKILL_TEMPLATE_ROOT / "references" / "ADAPTER_MAPPING.md"
ATTRIBUTION_PATH = SKILL_TEMPLATE_ROOT / "references" / "TENZING_ATTRIBUTION.md"
TENZING_LOOP_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "TENZING_LOOP.md"
CANDIDATE_PROPOSAL_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "CANDIDATE_PROPOSAL.md"
LEARNING_RULES_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "LEARNING_RULES.md"
SCORE_AGGREGATION_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "SCORE_AGGREGATION.md"
DATA_ISOLATION_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "DATA_ISOLATION.md"
CONFIRMATION_GATE_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "CONFIRMATION_GATE.md"
RUN_CONTRACT_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "RUN_CONTRACT.md"
EXPERIMENT_STATE_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "EXPERIMENT_STATE.md"
EVALUATOR_EVIDENCE_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "EVALUATOR_EVIDENCE.md"
TARGET_PROVIDER_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "TARGET_PROVIDER_CONTRACT.md"
PARALLEL_ROUNDS_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "PARALLEL_ROUNDS.md"
RUNTIME_GAPS_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "RUNTIME_GAPS.md"
SCORECARD_PATH = SKILL_TEMPLATE_ROOT / "protocol" / "SCORECARD.md"
LOCAL_FOUNDRY_PATH = SKILL_TEMPLATE_ROOT / "profiles" / "LOCAL_FOUNDRY.md"
FORBIDDEN_STRINGS = (
    "luffy-test-agent-repo-002",
    "luechen-swedencentral-foundry",
    "recover-foundry-agent",
    "winner-verification",
    "foundry-target-lease-retire",
    "acceptance-candidates",
    "vendored runtime",
)


def _yaml_paths() -> list[Path]:
    paths = {
        *SKILL_TEMPLATE_ROOT.rglob("*.yml"),
        *SKILL_TEMPLATE_ROOT.rglob("*.yaml"),
    }
    return sorted(paths)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _is_tenzing_reference(path: Path) -> bool:
    if not path.is_relative_to(SKILL_TEMPLATE_ROOT):
        return False
    relative = path.relative_to(SKILL_TEMPLATE_ROOT)
    return relative.parts[:2] == ("references", "tenzing")


def _expected_tenzing_hashes() -> dict[str, str]:
    return {
        path: digest
        for digest, path in (
            line.split("  ", 1)
            for line in TENZING_SNAPSHOT_PATH.read_text(encoding="utf-8").splitlines()
            if line
        )
    }


def test_every_template_yaml_document_parses() -> None:
    for path in _yaml_paths():
        assert yaml.safe_load(_read(path)) is not None, path


def test_templates_omit_forbidden_production_artifacts_and_strings() -> None:
    assert not (SKILL_TEMPLATE_ROOT / "references" / "acceptance-candidates").exists()

    text_paths = [
        path for path in SKILL_TEMPLATE_ROOT.rglob("*.md") if not _is_tenzing_reference(path)
    ]
    combined = "\n".join(_read(path).lower() for path in text_paths)
    for forbidden in FORBIDDEN_STRINGS:
        assert forbidden.lower() not in combined


def test_tenzing_snapshot_hashes_are_exact() -> None:
    expected = _expected_tenzing_hashes()
    actual_paths = {
        f"{TENZING_SNAPSHOT_PREFIX}{path.relative_to(TENZING_ROOT).as_posix()}"
        for path in TENZING_ROOT.rglob("*")
        if path.is_file()
    }

    assert actual_paths == set(expected)
    for relative_path, digest in expected.items():
        relative = Path(relative_path)
        assert relative.parts[:3] == (
            ".github",
            "skills",
            "foundry-agent-optimizer",
        )
        actual = SKILL_TEMPLATE_ROOT / Path(*relative.parts[3:])
        assert hashlib.sha256(actual.read_bytes()).hexdigest() == digest


def test_skill_and_tenzing_attribution_stay_in_sync() -> None:
    skill = _read(SKILL_PATH)
    adapter = _read(ADAPTER_PATH)
    attribution = _read(ATTRIBUTION_PATH)

    assert ".foundry-opt/registry.yaml" in skill
    assert ".foundry-opt/bootstrap.lock.json" in skill
    assert "migration inputs only" in skill
    assert "OIDC only" in skill
    assert "original issue" in skill
    assert "validating dataset only for the provisional winner" in skill
    assert "early draft pull request" in skill
    assert "read-only reference material" in skill
    assert "standard Copilot" in skill
    assert "Issue-driven Tenzing loop" in skill
    assert "protocol/SCORE_AGGREGATION.md" in skill
    assert "protocol/DATA_ISOLATION.md" in skill
    assert "protocol/CONFIRMATION_GATE.md" in skill
    assert "protocol/RUN_CONTRACT.md" in skill
    assert "protocol/EXPERIMENT_STATE.md" in skill
    assert "protocol/EVALUATOR_EVIDENCE.md" in skill
    assert "protocol/SCORECARD.md" in skill
    assert "protocol/TARGET_PROVIDER_CONTRACT.md" in skill
    assert "protocol/PARALLEL_ROUNDS.md" in skill
    assert "protocol/RUNTIME_GAPS.md" in skill
    assert "final validating" in skill
    assert "`avgScore`; pass rate is diagnostic only" in skill
    assert "Current lineage boundary" in skill
    assert "profiles/LOCAL_FOUNDRY.md" in skill
    assert "local Tenzing observe/propose/learn loop" in skill
    assert "Do not submit a service-owned optimization job" in skill
    assert "An `azd` optimization provider" in skill
    assert "Bootstrap for first-time owners" in skill
    assert "Advanced and recovery" in skill
    assert "deployable winning patch" in skill

    assert "redacted, idempotent candidate update to the original issue" in adapter
    assert "whole round with its frozen incumbent" in adapter
    assert "early Copilot pull request" in adapter
    assert "Never:" in adapter
    assert "publish a regular version" in adapter
    assert "Idea lineage versus Git ancestry" in adapter

    assert "7300a83fc7378f0f1a401dbdf8ed28358ccf1732" in attribution
    assert "read-only snapshot" in attribution
    assert (SKILL_TEMPLATE_ROOT / "references" / "tenzing" / "LICENSE").is_file()
    assert (SKILL_TEMPLATE_ROOT / "references" / "tenzing" / "INIT.md").is_file()


def test_supported_tenzing_protocol_is_explicit_and_runtime_compatible() -> None:
    skill = _read(SKILL_PATH)
    loop = _read(TENZING_LOOP_PATH)
    proposal = _read(CANDIDATE_PROPOSAL_PATH)
    learning = _read(LEARNING_RULES_PATH)

    for name in (
        "protocol/TENZING_LOOP.md",
        "protocol/CANDIDATE_PROPOSAL.md",
        "protocol/LEARNING_RULES.md",
        "protocol/SCORE_AGGREGATION.md",
        "protocol/DATA_ISOLATION.md",
        "protocol/CONFIRMATION_GATE.md",
        "protocol/RUN_CONTRACT.md",
        "protocol/EXPERIMENT_STATE.md",
        "protocol/EVALUATOR_EVIDENCE.md",
        "protocol/SCORECARD.md",
        "protocol/TARGET_PROVIDER_CONTRACT.md",
        "protocol/PARALLEL_ROUNDS.md",
        "protocol/RUNTIME_GAPS.md",
    ):
        assert name in skill

    for action in (
        "handoff-candidate",
        "complete-candidate",
        "finish",
        "blocked",
        "terminal",
    ):
        assert action in loop

    assert "one execution parent" in proposal
    assert "Additional idea parents" in proposal
    assert "positive, negative, or contrast" in proposal
    assert "closed earlier rounds" in proposal
    assert "Do not pass unsupported" in proposal
    assert "Platform failure" in learning
    assert "Do not infer that the hypothesis itself was false" in learning
    assert "raw prompts, responses, traces, dataset rows" in learning


def test_score_aggregation_uses_complete_split_and_validation_headline() -> None:
    skill = _read(SKILL_PATH)
    loop = _read(TENZING_LOOP_PATH)
    profile = _read(LOCAL_FOUNDRY_PATH)
    score = _read(SCORE_AGGREGATION_PATH)

    for required in (
        "taskScore_i = sum(evaluatorScores_i) / evaluatorCount",
        "candidateAvgScore = sum(taskScore_i for i in 1..N) / N",
        "retrieve all output-item pages",
        "verify exactly `N` unique task results",
        "do not average only successful or returned items",
        "winnerValidatingAvgScore",
        "Pass rate",
    ):
        assert required in score

    assert "`avgScore`; pass rate is diagnostic only" in skill
    assert "winner's final validating" in loop
    assert "complete validating task count" in profile


def test_final_scorecard_requires_detailed_english_mutation_evidence() -> None:
    skill = _read(SKILL_PATH)
    scorecard = _read(SCORECARD_PATH)
    state = _read(EXPERIMENT_STATE_PATH)
    score = _read(SCORE_AGGREGATION_PATH)

    for required in (
        "Write every canonical scorecard artifact in English only",
        "Required mutation idea ledger",
        "Hypothesis",
        "Concrete change",
        "Idea parents",
        "Exact contribution",
        "Expected mechanism",
        "Observed outcome",
        "Required search scorecard",
        "Required confirmation scorecard",
        "Required final validation scorecard",
        "solid execution-parent edges",
        "dashed idea-parent edges",
        "incidents and exclusions",
        "whether the winner was applied to the target source",
        "`scorecard.json`",
        "`results.tsv`",
    ):
        assert required in scorecard

    assert "protocol/SCORECARD.md" in skill
    assert "English-only detailed scorecard" in skill
    assert "A short main-change label never replaces" in skill
    assert "SCORECARD.md" in state
    assert "Write the canonical scorecard and all of its projections in English only" in score


def test_confirmation_gate_requires_two_split_improvements() -> None:
    skill = _read(SKILL_PATH)
    loop = _read(TENZING_LOOP_PATH)
    profile = _read(LOCAL_FOUNDRY_PATH)
    gate = _read(CONFIRMATION_GATE_PATH)

    for required in (
        "training/search",
        "confirmation",
        "final validation",
        "candidateTrainingAvgScore <= currentBestTrainingAvgScore",
        "candidateConfirmationAvgScore > currentBestConfirmationAvgScore",
        "Compare with the current best, not only the original baseline",
        "Only a candidate promoted on both training and confirmation resets",
        "confirmation task-level evidence",
    ):
        assert required in gate

    assert "confirmation `avgScore` strictly improve" in skill
    assert "Do not promote it from training" in loop
    assert "confirmation only when training `avgScore` strictly exceeds" in profile


def test_hidden_evaluation_data_cannot_influence_mutations() -> None:
    skill = _read(SKILL_PATH)
    isolation = _read(DATA_ISOLATION_PATH)
    gate = _read(CONFIRMATION_GATE_PATH)
    contract = _read(RUN_CONTRACT_PATH)
    profile = _read(LOCAL_FOUNDRY_PATH)
    state = _read(EXPERIMENT_STATE_PATH)
    scorecard = _read(SCORECARD_PATH)

    for required in (
        "expose only the training/search projection",
        "Do not search, print, summarize, sample, or run ad hoc analysis",
        "Confirmation remains hidden across rounds",
        "Final validation is one-way",
        "Treat a hidden split as contaminated",
        "never-exposed reserved rows",
        "winner-freeze receipt",
    ):
        assert required in isolation

    assert "protocol/DATA_ISOLATION.md" in skill
    assert "aggregate-only across all rounds" in skill
    assert "Follow `DATA_ISOLATION.md`" in gate
    assert "mutation-loop visibility allowlist" in contract
    assert "Do not inspect the original combined dataset" in profile
    assert "contamination and replacement receipts" in state
    assert "data-exposure incident" in scorecard


def test_extended_protocol_contracts_cover_runtime_gaps_and_adapters() -> None:
    proposal = _read(CANDIDATE_PROPOSAL_PATH)
    run_contract = _read(RUN_CONTRACT_PATH)
    state = _read(EXPERIMENT_STATE_PATH)
    evidence = _read(EVALUATOR_EVIDENCE_PATH)
    target = _read(TARGET_PROVIDER_PATH)
    parallel = _read(PARALLEL_ROUNDS_PATH)
    gaps = _read(RUNTIME_GAPS_PATH)
    adapter = _read(ADAPTER_PATH)

    assert "Primary mutation target" in proposal
    assert "one main surface" in proposal
    assert "Hash the normalized contract" in run_contract
    assert "one configured worktree root outside" in run_contract
    assert "execution mode: `sequential` or `parallel_rounds`" in run_contract
    assert "<state-root>/<run-id>/worktrees/" in state
    assert "multiple idea parents with evidence role and exact contribution" in state
    assert "same-round, forward, and cyclic parents" in state
    assert "Score-only evaluators" in evidence
    assert "canonical score" in evidence
    assert "The target does not:" in target
    assert "Tenzing owns:" in target
    assert "Providers must declare" in target
    assert "maximum concurrent drafts, deployments, and evaluations" in target
    assert "A completion-order winner is invalid" in parallel
    assert "highest confirmation `avgScore`" in parallel
    assert "positive`, `negative`, or `contrast`" in parallel
    assert "plateau counts closed rounds" in parallel
    assert "confirmation-gate controller states" in gaps
    assert "deterministic parallel-round scheduler" in gaps
    assert "do not describe skill instructions as runtime enforcement" in gaps
    assert "Confirm a training improvement" in adapter
    assert "Adapter boundaries" in adapter


def test_local_foundry_profile_is_provider_neutral_and_assurance_bound() -> None:
    profile = _read(LOCAL_FOUNDRY_PATH)
    normalized = " ".join(profile.split())

    for required in (
        "Local Tenzing loop with a Foundry target",
        "aligned active standard baseline",
        "Prompt agent",
        "Hosted agent",
        "prompt-definition hash",
        "Prompt candidates do not require Docker",
        "The local coding agent owns",
        "The local `foundry-opt` adapter owns",
        "Evaluate the existing aligned regular baseline version",
        "Create an owned draft",
        "require the returned version to start with `draft-`",
        "normal create/update deployment API",
        "Run the final validating dataset only for the provisional winner",
        "Final winner gate",
        "Delete every operation-owned draft",
        "Target-provider binding",
        "explicit editable paths and mutation dimensions",
        "must not assume an agent name",
        "Never fall back to regular versions",
    ):
        assert required in normalized

    assert "capabilities discovered during preflight" in normalized


def test_skill_bootstrap_owner_flow_uses_owner_commands_and_terms() -> None:
    skill = _read(SKILL_PATH)
    normalized = " ".join(skill.split())

    for required in (
        "foundry-opt bootstrap review discovery",
        "foundry-opt bootstrap review plan",
        "foundry-opt bootstrap connect plan",
        "foundry-opt bootstrap resources",
        "Do not paste raw JSON into owner-facing updates",
        "registered` — the agent is listed in `.foundry-opt/registry.yaml`",
        "`enabled` — the reviewed registry/profile intends the agent to participate",
        "`verified` — reviewed evidence or receipt-backed verification is attached",
        "`deployable` — policy currently allows exact-source deployment",
        "combined connection approval",
        "issue-supplied Foundry",
    ):
        assert required in normalized
