# Fix artifact existence probe for directory-shaped object URIs (#1365)

## Why

The failure-state artifact probe (`_artifact_uri_missing_status`) misjudges every
canonical directory-shaped `forcing_package_uri` (5 segments, trailing `/`) as
"missing" on deployments with an object-store root configured (node-22
production): `validate_object_path` only admits file keys (`len(parts) >
len(pattern.segments)`), the resulting `ValueError` is swallowed into
`missing=True`, and the recovery leg emits `FORCING_PACKAGE_URI_MISSING` even
though the package physically exists — a false deadlock the operator can only
clear by quarantining the row. The sidecar tier (#1203) already derives a
manifest FILE key before probing; the journal/direct tier-1/2 leg and the
copyback leg still hand the probe the raw directory URI.

Second gap (bundled by the issue, not a separate ticket): with no object-store
root configured, `_object_manifest_is_missing` fail-opens (`return False`) for
ANY URI — a guard that claims fail-closed silently passes everything, with no
evidence that no probe ran.

## What Changes

- Tier-1/2 (journal/direct) forcing leg: a package-prefix-shaped object URI is
  never probed directly; the trigger is validator admissibility — any recorded
  object URI that `validate_object_path` does not admit as a FILE key, with or
  without the trailing `/` (round-1 amendment: the handoff lane normalizes the
  slash away, both shapes live in `met.forcing_version`) — and the probe target
  becomes the package manifest FILE key derived from it (producer-isomorphic
  `_package_manifest_uri` construction, existing
  `_FORCING_PACKAGE_MANIFEST_FILENAME` constant — never hand-joined).
- Root-unconfigured ruling (explicit decision, D2): the object-URI branch of
  `_artifact_uri_missing_status` fails CLOSED — `(True,
  "object_store_root_unconfigured")` — so "no probe ran" is distinguishable in
  blocker evidence from "probed, absent" (`unsafe_reason=None`).
- Probe-fault containment (round-1 amendment): a store-side `ObjectStoreError`
  is contained inside the probe as `(True, "artifact_probe_error")` — a third
  distinguishable unsafe reason, rejected by the authorized repair channel —
  and a `ValueError` raised while classifying the recorded URI's shape never
  escapes the decision path; the scheduler pass survives both.
- Copyback leg: shares the probe, so it inherits the root ruling; the
  directory-shape witness derivation is forcing-specific and documented as not
  applicable to copyback (no canonical witness filename exists for a copyback
  source directory) via code comment.
- Test fixtures gain the production-shaped (5-segment, trailing-`/`) URI with a
  configured root; the `tests/test_production_scheduler.py:89-95` coincidence
  comment is updated.

## Impact

- Affected specs: `job-retry-mechanism` (ADDED requirement).
- Affected code: `services/orchestrator/scheduler_state_failure.py`
  (`_artifact_uri_missing_status`, tier-1/2 forcing leg, copyback leg comment),
  `tests/test_production_scheduler.py`, and
  `docs/runbooks/current-production-ops.md` (journal/direct-tier
  `probe`/`probe_key` semantics plus the `unsafe_reason` operator routing
  table — round-1 amendment).
- Not touched: `packages/common/storage.py` closed-world path grammar,
  producer write-side URI shapes, sidecar tier behavior, raw-manifest repair
  lanes (`_missing_raw_manifest_repair_evidence` / downstream twin call
  `_object_manifest_is_missing` directly with file keys — out of scope).

`design.md` carries the decision record and Invariant Matrix (fixture level
expanded, repair intensity high).
