# Production Closure Agent Instructions

This file scopes root `AGENTS.md` for `services/production_closure/`. The
current authority for shared governance vocabulary is `openspec/glossary.md`;
reuse terms such as lane, current authority, historical evidence, and
budget-counted finding exactly as the glossary defines them.

## Required Reading

- `openspec/changes/governance-7-structural-entropy-controls/specs/scoped-agent-context-governance/spec.md`
- `docs/runbooks/node-27-bringup-checklist.md`
- `docs/governance/TWO_NODE_E2E_EVIDENCE_LANE_INVENTORY.md`
- `docs/governance/READINESS_VALIDATION_LANE_INVENTORY.md`
- `openspec/glossary.md`

`docs/runbooks/node-27-bringup-checklist.md` is the runbook freshness anchor
for display_readonly live receipts. It separates deterministic/local checks from
node-27 live DB/display/browser receipts; do not claim production readiness from
mocked, deterministic, or historical evidence unless the inventory explicitly
marks that evidence as review-only.

## Lane Ownership

- Keep the stable public entrypoints intact:
  `validate_two_node_e2e_evidence(config)` for two-node E2E evidence and
  `validate_readiness(config)` / `validate_readiness_item(item)` for production
  readiness. Future lane modules must sit behind those entrypoints until
  equivalence is proven by focused tests.
- Treat `two_node_e2e_evidence.py` and `readiness_validation.py` as aggregators
  with governed lanes, not as places to add unrelated shortcuts. New lane names,
  discovery aliases, status semantics, blocker/finding namespaces, pass aliases,
  or output fields require the matching inventory update.
- Do not perform lane extraction in a scoped-instruction or inventory-only
  issue. Extraction requires a separate issue that preserves the existing
  entrypoint, lane result shape, blocker/finding split, redaction behavior, and
  final aggregation semantics.

## Evidence Contracts

- Evidence schemas, producer identities, strict identities, current-run binding,
  artifact digests, and final status semantics are contracts. Do not accept
  wrapper metadata, diagnostics, stale bundles, or sibling-source artifacts as a
  substitute for producer-backed evidence.
- Public summaries must stay redacted. Database URLs, credentials, auth tokens,
  local private paths, log URI secrets, and raw live-proof payloads must not
  appear in committed docs, PR comments, or generated public artifacts.
- Path safety rules are part of the lane contract. Two-node E2E artifact/final
  evidence paths use approved evidence roots. Readiness validation uses safe
  configured roots and files: bounded JSON reads, regular files only, no
  symlink/traversal escape, containment for writes, no unsafe overwrite without
  explicit `force`, and stable blocker codes for unsafe or stale evidence.
- Keep deterministic review evidence separate from live proof. Dependency
  summaries and scheduler review evidence can help reviewers, but they do not
  satisfy node-27 live readiness or two-node E2E closure unless the current
  authority says a live receipt was produced.

## Readonly Boundary Invariants

- Node-27 is the live DB/display/frontend oracle for readonly and display
  receipts. Its display_readonly validation must prove denied-write behavior and
  must not create retry/cancel/Slurm/control-plane receipts.
- Node-22 remains the compute/Slurm oracle only for sbatch, Slurm gateway, SHUD
  runtime, or scheduling behavior. Do not route node-27 readiness through
  node-22-only evidence unless the runbook or inventory names that cross-plane
  source explicitly.
- A PASS final status must reflect the governed lane status ordering and source
  scope. Missing, stale, unsafe, incomplete, or non-authoritative proof is a
  blocker; evidence that proves an unsafe condition is a finding.

## Focused Verification

Always run the issue-required governance checks after changing this file or
production_closure scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_script.py
openspec validate --all --strict --no-interactive
```

For two-node E2E evidence lane changes, add the relevant focused command,
commonly:

```bash
uv run pytest -q tests/test_two_node_e2e_evidence.py
```

For readiness validation lane changes, add the relevant focused command,
commonly:

```bash
uv run pytest -q tests/test_production_readiness_validation.py
```
