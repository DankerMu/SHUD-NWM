# Production Closure Agent Instructions

This file scopes root `AGENTS.md` for `services/production_closure/`. The
current authority for shared governance vocabulary is `openspec/glossary.md`;
reuse terms such as lane, current authority, historical evidence, and
budget-counted finding exactly as the glossary defines them.

## Required Reading

- `openspec/changes/governance-7-structural-entropy-controls/specs/scoped-agent-context-governance/spec.md`
- `docs/runbooks/node-27-bringup-checklist.md`
- `openspec/glossary.md`

`docs/runbooks/node-27-bringup-checklist.md` is the runbook freshness anchor
for display_readonly live receipts. It separates deterministic/local checks from
node-27 live DB/display/browser receipts; do not claim production readiness from
mocked, deterministic, or historical evidence.

## What Lives Here

- C2 read-only DB boundary validator: `readonly_db_validation` (entrypoint,
  driven by `scripts/validate_readonly_db_boundary.py`) with its owners
  `readonly_db_types`, `readonly_db_route_smoke`, `readonly_db_merge`,
  `readonly_db_permission_probes`, `readonly_db_probe_adapter`, and the shared
  `identity_matching` helper. It loads `apps/api/readonly_validation_probe.py`
  dynamically.
- C4 production acceptance: `c4_production_acceptance` and
  `c4_production_acceptance_io`, driven by
  `scripts/node27_c4_production_acceptance.py`.

Do not add new lanes here; a new validator needs its own issue and owner.

## Evidence Contracts

- Evidence schemas, producer identities, strict identities, current-run binding,
  artifact digests, and final status semantics are contracts. Do not accept
  wrapper metadata, diagnostics, stale bundles, or sibling-source artifacts as a
  substitute for producer-backed evidence.
- Public summaries must stay redacted. Database URLs, credentials, auth tokens,
  local private paths, log URI secrets, and raw live-proof payloads must not
  appear in committed docs, PR comments, or generated public artifacts.
- Path safety rules are part of the contract: bounded JSON reads, regular files
  only, no symlink/traversal escape, containment for writes, no unsafe overwrite
  without explicit `force`, and stable blocker codes for unsafe or stale
  evidence.

## Readonly Boundary Invariants

- Node-27 is the live DB/display/frontend oracle for readonly and display
  receipts. Its display_readonly validation must prove denied-write behavior and
  must not create retry/cancel/Slurm/control-plane receipts.
- Node-22 remains the compute/Slurm oracle only for sbatch, Slurm gateway, SHUD
  runtime, or scheduling behavior. Do not route node-27 acceptance through
  node-22-only evidence unless the runbook names that cross-plane source
  explicitly.
- A PASS final status must reflect the governed status ordering and source
  scope. Missing, stale, unsafe, incomplete, or non-authoritative proof is a
  blocker; evidence that proves an unsafe condition is a finding.

## Focused Verification

Always run the issue-required governance checks after changing this file or
production_closure scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For C2 or C4 changes, add the relevant focused command, commonly:

```bash
uv run pytest -q tests/test_readonly_db_*.py tests/test_identity_matching.py
uv run pytest -q tests/test_node27_c4_production_acceptance*.py
```
