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
- Documentation / migration notes: not selected — behavior documented in spec
  delta + code comments; no operator-facing doc change required by AC.
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
  journal-tier `forcing_provenance` assertions (`:9550`, `:9700`, `:9828`) to
  include the new `probe`/`probe_key` keys — extended, never relaxed to subset
  checks.
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
  missing_forcing` (baseline 36 passed, must not shrink), targeted new tests,
  `uv run ruff check .`.

## Non-goals

- `validate_object_path` grammar, producer write-side shapes, sidecar tier
  logic, raw-manifest repair lanes (`_object_manifest_is_missing` direct
  callers), #1367 placeholder guard, #1203 write side, local-path leg.
