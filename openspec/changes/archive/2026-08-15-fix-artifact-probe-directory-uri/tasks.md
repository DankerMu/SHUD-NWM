# Tasks: fix-artifact-probe-directory-uri (#1365)

Fixture level: expanded · Repair intensity: high · Seams under test: the public
decision seam `_missing_upstream_forecast_artifact_evidence` (via
`evaluate_candidate`-level fixtures where practical) plus the probe unit seam
`_artifact_uri_missing_status` — declared by the issue's 复核命令; no new seams.

## Risk packs (considered)

- File IO / path safety / overwrite: **selected** — probe path resolution,
  derived-key construction, symlink/containment behavior unchanged but must be
  regression-anchored.
- Error handling / rollback / partial outputs: **selected** — fail-closed
  semantics, stable blocker codes, `unsafe_reason` contract.
- Schema / columns / units / field names: **selected** — evidence payload shape
  (`forcing_provenance.probe/probe_key`, `unsafe_reason` values).
- Legacy compatibility / examples: **selected** — existing 6-seg file-key
  behavior, sidecar tier, raw-manifest lanes must be byte-identical decisions.
- Concurrency / shared state / ordering: not selected — probe is a pure read.
- Public API / CLI / script entry: not selected — no public API change.
- Auth / permissions / secrets: not selected — no credential surface.
- Config / project setup: not selected — no new config; root env vars already
  exist.
- Resource limits / large input / discovery: not selected — `exists` probe
  only, no new reads.
- Release / packaging / dependency compatibility: not selected — no deps.
- Documentation / migration notes: **selected** (round-1 re-selection, cand-07
  — the PR introduces a new non-repairable blocker class and new
  `probe`/`probe_key` semantics the operator routing table must know) —
  `docs/runbooks/current-production-ops.md` artifact-guard section: journal/
  direct-tier `probe`/`probe_key` semantics line, `object_store_root_unconfigured`
  routing row (remedy = configuration, rebuild ineffective), `artifact_probe_error`
  routing row (remedy = clear filesystem fault, rebuild ineffective).
- Domain packs (forcing/time-series, geospatial, solver): not selected — no
  numerics/geometry; forcing surface touched only at the probe layer.

## Tasks

- [x] 1. Extract the shared witness-key derivation helper (producer-isomorphic,
  reused by `_sidecar_manifest_probe_key` construction and the new tier-1/2
  leg); no hand-joined manifest literals outside it.
- [x] 2. Tier-1/2 forcing leg: derive witness key for directory-shaped object
  URIs before probing; surface `probe`/`probe_key` in `forcing_provenance`;
  blocker `artifact_uri` stays the recorded package URI.
- [x] 3. Root-unconfigured ruling in `_artifact_uri_missing_status` object
  branch: `(True, "object_store_root_unconfigured")`, no probe call.
- [x] 4. Copyback leg comment recording D3 (inherits root ruling, no witness
  derivation, no production writer).
- [x] 5. Tests (`tests/test_production_scheduler.py`): production-shaped
  directory URI fixture + configured-root scenarios; update the `:89-95`
  coincidence comment; adjust tests that leaned on root-unconfigured fail-open
  (configure root, never weaken assertions); update the exact-equality
  journal-tier `forcing_provenance` assertions to include the new
  `probe`/`probe_key` keys — extended, never relaxed to subset checks. (Of the
  three anchors listed at fixture time, `:9550` was the journal-tier one; the
  `:9700`/`:9828` dicts turned out to be sidecar `source=absent` payloads and
  were correctly left untouched.)
- [x] 6. Repair-authorization interaction (D5): tests for root-unconfigured
  blocker rejected as `forcing_artifact_reference_unsafe`, and the
  configured-root probed-absent pair staying repair-eligible
  (`test_repair_authorization_accepts_both_missing_forcing_blocker_pairs`
  unweakened).

## Required evidence (maps every selected pack)

- Positive: root configured + package present + directory URI (5-seg trailing
  `/`) -> not missing; recovery leg does NOT emit
  `FORCING_PACKAGE_URI_MISSING`. [File IO, Legacy]
- Negative: root configured + package absent + directory URI -> missing,
  `FORCING_PACKAGE_URI_MISSING`, `unsafe_reason=None`. [Error handling]
- Root unconfigured + `s3://nhms/totally/bogus/nonexistent.json` -> `(True,
  "object_store_root_unconfigured")`; candidate-level blocker carries the
  reason. [Error handling, Schema]
- File-shaped 6-seg URI regression: present -> not missing; absent -> missing.
  [Legacy]
- Copyback object URI + root unconfigured -> `COPYBACK_SOURCE_MISSING` with
  `unsafe_reason="object_store_root_unconfigured"`. [Error handling]
- Provenance payload (Schema): root configured + directory URI + manifest
  present -> `forcing_provenance["probe"] == "manifest"` and
  `forcing_provenance["probe_key"] == "<uri.rstrip('/')>/forcing_package.json"`
  derived via the shared helper; blocker `artifact_uri` remains the recorded
  directory URI. [Schema]
- Repair gate (D5): root-unconfigured blocker + authorized repair ->
  `rejected("forcing_artifact_reference_unsafe")`; root configured +
  probed-absent blocker -> repair accepted (both existing blocker pairs).
  [Error handling, Legacy]
- Sidecar-tier decisions unchanged (existing sidecar tests stay green
  untouched). [Legacy]
- Commands: `uv run pytest -q tests/test_production_scheduler.py -k
  missing_forcing` (issue text said baseline 36; measured master baseline is
  40 — the binding floor; 42 after round 0, must not shrink), targeted new
  tests, `uv run ruff check .`.

## Round-1 fix tasks (Phase 5/6)

- [x] 7. Contain `ObjectStoreError` in `_artifact_uri_missing_status` object
  branch as `(True, "artifact_probe_error")` (cand-01); tests: symlinked leaf +
  monkeypatched raise at unit and decision seams, pass survives, repair gate
  rejects.
- [x] 8. Re-key derivation trigger off `validate_object_path` admissibility
  (cand-05); tests: no-trailing-slash 5-seg shapes (bare + `s3://`) present →
  not missing / absent → missing, and 6-seg file key never double-derived.
- [x] 9. Narrow the copyback D3 comment to pattern-depth truth (cand-03).
- [x] 10. De-vacuate `test_unattributed_cycle_marker_does_not_drive_a_manual_
  retry_decision`: fixture + positive pin (cand-04).
- [x] 11. Runbook amendments per re-selected Documentation pack (cand-07).

## Non-goals

- `validate_object_path` grammar, producer write-side shapes, sidecar tier
  logic, raw-manifest repair lanes (`_object_manifest_is_missing` direct
  callers), #1367 placeholder guard, #1203 write side, local-path leg.
