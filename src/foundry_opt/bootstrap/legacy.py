from __future__ import annotations

from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    BootstrapSidecar,
    DecisionPolicy,
    DefaultEvaluatorBundle,
    DeploymentSettings,
    DistributionSettings,
    EvaluatorNormalization,
    EvaluatorReference,
    ExplicitAgentEntry,
    FoundryProjectSettings,
    GitHubSettings,
    HardGuardrail,
    IdentitySettings,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    IssueEvaluatorRequestEntry,
    LegacyMigrationProposal,
    ResolvedEvaluator,
    ResolvedWeightedObjective,
    RootRegistry,
    RuntimeProtocolSettings,
    VerificationBundle,
    VerificationSettings,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import load_strict_yaml_mapping


def import_legacy_single_agent_documents(*, lock_document: str | bytes, policy_document: str | bytes, metadata_document: str | bytes) -> LegacyMigrationProposal:
    lock_payload = load_strict_yaml_mapping(lock_document, subject='legacy lock')
    policy_payload = load_strict_yaml_mapping(policy_document, subject='legacy policy')
    metadata_payload = load_strict_yaml_mapping(metadata_document, subject='legacy metadata')
    try:
        hosted = metadata_payload['hosted_runtime']
        oidc = metadata_payload['oidc']
        principals = metadata_payload['oidc']['principals']
        if len(principals) != 2:
            raise BootstrapConfigError('legacy migration requires exactly two principals for unresolved shared identity review')
        development = metadata_payload['development_evaluation']
        validating = metadata_payload['validating_evaluation']
        resolved = tuple(
            ResolvedEvaluator(
                reference=EvaluatorReference(evaluator_id=str(value), provenance='reused_existing'),
                normalization=EvaluatorNormalization(kind='pass_fail'),
                weight=1.0,
            )
            for value in development['custom_evaluator_ids']
        )
        bundle = DefaultEvaluatorBundle(
            objective=ResolvedWeightedObjective.create(resolved),
            datasets=(
                ImmutableDatasetReference(dataset_id=str(development['dataset_id'])),
                ImmutableDatasetReference(dataset_id=str(validating['dataset_id'])),
            ),
            definitions=(
                ImmutableDefinitionReference(definition_id=str(development['resolved_evaluation_id'])),
                ImmutableDefinitionReference(definition_id=str(validating['resolved_evaluation_id'])),
            ),
        )
        source_root = str(policy_payload['source_root'])
        repo_agent_id = str(metadata_payload['agent_name']).casefold().replace(' ', '-')
        sidecar = BootstrapSidecar(
            repo_agent_id=repo_agent_id,
            source_root=source_root,
            package_root=source_root,
            editable_paths=tuple(policy_payload['editable_paths']),
            runtime=RuntimeProtocolSettings(
                kind=str(hosted['kind']),
                runtime=str(hosted['runtime']),
                entrypoint=tuple(hosted['entry_point']),
                dependency_resolution=str(hosted['dependency_resolution']),
                protocol_name=str(hosted['protocol_name']),
                protocol_version=str(hosted['protocol_version']),
                cpu=str(hosted.get('cpu')) if hosted.get('cpu') is not None else None,
                memory=str(hosted.get('memory')) if hosted.get('memory') is not None else None,
                model_environment_variable=str(hosted.get('model_environment_variable')) if hosted.get('model_environment_variable') is not None else None,
            ),
            foundry_project=FoundryProjectSettings(
                project_endpoint=str(metadata_payload['project_endpoint']),
                account_resource_id=str(metadata_payload['foundry_account_resource_id']),
                agent_name=str(metadata_payload['agent_name']),
                expected_version=None,
                model_deployment_aliases=tuple(item['alias'] for item in metadata_payload['model_deployments']),
            ),
            baseline_model=str(policy_payload['baseline_model']),
            allowed_models=tuple(policy_payload['allowed_models']),
            min_candidates=int(policy_payload['min_candidates']),
            max_candidates=int(policy_payload['max_candidates']),
            primary_metric=str(policy_payload['primary_metric']),
            decision_policy=DecisionPolicy(**policy_payload['decision_rules']),
            hard_guardrails=tuple(HardGuardrail(evaluator_name=name, required_pass_rate=float(config['required_pass_rate']), required=bool(config.get('required', True))) for name, config in policy_payload['hard_guardrails'].items()),
            deployment=DeploymentSettings(environment='foundry-production', enabled=True, require_aligned_binding=True),
            verification=VerificationSettings(
                mode='required',
                evaluation_gate_policy='require_foundry_evaluation',
                bundle=VerificationBundle(
                    development_dataset=ImmutableDatasetReference(dataset_id=str(development['dataset_id'])),
                    validating_dataset=ImmutableDatasetReference(dataset_id=str(validating['dataset_id'])),
                    development_definition=ImmutableDefinitionReference(definition_id=str(development['resolved_evaluation_id'])),
                    validating_definition=ImmutableDefinitionReference(definition_id=str(validating['resolved_evaluation_id'])),
                    default_evaluator_bundle=bundle,
                ),
            ),
        )
        registry = RootRegistry(
            distribution=DistributionSettings(repository=str(lock_payload['repository_url']), channel='legacy-import', pin=str(lock_payload['commit'])),
            github=GitHubSettings(optimizer_environment='copilot', deployment_environment='foundry-production', client_id_variable='AZURE_OPTIMIZER_CLIENT_ID'),
            identity=IdentitySettings(kind='unresolved_migration', resource_id=None),
            agents=(ExplicitAgentEntry(agent_id=repo_agent_id, root=source_root, config_path=f'{source_root}/.foundry/foundry-opt.yaml', enabled=True),),
        )
        diagnostics = (
            f"repository_id={metadata_payload['repository_id']}",
            f"oidc_issuer={oidc['issuer']}",
            f"oidc_audience={oidc['audience']}",
            f"repository_id_claim={oidc['repository_id_claim']}",
            f"principals={len(principals)}",
        )
        actions = (
            BootstrapAction(action_id='resolve-shared-identity', phase='azure', stage='planned', kind='unresolved-shared-identity', target_agent_id=repo_agent_id, diagnostics=diagnostics),
        )
        return LegacyMigrationProposal(registry=registry, sidecars=(sidecar,), actions=actions)
    except KeyError as exc:
        raise BootstrapConfigError(f'missing legacy field: {exc.args[0]}') from exc
