from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from foundry_opt import runtime_provenance
from foundry_opt.repository_contracts import RepositoryRegistry
from foundry_opt.runtime_provenance import resolve_optimizer_distribution, verify_runtime_checkout


def _git(root: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *arguments],
        check=True,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "GIT_CONFIG_COUNT": "0",
            "GIT_AUTHOR_NAME": "Test",
            "GIT_AUTHOR_EMAIL": "test@example.invalid",
            "GIT_COMMITTER_NAME": "Test",
            "GIT_COMMITTER_EMAIL": "test@example.invalid",
        },
    ).stdout.strip()


def create_main_session(
    customer: Path, monkeypatch: pytest.MonkeyPatch, *, package_path: str = "."
) -> tuple[RepositoryRegistry, dict[str, str]]:
    runtime = customer.parent / "shared-runtime"
    runtime.mkdir()
    _git(runtime, "init", "-b", "main")
    _git(runtime, "config", "core.autocrlf", "false")
    _git(runtime, "remote", "add", "origin", "https://github.com/example/runtime.git")
    package = runtime / package_path
    source = package / "src" / "foundry_opt" / "runtime_provenance.py"
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 'main A'\n", encoding="utf-8")
    (package / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    skill = runtime / "plugins" / "optimizer"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Optimizer\n", encoding="utf-8")
    _git(runtime, "add", ".")
    _git(runtime, "commit", "-m", "runtime A")
    sha = _git(runtime, "rev-parse", "HEAD")
    _git(runtime, "update-ref", "refs/remotes/origin/main", sha)
    _git(runtime, "checkout", "--detach", sha)

    registry_path = customer / ".foundry-opt" / "registry.yaml"
    if registry_path.is_file():
        payload = yaml.safe_load(registry_path.read_text(encoding="utf-8"))
    else:
        payload = {
            "schema_version": 2,
            "github": {
                "optimizer_environment": "copilot",
                "deployment_environment": "production",
                "client_id_variable": "AZURE_OPTIMIZER_CLIENT_ID",
            },
            "identity": {"kind": "unresolved_migration"},
            "agents": [],
        }
        registry_path.parent.mkdir(parents=True)
        _git(customer, "init")
        _git(customer, "config", "core.autocrlf", "false")
    payload["distribution"] = {
        "repository": "https://github.com/example/runtime",
        "channel": "reviewed",
        "pin": "a" * 40,
        "package_path": package_path,
        "uv_lock_sha256": "b" * 64,
        "optimizer_skill_path": "plugins/optimizer",
    }
    payload["github"]["copilot_runtime"] = "main"
    registry_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    _git(customer, "add", ".foundry-opt/registry.yaml")
    _git(customer, "commit", "-m", "enable main runtime")
    monkeypatch.setattr(runtime_provenance, "_loaded_runtime_source", lambda: source)
    return RepositoryRegistry.from_document(payload), {
        "GITHUB_ACTIONS": "true",
        "GITHUB_EVENT_NAME": "dynamic",
        "GITHUB_WORKSPACE": str(customer),
        "FOUNDRY_OPT_SHARED_ROOT": str(runtime),
        "FOUNDRY_OPT_PACKAGE_ROOT": str(package),
        "FOUNDRY_OPT_SKILL_SOURCE": str(skill),
        "FOUNDRY_OPT_RUNTIME_SHA": sha,
    }


@pytest.fixture
def main_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    customer = tmp_path / "customer"
    customer.mkdir()
    return create_main_session(customer, monkeypatch)


def test_main_sessions_resolve_once_without_mutating_registry_or_fetching(
    main_session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, environment = main_session
    customer = Path(environment["GITHUB_WORKSPACE"])
    registry_path = customer / ".foundry-opt" / "registry.yaml"
    original = registry_path.read_bytes()
    runtime = Path(environment["FOUNDRY_OPT_SHARED_ROOT"])
    first = resolve_optimizer_distribution(registry, environment=environment)
    assert first.pin == environment["FOUNDRY_OPT_RUNTIME_SHA"]
    assert first.pin != registry.distribution.pin
    assert first.uv_lock_sha256 == hashlib.sha256((runtime / "uv.lock").read_bytes()).hexdigest()
    checkout = verify_runtime_checkout(registry, runtime, environment=environment)
    assert checkout.commit == first.pin
    assert checkout.package_root == runtime

    (runtime / "uv.lock").write_text("version = 2\n", encoding="utf-8")
    _git(runtime, "add", ".")
    _git(runtime, "commit", "-m", "runtime B")
    second_sha = _git(runtime, "rev-parse", "HEAD")
    _git(runtime, "update-ref", "refs/remotes/origin/main", second_sha)
    second_environment = {**environment, "FOUNDRY_OPT_RUNTIME_SHA": second_sha}
    commands: list[tuple[str, ...]] = []
    original_git = runtime_provenance._git_bytes

    def local_git(root: Path, *arguments: str) -> bytes:
        commands.append(arguments)
        assert not {"fetch", "pull", "ls-remote"} & set(arguments)
        return original_git(root, *arguments)

    monkeypatch.setattr(runtime_provenance, "_git_bytes", local_git)
    second = resolve_optimizer_distribution(registry, environment=second_environment)
    assert second.pin == second_sha != first.pin
    assert second.uv_lock_sha256 != first.uv_lock_sha256
    assert resolve_optimizer_distribution(registry, environment=second_environment) == second
    assert commands
    assert registry_path.read_bytes() == original
    assert registry.distribution.pin == "a" * 40
    with pytest.raises(ValueError, match="HEAD"):
        resolve_optimizer_distribution(registry, environment=environment)


@pytest.mark.parametrize(
    "context",
    [{}, {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "push"},
     {"GITHUB_ACTIONS": "true", "GITHUB_EVENT_NAME": "workflow_dispatch"},
     {"GITHUB_EVENT_NAME": "dynamic"}],
)
def test_non_copilot_context_keeps_recorded_distribution(main_session, context) -> None:
    registry, _ = main_session
    assert resolve_optimizer_distribution(registry, environment=context) is registry.distribution


def test_pinned_registry_cannot_opt_in_via_environment(main_session) -> None:
    registry, environment = main_session
    pinned = registry.model_copy(update={
        "github": registry.github.model_copy(update={"copilot_runtime": "pinned"}),
    })
    assert resolve_optimizer_distribution(pinned, environment=environment) is pinned.distribution
    assert pinned.github.copilot_runtime == "pinned"


def test_copilot_runtime_schema_defaults_to_pinned_and_rejects_unknown_mode(main_session) -> None:
    registry, _ = main_session
    payload = registry.model_dump(mode="json")
    del payload["github"]["copilot_runtime"]
    assert RepositoryRegistry.from_document(payload).github.copilot_runtime == "pinned"
    payload["github"]["copilot_runtime"] = "arbitrary-branch"
    with pytest.raises(ValueError, match="copilot_runtime"):
        RepositoryRegistry.from_document(payload)


def test_pinned_checkout_verification_keeps_original_contract(main_session) -> None:
    registry, environment = main_session
    runtime = Path(environment["FOUNDRY_OPT_SHARED_ROOT"])
    distribution = registry.distribution.model_copy(update={
        "pin": environment["FOUNDRY_OPT_RUNTIME_SHA"],
        "uv_lock_sha256": hashlib.sha256((runtime / "uv.lock").read_bytes()).hexdigest(),
    })
    registry = registry.model_copy(update={"distribution": distribution})
    checkout = verify_runtime_checkout(registry, runtime, environment={})
    assert checkout.commit == distribution.pin
    assert checkout.uv_lock_sha256 == distribution.uv_lock_sha256
    with pytest.raises(ValueError, match="commit"):
        verify_runtime_checkout(
            registry.model_copy(update={
                "distribution": distribution.model_copy(update={"pin": "a" * 40}),
            }),
            runtime, environment={},
        )


def test_uncommitted_main_policy_cannot_enable_session_runtime(main_session) -> None:
    registry, environment = main_session
    customer = Path(environment["GITHUB_WORKSPACE"])
    registry_path = customer / ".foundry-opt" / "registry.yaml"
    main_bytes = registry_path.read_bytes()
    payload = yaml.safe_load(main_bytes)
    payload["github"]["copilot_runtime"] = "pinned"
    registry_path.write_text(yaml.safe_dump(payload), encoding="utf-8")
    _git(customer, "add", ".")
    _git(customer, "commit", "-m", "committed policy is pinned")
    registry_path.write_bytes(main_bytes)
    with pytest.raises(ValueError, match="committed registry"):
        resolve_optimizer_distribution(registry, environment=environment)


@pytest.mark.parametrize(
    "origin", ["https://github.com/example/runtime", "git@github.com:example/runtime.git"],
)
def test_main_origin_accepts_conventional_normalization(main_session, origin: str) -> None:
    registry, environment = main_session
    _git(Path(environment["FOUNDRY_OPT_SHARED_ROOT"]), "remote", "set-url", "origin", origin)
    assert resolve_optimizer_distribution(registry, environment=environment).pin == environment["FOUNDRY_OPT_RUNTIME_SHA"]


def test_main_rejects_different_explicit_checkout(main_session) -> None:
    registry, environment = main_session
    with pytest.raises(ValueError, match="selected main runtime checkout"):
        verify_runtime_checkout(
            registry, Path(environment["GITHUB_WORKSPACE"]), environment=environment,
        )


@pytest.mark.parametrize(
    "name",
    ["FOUNDRY_OPT_RUNTIME_SHA", "FOUNDRY_OPT_SHARED_ROOT", "FOUNDRY_OPT_PACKAGE_ROOT",
     "FOUNDRY_OPT_SKILL_SOURCE", "GITHUB_WORKSPACE"],
)
def test_main_requires_complete_session_context(main_session, name: str) -> None:
    registry, environment = main_session
    del environment[name]
    with pytest.raises(ValueError, match=name):
        resolve_optimizer_distribution(registry, environment=environment)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ("sha", "exact commit"),
        ("head", "HEAD"),
        ("branch", "origin/main"),
        ("attached", "detached"),
        ("origin", "origin"),
        ("lock", "contents"),
        ("source", "contents"),
        ("skill-contents", "contents"),
        ("skill-missing", "SKILL.md"),
        ("package-path", "PACKAGE_ROOT"),
        ("skill-path", "SKILL_SOURCE"),
        ("untracked-source", "untracked"),
        ("committed-registry", "committed registry"),
    ],
)
def test_main_rejects_invalid_provenance(main_session, mutation: str, message: str) -> None:
    registry, environment = main_session
    runtime = Path(environment["FOUNDRY_OPT_SHARED_ROOT"])
    if mutation == "sha":
        environment["FOUNDRY_OPT_RUNTIME_SHA"] = "main"
    elif mutation == "head":
        environment["FOUNDRY_OPT_RUNTIME_SHA"] = "c" * 40
    elif mutation == "branch":
        _git(runtime, "update-ref", "-d", "refs/remotes/origin/main")
    elif mutation == "attached":
        _git(runtime, "checkout", "main")
    elif mutation == "origin":
        _git(runtime, "remote", "set-url", "origin", "https://github.com/attacker/runtime")
    elif mutation in {"lock", "source", "skill-contents"}:
        relative = {
            "lock": "uv.lock", "source": "src/foundry_opt/runtime_provenance.py",
            "skill-contents": "plugins/optimizer/SKILL.md",
        }[mutation]
        (runtime / relative).write_text("tampered\n", encoding="utf-8")
        _git(runtime, "update-index", "--assume-unchanged", relative)
    elif mutation == "skill-missing":
        (runtime / "plugins" / "optimizer" / "SKILL.md").unlink()
    elif mutation == "package-path":
        environment["FOUNDRY_OPT_PACKAGE_ROOT"] = str(runtime.parent)
    elif mutation == "skill-path":
        environment["FOUNDRY_OPT_SKILL_SOURCE"] = str(runtime)
    elif mutation == "untracked-source":
        (runtime / "src" / "foundry_opt" / "injected.py").write_text("VALUE = 1\n")
    else:
        registry = registry.model_copy(update={
            "distribution": registry.distribution.model_copy(update={"repository": "https://github.com/attacker/runtime"}),
        })
    with pytest.raises(ValueError, match=message):
        resolve_optimizer_distribution(registry, environment=environment)


