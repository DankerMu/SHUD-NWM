# Common Package Agent Instructions

This file is the scoped supplement of root `AGENTS.md` for `packages/common/`.
Shared vocabulary lives in `openspec/glossary.md`; use its terms (current
authority, compatibility facade, active entrypoint) exactly as defined there.

## Required Reading

- `docs/governance/ROLE_BOUNDARY.md`
- `docs/adr/0009-path-canonicalization-dereference-doctrine.md`
- `docs/runbooks/tier-node27-timeseries-storage.md`
- `tests/import_layering_allowlist.json`

`packages/common` is the `shared_contract` role in
`docs/governance/ROLE_BOUNDARY.md`: every plane imports it, so a change here
changes compute, display, ingest, and gateway behavior at once.

## Ownership Boundaries

- This is the bottom layer. Code under `packages/` must not import `workers`,
  `services`, `apps`, or `scripts`. `tests/test_import_layering_guard.py`
  parses every module and fails on an upward import; the only tolerated
  exceptions are the entries in `tests/import_layering_allowlist.json`. Never
  add an entry: move the needed contract down instead. The guard also fails on
  a stale entry, so remove the entry in the same change that removes the import.
- Contracts that several planes parse belong under `packages/common/contracts/`
  (today `packages/common/contracts/direct_grid.py`,
  `packages/common/contracts/time_normalization.py`,
  `packages/common/contracts/region.py` and
  `packages/common/contracts/hydrologic_fingerprint.py`). Import them from
  there: the old worker-side shims were deleted, and
  `tests/test_import_layering_guard.py` rejects old-path imports. Put new
  logic in the contract module.
- `model_registry.py` is a stable import path: `PsycopgModelRegistryStore` is
  composed from the `model_registry_*.py` owner mixins, and every name they
  define is re-exported by identity. Add behavior to the owner module, not to
  the facade.
- Do not hide a role-specific control-plane action (Slurm submission, scheduler
  decisions, display-only routing) inside a common helper. Shared helpers carry
  contracts, identity, storage, and guard primitives only.
- Source identifiers go through `source_identity.py:normalize_source_id()`;
  do not re-implement case or alias rules at a call site.

## State And Mutation Fences

- Filesystem writes that publish evidence, manifests, or artifacts use the
  no-follow, exclusive, or atomic helpers in `safe_fs.py`,
  `safe_fs_publication.py`, and `object_store.py`
  (`LocalObjectStore.write_bytes_atomic`). Evidence readers use the
  descriptor-pinned, bounded readers in `evidence_io.py`. Do not replace them
  with a bare `open()` plus `os.replace`.
- A path canonicalization site that tolerates the non-strict
  `os.path.realpath` fallback must satisfy one clause of ADR 0009 and record the
  clause marker in the function body; otherwise it re-checks strictly.
  `tests/test_path_canonicalization_family_guard.py` enforces the family.
- Ingest writes into the guarded TimescaleDB hypertables call
  `timescale_write_guard.py:check_batch_targets_uncompressed()` before the
  delete-and-insert and take the shared ingest fence from
  `timeseries_compression_fence.py` (`try_ingest_fence`, raising
  `IngestFenceBusy` when chunk DDL holds it). A new delete site without the
  guard fails `tests/test_timescale_write_guard_wire_site_invariant.py`.
- Payloads that may carry credentials or secret-bearing URLs pass through
  `redaction.py` (`redact_payload`, `redact_text`) before they reach logs,
  evidence, or API responses.
- Capacity and storage claims for the node-27 timeseries tier are measured, not
  copied: follow `docs/runbooks/tier-node27-timeseries-storage.md`.

## Focused Verification

Always run the issue-required governance checks after changing this file or
`packages/common` scoped context:

```bash
uv run pytest -q tests/test_entropy_audit_*.py
openspec validate --all --strict --no-interactive
```

`packages/common` has no directory rule in the CI selector, so ask the selector
which suites own the module you changed, then run them:

```bash
printf '%s\n' packages/common/safe_fs.py | uv run python scripts/select_ci_tests.py
```

For identity, filesystem-safety, and object-store root changes, a common
focused set is:

```bash
uv run pytest -q tests/test_source_identity.py tests/test_safe_fs.py tests/test_safe_fs_lock.py tests/test_object_store_roots.py
```

Any import change under `packages/` also runs the layering guard:

```bash
uv run pytest -q tests/test_import_layering_guard.py
```
