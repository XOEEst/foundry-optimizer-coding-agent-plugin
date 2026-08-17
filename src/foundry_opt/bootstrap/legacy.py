from __future__ import annotations

from foundry_opt.bootstrap.contracts import (
    BootstrapAction,
    BootstrapSidecar,
    DecisionPolicy,
    DefaultEvaluatorBundle,
    DeploymentSettings,
    DistributionSettings,
    EvaluatorLineageEntry,
    EvaluatorReference,
    ExplicitAgentEntry,
    FoundryProjectSettings,
    HardGuardrail,
    IdentitySettings,
    ImmutableDatasetReference,
    ImmutableDefinitionReference,
    IssueEvaluatorRequestEntry,
    LegacyMigrationProposal,
    ResolvedWeightedObjective,
    RootRegistry,
    RuntimeProtocolSettings,
)
from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.poc.config import load_strict_yaml_mapping


def import_legacy_single_agent_documents(*, lock_document: str | bytes, policy_document: str | bytes, metadata_document: str | bytes) -> LegacyMigrationProposal:
    lock_payload = load_strict_yaml_mapping(lock_document, subject='legacy lock')
    policy_payload = load_strict_yaml_mapping(policy_document, subject='legacy policy')
    metadata_payload = load_strict_yaml_mapping(metadata_document, subject='legacy metadata')
    try:
        development = metadata_payload['development_evaluation']
        validating = metadata_payload['validating_evaluation']
        default_entries = tuple(
            IssueEvaluatorRequestEntry(
                evaluator=EvaluatorReference(evaluator_id=str(value), provenance='reused_existing'),
            )
            for value in development['custom_evaluator_ids']
        )
        bundle = DefaultEvaluatorBundle(
            objective=ResolvedWeightedObjective.create(default_entries),
            datasets=(ImmutableDatasetReference(dataset_id=str(development['dataset_id'])), ImmutableDatasetReference(dataset_id=str(validating['dataset_id']))),
            definitions=(
                ImmutableDefinitionReference(definition_id=str(development['resolved_evaluation_id'])),
                ImmutableDefinitionReference(definition_id=str(validating['resolved_evaluation_id'])),
            ),
            evaluator_lineage=tuple(
                EvaluatorLineageEntry(evaluator=entry.evaluator, source='legacy_metadata') for entry in default_entries
            ),
        )
        sidecar = BootstrapSidecar(
            repo_agent_id=str(metadata_payload['agent_name']).casefold().replace(' ', '-'),
            source_root=str(policy_payload['source_root']),
            package_root=str(lock_payload['package_path']),
            editable_paths=tuple(policy_payload['editable_paths']),
            runtime=RuntimeProtocolSettings(
                kind=str(metadata_payload['hosted_runtime']['kind']),
                entrypoint=tuple(metadata_payload['hosted_runtime']['entry_point']),
                protocol_name=str(metadata_payload['hosted_runtime']['protocol_name']),
                protocol_version=str(metadata_payload['hosted_runtime']['protocol_version']),
            ),
            foundry_project=FoundryProjectSettings(
                project_endpoint=str(metadata_payload['project_endpoint']),
                account_resource_id=str(metadata_payload['foundry_account_resource_id']),
                agent_name=str(metadata_payload['agent_name']),
                expected_version=str(lock_payload['commit']),
            ),
            baseline_model=str(policy_payload['baseline_model']),
            allowed_models=tuple(policy_payload['allowed_models']),
            max_candidates=int(policy_payload['max_candidates']),
            decision_policy=DecisionPolicy(**policy_payload['decision_rules']),
            development_dataset=ImmutableDatasetReference(dataset_id=str(development['dataset_id'])),
            validating_dataset=ImmutableDatasetReference(dataset_id=str(validating['dataset_id'])),
            default_evaluator_bundle=bundle,
            hard_guardrails=tuple(
                HardGuardrail(evaluator_name=name, required_pass_rate=float(config['required_pass_rate']), required=bool(config.get('required', True)))
                for name, config in policy_payload['hard_guardrails'].items()
            ),
            deployment=DeploymentSettings(environment='foundry-production', enabled=True, eligibility='eligible'),
        )
        registry = RootRegistry(
            distribution=DistributionSettings(
                repository=str(metadata_payload['repository_identity']),
                channel='legacy-import',
                pin=str(lock_payload['commit']),
                optimizer_environment='copilot',
                deployment_environment='foundry-production',
                optimizer_client_id_variable='AZURE_OPTIMIZER_CLIENT_ID',
                deployment_client_id_variable='AZURE_DEPLOYMENT_CLIENT_ID',
            ),
            identity=IdentitySettings(kind='azure_subscription', resource_id=str(metadata_payload['foundry_account_resource_id'])),
            agents=(
                ExplicitAgentEntry(
                    agent_id=sidecar.repo_agent_id,
                    root=sidecar.source_root,
                    config_path=str(policy_payload['metadata_path']),
                    enabled=True,
                ),
            ),
        )
        return LegacyMigrationProposal(
            registry=registry,
            sidecars=(sidecar,),
            actions=(BootstrapAction(action_id='review-legacy-import', phase='repository', stage='planned', kind='review-migration', target_agent_id=sidecar.repo_agent_id),),
        )
    except KeyError as exc:
        raise BootstrapConfigError(f'missing legacy field: {exc.args[0]}') from exc
