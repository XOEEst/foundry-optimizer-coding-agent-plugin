from __future__ import annotations

import pytest

from foundry_opt.bootstrap.contracts import (
    AgentSidecarGroup,
    DistributionSettings,
    GitHubSettings,
    ManagedFileLockEntry,
    RootRegistry,
    SharedIdentity,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError


def test_root_registry_rejects_casefold_duplicate_agent_ids() -> None:
    with pytest.raises(BootstrapConfigError):
        RootRegistry.from_document({
            'distribution': {
                'repository_defaults_ref': 'defaults@1',
                'github': GitHubSettings(repository='repo/name', default_branch='main', issue_label='opt').model_dump(mode='json'),
                'identity': SharedIdentity(tenant_id='tenant', subscription_id='sub', project_id='project').model_dump(mode='json'),
                'agents': (
                    {
                        'agent_id': 'agent-one',
                        'role': 'primary',
                        'provider': 'github',
                        'sidecar': {'roots': ('src/one',)},
                    },
                    {
                        'agent_id': 'Agent-One',
                        'role': 'primary',
                        'provider': 'github',
                        'sidecar': {'roots': ('src/two',)},
                    },
                ),
            }
        })


def test_sidecar_overlapping_roots_require_shared_source_relationship() -> None:
    with pytest.raises(BootstrapConfigError):
        AgentSidecarGroup.from_document({'roots': ('src/agent', 'src/agent/tests')})


def test_managed_lock_entry_rejects_unsafe_paths() -> None:
    with pytest.raises(BootstrapConfigError):
        ManagedFileLockEntry.from_document({'path':'../secrets.txt', 'sha256':'a' * 64, 'owner_agent_id':'agent-one'})
