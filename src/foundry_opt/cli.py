from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx
import typer
from pydantic import ValidationError

from foundry_opt import __version__
from foundry_opt.poc import runtime as poc_runtime
from foundry_opt.poc.auth import (
    AuthError,
    build_client_assertion_credential,
    detect_github_actions_oidc,
)
from foundry_opt.poc.bootstrap import (
    read_bootstrap_receipt,
    verify_shared_checkout,
    write_bootstrap_receipt,
    load_shared_pin,
)
from foundry_opt.poc.candidate import CandidateError
from foundry_opt.poc.config import (
    AgentMetadata,
    POCConfigurationError,
    RepositoryPolicy,
    OptimizeIssueRequest,
    apply_issue_request,
    load_agent_metadata,
    load_repository_policy,
)
from foundry_opt.poc.controller import ControllerError, OptimizeJobController
from foundry_opt.poc.deploy import (
    DEFAULT_DEPLOYMENT_ENVIRONMENT,
    DEPLOYMENT_ROOT_ENV,
    DeploymentError,
    DeploymentGuardrailError,
    DeploymentPostPublishError,
    DeploymentSupersededError,
    load_deployment_settings,
    publish_deployment,
    run_deployment_preflight,
)
from foundry_opt.poc.foundry import (
    AzureProjectsEvaluationBackend,
    CleanupError,
    ContractError,
    DraftUnavailableError,
    FoundryPocClient,
    RouteDriftError,
    RouteFingerprint,
    RouteModeError,
    ServiceError,
)
from foundry_opt.poc.github import (
    BrokerRemoteError,
    BrokerUnavailableError,
    IssueBinding,
    PullRequestBinding,
    RepositoryIdentity,
    UnixSocketBrokerClient,
    UnixSocketBrokerServer,
)
from foundry_opt.poc.issue import IssueDocumentError, parse_issue_body
from foundry_opt.poc.runtime import (
    BOOTSTRAP_RECEIPT_ENV,
    BROKER_SOCKET_ENV,
    DEADLINE_SECONDS_ENV,
    DEFAULT_DEADLINE_SECONDS,
    RUNTIME_SIDECAR_FILENAME,
    STATE_ROOT_ENV,
    ControllerFoundryOperations,
    RuntimeIntegrationError,
    RuntimePaths,
    RuntimeSettings,
    RuntimeSidecarStore,
    build_hosted_definition,
    build_job_identity,
    build_runtime_controller,
    capture_route_fingerprint,
    load_runtime_paths,
    load_runtime_settings,
)
from foundry_opt.poc.state import (
    JobIdentity,
    JobState,
    JobStateStore,
    STATE_FILENAME,
    StateError,
)
from foundry_opt.poc.source import SourcePackagingError


app = typer.Typer(no_args_is_help=True, pretty_exceptions_enable=False)
bootstrap_app = typer.Typer(no_args_is_help=True)
broker_app = typer.Typer(no_args_is_help=True)
issue_app = typer.Typer(no_args_is_help=True)
job_app = typer.Typer(no_args_is_help=True)
acceptance_app = typer.Typer(no_args_is_help=True)
deploy_app = typer.Typer(no_args_is_help=True)
app.add_typer(bootstrap_app, name="bootstrap")
app.add_typer(broker_app, name="broker")
app.add_typer(issue_app, name="issue")
app.add_typer(job_app, name="job")
app.add_typer(acceptance_app, name="acceptance")
app.add_typer(deploy_app, name="deploy")

_PIN_PATH = Path(".github/foundry-opt.lock.yml")
_POLICY_PATH = Path(".github/foundry-optimizer.yaml")
_METADATA_PATH = Path(".foundry/agent-metadata.yaml")
_MAX_EVENT_BYTES = 1024 * 1024
_MAX_TOKEN_BYTES = 16 * 1024
_MAX_REQUEST_BYTES = 128 * 1024
_ISSUE_REQUEST_FILENAME = "optimize-job-poc-issue-request.json"
_ACCEPTANCE_STATE_NAMESPACE = ".acceptance-smoke"
_GITHUB_EVENT_PATH_ENV = "GITHUB_EVENT_PATH"
_GITHUB_WORKSPACE_ENV = "GITHUB_WORKSPACE"
_GITHUB_HEAD_REF_ENV = "GITHUB_HEAD_REF"
_GITHUB_REF_NAME_ENV = "GITHUB_REF_NAME"
_GITHUB_BINDING_ENV = "FOUNDRY_OPT_GITHUB_BINDING"
_PULL_REQUEST_NUMBER_ENV = "FOUNDRY_OPT_PULL_REQUEST_NUMBER"
_PULL_REQUEST_BASE_BRANCH_ENV = "FOUNDRY_OPT_PULL_REQUEST_BASE_BRANCH"
_PULL_REQUEST_HEAD_BRANCH_ENV = "FOUNDRY_OPT_PULL_REQUEST_HEAD_BRANCH"
_PULL_REQUEST_EXPECTED_AUTHOR_ENV = "FOUNDRY_OPT_PULL_REQUEST_EXPECTED_AUTHOR"
_TOKEN_SHAPE_PATTERN = re.compile(
    r"(?:gh[pousr]_[A-Za-z0-9_]{8,}|ghs-[A-Za-z0-9_-]{8,}|github_pat_[A-Za-z0-9_]{20,})"
)
_CONTROL_PATTERN = re.compile(r"[\x00-\x1f\x7f]+")
_LINKED_ISSUE_REFERENCE_PATTERN = re.compile(
    r"(?i)\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+"
    r"(?:(?P<owner>[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})?)/"
    r"(?P<name>[A-Za-z0-9._-]{1,100}))?#(?P<number>[1-9][0-9]*)\b"
)

_JOB_COMMAND_ERRORS = (
    AuthError,
    CandidateError,
    CleanupError,
    ContractError,
    ControllerError,
    DeploymentError,
    DraftUnavailableError,
    IssueDocumentError,
    POCConfigurationError,
    RouteDriftError,
    RouteModeError,
    RuntimeIntegrationError,
    ServiceError,
    StateError,
    SourcePackagingError,
    ValidationError,
    typer.BadParameter,
)


@dataclass(frozen=True, slots=True)
class _LoadedBinding:
    path: Path
    issue: IssueBinding
    pull_request: PullRequestBinding | None


@dataclass(frozen=True, slots=True)
class _IssueStartContext:
    issue_number: int
    job_id: str
    request: OptimizeIssueRequest
    issue_binding: IssueBinding | None
    binding: _LoadedBinding | None


@dataclass(frozen=True, slots=True)
class _JobRuntimeContext:
    repository_root: Path
    paths: RuntimePaths
    settings: RuntimeSettings
    request: OptimizeIssueRequest
    request_digest_sha256: str
    state: JobState | None
    controller: OptimizeJobController | None
    binding: _LoadedBinding | None
    identity: JobIdentity


@dataclass(frozen=True, slots=True)
class _PullRequestBindingInputs:
    pull_request_number: int | None
    base_branch: str | None
    head_branch: str | None
    expected_author_login: str | None

    @property
    def any_pr_fields(self) -> bool:
        return any(
            value is not None
            for value in (
                self.pull_request_number,
                self.base_branch,
                self.head_branch,
                self.expected_author_login,
            )
        )


@dataclass(frozen=True, slots=True)
class _AcceptanceFoundryHandle:
    operations: ControllerFoundryOperations
    close: Callable[[], None]


@app.command()
def version() -> None:
    """Print the package version."""

    typer.echo(__version__)


@app.command("validate-config")
def validate_config(
    repository: Path = typer.Option(Path("."), "--repository"),
    shared_checkout: Path | None = typer.Option(None, "--shared-checkout"),
) -> None:
    """Validate the target repository contract and optional shared checkout."""

    root = _repository_root(repository)
    pin = load_shared_pin(root / _PIN_PATH)
    metadata = load_agent_metadata(root / _METADATA_PATH)
    policy = load_repository_policy(
        root / _POLICY_PATH,
        metadata_path=root / _METADATA_PATH,
    )
    _verify_repository_identity(root, metadata.repository_identity)
    receipt = (
        verify_shared_checkout(pin, shared_checkout)
        if shared_checkout is not None
        else None
    )
    _echo_json(
        {
            "agent_name": metadata.agent_name,
            "allowed_models": list(policy.allowed_models),
            "metadata_path": policy.metadata_path,
            "repository": metadata.repository_identity,
            "shared_commit": pin.commit,
            "shared_receipt_sha256": (
                receipt.receipt_sha256 if receipt is not None else None
            ),
            "source_root": policy.source_root,
            "status": "valid",
        }
    )


@app.command()
def preflight(
    repository: Path = typer.Option(Path("."), "--repository"),
    offline: bool = typer.Option(False, "--offline"),
) -> None:
    """Verify bootstrap, policy, metadata, OIDC, and broker prerequisites."""

    root = _repository_root(repository)
    pin = load_shared_pin(root / _PIN_PATH)
    metadata = load_agent_metadata(root / _METADATA_PATH)
    policy = load_repository_policy(
        root / _POLICY_PATH,
        metadata_path=root / _METADATA_PATH,
    )
    _verify_repository_identity(root, metadata.repository_identity)

    receipt_path = _required_environment_path(
        "FOUNDRY_OPT_BOOTSTRAP_RECEIPT",
        offline=offline,
    )
    broker_socket = _required_environment_path(
        "FOUNDRY_OPT_BROKER_SOCKET",
        offline=offline,
    )
    state_root = _required_environment_path(
        "FOUNDRY_OPT_STATE_ROOT",
        offline=offline,
    )
    receipt = read_bootstrap_receipt(receipt_path) if receipt_path else None
    if receipt is not None and (
        receipt.repository != pin.repository_url
        or receipt.commit != pin.commit
        or receipt.lock_sha256 != pin.uv_lock_sha256
    ):
        raise typer.BadParameter("bootstrap receipt does not match the shared pin")
    if not offline and not detect_github_actions_oidc():
        raise typer.BadParameter("GitHub Actions OIDC is unavailable")
    if broker_socket is not None and not broker_socket.exists():
        raise typer.BadParameter("GitHub issue broker socket is unavailable")
    if state_root is not None and (
        not state_root.exists() or not state_root.is_dir()
    ):
        raise typer.BadParameter("optimize-job state root is unavailable")
    route_fingerprint = None
    if not offline:
        runtime_paths = load_runtime_paths(root, environment=os.environ)
        runtime_settings = load_runtime_settings(
            runtime_paths,
            environment=os.environ,
        )
        route_fingerprint = capture_route_fingerprint(
            repository=root,
            environment=os.environ,
            paths=runtime_paths,
            settings=runtime_settings,
        )

    _echo_json(
        {
            "agent_name": metadata.agent_name,
            "broker_socket": str(broker_socket) if broker_socket else None,
            "candidate_range": [policy.min_candidates, policy.max_candidates],
            "foundry_route_fingerprint": (
                None if route_fingerprint is None else route_fingerprint.sha256
            ),
            "oidc": not offline,
            "repository": metadata.repository_identity,
            "shared_commit": pin.commit,
            "status": "ready",
        }
    )


