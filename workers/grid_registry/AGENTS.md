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
- `drift_lifecycle.py` holds the library supersession composer, `stability.py`
  owns the pre-registration signature stability sweep, and
  `shared_binding_eligibility.py` owns the shared-binding grant. They are
  decision layers over store protocols; keep SQL out of them.
- `packages/common/grid_registry_store.py` owns the registry store SQL and its
  append-only guarantees, and `__main__.py` is the first-registration CLI
  (`python -m workers.grid_registry`). Workers may import `packages`, never
  `services` or `apps`.
- `scripts/register_grid_snapshot_drift.py` is the operator drift tool and does
  not go through the store. It validates with `read_input_record` and
  `prepare_snapshot` (the same checks as first registration), then carries its
  own SQL in one transaction: insert into `met.canonical_grid_snapshot` and
  `met.canonical_grid_cell`, supersede the prior snapshot, and invalidate
  derived `met.met_station` and `met.interp_weight` rows.

## State And Mutation Fences

- Registration is append-only and immutable. `insert_snapshot` writes one
  snapshot plus its ordered cells atomically; delete and identity-field update
  primitives always raise `RegistryImmutabilityError`.
- Post-insert writes today: the store primitives `supersede` and
  `extend_applicable_source_ids` (shared eligibility), plus the drift script's
  raw `UPDATE` statements (prior `superseded_at`, and `active_flag` and
  `superseded_at` on derived `met.met_station` and `met.interp_weight` rows).
  Do not add another mutation path without a spec change.
- A different signature for an existing `(source_id, grid_id)` is drift:
  `register_snapshot` refuses it with `GridDriftDetectedError`, and the
  operator path is `scripts/register_grid_snapshot_drift.py`.
  `reject_inplace_signature_replacement` always raises; never edit a
  signature in place.
- `drift_lifecycle.py:register_new_version()` is a library-level composer with
  no production caller today; only its tests drive it, and the
  `mark_derived_stale` primitive it needs exists only as a protocol and test
  fakes. Its rollback is best-effort across store primitives: if marking
  derived caches stale fails it restores the prior `superseded_at`. Preserve
  that inversion when touching the composer.
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