def test_main_rejects_runtime_loaded_from_other_checkout(
    main_session, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry, environment = main_session
    monkeypatch.setattr(runtime_provenance, "_loaded_runtime_source", lambda: Path(__file__))
    with pytest.raises(ValueError, match="loaded runtime"):
        resolve_optimizer_distribution(registry, environment=environment)


def test_main_supports_approved_nested_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    customer = tmp_path / "customer"
    customer.mkdir()
    registry, environment = create_main_session(customer, monkeypatch, package_path="packages/runtime")
    checkout = verify_runtime_checkout(
        registry, Path(environment["FOUNDRY_OPT_SHARED_ROOT"]), environment=environment,
    )
    assert checkout.package_root == Path(environment["FOUNDRY_OPT_PACKAGE_ROOT"])


def test_main_rejects_checkout_inside_customer_worktree(main_session) -> None:
    registry, environment = main_session
    customer = Path(environment["GITHUB_WORKSPACE"])
    nested = customer / "shared-runtime"
    Path(environment["FOUNDRY_OPT_SHARED_ROOT"]).rename(nested)
    environment["FOUNDRY_OPT_SHARED_ROOT"] = str(nested)
    with pytest.raises(ValueError, match="outside"):
        resolve_optimizer_distribution(registry, environment=environment)