@deploy_app.command("preflight")
def deploy_preflight(
    repository: Path = typer.Option(Path("."), "--repository"),
    release_commit: str | None = typer.Option(None, "--release-commit"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    artifact_root: Path | None = typer.Option(
        None,
        "--artifact-root",
        envvar=DEPLOYMENT_ROOT_ENV,
    ),
    deployment_environment: str = typer.Option(
        DEFAULT_DEPLOYMENT_ENVIRONMENT,
        "--environment",
    ),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Validate the exact merge commit and service-managed latest deployment mode."""

    try:
        settings = load_deployment_settings(
            repository,
            environment=os.environ,
            release_commit=release_commit,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            artifact_root=artifact_root,
            deadline_seconds=deadline_seconds,
            deployment_environment=deployment_environment,
        )
        result = run_deployment_preflight(settings, environment=os.environ)
        _echo_json(result.model_dump(mode="json"))
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@deploy_app.command("publish")
def deploy_publish(
    repository: Path = typer.Option(Path("."), "--repository"),
    release_commit: str | None = typer.Option(None, "--release-commit"),
    receipt: Path | None = typer.Option(None, "--receipt"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    artifact_root: Path | None = typer.Option(
        None,
        "--artifact-root",
        envvar=DEPLOYMENT_ROOT_ENV,
    ),
    deployment_environment: str = typer.Option(
        DEFAULT_DEPLOYMENT_ENVIRONMENT,
        "--environment",
    ),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Validate one exact source ZIP as a draft, then publish a regular version."""

    try:
        settings = load_deployment_settings(
            repository,
            environment=os.environ,
            release_commit=release_commit,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            artifact_root=artifact_root,
            deadline_seconds=deadline_seconds,
            deployment_environment=deployment_environment,
        )
        result = publish_deployment(settings, environment=os.environ)
        payload = result.model_dump(mode="json")
        if receipt is not None:
            _write_json_document(receipt, payload)
        _echo_json(payload)
    except DeploymentGuardrailError as error:
        _echo_json(
            {
                "error": _redact_text(str(error)) or "deployment guardrails failed",
                "evaluation_link": error.evaluation_link,
                "guardrails": [
                    item.model_dump(mode="json")
                    for item in error.guardrails
                ],
                "status": "blocked",
            }
        )
        raise typer.Exit(code=2)
    except DeploymentSupersededError as error:
        payload = {
            "current_main_commit": error.current_main_commit,
            "published": False,
            "release_commit": error.release_commit,
            "status": "superseded",
        }
        if receipt is not None:
            _write_json_document(receipt, payload)
        _echo_json(payload)
    except DeploymentPostPublishError as error:
        _echo_json(
            {
                "error": _redact_text(str(error)) or "post-publish verification failed",
                "published_version": error.reference.version,
                "status": "blocked",
            }
        )
        raise typer.Exit(code=2)
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@bootstrap_app.command("verify")
def bootstrap_verify(
    pin_path: Path = typer.Option(..., "--pin"),
    checkout: Path = typer.Option(..., "--checkout"),
    receipt: Path = typer.Option(..., "--receipt"),
) -> None:
    """Verify an exact shared checkout and persist its bootstrap receipt."""

    pin = load_shared_pin(pin_path)
    verified = verify_shared_checkout(pin, checkout)
    write_bootstrap_receipt(receipt, verified)
    _echo_json(
        {
            "commit": verified.commit,
            "receipt": str(receipt.resolve()),
            "receipt_sha256": verified.receipt_sha256,
            "repository": verified.repository,
            "status": "verified",
        }
    )


@issue_app.command("parse")
def issue_parse(
    body_file: Path = typer.Option(..., "--body-file"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
) -> None:
    """Parse an optimize issue and optionally prove it narrows policy."""

    parsed = parse_issue_body(body_file.read_text(encoding="utf-8"))
    request = OptimizeIssueRequest(
        goal=parsed.goal,
        observed_failures=(parsed.observed_failures,),
        constraints=(parsed.constraints,),
        candidate_budget=parsed.candidate_budget,
        model_subset=parsed.candidate_models or None,
        editable_scope_subset=parsed.editable_scope or None,
    )
    narrowed = None
    if policy_path is not None:
        policy = load_repository_policy(
            policy_path,
            metadata_path=metadata_path,
        )
        narrowed = apply_issue_request(policy, request)
    _echo_json(
        {
            "request": request.model_dump(mode="json"),
            "narrowed_policy": (
                narrowed.model_dump(mode="json") if narrowed is not None else None
            ),
            "status": "valid",
        }
    )


@broker_app.command("launch")
def broker_launch(
    event: Path = typer.Option(..., "--event"),
    binding: Path = typer.Option(..., "--binding"),
    socket: Path = typer.Option(..., "--socket"),
    head_ref: str | None = typer.Option(None, "--head-ref"),
    ref_name: str | None = typer.Option(None, "--ref-name"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
) -> None:
    """Launch the setup-token broker as a detached process."""

    if not token_stdin:
        raise typer.BadParameter("--token-stdin is required")
    token = sys.stdin.read(_MAX_TOKEN_BYTES + 1)
    if not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise typer.BadParameter("setup token is unavailable or oversized")
    token = token.strip()
    if not token or "\r" in token or "\n" in token:
        raise typer.BadParameter("setup token is malformed")

    issue_binding = _issue_binding_from_event_context(
        event,
        token=token,
        head_ref=head_ref,
        ref_name=ref_name,
    )
    _write_binding(binding, issue=issue_binding, pull_request=None)
    socket = socket.resolve()
    socket.parent.mkdir(parents=True, exist_ok=True)
    if socket.exists():
        socket.unlink()

    command = [
        sys.executable,
        "-m",
        "foundry_opt",
        "broker",
        "serve",
        "--binding",
        str(binding.resolve()),
        "--socket",
        str(socket),
        "--token-stdin",
    ]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
        encoding="utf-8",
    )
    assert process.stdin is not None
    process.stdin.write(token)
    process.stdin.close()
    token = ""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if socket.exists():
            _echo_json(
                {
                    "binding": str(binding.resolve()),
                    "pid": process.pid,
                    "socket": str(socket),
                    "status": "listening",
                }
            )
            return
        if process.poll() is not None:
            raise typer.BadParameter("GitHub issue broker failed to start")
        time.sleep(0.05)
    raise typer.BadParameter("GitHub issue broker did not become ready")


@broker_app.command("serve", hidden=True)
def broker_serve(
    binding: Path = typer.Option(..., "--binding"),
    socket: Path = typer.Option(..., "--socket"),
    token_stdin: bool = typer.Option(False, "--token-stdin"),
) -> None:
    """Serve the setup-token broker in the detached child process."""

    if not token_stdin:
        raise typer.BadParameter("--token-stdin is required")
    token = sys.stdin.read(_MAX_TOKEN_BYTES + 1).strip()
    if not token or len(token.encode("utf-8")) > _MAX_TOKEN_BYTES:
        raise typer.BadParameter("setup token is unavailable or oversized")
    server = UnixSocketBrokerServer(
        socket_path=socket,
        binding_path=binding,
        token=token,
    )
    token = ""
    try:
        server.serve_forever(idle_timeout_seconds=3600.0)
    finally:
        server.close()


@broker_app.command("bind-pr")
def broker_bind_pr(
    repository: Path = typer.Option(Path("."), "--repository"),
    binding: Path = typer.Option(..., "--binding", envvar=_GITHUB_BINDING_ENV),
    socket: Path = typer.Option(..., "--socket", envvar=BROKER_SOCKET_ENV),
    head_branch: str | None = typer.Option(None, "--head-branch"),
    pull_request_number: int | None = typer.Option(None, "--pull-request"),
    base_branch: str | None = typer.Option(None, "--base-branch"),
    expected_author_login: str | None = typer.Option(None, "--expected-author"),
) -> None:
    """Discover and bind the exact early pull request through the broker."""

    checkout = _repository_from_environment(repository)
    resolved_head_branch = (
        head_branch if head_branch is not None else _resolve_checkout_head_branch(checkout)
    )
    broker = UnixSocketBrokerClient(socket_path=socket)
    broker.ensure_pull_request_binding(
        request_id="bind-pr",
        head_branch=resolved_head_branch,
        timeout_seconds=10.0,
    )
    loaded = _load_binding(binding)
    if loaded.pull_request is None:
        raise RuntimeIntegrationError("pull request binding is unavailable after broker discovery")
    _assert_pull_request_binding_matches_inputs(
        actual=loaded.pull_request,
        supplied=_PullRequestBindingInputs(
            pull_request_number=pull_request_number,
            base_branch=base_branch,
            head_branch=head_branch,
            expected_author_login=expected_author_login,
        ),
    )
    _echo_json(
        {
            "binding": str(binding.resolve()),
            "pull_request": loaded.pull_request.pull_request_number,
            "author": loaded.pull_request.expected_author_login,
            "author_type": loaded.pull_request.expected_author_type,
            "base_branch": loaded.pull_request.base_branch,
            "head_branch": loaded.pull_request.head_branch,
            "head_sha": loaded.pull_request.head_sha,
            "status": "bound",
        }
    )


@job_app.command("start")
def job_start(
    repository: Path = typer.Option(Path("."), "--repository"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    body_file: Path | None = typer.Option(None, "--body-file"),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    base_commit: str | None = typer.Option(None, "--base-commit"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    broker_socket_path: Path | None = typer.Option(
        None,
        "--broker-socket",
        envvar=BROKER_SOCKET_ENV,
    ),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Start an optimize job from the issue event/body and record the baseline."""

    try:
        runtime = _prepare_start_runtime(
            repository=repository,
            event=event,
            body_file=body_file,
            binding=binding,
            issue_number=issue_number,
            job_id=job_id,
            base_commit=base_commit,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            broker_socket_path=broker_socket_path,
            state_root=state_root,
            deadline_seconds=deadline_seconds,
        )
        state = runtime.controller.start(runtime.identity)
        refreshed_binding = _ensure_runtime_pull_request_binding(
            runtime=runtime,
            identity=runtime.identity,
            checkout=runtime.repository_root,
            pull_request=None,
            require_present=False,
            verify_checkout=False,
        )
        request_digest_sha256 = _persist_issue_request(
            _issue_request_path(runtime.paths.job_root),
            runtime.request,
        )
        _echo_json(
            _job_payload(
                state=state,
                request=runtime.request,
                request_digest_sha256=request_digest_sha256,
                next_action=_next_action(state, binding=refreshed_binding),
                policy=runtime.settings.policy,
                pull_request_binding_present=_has_pull_request_binding(
                    refreshed_binding
                ),
            )
        )
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@job_app.command("status")
def job_status(
    repository: Path = typer.Option(Path("."), "--repository"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
) -> None:
    """Load trusted optimize-job state and emit redacted deterministic JSON."""

    try:
        state, request, request_digest_sha256, loaded_binding = _load_job_snapshot(
            repository=repository,
            event=event,
            binding=binding,
            issue_number=issue_number,
            job_id=job_id,
            state_root=state_root,
        )
        _echo_json(
            _job_payload(
                state=state,
                request=request,
                request_digest_sha256=request_digest_sha256,
                next_action=_next_action(state, binding=loaded_binding),
                pull_request_binding_present=_has_pull_request_binding(loaded_binding),
            )
        )
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@job_app.command("handoff")
def job_handoff(
    candidate_id: str = typer.Option(..., "--candidate"),
    model: str = typer.Option(..., "--model"),
    hypothesis: str = typer.Option(..., "--hypothesis"),
    parent_id: str | None = typer.Option(None, "--parent"),
    repository: Path = typer.Option(Path("."), "--repository"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    broker_socket_path: Path | None = typer.Option(
        None,
        "--broker-socket",
        envvar=BROKER_SOCKET_ENV,
    ),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Create one candidate workspace inside the trusted optimize-job root."""

    try:
        runtime = _load_existing_runtime(
            repository=repository,
            event=event,
            binding=binding,
            issue_number=issue_number,
            job_id=job_id,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            broker_socket_path=broker_socket_path,
            state_root=state_root,
            deadline_seconds=deadline_seconds,
        )
        _validate_candidate_handoff(
            state=runtime.state,
            request=runtime.request,
            candidate_id=candidate_id,
            model=model,
            parent_id=parent_id,
            settings=runtime.settings,
        )
        prepared = runtime.controller.handoff_candidate(
            candidate_id,
            model=model,
            hypothesis=hypothesis,
            parent_id=parent_id,
        )
        state = runtime.controller.resume()
        _echo_json(
            {
                "candidate": {
                    "candidate_id": prepared.candidate_id,
                    "model": prepared.model,
                    "parent_id": prepared.parent_id,
                    "workspace": str(prepared.workspace_path),
                },
                "job": {
                    "job_id": state.identity.job_id,
                    "issue_number": state.identity.issue_number,
                    "state_digest_sha256": state.digest_sha256,
                },
                "next_action": "complete-candidate",
                "status": "ok",
            }
        )
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@job_app.command("complete")
def job_complete(
    candidate_id: str = typer.Option(..., "--candidate"),
    repository: Path = typer.Option(Path("."), "--repository"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    broker_socket_path: Path | None = typer.Option(
        None,
        "--broker-socket",
        envvar=BROKER_SOCKET_ENV,
    ),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Finalize and evaluate one candidate workspace."""

    try:
        runtime = _load_existing_runtime(
            repository=repository,
            event=event,
            binding=binding,
            issue_number=issue_number,
            job_id=job_id,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            broker_socket_path=broker_socket_path,
            state_root=state_root,
            deadline_seconds=deadline_seconds,
        )
        state = runtime.controller.complete_candidate(candidate_id)
        _echo_json(
            _job_payload(
                state=state,
                request=runtime.request,
                request_digest_sha256=runtime.request_digest_sha256,
                next_action=_next_action(state, binding=runtime.binding),
                policy=runtime.settings.policy,
                pull_request_binding_present=_has_pull_request_binding(runtime.binding),
            )
        )
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@job_app.command("finish")
def job_finish(
    repository: Path = typer.Option(Path("."), "--repository"),
    destination_checkout: Path | None = typer.Option(None, "--destination-checkout"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    pull_request_number: int | None = typer.Option(
        None,
        "--pull-request",
        envvar=_PULL_REQUEST_NUMBER_ENV,
    ),
    base_branch: str | None = typer.Option(
        None,
        "--base-branch",
        envvar=_PULL_REQUEST_BASE_BRANCH_ENV,
    ),
    head_branch: str | None = typer.Option(
        None,
        "--head-branch",
        envvar=_PULL_REQUEST_HEAD_BRANCH_ENV,
    ),
    expected_author_login: str | None = typer.Option(
        None,
        "--expected-author",
        envvar=_PULL_REQUEST_EXPECTED_AUTHOR_ENV,
    ),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    broker_socket_path: Path | None = typer.Option(
        None,
        "--broker-socket",
        envvar=BROKER_SOCKET_ENV,
    ),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Run the validating dataset if needed, project the winner, or close no-winner."""

    try:
        pull_request = _PullRequestBindingInputs(
            pull_request_number=pull_request_number,
            base_branch=base_branch,
            head_branch=head_branch,
            expected_author_login=expected_author_login,
        )
        if pull_request.any_pr_fields and binding is None:
            raise typer.BadParameter("--binding is required when pull request inputs are supplied")
        runtime = _load_existing_runtime(
            repository=repository,
            event=event,
            binding=binding,
            issue_number=issue_number,
            job_id=job_id,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            broker_socket_path=broker_socket_path,
            state_root=state_root,
            deadline_seconds=deadline_seconds,
        )
        state = _finish_runtime_job(
            runtime=runtime,
            destination_checkout=(
                runtime.repository_root
                if destination_checkout is None
                else destination_checkout.resolve(strict=False)
            ),
            pull_request=pull_request,
        )
        refreshed_binding = _load_binding(runtime.binding.path) if runtime.binding else None
        _echo_json(
            _job_payload(
                state=state,
                request=runtime.request,
                request_digest_sha256=runtime.request_digest_sha256,
                next_action=_next_action(state, binding=refreshed_binding),
                policy=runtime.settings.policy,
                pull_request_binding_present=_has_pull_request_binding(refreshed_binding),
            )
        )
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@job_app.command("resume")
def job_resume(
    repository: Path = typer.Option(Path("."), "--repository"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    broker_socket_path: Path | None = typer.Option(
        None,
        "--broker-socket",
        envvar=BROKER_SOCKET_ENV,
    ),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Resume by revalidating persisted optimize-job state against current runtime inputs."""

    try:
        runtime = _load_existing_runtime(
            repository=repository,
            event=event,
            binding=binding,
            issue_number=issue_number,
            job_id=job_id,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            broker_socket_path=broker_socket_path,
            state_root=state_root,
            deadline_seconds=deadline_seconds,
            build_controller=False,
        )
        payload = _job_payload(
            state=runtime.state,
            request=runtime.request,
            request_digest_sha256=runtime.request_digest_sha256,
            next_action=_next_action(runtime.state, binding=runtime.binding),
            pull_request_binding_present=_has_pull_request_binding(runtime.binding),
        )
        payload["resumed"] = True
        _echo_json(payload)
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


@acceptance_app.command("smoke")
def acceptance_smoke(
    repository: Path = typer.Option(Path("."), "--repository"),
    event: Path | None = typer.Option(None, "--event", envvar=_GITHUB_EVENT_PATH_ENV),
    binding: Path | None = typer.Option(None, "--binding", envvar=_GITHUB_BINDING_ENV),
    issue_number: int | None = typer.Option(None, "--issue-number"),
    job_id: str | None = typer.Option(None, "--job-id"),
    base_commit: str | None = typer.Option(None, "--base-commit"),
    cleanup: bool = typer.Option(True, "--cleanup/--no-cleanup"),
    policy_path: Path | None = typer.Option(None, "--policy"),
    metadata_path: Path | None = typer.Option(None, "--metadata"),
    pin_path: Path | None = typer.Option(None, "--pin"),
    bootstrap_receipt_path: Path | None = typer.Option(
        None,
        "--bootstrap-receipt",
        envvar=BOOTSTRAP_RECEIPT_ENV,
    ),
    broker_socket_path: Path | None = typer.Option(
        None,
        "--broker-socket",
        envvar=BROKER_SOCKET_ENV,
    ),
    state_root: Path | None = typer.Option(None, "--state-root", envvar=STATE_ROOT_ENV),
    deadline_seconds: float = typer.Option(
        DEFAULT_DEADLINE_SECONDS,
        "--deadline-seconds",
        envvar=DEADLINE_SECONDS_ENV,
    ),
) -> None:
    """Exercise one Foundry draft baseline create/evaluate/cleanup cycle without routing."""

    try:
        if not cleanup:
            raise typer.BadParameter("--cleanup must remain enabled for acceptance smoke")
        repository_root = _repository_from_environment(repository)
        loaded_binding = _load_binding(binding) if binding is not None else None
        resolved_issue_number = _resolve_issue_number(
            explicit=issue_number,
            binding=(None if loaded_binding is None else loaded_binding.issue.issue_number),
            event_binding=(_issue_binding_from_event(event).issue_number if event else None),
        )
        acceptance_job_id = (
            job_id if job_id is not None else f"acceptance-{resolved_issue_number}"
        )
        acceptance_state_root = _acceptance_state_root(state_root)
        paths = load_runtime_paths(
            repository_root,
            environment=os.environ,
            job_id=acceptance_job_id,
            policy_path=policy_path,
            metadata_path=metadata_path,
            pin_path=pin_path,
            bootstrap_receipt_path=bootstrap_receipt_path,
            broker_socket_path=broker_socket_path,
            state_root=acceptance_state_root,
        )
        _assert_acceptance_smoke_paths_are_fresh(paths)
        settings = load_runtime_settings(
            paths,
            environment=os.environ,
            base_commit=base_commit,
            deadline_seconds=deadline_seconds,
        )
        route = capture_route_fingerprint(
            repository=repository_root,
            environment=os.environ,
            paths=paths,
            settings=settings,
            deadline_seconds=deadline_seconds,
        )
        identity = build_job_identity(
            settings=settings,
            issue_number=resolved_issue_number,
            job_id=acceptance_job_id,
            route_fingerprint=route,
        )
        handle = _create_acceptance_foundry_handle(
            settings=settings,
            paths=paths,
            route=route,
            environment=os.environ,
        )
        try:
            result = handle.operations.evaluate_baseline(identity)
        finally:
            handle.close()
        if result.status != "ok" or result.evaluation is None:
            raise RuntimeIntegrationError(result.reason or "acceptance smoke baseline evaluation failed")
        sidecar = RuntimeSidecarStore(paths.sidecar_path).load().baseline
        if sidecar is None:
            raise RuntimeIntegrationError("acceptance smoke did not persist trusted baseline sidecars")
        if sidecar.pending_reference is not None or sidecar.cleanup_receipt_id is None:
            raise RuntimeIntegrationError("acceptance smoke did not clean up its draft baseline")
        _echo_json(
            {
                "cleanup": True,
                "issue_number": resolved_issue_number,
                "job_id": acceptance_job_id,
                "metrics": _evaluation_payload(result.evaluation),
                "route_fingerprint": route.sha256,
                "route_unchanged": True,
                "sidecar": {
                    "baseline_cleanup_recorded": True,
                    "pending_reference": False,
                },
                "status": "ok",
            }
        )
    except _JOB_COMMAND_ERRORS as error:
        _emit_blocked(error)


def _prepare_start_runtime(
    *,
    repository: Path,
    event: Path | None,
    body_file: Path | None,
    binding: Path | None,
    issue_number: int | None,
    job_id: str | None,
    base_commit: str | None,
    policy_path: Path | None,
    metadata_path: Path | None,
    pin_path: Path | None,
    bootstrap_receipt_path: Path | None,
    broker_socket_path: Path | None,
    state_root: Path | None,
    deadline_seconds: float,
) -> _JobRuntimeContext:
    repository_root = _repository_from_environment(repository)
    start = _resolve_start_context(
        event=event,
        body_file=body_file,
        binding=binding,
        issue_number=issue_number,
        job_id=job_id,
    )
    paths = load_runtime_paths(
        repository_root,
        environment=os.environ,
        job_id=start.job_id,
        policy_path=policy_path,
        metadata_path=metadata_path,
        pin_path=pin_path,
        bootstrap_receipt_path=bootstrap_receipt_path,
        broker_socket_path=broker_socket_path,
        state_root=state_root,
    )
    settings = load_runtime_settings(
        paths,
        environment=os.environ,
        base_commit=base_commit,
        deadline_seconds=deadline_seconds,
    )
    if start.issue_binding is not None:
        _assert_issue_binding_matches_metadata(start.issue_binding, settings.metadata)
    narrowed = _narrow_runtime_settings(settings, start.request)
    if not paths.job_state_path.is_file():
        _delete_file_if_present(_issue_request_path(paths.job_root))
    request_digest_sha256 = _request_digest(start.request)
    route = capture_route_fingerprint(
        repository=repository_root,
        environment=os.environ,
        paths=paths,
        settings=narrowed,
        deadline_seconds=deadline_seconds,
    )
    identity = build_job_identity(
        settings=narrowed,
        issue_number=start.issue_number,
        route_fingerprint=route,
        job_id=start.job_id,
    )
    controller = build_runtime_controller(
        repository=repository_root,
        identity=identity,
        environment=os.environ,
        paths=paths,
        settings=narrowed,
        captured_route=route,
        deadline_seconds=deadline_seconds,
    )
    return _JobRuntimeContext(
        repository_root=repository_root,
        paths=paths,
        settings=narrowed,
        request=start.request,
        request_digest_sha256=request_digest_sha256,
        state=None,
        controller=controller,
        binding=start.binding,
        identity=identity,
    )


def _load_existing_runtime(
    *,
    repository: Path,
    event: Path | None,
    binding: Path | None,
    issue_number: int | None,
    job_id: str | None,
    policy_path: Path | None,
    metadata_path: Path | None,
    pin_path: Path | None,
    bootstrap_receipt_path: Path | None,
    broker_socket_path: Path | None,
    state_root: Path | None,
    deadline_seconds: float,
    build_controller: bool = True,
) -> _JobRuntimeContext:
    repository_root = _repository_from_environment(repository)
    resolved_job_id, loaded_binding = _resolve_job_reference(
        event=event,
        binding=binding,
        issue_number=issue_number,
        job_id=job_id,
    )
    paths = load_runtime_paths(
        repository_root,
        environment=os.environ,
        job_id=resolved_job_id,
        policy_path=policy_path,
        metadata_path=metadata_path,
        pin_path=pin_path,
        bootstrap_receipt_path=bootstrap_receipt_path,
        broker_socket_path=broker_socket_path,
        state_root=state_root,
    )
    request, request_digest_sha256 = _load_persisted_issue_request(
        _issue_request_path(paths.job_root)
    )
    state_store = JobStateStore(paths.job_state_path)
    state = state_store.load()
    event_binding, event_request = _load_runtime_issue_event(event)
    settings = load_runtime_settings(
        paths,
        environment=os.environ,
        base_commit=state.identity.base_commit,
        deadline_seconds=deadline_seconds,
    )
    trusted_issue_binding = (
        loaded_binding.issue if loaded_binding is not None else event_binding
    )
    if trusted_issue_binding is not None:
        _assert_issue_binding_matches_metadata(trusted_issue_binding, settings.metadata)
    if event_request is not None and event_request != request:
        raise RuntimeIntegrationError(
            "persisted optimize-job issue request does not match the current issue body"
        )
    narrowed = _narrow_runtime_settings(settings, request)
    if state.identity.min_candidates != narrowed.policy.min_candidates:
        raise RuntimeIntegrationError(
            "persisted optimize-job issue request does not match the trusted optimize-job identity"
        )
    _assert_persisted_runtime_identity(
        state=state,
        settings=narrowed,
        job_id=resolved_job_id,
        issue_number=_resolve_existing_issue_number(
            explicit=issue_number,
            binding=loaded_binding,
            event_binding=event_binding,
            fallback=state.identity.issue_number,
        ),
    )
    controller = None
    if build_controller:
        controller = build_runtime_controller(
            repository=repository_root,
            identity=state.identity,
            environment=os.environ,
            paths=paths,
            settings=narrowed,
            captured_route=state.identity.route_fingerprint,
            deadline_seconds=deadline_seconds,
        )
    return _JobRuntimeContext(
        repository_root=repository_root,
        paths=paths,
        settings=narrowed,
        request=request,
        request_digest_sha256=request_digest_sha256,
        state=state,
        controller=controller,
        binding=loaded_binding,
        identity=state.identity,
    )


def _load_runtime_issue_event(
    event: Path | None,
) -> tuple[IssueBinding | None, OptimizeIssueRequest | None]:
    if event is None:
        return None, None
    payload = _read_json_object(event, max_bytes=_MAX_EVENT_BYTES)
    binding = _issue_binding_from_event_payload(payload)
    issue = _mapping(payload.get("issue"), "issue")
    body = issue.get("body")
    if type(body) is not str or not body.strip():
        return binding, None
    return binding, _issue_request_from_body(body)


def _resolve_existing_issue_number(
    *,
    explicit: int | None,
    binding: _LoadedBinding | None,
    event_binding: IssueBinding | None,
    fallback: int,
) -> int:
    values = [
        value
        for value in (
            explicit,
            (None if binding is None else binding.issue.issue_number),
            (None if event_binding is None else event_binding.issue_number),
        )
        if value is not None
    ]
    if not values:
        return fallback
    return _resolve_issue_number(
        explicit=explicit,
        binding=(None if binding is None else binding.issue.issue_number),
        event_binding=(None if event_binding is None else event_binding.issue_number),
    )


def _assert_persisted_runtime_identity(
    *,
    state: JobState,
    settings: RuntimeSettings,
    job_id: str,
    issue_number: int,
) -> None:
    poc_runtime._assert_identity_matches_settings(state.identity, settings)
    expected_identity = build_job_identity(
        settings=settings,
        issue_number=issue_number,
        route_fingerprint=state.identity.route_fingerprint,
        job_id=job_id,
    )
    if state.identity != expected_identity:
        raise RuntimeIntegrationError(
            "persisted optimize-job identity does not match current runtime settings"
        )


def _load_job_snapshot(
    *,
    repository: Path,
    event: Path | None,
    binding: Path | None,
    issue_number: int | None,
    job_id: str | None,
    state_root: Path | None,
) -> tuple[JobState, OptimizeIssueRequest, str, _LoadedBinding | None]:
    del repository
    resolved_job_id, loaded_binding = _resolve_job_reference(
        event=event,
        binding=binding,
        issue_number=issue_number,
        job_id=job_id,
    )
    job_root = _job_root_from_state_root(
        state_root=state_root,
        job_id=resolved_job_id,
    )
    request, request_digest_sha256 = _load_persisted_issue_request(
        _issue_request_path(job_root)
    )
    state = JobStateStore(job_root / STATE_FILENAME).load()
    if state.identity.min_candidates != request.candidate_budget:
        raise RuntimeIntegrationError(
            "persisted optimize-job issue request does not match the trusted optimize-job identity"
        )
    return state, request, request_digest_sha256, loaded_binding


def _resolve_start_context(
    *,
    event: Path | None,
    body_file: Path | None,
    binding: Path | None,
    issue_number: int | None,
    job_id: str | None,
) -> _IssueStartContext:
    loaded_binding = _load_binding(binding) if binding is not None else None
    event_binding: IssueBinding | None = None
    event_body: str | None = None
    if event is not None:
        event_binding, event_body = _load_issue_event(event)
    if loaded_binding is not None and event_binding is not None and loaded_binding.issue != event_binding:
        raise typer.BadParameter("issue event and trusted binding do not describe the same optimize job")
    resolved_issue_number = _resolve_issue_number(
        explicit=issue_number,
        binding=(None if loaded_binding is None else loaded_binding.issue.issue_number),
        event_binding=(None if event_binding is None else event_binding.issue_number),
    )
    resolved_job_id = _resolve_job_id(
        explicit=job_id,
        issue_number=resolved_issue_number,
        binding_job_id=(None if loaded_binding is None else loaded_binding.issue.job_id),
        event_job_id=(None if event_binding is None else event_binding.job_id),
    )
    body = _load_body_file(body_file) if body_file is not None else event_body
    if body is None:
        raise typer.BadParameter("--body-file or --event with issue.body is required")
    return _IssueStartContext(
        issue_number=resolved_issue_number,
        job_id=resolved_job_id,
        request=_issue_request_from_body(body),
        issue_binding=event_binding or (None if loaded_binding is None else loaded_binding.issue),
        binding=loaded_binding,
    )


def _resolve_job_reference(
    *,
    event: Path | None,
    binding: Path | None,
    issue_number: int | None,
    job_id: str | None,
) -> tuple[str, _LoadedBinding | None]:
    loaded_binding = _load_binding(binding) if binding is not None else None
    event_binding = _issue_binding_from_event(event) if event is not None else None
    if loaded_binding is not None and event_binding is not None and loaded_binding.issue != event_binding:
        raise typer.BadParameter("issue event and trusted binding do not describe the same optimize job")
    if job_id is not None:
        if loaded_binding is not None and loaded_binding.issue.job_id != job_id:
            raise typer.BadParameter("job_id does not match the trusted binding")
        if event_binding is not None and event_binding.job_id != job_id:
            raise typer.BadParameter("job_id does not match the issue event")
        return job_id, loaded_binding
    if loaded_binding is not None:
        if issue_number is not None and loaded_binding.issue.issue_number != issue_number:
            raise typer.BadParameter("issue_number does not match the trusted binding")
        return loaded_binding.issue.job_id, loaded_binding
    if event_binding is not None:
        if issue_number is not None and event_binding.issue_number != issue_number:
            raise typer.BadParameter("issue_number does not match the issue event")
        return event_binding.job_id, loaded_binding
    if issue_number is None:
        raise typer.BadParameter("--job-id, --issue-number, --event, or --binding is required")
    return f"optimize-{issue_number}", loaded_binding


def _resolve_issue_number(
    *,
    explicit: int | None,
    binding: int | None,
    event_binding: int | None,
) -> int:
    values = [value for value in (explicit, binding, event_binding) if value is not None]
    if not values:
        raise typer.BadParameter("--issue-number or a trusted issue event/binding is required")
    if len(set(values)) != 1:
        raise typer.BadParameter("issue_number is inconsistent across the trusted inputs")
    return values[0]


def _resolve_job_id(
    *,
    explicit: str | None,
    issue_number: int,
    binding_job_id: str | None,
    event_job_id: str | None,
    prefix: str = "optimize",
) -> str:
    values = [value for value in (explicit, binding_job_id, event_job_id) if value is not None]
    if values and len(set(values)) != 1:
        raise typer.BadParameter("job_id is inconsistent across the trusted inputs")
    if explicit is not None:
        return explicit
    if binding_job_id is not None:
        return binding_job_id
    if event_job_id is not None:
        return event_job_id
    return f"{prefix}-{issue_number}"


def _repository_from_environment(repository: Path) -> Path:
    candidate = repository
    if candidate == Path("."):
        workspace = os.environ.get(_GITHUB_WORKSPACE_ENV)
        if workspace:
            candidate = Path(workspace)
    return _repository_root(candidate)


def _load_body_file(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise typer.BadParameter("issue body file is unavailable") from error


def _load_issue_event(path: Path) -> tuple[IssueBinding, str]:
    event = _read_json_object(path, max_bytes=_MAX_EVENT_BYTES)
    binding = _issue_binding_from_event_payload(event)
    issue = _mapping(event.get("issue"), "issue")
    body = issue.get("body")
    if type(body) is not str or not body.strip():
        raise typer.BadParameter("event issue.body is unavailable")
    return binding, body


def _issue_request_from_body(body: str) -> OptimizeIssueRequest:
    parsed = parse_issue_body(body)
    return OptimizeIssueRequest(
        goal=parsed.goal,
        observed_failures=(parsed.observed_failures,),
        constraints=(parsed.constraints,),
        candidate_budget=parsed.candidate_budget,
        model_subset=parsed.candidate_models or None,
        editable_scope_subset=parsed.editable_scope or None,
    )


def _issue_request_path(job_root: Path) -> Path:
    return (job_root / _ISSUE_REQUEST_FILENAME).resolve(strict=False)


def _persist_issue_request(path: Path, request: OptimizeIssueRequest) -> str:
    digest = _request_digest(request)
    if path.is_file():
        existing, existing_digest = _load_persisted_issue_request(path)
        if existing != request:
            raise RuntimeIntegrationError(
                "persisted optimize-job issue request does not match the current issue body"
            )
        return existing_digest
    payload = request.model_dump(mode="json")
    envelope = {
        "content_sha256": digest,
        "request": payload,
    }
    encoded = _canonical_json_bytes(envelope)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{hashlib.sha256(encoded).hexdigest()[:12]}.tmp")
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except OSError as error:
        raise RuntimeIntegrationError("persisted optimize-job issue request could not be written") from error
    return digest


def _write_json_document(path: Path, payload: object) -> None:
    encoded = _canonical_json_bytes(payload)
    try:
        resolved = path.resolve(strict=False)
        resolved.parent.mkdir(parents=True, exist_ok=True)
        temporary = resolved.with_name(
            f".{resolved.name}.{hashlib.sha256(encoded).hexdigest()[:12]}.tmp"
        )
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, resolved)
    except OSError as error:
        raise RuntimeIntegrationError("JSON document could not be written") from error


def _load_persisted_issue_request(path: Path) -> tuple[OptimizeIssueRequest, str]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise RuntimeIntegrationError("persisted optimize-job issue request is unavailable") from error
    if len(data) > _MAX_REQUEST_BYTES:
        raise RuntimeIntegrationError("persisted optimize-job issue request exceeds the supported size")
    try:
        payload = json.loads(data, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeIntegrationError("persisted optimize-job issue request is not valid JSON") from error
    if type(payload) is not dict:
        raise RuntimeIntegrationError("persisted optimize-job issue request must be a JSON object")
    if set(payload) != {"content_sha256", "request"}:
        raise RuntimeIntegrationError("persisted optimize-job issue request envelope is invalid")
    digest = payload["content_sha256"]
    request_payload = payload["request"]
    if type(digest) is not str or type(request_payload) is not dict:
        raise RuntimeIntegrationError("persisted optimize-job issue request envelope types are invalid")
    request = OptimizeIssueRequest.from_document(request_payload)
    computed = _request_digest(request)
    if computed != digest:
        raise RuntimeIntegrationError("persisted optimize-job issue request digest does not match its content")
    return request, digest


def _request_digest(request: OptimizeIssueRequest) -> str:
    return hashlib.sha256(
        _canonical_json_bytes(request.model_dump(mode="json"))
    ).hexdigest()


def _resolve_state_root_path(state_root: Path | None) -> Path:
    raw = state_root
    if raw is None:
        env_value = os.environ.get(STATE_ROOT_ENV)
        if env_value is None or not env_value.strip():
            raise typer.BadParameter(f"{STATE_ROOT_ENV} is required")
        raw = Path(env_value)
    return raw.resolve(strict=False)


def _acceptance_state_root(state_root: Path | None) -> Path:
    resolved_state_root = _resolve_state_root_path(state_root)
    if resolved_state_root.name == _ACCEPTANCE_STATE_NAMESPACE:
        return resolved_state_root
    return (resolved_state_root / _ACCEPTANCE_STATE_NAMESPACE).resolve(strict=False)


def _assert_acceptance_smoke_paths_are_fresh(paths: RuntimePaths) -> None:
    issue_request_path = _issue_request_path(paths.job_root)
    reuse_detected = (
        paths.sidecar_path.exists()
        or paths.job_state_path.exists()
        or issue_request_path.exists()
        or any(paths.workspace_root.iterdir())
        or any(
            entry != paths.workspace_root
            for entry in paths.artifact_root.iterdir()
        )
        or any(
            entry not in (paths.artifact_root, paths.sidecar_path, paths.job_state_path, issue_request_path)
            for entry in paths.job_root.iterdir()
        )
    )
    if reuse_detected:
        raise RuntimeIntegrationError(
            "acceptance smoke refuses to reuse existing isolated state"
        )


def _job_root_from_state_root(*, state_root: Path | None, job_id: str) -> Path:
    resolved_state_root = _resolve_state_root_path(state_root)
    if not resolved_state_root.is_dir():
        raise RuntimeIntegrationError("optimize-job state root is unavailable")
    return (resolved_state_root / job_id).resolve(strict=False)


def _narrow_runtime_settings(
    settings: RuntimeSettings,
    request: OptimizeIssueRequest,
) -> RuntimeSettings:
    narrowed = apply_issue_request(settings.policy, request)
    if request.model_subset is not None and settings.policy.baseline_model not in narrowed.allowed_models:
        narrowed = narrowed.model_copy(
            update={
                "allowed_models": (
                    settings.policy.baseline_model,
                    *narrowed.allowed_models,
                )
            }
        )
    return settings.model_copy(update={"policy": narrowed})


def _assert_issue_binding_matches_metadata(
    binding: IssueBinding,
    metadata: AgentMetadata,
) -> None:
    if binding.repository.full_name != metadata.repository_identity:
        raise RuntimeIntegrationError("issue event repository does not match trusted agent metadata")
    if binding.repository.repository_id != metadata.repository_id:
        raise RuntimeIntegrationError("issue event repository_id does not match trusted agent metadata")


def _validate_candidate_handoff(
    *,
    state: JobState,
    request: OptimizeIssueRequest,
    candidate_id: str,
    model: str,
    parent_id: str | None,
    settings: RuntimeSettings,
) -> None:
    del parent_id
    existing = state.candidate(candidate_id)
    if existing is None and len(state.candidates) >= request.candidate_budget:
        raise RuntimeIntegrationError("candidate budget is exhausted for this optimize job")
    allowed_models = (
        settings.policy.allowed_models
        if request.model_subset is None
        else request.model_subset
    )
    if not _model_allowed(model, settings, allowed_models=allowed_models):
        raise RuntimeIntegrationError("candidate model is outside the narrowed repository policy")


def _model_allowed(
    model: str,
    settings: RuntimeSettings,
    *,
    allowed_models: tuple[str, ...],
) -> bool:
    runtime_variable = settings.metadata.hosted_runtime.model_environment_variable
    requested = build_hosted_definition(settings.metadata, model).payload[
        "environment_variables"
    ][runtime_variable]
    allowed = {
        build_hosted_definition(settings.metadata, allowed_model).payload[
            "environment_variables"
        ][runtime_variable]
        for allowed_model in allowed_models
    }
    return str(requested).casefold() in {str(item).casefold() for item in allowed}


def _job_payload(
    *,
    state: JobState,
    request: OptimizeIssueRequest,
    request_digest_sha256: str,
    next_action: str,
    policy: RepositoryPolicy | None = None,
    pull_request_binding_present: bool = False,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "baseline": (
            None
            if state.baseline is None
            else {
                **_evaluation_payload(state.baseline.evaluation),
                "comment_recorded": state.baseline.comment_receipt is not None,
            }
        ),
        "candidates": [_candidate_payload(state, candidate) for candidate in state.candidates],
        "decision": _decision_payload(state.decision),
        "job": {
            "base_commit": state.identity.base_commit,
            "candidate_budget": state.identity.min_candidates,
            "completed_candidates": state.completed_candidate_count,
            "issue_number": state.identity.issue_number,
            "job_id": state.identity.job_id,
            "remaining_candidate_slots": max(
                state.identity.min_candidates - len(state.candidates),
                0,
            ),
            "repository": state.identity.repository,
            "route_fingerprint": state.identity.route_fingerprint,
            "shared_commit": state.identity.shared_commit,
            "source_root": state.identity.source_root,
            "state_digest_sha256": state.digest_sha256,
            "terminal_outcome": state.terminal_outcome,
        },
        "next_action": next_action,
        "pending_candidates": _pending_candidate_ids(state),
        "pending_cleanup_candidates": _pending_cleanup_candidates(state),
        "pull_request_binding_present": pull_request_binding_present,
        "request": {
            "candidate_budget": request.candidate_budget,
            "editable_scope_subset": (
                None
                if request.editable_scope_subset is None
                else list(request.editable_scope_subset)
            ),
            "model_subset": (
                None if request.model_subset is None else list(request.model_subset)
            ),
            "request_sha256": request_digest_sha256,
        },
        "status": "ok",
    }
    blocker = _blocked_reason(state, binding_present=pull_request_binding_present)
    if blocker is not None:
        payload["blocker"] = blocker
    if policy is not None:
        payload["policy"] = {
            "allowed_models": list(policy.allowed_models),
            "editable_paths": list(policy.editable_paths),
        }
    return payload


def _evaluation_payload(summary: object | None) -> dict[str, Any] | None:
    if summary is None:
        return None
    evaluation = summary
    return {
        "focused_cases_improved": evaluation.focused_cases_improved,
        "focused_cases_regressed": evaluation.focused_cases_regressed,
        "guardrails": [
            {
                "name": guardrail.name,
                "passed": guardrail.passed,
                "score": guardrail.score,
            }
            for guardrail in evaluation.guardrails
        ],
        "latency_ms": evaluation.latency_ms,
        "primary_score": evaluation.primary_score,
        "successful": evaluation.successful,
        "token_count": evaluation.token_count,
    }


def _candidate_payload(state: JobState, candidate: object) -> dict[str, Any]:
    return {
        "assessment": _assessment_payload(candidate.assessment),
        "candidate_id": candidate.handoff.candidate_id,
        "comment_recorded": candidate.comment_receipt is not None,
        "development": _evaluation_payload(candidate.development),
        "draft": {
            "cleaned": candidate.cleanup_receipt is not None,
            "present": candidate.draft_id is not None,
        },
        "finalized": candidate.finalized is not None,
        "model": candidate.handoff.model,
        "parent_id": candidate.handoff.parent_id,
        "phase": _candidate_phase(state, candidate),
        "validating": _evaluation_payload(candidate.validating),
        "workspace": str(candidate.handoff.workspace_path),
    }


def _candidate_phase(state: JobState, candidate: object) -> str:
    if candidate.assessment is None:
        return "handoff"
    if state.final_winner_id == candidate.handoff.candidate_id:
        return "winner"
    return candidate.assessment.outcome


def _assessment_payload(assessment: object | None) -> dict[str, Any] | None:
    if assessment is None:
        return None
    return {
        "aggregate_delta": assessment.aggregate_delta,
        "changed_path_count": assessment.changed_path_count,
        "focused_cases_improved": assessment.focused_cases_improved,
        "focused_cases_regressed": assessment.focused_cases_regressed,
        "guardrail_failures": list(assessment.guardrail_failures),
        "outcome": assessment.outcome,
        "primary_score": assessment.primary_score,
        "reason": _redact_text(assessment.reason),
        "token_count": assessment.token_count,
        "validating_passed": assessment.validating_passed,
    }


def _decision_payload(decision: object | None) -> dict[str, Any] | None:
    if decision is None:
        return None
    return {
        "outcome": decision.outcome,
        "provisional_winner_id": decision.provisional_winner_id,
        "reason": _redact_text(decision.reason),
        "validating_candidate_id": decision.validating_candidate_id,
        "validating_passed": decision.validating_passed,
        "winner_id": decision.winner_id,
    }


def _next_action(state: JobState, *, binding: _LoadedBinding | None) -> str:
    if state.baseline is None:
        return "blocked"
    if _pending_candidate_ids(state):
        return "complete-candidate"
    if state.terminal_outcome is not None:
        if _terminal_side_effects_complete(state):
            return "terminal"
        if _closure_required(state) and not _has_pull_request_binding(binding):
            return "blocked"
        return "finish"
    if state.completed_candidate_count < state.identity.min_candidates:
        if len(state.candidates) < state.identity.min_candidates:
            return "handoff-candidate"
        return "blocked"
    if _closure_required(state) and not _has_pull_request_binding(binding):
        return "blocked"
    return "finish"


def _blocked_reason(state: JobState, *, binding_present: bool) -> str | None:
    if state.baseline is None:
        return "baseline is unavailable"
    if state.completed_candidate_count < state.identity.min_candidates and len(state.candidates) >= state.identity.min_candidates and not _pending_candidate_ids(state):
        return "candidate budget is exhausted before the minimum completed candidate count was reached"
    if _closure_required(state) and not binding_present:
        return "pull request binding is required for no-winner closure"
    return None


def _pending_candidate_ids(state: JobState) -> list[str]:
    return [
        candidate.handoff.candidate_id
        for candidate in state.candidates
        if candidate.assessment is None or candidate.comment_receipt is None
    ]


def _pending_cleanup_candidates(state: JobState) -> list[str]:
    return [
        candidate.handoff.candidate_id
        for candidate in state.candidates
        if candidate.draft_id is not None and candidate.cleanup_receipt is None
    ]


def _terminal_side_effects_complete(state: JobState) -> bool:
    if state.final_comment_receipt is None:
        return False
    if state.terminal_outcome == "winner" and state.projection_receipt is None:
        return False
    if state.terminal_outcome == "no_winner" and state.no_winner_receipt is None:
        return False
    return not _pending_cleanup_candidates(state)


def _closure_required(state: JobState) -> bool:
    if state.no_winner_receipt is not None:
        return False
    if state.terminal_outcome == "no_winner":
        return True
    if state.decision is None:
        return False
    if state.decision.provisional_winner_id is None:
        return True
    candidate = state.candidate(state.decision.provisional_winner_id)
    if candidate is None or candidate.validating is None:
        return False
    return state.decision.outcome == "no_winner" and state.final_winner_id is None


def _has_pull_request_binding(binding: _LoadedBinding | None) -> bool:
    return binding is not None and binding.pull_request is not None


def _has_verified_pull_request_binding(binding: _LoadedBinding | None) -> bool:
    return (
        binding is not None
        and binding.pull_request is not None
        and binding.pull_request.head_sha is not None
        and binding.pull_request.expected_author_type is not None
    )


def _finish_runtime_job(
    *,
    runtime: _JobRuntimeContext,
    destination_checkout: Path,
    pull_request: _PullRequestBindingInputs,
) -> JobState:
    controller = runtime.controller
    assert controller is not None
    state = controller._store.load()
    state = controller._retry_pending_cleanups(state)
    if state.terminal_outcome == "no_winner" and state.no_winner_receipt is None:
        state = controller._ensure_final_comment(state)
        _ensure_runtime_pull_request_binding(
            runtime=runtime,
            identity=state.identity,
            checkout=destination_checkout,
            pull_request=pull_request,
            require_present=True,
            verify_checkout=False,
        )
    if state.terminal_outcome == "winner" and state.projection_receipt is None:
        _ensure_runtime_pull_request_binding(
            runtime=runtime,
            identity=state.identity,
            checkout=destination_checkout,
            pull_request=pull_request,
            require_present=True,
            verify_checkout=True,
        )
    if state.terminal_outcome is not None:
        return controller.finish(destination_checkout)
    if state.baseline is None:
        raise ControllerError("baseline must be recorded before finish")
    if state.completed_candidate_count < state.identity.min_candidates:
        raise ControllerError("minimum candidate count has not been reached")
    state = controller._apply_development_decision(state)
    decision = state.decision
    if decision is None:
        raise ControllerError("decision state is unavailable")
    if decision.provisional_winner_id is None:
        state = controller._ensure_final_comment(state)
        _ensure_runtime_pull_request_binding(
            runtime=runtime,
            identity=state.identity,
            checkout=destination_checkout,
            pull_request=pull_request,
            require_present=True,
            verify_checkout=False,
        )
        return controller.finish(destination_checkout)
    winner = state.candidate(decision.provisional_winner_id)
    if winner is None or winner.finalized is None:
        raise ControllerError("provisional winner is missing finalized artifacts")
    if winner.validating is None:
        validating = controller._foundry.evaluate_validating(winner.finalized)
        if validating.status != "ok":
            state = controller._record_validating_platform_failure(
                controller._store.load(),
                decision=decision,
                reason=validating.reason or "validating run failed",
            )
            state = controller._ensure_final_comment(controller._store.load())
            return controller._retry_pending_cleanups(state)
        controller._store.update(
            lambda current: current.with_candidate(
                current.candidate(decision.provisional_winner_id).model_copy(
                    update={"validating": validating.evaluation}
                )
            )
        )
    state = controller._apply_final_decision(controller._store.load())
    if state.decision is None:
        raise ControllerError("final decision is unavailable")
    if state.decision.outcome == "no_winner":
        state = controller._ensure_final_comment(state)
        _ensure_runtime_pull_request_binding(
            runtime=runtime,
            identity=state.identity,
            checkout=destination_checkout,
            pull_request=pull_request,
            require_present=True,
            verify_checkout=False,
        )
    else:
        _ensure_runtime_pull_request_binding(
            runtime=runtime,
            identity=state.identity,
            checkout=destination_checkout,
            pull_request=pull_request,
            require_present=True,
            verify_checkout=True,
        )
    return controller.finish(destination_checkout)


def _ensure_runtime_pull_request_binding(
    *,
    runtime: _JobRuntimeContext,
    identity: JobIdentity,
    checkout: Path,
    pull_request: _PullRequestBindingInputs | None,
    require_present: bool,
    verify_checkout: bool,
) -> _LoadedBinding | None:
    if runtime.binding is None:
        if require_present:
            raise RuntimeIntegrationError("pull request binding is required for this optimize job")
        return None
    current = _load_binding(runtime.binding.path)
    trusted_verified_pull_request = (
        current.pull_request if _has_verified_pull_request_binding(current) else None
    )
    if (
        current.issue.issue_number != identity.issue_number
        or current.issue.job_id != identity.job_id
    ):
        raise RuntimeIntegrationError("trusted binding does not match the optimize-job identity")
    checkout_head_branch = _resolve_checkout_head_branch(checkout)
    if not _has_verified_pull_request_binding(current) or verify_checkout:
        try:
            broker = UnixSocketBrokerClient(socket_path=runtime.paths.broker_socket_path)
            broker.ensure_pull_request_binding(
                request_id="ensure-pr-binding",
                head_branch=checkout_head_branch,
                timeout_seconds=min(runtime.settings.deadline_seconds, 10.0),
            )
            current = _load_binding(runtime.binding.path)
        except (BrokerRemoteError, BrokerUnavailableError) as error:
            if verify_checkout:
                raise poc_runtime.runtime_integration_error_from_broker_failure(
                    error,
                    fallback="pull request binding is unavailable",
                ) from error
            if not _has_verified_pull_request_binding(current):
                if require_present:
                    raise poc_runtime.runtime_integration_error_from_broker_failure(
                        error,
                        fallback="pull request binding is unavailable",
                    ) from error
                return current
    if current.pull_request is None:
        if require_present:
            raise RuntimeIntegrationError("pull request binding is required for this optimize job")
        return current
    if verify_checkout and trusted_verified_pull_request is not None:
        _assert_live_pull_request_binding_matches_trusted(
            trusted=trusted_verified_pull_request,
            live=current.pull_request,
        )
    if pull_request is not None:
        _assert_pull_request_binding_matches_inputs(
            actual=current.pull_request,
            supplied=pull_request,
        )
    if verify_checkout:
        _verify_destination_checkout_matches_binding(
            checkout=checkout,
            binding=current.pull_request,
            checkout_head_branch=checkout_head_branch,
        )
    return current


def _assert_pull_request_binding_matches_inputs(
    *,
    actual: PullRequestBinding,
    supplied: _PullRequestBindingInputs,
) -> None:
    if supplied.pull_request_number is not None and (
        actual.pull_request_number != supplied.pull_request_number
    ):
        raise RuntimeIntegrationError(
            "trusted pull request binding does not match --pull-request"
        )
    if supplied.base_branch is not None and actual.base_branch != supplied.base_branch:
        raise RuntimeIntegrationError(
            "trusted pull request binding does not match --base-branch"
        )
    if supplied.head_branch is not None and actual.head_branch != supplied.head_branch:
        raise RuntimeIntegrationError(
            "trusted pull request binding does not match --head-branch"
        )
    if (
        supplied.expected_author_login is not None
        and actual.expected_author_login.casefold()
        != supplied.expected_author_login.casefold()
    ):
        raise RuntimeIntegrationError(
            "trusted pull request binding does not match --expected-author"
        )


def _assert_live_pull_request_binding_matches_trusted(
    *,
    trusted: PullRequestBinding,
    live: PullRequestBinding,
) -> None:
    drifted_fields: list[str] = []
    if live.repository != trusted.repository:
        drifted_fields.append("repository")
    if live.issue_number != trusted.issue_number:
        drifted_fields.append("issue_number")
    if live.pull_request_number != trusted.pull_request_number:
        drifted_fields.append("pull_request_number")
    if live.base_branch != trusted.base_branch:
        drifted_fields.append("base_branch")
    if live.head_branch != trusted.head_branch:
        drifted_fields.append("head_branch")
    if live.head_sha != trusted.head_sha:
        drifted_fields.append("head_sha")
    if (
        live.expected_author_login.casefold()
        != trusted.expected_author_login.casefold()
    ):
        drifted_fields.append("expected_author_login")
    if live.expected_author_type != trusted.expected_author_type:
        drifted_fields.append("expected_author_type")
    if drifted_fields:
        raise RuntimeIntegrationError(
            "live pull request binding drifted from the trusted binding: "
            + ", ".join(drifted_fields)
        )


def _verify_destination_checkout_matches_binding(
    *,
    checkout: Path,
    binding: PullRequestBinding,
    checkout_head_branch: str,
) -> None:
    if checkout_head_branch != binding.head_branch:
        raise RuntimeIntegrationError(
            "destination checkout branch does not match the bound pull request head branch"
        )
    if binding.head_sha is None:
        raise RuntimeIntegrationError(
            "trusted pull request binding is missing the bound pull request head commit"
        )
    checkout_head = _checkout_head_sha(checkout)
    if checkout_head.casefold() != binding.head_sha.casefold():
        raise RuntimeIntegrationError(
            "destination checkout HEAD does not match the bound pull request head commit"
        )


def _resolve_checkout_head_branch(checkout: Path) -> str:
    for arguments in (
        ("symbolic-ref", "--quiet", "--short", "HEAD"),
        ("rev-parse", "--abbrev-ref", "HEAD"),
    ):
        try:
            branch = _git_text(checkout, *arguments)
        except RuntimeIntegrationError:
            branch = ""
        if branch and branch != "HEAD":
            return branch
    try:
        branches = [
            line.strip()
            for line in _git_text(
                checkout,
                "branch",
                "--points-at",
                "HEAD",
                "--format=%(refname:short)",
            ).splitlines()
            if line.strip()
        ]
    except RuntimeIntegrationError:
        branches = []
    if len(branches) == 1:
        return branches[0]
    for name in (
        _PULL_REQUEST_HEAD_BRANCH_ENV,
        _GITHUB_HEAD_REF_ENV,
        _GITHUB_REF_NAME_ENV,
    ):
        raw = os.environ.get(name)
        if raw is not None and raw.strip():
            return raw.strip()
    raise RuntimeIntegrationError(
        "destination checkout branch could not be determined"
    )


def _checkout_head_sha(checkout: Path) -> str:
    return _git_text(checkout, "rev-parse", "HEAD").casefold()


def _git_text(checkout: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(checkout), *arguments],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise RuntimeIntegrationError("git repository state is unavailable")
    return completed.stdout.strip()


def _load_binding(path: Path) -> _LoadedBinding:
    payload = _read_json_object(path, max_bytes=32 * 1024)
    issue = IssueBinding.model_validate(payload.get("issue"))
    pull_request_payload = payload.get("pull_request")
    pull_request = (
        None
        if pull_request_payload is None
        else PullRequestBinding.model_validate(pull_request_payload)
    )
    return _LoadedBinding(
        path=path.resolve(strict=False),
        issue=issue,
        pull_request=pull_request,
    )


def _create_acceptance_foundry_handle(
    *,
    settings: RuntimeSettings,
    paths: RuntimePaths,
    route: RouteFingerprint,
    environment: dict[str, str],
) -> _AcceptanceFoundryHandle:
    credential = build_client_assertion_credential(
        poc_runtime._build_oidc_config(settings.metadata),
        environment=environment,
    )
    evaluation_backend = AzureProjectsEvaluationBackend(
        project_endpoint=settings.metadata.project_endpoint,
        credential=credential,
    )
    client = FoundryPocClient(
        settings.metadata.project_endpoint,
        credential,
        evaluation_backend=evaluation_backend,
    )
    operations = ControllerFoundryOperations(
        repository=paths.repository_root,
        source_root=settings.policy.source_root,
        policy=settings.policy,
        metadata=settings.metadata,
        client=client,
        artifact_state_path=paths.job_root,
        route_fingerprint=route,
        deadline_seconds=settings.deadline_seconds,
    )
    def close() -> None:
        _close_if_supported(client)
        _close_if_supported(evaluation_backend)
        _close_if_supported(credential)
    return _AcceptanceFoundryHandle(
        operations=operations,
        close=close,
    )


def _delete_file_if_present(path: Path) -> None:
    if path.exists():
        path.unlink()


def _close_if_supported(value: object) -> None:
    closer = getattr(value, "close", None)
    if callable(closer):
        closer()


def _redact_text(value: str | None) -> str | None:
    if value is None:
        return None
    text = " ".join(value.split())
    text = _CONTROL_PATTERN.sub(" ", text)
    text = _TOKEN_SHAPE_PATTERN.sub("******", text)
    return text[:240]


def _emit_blocked(error: Exception) -> None:
    _echo_json(
        {
            "error": _redact_text(str(error)) or "blocked",
            "next_action": "blocked",
            "status": "blocked",
        }
    )
    raise typer.Exit(code=2)


def _canonical_json_bytes(payload: object) -> bytes:
    return (
        json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        + b"\n"
    )


def _repository_root(repository: Path) -> Path:
    try:
        root = repository.resolve(strict=True)
    except OSError as error:
        raise typer.BadParameter("repository is unavailable") from error
    completed = subprocess.run(
        ["git", "-C", str(root), "rev-parse", "--show-toplevel"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise typer.BadParameter("repository must be a Git worktree")
    discovered = Path(completed.stdout.strip()).resolve(strict=True)
    if discovered != root:
        raise typer.BadParameter("repository must be the Git worktree root")
    return root


def _verify_repository_identity(root: Path, expected: str) -> None:
    completed = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", "origin"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_git_environment(),
    )
    if completed.returncode != 0:
        raise typer.BadParameter("repository origin is unavailable")
    remote = completed.stdout.strip().removesuffix(".git")
    normalized = remote.replace("git@github.com:", "https://github.com/")
    if normalized != f"https://github.com/{expected}":
        raise typer.BadParameter("repository origin does not match metadata")


def _git_environment() -> dict[str, str]:
    environment = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return environment


def _required_environment_path(name: str, *, offline: bool) -> Path | None:
    raw = os.environ.get(name)
    if raw is None:
        if offline:
            return None
        raise typer.BadParameter(f"{name} is required")
    path = Path(raw).resolve()
    return path


def _issue_binding_from_event(path: Path) -> IssueBinding:
    event = _read_json_object(path, max_bytes=_MAX_EVENT_BYTES)
    return _issue_binding_from_event_payload(event)


def _normalized_head_ref(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if normalized.startswith("refs/heads/"):
        normalized = normalized.removeprefix("refs/heads/")
    if not normalized:
        return None
    return normalized


def _candidate_head_refs(
    event: dict[str, Any],
    *,
    head_ref: str | None,
    ref_name: str | None,
) -> list[str]:
    candidates: list[str] = []

    def add(value: str | None) -> None:
        normalized = _normalized_head_ref(value)
        if normalized is None or normalized in candidates:
            return
        candidates.append(normalized)

    add(head_ref)
    add(ref_name)
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        head = pull_request.get("head")
        if isinstance(head, dict):
            add(head.get("ref"))
    issue = event.get("issue")
    if isinstance(issue, dict):
        pull_request = issue.get("pull_request")
        if isinstance(pull_request, dict):
            head = pull_request.get("head")
            if isinstance(head, dict):
                add(head.get("ref"))
    add(event.get("head_ref") if isinstance(event.get("head_ref"), str) else None)
    add(event.get("ref") if isinstance(event.get("ref"), str) else None)
    return candidates


def _linked_issue_numbers_from_body(
    body: str,
    *,
    repository_full_name: str,
) -> set[int]:
    numbers: set[int] = set()
    for match in _LINKED_ISSUE_REFERENCE_PATTERN.finditer(body):
        owner = match.group("owner")
        name = match.group("name")
        if owner is not None or name is not None:
            if f"{owner}/{name}" != repository_full_name:
                continue
        numbers.add(int(match.group("number")))
    return numbers


def _linked_issue_number_from_open_pull_request_branch(
    repository: RepositoryIdentity,
    *,
    head_ref: str,
    token: str,
) -> int | None:
    try:
        response = httpx.get(
            f"https://api.github.com/repos/{repository.full_name}/pulls",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "User-Agent": f"foundry-opt/{__version__}",
                "X-GitHub-Api-Version": "2026-03-10",
            },
            params={
                "direction": "asc",
                "head": f"{repository.owner}:{head_ref}",
                "per_page": 20,
                "sort": "created",
                "state": "open",
            },
            follow_redirects=True,
            timeout=30.0,
        )
    except httpx.HTTPError as error:
        raise typer.BadParameter(
            f"pull request lookup for branch '{head_ref}' failed"
        ) from error
    if response.status_code != 200:
        raise typer.BadParameter(
            f"pull request lookup for branch '{head_ref}' failed with HTTP {response.status_code}"
        )
    try:
        payload = response.json()
    except ValueError as error:
        raise typer.BadParameter(
            f"pull request lookup for branch '{head_ref}' returned invalid JSON"
        ) from error
    if not isinstance(payload, list):
        raise typer.BadParameter(
            f"pull request lookup for branch '{head_ref}' returned an invalid payload"
        )
    linked_issue_numbers: set[int] = set()
    for item in payload:
        if not isinstance(item, dict):
            raise typer.BadParameter(
                f"pull request lookup for branch '{head_ref}' returned an invalid item"
            )
        body = item.get("body")
        if type(body) is str:
            linked_issue_numbers.update(
                _linked_issue_numbers_from_body(
                    body,
                    repository_full_name=repository.full_name,
                )
            )
    if len(linked_issue_numbers) > 1:
        raise typer.BadParameter(
            f"open pull request body for branch '{head_ref}' references multiple optimize-job issues"
        )
    if not linked_issue_numbers:
        return None
    return next(iter(linked_issue_numbers))


def _issue_binding_from_event_context(
    path: Path,
    *,
    token: str,
    head_ref: str | None = None,
    ref_name: str | None = None,
) -> IssueBinding:
    event = _read_json_object(path, max_bytes=_MAX_EVENT_BYTES)
    try:
        return _issue_binding_from_event_payload(event)
    except typer.BadParameter as error:
        repository = _mapping(event.get("repository"), "repository")
        full_name = _required_string(repository.get("full_name"), "repository.full_name")
        owner, separator, name = full_name.partition("/")
        if separator != "/" or not owner or not name:
            raise typer.BadParameter("event repository.full_name is invalid") from error
        repository_id = repository.get("id")
        if type(repository_id) is not int or repository_id <= 0:
            raise typer.BadParameter("event repository.id is invalid") from error
        repository_identity = RepositoryIdentity(
            owner=owner,
            name=name,
            repository_id=repository_id,
        )
        for candidate in _candidate_head_refs(
            event,
            head_ref=head_ref,
            ref_name=ref_name,
        ):
            issue_number = _linked_issue_number_from_open_pull_request_branch(
                repository_identity,
                head_ref=candidate,
                token=token,
            )
            if issue_number is None:
                continue
            return IssueBinding(
                repository=repository_identity,
                issue_number=issue_number,
                job_id=f"optimize-{issue_number}",
                comment_author_login="github-actions[bot]",
            )
        raise error


def _issue_binding_from_event_payload(event: dict[str, Any]) -> IssueBinding:
    repository = _mapping(event.get("repository"), "repository")
    full_name = _required_string(repository.get("full_name"), "repository.full_name")
    owner, separator, name = full_name.partition("/")
    if separator != "/" or not owner or not name:
        raise typer.BadParameter("event repository.full_name is invalid")
    repository_id = repository.get("id")
    if type(repository_id) is not int or repository_id <= 0:
        raise typer.BadParameter("event repository.id is invalid")
    issue = _mapping(event.get("issue"), "issue")
    issue_number = issue.get("number")
    if (
        type(issue_number) is int
        and issue_number > 0
        and not isinstance(issue.get("pull_request"), dict)
    ):
        return IssueBinding(
            repository=RepositoryIdentity(
                owner=owner,
                name=name,
                repository_id=repository_id,
            ),
            issue_number=issue_number,
            job_id=f"optimize-{issue_number}",
            comment_author_login="github-actions[bot]",
        )
    linked_issue_numbers: set[int] = set()
    issue_body = issue.get("body")
    if type(issue_body) is str:
        linked_issue_numbers.update(
            _linked_issue_numbers_from_body(
                issue_body,
                repository_full_name=full_name,
            )
        )
    pull_request = event.get("pull_request")
    if isinstance(pull_request, dict):
        pull_request_body = pull_request.get("body")
        if type(pull_request_body) is str:
            linked_issue_numbers.update(
                _linked_issue_numbers_from_body(
                    pull_request_body,
                    repository_full_name=full_name,
                )
            )
    if len(linked_issue_numbers) > 1:
        raise typer.BadParameter(
            "event pull request body references multiple optimize-job issues"
        )
    if not linked_issue_numbers:
        raise typer.BadParameter("event does not identify an optimize-job issue")
    issue_number = next(iter(linked_issue_numbers))
    return IssueBinding(
        repository=RepositoryIdentity(
            owner=owner,
            name=name,
            repository_id=repository_id,
        ),
        issue_number=issue_number,
        job_id=f"optimize-{issue_number}",
        comment_author_login="github-actions[bot]",
    )


def _write_binding(
    path: Path,
    *,
    issue: IssueBinding,
    pull_request: PullRequestBinding | None,
) -> None:
    document = {
        "issue": issue.model_dump(mode="json"),
        "pull_request": (
            pull_request.model_dump(mode="json")
            if pull_request is not None
            else None
        ),
    }
    payload = (
        json.dumps(document, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode("utf-8")
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(
        f".{path.name}.{hashlib.sha256(payload).hexdigest()[:12]}.tmp"
    )
    with temporary.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _read_json_object(path: Path, *, max_bytes: int) -> dict[str, Any]:
    try:
        data = path.read_bytes()
    except OSError as error:
        raise typer.BadParameter("JSON document is unavailable") from error
    if len(data) > max_bytes:
        raise typer.BadParameter("JSON document exceeds the supported size")
    try:
        value = json.loads(data, object_pairs_hook=_strict_json_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise typer.BadParameter("JSON document is invalid") from error
    if type(value) is not dict:
        raise typer.BadParameter("JSON document must be an object")
    return value


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate JSON key: {key}")
        value[key] = item
    return value


def _mapping(value: object, field: str) -> dict[str, Any]:
    if type(value) is not dict:
        raise typer.BadParameter(f"event {field} is invalid")
    return value


def _required_string(value: object, field: str) -> str:
    if type(value) is not str or not value or len(value) > 512:
        raise typer.BadParameter(f"event {field} is invalid")
    return value


def _echo_json(value: object) -> None:
    typer.echo(
        json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    )


def main() -> None:
    app()
