from __future__ import annotations

from pathlib import Path

from foundry_opt.bootstrap.legacy import import_legacy_single_agent_documents


def test_legacy_import_builds_migration_proposal() -> None:
    root = Path(__file__).resolve().parents[2] / 'src' / 'foundry_opt' / 'templates' / 'customer-repo'
    proposal = import_legacy_single_agent_documents(
        lock_document=(root / '.github' / 'foundry-opt.lock.yml').read_text(encoding='utf-8'),
        policy_document=(root / '.github' / 'foundry-optimizer.yaml').read_text(encoding='utf-8'),
        metadata_document=(root / '.foundry' / 'agent-metadata.yaml').read_text(encoding='utf-8'),
    )
    assert proposal.registry.github.client_id_variable == 'AZURE_OPTIMIZER_CLIENT_ID'
    assert proposal.registry.identity.client_id is None
    assert proposal.sidecars[0].package_root == 'agent'
    assert proposal.sidecars[0].foundry_project.expected_version is None
    assert 'repository_id=123456789' in proposal.actions[0].diagnostics
