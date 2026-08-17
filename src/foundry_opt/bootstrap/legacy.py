from __future__ import annotations

from foundry_opt.bootstrap.contracts import (
    AgentSidecarGroup,
    BindingState,
    CloudResourceOwnership,
    DistributionSettings,
    ExplicitAgentEntry,
    GitHubSettings,
    ManagedFileLockEntry,
    RootRegistry,
    SharedIdentity,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import load_strict_yaml_mapping


def import_legacy_single_agent_documents(*, lock_document: str | bytes, policy_document: str | bytes, metadata_document: str | bytes) -> RootRegistry:
    lock_payload = load_strict_yaml_mapping(lock_document, subject='legacy lock')
    policy_payload = load_strict_yaml_mapping(policy_document, subject='legacy policy')
    metadata_payload = load_strict_yaml_mapping(metadata_document, subject='legacy metadata')
    try:
        repository = str(metadata_payload['repository_identity'])
        branch = str(metadata_payload['default_branch'])
        tenant_id = str(metadata_payload['oidc']['tenant_id'])
        subscription_id = str(metadata_payload['oidc']['subscription_id'])
        project_id = str(metadata_payload['project_endpoint'])
        source_root = str(policy_payload['source_root'])
        agent_name = str(metadata_payload['agent_name']).casefold().replace(' ', '-')
        sidecar = AgentSidecarGroup(
            roots=(source_root,),
            managed_files=(ManagedFileLockEntry(path='.github/foundry-opt.lock.yml', sha256=str(lock_payload['uv_lock_sha256']), owner_agent_id=agent_name),),
            bindings=(BindingState(agent_id=agent_name, state='planned'),),
            cloud_resources=(CloudResourceOwnership(resource_kind='foundry_project', resource_id=project_id, owner_agent_id=agent_name, binding_state='planned'),),
        )
        return RootRegistry(
            distribution=DistributionSettings(
                repository_defaults_ref='legacy@v1',
                github=GitHubSettings(repository=repository, default_branch=branch, issue_label='optimize'),
                identity=SharedIdentity(tenant_id=tenant_id, subscription_id=subscription_id, project_id=project_id),
                agents=(ExplicitAgentEntry(agent_id=agent_name, role='primary', provider='legacy-import', sidecar=sidecar),),
            )
        )
    except KeyError as exc:
        raise BootstrapConfigError(f'missing legacy field: {exc.args[0]}') from exc
