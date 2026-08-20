# Binding evidence

Discovery classifies every candidate agent root as `bound-aligned`, `bound-diverged`,
`bound-unknown`, `ready-unbound`, or `not-ready`. Only `bound-aligned` allows deployment, so a
real deployed baseline must be able to prove alignment. Local repository facts alone cannot do
that: the repository says what *should* be deployed, never what *is*.

`bootstrap discover` therefore accepts reviewed, non-secret **binding evidence** describing the
deployed immutable agent version. Without evidence a repository that declares a binding stays
`bound-unknown`, which is the correct fail-closed answer, not a defect.

## Never trust metadata alone

An evidence record must carry both content fingerprints. A deployed version can advertise the
expected project endpoint, agent name, and version while running entirely different content, so:

- `ObservedAgentBinding` requires `source_fingerprint` and `package_fingerprint`.
- Discovery refuses to return `bound-aligned` when either fingerprint is missing; a metadata-only
  observation is reported as `bound-diverged` with
  `observed binding evidence lacks both content fingerprints; metadata alone cannot prove alignment`.
- Unknown keys in a raw evidence mapping are rejected rather than ignored, so a typo such as
  `sourceFingerprint` cannot silently degrade a comparison.

## Evidence document

`schemas/binding-evidence.schema.json` is generated from `BindingEvidenceInput`:

```json
{
  "schema_version": 1,
  "evidence_version": 1,
  "repository_id": "org/repo",
  "agents": [
    {
      "schema_version": 1,
      "root": "app",
      "repo_agent_id": "app",
      "project_endpoint": "https://example.services.ai.azure.com/api/projects/example",
      "agent_name": "example-agent",
      "agent_version": "1",
      "source_fingerprint": "<sha256>",
      "package_fingerprint": "<sha256>",
      "evidence_provenance": "foundry_agent_code_download",
      "code_content_hash": "<sha256 of the downloaded code archive>",
      "code_content_hash_verified": true,
      "observed_at": "2026-08-17T00:00:00Z"
    }
  ]
}
```

Only identifiers, digests, and timestamps are persisted; no repository content, prompts,
responses, traces, or dataset rows ever appear in an evidence document.

`evidence_provenance` is either:

| Value | Meaning |
| --- | --- |
| `foundry_agent_code_download` | Fingerprints were derived by downloading the immutable agent version's code archive. Requires `code_content_hash` and `code_content_hash_verified: true`. |
| `reviewed_operator_attestation` | A human reviewed the deployed version and attests to the digests. Cannot claim a verified content hash. |

## Supplying evidence

Exactly one source may be used per discovery run; supplying both fails closed with
`binding-evidence-conflict`.

```bash
# 0. read the local digests the evidence must reproduce
foundry-opt bootstrap discover --repo-root . --repository-id org/repo --plan-input plan-input.json

# 1. observe the deployed immutable version (uses the project adapter)
foundry-opt bootstrap binding-evidence \
  --repo-root . --plan-input plan-input.json --output binding-evidence.json

# 2. review the file, then discover with it
foundry-opt bootstrap discover \
  --repo-root . --repository-id org/repo --plan-input plan-input.json \
  --binding-evidence binding-evidence.json
```

`bootstrap discover` emits an `agents` array with the local facts needed to construct or review
an evidence document and to record a pilot receipt:

```json
{"agents": [{"repoAgentId": "app", "root": "app",
  "configPath": "app/.foundry/agent-metadata.yaml", "sourceRoot": "app", "packageRoot": "app",
  "sourceFingerprint": "<sha256>", "packageFingerprint": "<sha256>",
  "classification": "bound-unknown", "detail": "expected binding exists without observed evidence",
  "confidence": 0.95, "blockers": [], "approvedSharedSourceRepoAgentIds": []}]}
```

The same records are persisted in the operation state under
`selection_plan.discovered_agents`, so a later review can re-check the exact digests an
observation had to reproduce. `candidates` (binding assessments only) is still emitted for
compatibility.

## Discovery root and managed root

`repository.selected_agents` keeps two roots when repository-level metadata describes code in
a child directory:

- `discovery_root` is the candidate root emitted as `root` by discovery. Binding evidence and
  later rediscovery continue to use this value.
- `root` is the managed agent directory written to `.foundry-opt/registry.yaml`. Managed
  sidecars, editable paths, evaluation policy, and deployment change detection use this value.

For example, discovery can report `root: "."` and `sourceRoot: "agent"`. The reviewed plan input
must preserve both facts:

```json
{
  "repo_agent_id": "luffy-travel-approver",
  "discovery_root": ".",
  "root": "agent",
  "config_path": "agent/.foundry/foundry-opt.yaml",
  "editable_paths": ["agent/**"]
}
```

Plan generation verifies these values against the persisted discovery record. It refuses a
different discovery root or a managed root that does not equal `sourceRoot` for a repository-root
candidate. Older plan inputs that used `root: "."` are normalized only when
`config_path` deterministically identifies a concrete managed directory such as
`agent/.foundry/foundry-opt.yaml`.

Alternatively the same document may be embedded in the reviewed plan input under
`binding_evidence`, in which case `--binding-evidence` must be omitted. Nested records are
cross-checked against `repository.selected_agents` (`discovery_root` and `repo_agent_id`) and,
when the evaluations phase is present, against the reviewed `project_endpoint`, `agent_name`,
and `agent_version`.

## Plan-time claim verification

`bootstrap evaluation plan` re-derives the classification whenever the plan input carries
binding evidence, and refuses to plan when a reviewed onboarding contract claims a different
classification:

```json
{"status": "error", "error": {"code": "binding-classification-mismatch",
 "details": {"repo_agent_id": "app", "approved": "bound-aligned", "observed": "bound-diverged"}}}
```

The successful response echoes `verified_binding_classifications`. Without evidence the map is
empty and the reviewed claim stands as-is — there is nothing to verify it against, which is
exactly why evidence is needed for a real deployed baseline.

## How observation works

`FoundryAdapter.observe_agent_binding` uses the real agent surface:

1. `agents.get_version(agent_name, agent_version)` resolves the immutable version and its
   `code_configuration.content_hash`.
2. `agents.download_code(agent_name, agent_version=...)` streams the version's code archive
   under a bounded byte budget.
3. The archive digest must reproduce the published `content_hash` exactly, or the observation
   fails closed.
4. Each archive entry is hashed individually and re-rooted onto its repository-relative path
   under the discovered `sourceRoot`/`packageRoot`. Absolute, traversing, and non-fingerprintable
   entries (for example `__pycache__`) are dropped exactly as discovery drops them.
5. Before each file hash, valid UTF-8 text without NUL bytes canonicalizes CRLF and bare CR line
   endings to LF. Binary and non-UTF-8 bytes remain byte-exact. This makes Windows and Linux
   checkouts comparable without hiding binary drift.
6. `fingerprint_files` — the same canonical algorithm discovery uses locally — produces the two
   digests, so observed and local fingerprints are directly comparable.

The exact runtime pin selects the fingerprint algorithm. After upgrading the runtime, rerun
discovery and review binding evidence under a fresh operation; never carry an old plan hash or
approval into the new runtime.

A project that publishes no downloadable code archive, or an agent version that does not match
the requested version, produces an error instead of a weaker classification. In that case an
explicit `reviewed_operator_attestation` document is the supported v1 path.
