# Grid Registry Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for
`workers/grid_registry/`. Shared vocabulary lives in `openspec/glossary.md`;
use its terms exactly as defined there.

## Required Reading

- `openspec/specs/grid-drift-lifecycle/spec.md`
- `openspec/specs/grid-signature-stability-verification/spec.md`
- `openspec/specs/source-shared-binding-eligibility/spec.md`
- `packages/common/grid_registry_store.py`

This worker registers canonical source grid snapshots and decides their
lifecycle. Direct-grid forcing and mapping builds trust a grid only through a
registered snapshot, so a wrong decision here propagates to every consumer.

## Ownership Boundaries

- `input_record.py` owns the input contract: it reads a canonical grid
  definition plus its metadata sidecar (validated against
  `schemas/grid_snapshot_metadata.schema.json`), normalizes longitudes and
  axis order, and returns a frozen `GridSnapshotInputRecord`. It touches no
  database or object store; longitude normalization is the forcing
  producer's `_normalize_longitude`, imported lazily.
- `registry.py` owns the writer (`register_snapshot`, `prepare_snapshot`): it
  computes `grid_signature` through `packages/common/grid_signature.py`,
  derives the key through `packages/common/canonical_grid_key.py`, and
  cross-checks the signature against the forcing producer's live grid-point
  path (lazy imports from `workers/forcing_producer/producer.py`).
- `drift_lifecycle.py` owns supersession decisions, `stability.py` owns the
  pre-registration signature stability sweep, and
  `shared_binding_eligibility.py` owns the shared-binding grant. They are
  decision layers over store protocols; keep SQL out of them.
- `packages/common/grid_registry_store.py` owns all SQL and the append-only
  guarantees. `__main__.py` is the CLI (`python -m workers.grid_registry`);
  `scripts/register_grid_snapshot_drift.py` is the operator wrapper for drift.
  Workers may import `packages`, never `services` or `apps`.

## State And Mutation Fences

- Registration is append-only and immutable. `insert_snapshot` writes one
  snapshot plus its ordered cells atomically; delete and identity-field update
  primitives always raise `RegistryImmutabilityError`.
- Only two post-insert writes exist: `supersede` (drift lifecycle) and
  `extend_applicable_source_ids` (shared eligibility). Do not add a third
  mutation path without a spec change.
- A different signature for an existing `(source_id, grid_id)` is drift:
  `register_snapshot` refuses it with `GridDriftDetectedError`, and
  `drift_lifecycle.py:register_new_version()` registers a new version,
  supersedes the prior, and marks derived caches stale.
  `reject_inplace_signature_replacement` always raises; never edit a
  signature in place.
- `register_new_version` rollback is best-effort across store primitives: if
  marking derived caches stale fails it restores the prior `superseded_at`.
  Preserve that inversion when touching the composer.
- Shared-binding denials run in a pinned order (key mismatch, missing
  comparison evidence, single-source verification, incomplete source ids) and
  raise `SharedBindingEligibilityError` subclasses; acceptance extends both
  snapshots with the same canonically ordered pair.
- Every failure is a typed error (`GridSnapshotInputError`,
  `RegistrationError`, `StabilityVerificationError`,
  `SharedBindingEligibilityError` families). The CLI maps input, registration,
  store, and source-id errors to exit 1 with one stderr line and prints the new
  `grid_snapshot_id` only on success.

## Focused Verification

Always run the issue-required governance checks after changing this file or
grid registry scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

For input-record, writer, drift, stability, eligibility, or bbox changes, run
the focused set (the directory has no selector rule):

```bash
uv run pytest -q tests/test_grid_drift_lifecycle.py tests/test_grid_stability_verification.py tests/test_shared_binding_eligibility.py tests/test_grid_registry_bbox.py
```
