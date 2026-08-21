"""Customer repository bootstrap contracts and orchestration."""

from __future__ import annotations

from foundry_opt.bootstrap.runner import (
    BootstrapQuestion,
    BootstrapRunner,
    BootstrapTurn,
)
from foundry_opt.bootstrap.local_commit import (
    BootstrapLocalCommitHandler,
    LocalGitCommitCoordinator,
    build_local_commit_context,
)
from foundry_opt.bootstrap.local_deploy import (
    BootstrapLocalDeploymentHandler,
    LocalDeploymentCoordinator,
)
from foundry_opt.bootstrap.repository_setup import (
    BootstrapRepositorySetupHandler,
    RepositorySetupCoordinator,
)
from foundry_opt.bootstrap.connection_setup import (
    BootstrapConnectionSetupHandler,
    ConnectionSetupCoordinator,
)

__all__ = [
    "BootstrapConnectionSetupHandler",
    "BootstrapLocalDeploymentHandler",
    "BootstrapLocalCommitHandler",
    "BootstrapQuestion",
    "BootstrapRepositorySetupHandler",
    "BootstrapRunner",
    "BootstrapTurn",
    "LocalGitCommitCoordinator",
    "LocalDeploymentCoordinator",
    "ConnectionSetupCoordinator",
    "RepositorySetupCoordinator",
    "build_local_commit_context",
]
