"""The approved Azure least-privilege role matrix must be real, narrow, and documented."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from foundry_opt.bootstrap.errors import BootstrapConfigError
from foundry_opt.bootstrap.input_contracts import (
    APPROVED_ROLE_DEFINITIONS,
    ApprovedRoleAssignment,
    FORBIDDEN_ROLE_DEFINITION_IDS,
    approved_role_definition,
)
from foundry_opt.bootstrap.providers.azure import AzureProviderError, _canonical_role_definition_id

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPOSITORY_ROOT / 'docs' / 'identity-rbac.md'
SUBSCRIPTION = '33333333-3333-3333-3333-333333333333'
SCOPE = f'/subscriptions/{SUBSCRIPTION}/resourceGroups/example-rg/providers/Microsoft.CognitiveServices/accounts/example'
GUID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
PLACEHOLDER_RE = re.compile(r'^0{8}-0{4}-0{4}-0{4}-0{11}[0-9]$')

CONTRACT_ERRORS = (BootstrapConfigError, ValidationError)


def _role_id(guid: str) -> str:
    return f'/subscriptions/{SUBSCRIPTION}/providers/Microsoft.Authorization/roleDefinitions/{guid}'


def _assignment(guid: str, *, alias: str, scope: str = SCOPE) -> ApprovedRoleAssignment:
    return ApprovedRoleAssignment(alias=alias, role_definition_id=_role_id(guid), scope=scope)


def test_matrix_contains_only_real_lowercase_guids() -> None:
    assert APPROVED_ROLE_DEFINITIONS
    for definition in APPROVED_ROLE_DEFINITIONS:
        assert GUID_RE.fullmatch(definition.role_definition_guid), definition
        assert not PLACEHOLDER_RE.fullmatch(definition.role_definition_guid), definition
        assert definition.purpose and definition.slug and definition.display_name
        assert definition.scope_kind in {'foundry', 'telemetry'}
    guids = [item.role_definition_guid for item in APPROVED_ROLE_DEFINITIONS]
    slugs = [item.slug for item in APPROVED_ROLE_DEFINITIONS]
    assert len(set(guids)) == len(guids)
    assert len(set(slugs)) == len(slugs)


def test_matrix_pins_the_documented_least_privilege_roles() -> None:
    assert {item.slug: item.role_definition_guid for item in APPROVED_ROLE_DEFINITIONS} == {
        'foundry-user': '53ca6127-db72-4b80-b1b0-d745d6d5456d',
        'foundry-project-runtime-user': '142bfaed-a13f-4c2d-bed2-6db62c4a1009',
        'foundry-agent-consumer': 'eed3b665-ab3a-47b6-8f48-c9382fb1dad6',
        'monitoring-reader': '43d0d8ad-25c7-4714-9337-8ba259a9fe05',
        'log-analytics-reader': '73c42c96-874c-492b-b04d-ab87d138a893',
    }


@pytest.mark.parametrize('definition', APPROVED_ROLE_DEFINITIONS, ids=lambda item: item.slug)
def test_every_approved_role_is_accepted(definition) -> None:
    assignment = _assignment(definition.role_definition_guid, alias=definition.slug)

    assert assignment.role_definition_id.endswith(definition.role_definition_guid)
    assert approved_role_definition(definition.role_definition_guid) is definition
    assert approved_role_definition(definition.role_definition_guid.upper()) is definition


@pytest.mark.parametrize(
    ('guid', 'name'),
    sorted(FORBIDDEN_ROLE_DEFINITION_IDS.items()),
    ids=lambda value: value if isinstance(value, str) else str(value),
)
def test_privileged_fallback_roles_are_refused(guid: str, name: str) -> None:
    with pytest.raises(CONTRACT_ERRORS, match='privileged fallback role'):
        _assignment(guid, alias='foundry-user')


def test_forbidden_matrix_covers_owner_contributor_and_project_manager() -> None:
    assert set(FORBIDDEN_ROLE_DEFINITION_IDS) == {
        '8e3af657-a8ff-443c-a75c-2fe8c4bcb635',
        'b24988ac-6180-42a0-ab88-20f7382dd24c',
        'eadc314b-1a2d-4efa-be10-5d325db5065e',
    }
    assert not set(FORBIDDEN_ROLE_DEFINITION_IDS) & {item.role_definition_guid for item in APPROVED_ROLE_DEFINITIONS}


def test_unknown_and_placeholder_roles_are_refused() -> None:
    with pytest.raises(CONTRACT_ERRORS, match='approved allow-list'):
        _assignment('ffffffff-ffff-ffff-ffff-ffffffffffff', alias='foundry-user')
    with pytest.raises(CONTRACT_ERRORS, match='approved allow-list'):
        _assignment('00000000-0000-0000-0000-000000000001', alias='foundry-user')


def test_alias_must_identify_the_granted_role() -> None:
    with pytest.raises(CONTRACT_ERRORS, match="alias starting with 'foundry-user'"):
        _assignment('53ca6127-db72-4b80-b1b0-d745d6d5456d', alias='reader')
    # Scope-qualified aliases stay allowed so one role can be assigned at two scopes.
    assert _assignment('53ca6127-db72-4b80-b1b0-d745d6d5456d', alias='foundry-user-project').alias == 'foundry-user-project'


def test_subscription_scope_is_refused() -> None:
    with pytest.raises(CONTRACT_ERRORS, match='subscription-scope role assignments are not allowed'):
        _assignment('53ca6127-db72-4b80-b1b0-d745d6d5456d', alias='foundry-user', scope=f'/subscriptions/{SUBSCRIPTION}')


def test_provider_independently_refuses_owner_and_contributor() -> None:
    for guid in ('8e3af657-a8ff-443c-a75c-2fe8c4bcb635', 'b24988ac-6180-42a0-ab88-20f7382dd24c'):
        with pytest.raises(AzureProviderError, match='Owner and Contributor'):
            _canonical_role_definition_id(_role_id(guid), SUBSCRIPTION)
    canonical = _canonical_role_definition_id(_role_id('53ca6127-db72-4b80-b1b0-d745d6d5456d'), SUBSCRIPTION)
    assert canonical.endswith('53ca6127-db72-4b80-b1b0-d745d6d5456d')


def _documented_rows(table_heading: str) -> dict[str, str]:
    text = DOC_PATH.read_text(encoding='utf-8')
    section = text.split(table_heading, 1)[1].split('\n## ', 1)[0]
    rows: dict[str, str] = {}
    for line in section.splitlines():
        if not line.startswith('|'):
            continue
        cells = [cell.strip() for cell in line.strip('|').split('|')]
        guid = next((cell.strip('`') for cell in cells if GUID_RE.fullmatch(cell.strip('`'))), None)
        if guid is not None:
            rows[guid] = cells[0].strip('`')
    return rows


def test_documentation_matches_the_code_matrix() -> None:
    approved = _documented_rows('## Approved least-privilege role matrix')
    refused = _documented_rows('## Explicitly refused roles')

    assert approved == {item.role_definition_guid: item.slug for item in APPROVED_ROLE_DEFINITIONS}
    assert set(refused) == set(FORBIDDEN_ROLE_DEFINITION_IDS)
    assert 'project-scoped `Foundry User` only' in DOC_PATH.read_text(encoding='utf-8')


def test_published_schema_advertises_the_role_matrix() -> None:
    schema = ApprovedRoleAssignment.model_json_schema()
    role_field = schema['properties']['role_definition_id']

    assert [item['role_definition_guid'] for item in role_field['x-approved-role-definitions']] == [
        item.role_definition_guid for item in APPROVED_ROLE_DEFINITIONS
    ]
    assert {item['role_definition_guid'] for item in role_field['x-refused-role-definitions']} == set(
        FORBIDDEN_ROLE_DEFINITION_IDS
    )
