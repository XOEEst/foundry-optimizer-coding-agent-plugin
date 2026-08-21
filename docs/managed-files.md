# Managed files

Bootstrap owns a small, explicit set of repository files. Everything else in a customer
repository stays customer-owned: existing workflows, instructions, skills, `AGENTS.md`, and
`CLAUDE.md` are never overwritten.

## Rendered managed payloads (v1)

The trusted manifest
(`src/foundry_opt/templates/customer-repo/.foundry-opt/managed-payloads.manifest.yaml`) pins
exactly these eight payloads. A plan input must carry the manifest id, version, and hash, and
the manifest is refused if the set differs.

| Template id | Destination | Ownership |
| --- | --- | --- |
| `registry` | `.foundry-opt/registry.yaml` | owned |
| `sidecar` | `<agent-root>/.foundry/foundry-opt.yaml` | owned, lightweight v2 profile during repository apply; receipt-bound verification enrichment during evaluation activation |
| `optimizer-instruction` | `.github/instructions/foundry-opt.instructions.md` | owned |
| `optimizer-issue-form` | `.github/ISSUE_TEMPLATE/foundry-optimize-agent.yml` | owned |
| `setup-semantic-patch` | `.github/workflows/copilot-setup-steps.yml` | shared template, semantic patch only |
| `validation-workflow` | `.github/workflows/foundry-opt-validation.yml` | owned |
| `deploy-workflow` | `.github/workflows/foundry-opt-deploy.yml` | owned |

Bootstrap uses the default Copilot cloud agent plus the managed repository
instructions and installed skill. A specialized custom agent is optional; an
example profile is available at
`examples/custom-agents/foundry-optimizer.agent.md`.

## The committed lock is generated, not rendered

Each registry entry's `root` is the managed agent directory, not necessarily the discovery
candidate root. When repository-level metadata is discovered at `.` but names `agent` as its
`sourceRoot`, the plan input retains `discovery_root: "."` for evidence while the registry uses
`root: agent`.

The approved repository phase converts the exact recognized pre-v1
`.github/workflows/copilot-setup-steps.yml` contract to the v1 managed workflow. Recognition is
fail-closed: the full legacy step sequence and legacy pin/validation markers must match. A
customized, missing, or ambiguous workflow is not overwritten; bootstrap writes the proposed
managed result to `.github/workflows/copilot-setup-steps.yml.foundry-proposed` for human review.

`.foundry-opt/bootstrap.lock.json` is the authoritative committed ownership ledger. Repository
apply generates it from the applied plan; it is **not** a managed payload and cannot be
declared as one. It records the engine and schema version, the runtime repository, channel and
exact revision, every managed or patched file with its ownership mode plus template/applied
digests, adopted GitHub environments and cloud resources, sidecar paths, and the last
activation outcome. It contains no credentials and no raw customer or evaluation content.

Manifests that try to render either the generated lock or the legacy shared pin fail closed.

## Legacy shared pin

`.github/foundry-opt.lock.yml` was the pre-v1 shared-revision pin. It is **not** produced by
v1 bootstrap and is no longer shipped in the customer templates. It remains readable for
migration only:

- legacy single-agent import (`import_legacy_single_agent_documents`) accepts it as one of the
  three legacy inputs, exercised through `tests/bootstrap/fixtures/templates/`;
- `foundry-opt bootstrap verify --pin <path>` still accepts an explicit `SharedPin` document.

## Verifying the runtime revision in v1

The managed setup workflow verifies the fetched runtime against the committed registry instead
of a legacy pin file:

```bash
foundry-opt bootstrap verify \
  --registry .foundry-opt/registry.yaml \
  --uv-lock-sha256 <rendered uv.lock digest> \
  --checkout "$FOUNDRY_OPT_SHARED_ROOT" \
  --receipt "$FOUNDRY_OPT_BOOTSTRAP_RECEIPT"
```

`distribution.repository` and `distribution.pin` supply the repository and exact commit;
verification still requires the checkout HEAD, package path, skill path, and `uv.lock` digest
to match, and writes the same redacted bootstrap receipt. A registry without an exact pin
fails closed.

## Changed-path contract

Deployment and validation treat `.foundry-opt/**` (registry, sidecar lock) and
`.github/workflows/**` as shared-contract changes that expand to every eligible agent. The
legacy lock path is no longer part of that contract.
