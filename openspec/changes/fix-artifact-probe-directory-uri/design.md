# Design: fix-artifact-probe-directory-uri (#1365)

Fixture level: expanded. Repair intensity: high (file IO/path safety + evidence
chain + failure/recovery lane). Project profile: NHMS.

## Context (verified anchors, HEAD of master 2026-08-14)

- Probe: `services/orchestrator/scheduler_state_failure.py:811`
  `_artifact_uri_missing_status` — object branch calls
  `_object_manifest_is_missing` (`scheduler_state_common.py:164`), swallows
  `(OSError, ValueError)` into `(True, None)`.
- Tier-1/2 leg: `scheduler_state_failure.py:473-475` probes the journal-borne
  `forcing_uri` verbatim (directory-shaped in production).
- Copyback leg: `scheduler_state_failure.py:504`.
- Sidecar tier already derives `manifest_probe_key` via
  `_sidecar_manifest_probe_key` (`:696`) + `_FORCING_PACKAGE_MANIFEST_FILENAME`
  (`:596`, `"forcing_package.json"`), isomorphic to producer
  `_package_manifest_uri` (`workers/forcing_producer/producer.py:3915`).
- Root-unconfigured: `_object_manifest_is_missing` returns `False` (fail-open)
  when neither `resource_profile["object_store_root"]` nor `OBJECT_STORE_ROOT`
  is set.

## Decisions

### D1 — Directory-shape witness derivation lives at the forcing call site

A directory-shaped object URI (after strip, non-local, ends with `/`) is never
handed to the probe. The tier-1/2 forcing leg derives the witness manifest FILE
key: `f"{uri.rstrip('/')}/{_FORCING_PACKAGE_MANIFEST_FILENAME}"` — same
construction as `_sidecar_manifest_probe_key`, extracted into one shared helper
(e.g. `_package_manifest_probe_uri(uri)`) so the two tiers cannot drift; no
hand-joined literals. The generic probe itself stays shape-agnostic: derivation
is forcing-domain knowledge (the witness filename is the forcing producer's
manifest), so it does not belong inside `_artifact_uri_missing_status`, which
also serves copyback.

Rejected alternative: teaching `validate_object_path` / `LocalObjectStore` a
`prefix_exists` notion — relaxes the repo-wide closed-world path validator,
"prefix exists" ≠ "package complete" (an empty directory would pass), and
requires re-auditing every `validate_object_path` caller (issue 解决思路 备选).

The probe result for the derived key keeps the existing evidence contract: the
blocker's `artifact_uri` stays the package (directory) URI the journal recorded;
the derived probe key is what was probed, surfaced in `forcing_provenance`
(`probe: "manifest"`, `probe_key: <derived>`) mirroring the sidecar tier's
evidence shape.

### D2 — Root-unconfigured fails CLOSED with a distinguishable reason

Ruling: in the object-URI branch of `_artifact_uri_missing_status`, when neither
`resource_profile["object_store_root"]` nor `OBJECT_STORE_ROOT` is configured,
return `(True, "object_store_root_unconfigured")` WITHOUT calling
`_object_manifest_is_missing`. Rationale:

- Consistent with this file's established doctrine: sidecar tier's
  `store_unconfigured` = "cannot witness", ObjectStoreError containment = "an
  unreadable probe is never a recovery". A guard that cannot probe must not
  vouch for existence.
- `unsafe_reason` propagates through the existing blocker evidence surface
  (`artifact_exists=False` + non-null `unsafe_reason` = "probe unreliable", vs
  `unsafe_reason=None` = "probed, determined absent") — zero new evidence
  plumbing, satisfies the issue AC that bogus URIs are never silently deemed
  present.
- Production (node-22) always configures `OBJECT_STORE_ROOT`
  (`infra/env/compute.scheduler-provider-refresh.env.example`); the ruling only
  bites test/dev deployments, loudly.

`_object_manifest_is_missing` itself is left unchanged: its other callers
(`_missing_raw_manifest_repair_evidence` `:992`, downstream twin `:1045`) probe
file-shaped raw-manifest keys in repair lanes with different fail-open
consequences — sweeping them in expands blast radius; recorded as an
out-of-scope observation for follow-up routing.

### D3 — Copyback leg: inherits D2, exempt from D1, by comment

The copyback leg shares `_artifact_uri_missing_status`, so root-unconfigured
fail-closed applies to it identically. Directory-shape witness derivation does
NOT apply: a copyback source directory has no canonical witness filename (the
forcing manifest name is producer domain), and the repo has no production
writer of `copyback_source_uri` (issue: theoretical leg). A code comment at the
copyback call site records this ruling (AC 4).

### D5 — Root-unconfigured blocker is intentionally non-repairable via the authorized repair channel

`scheduler_candidates.py:1617-1621` rejects the operator-authorized
missing-forcing repair when `artifact_guard.unsafe_reason` is non-null
(`forcing_artifact_reference_unsafe`). Under D2 a root-unconfigured deployment
therefore produces a blocker that this channel refuses — and that is the
intended ruling, not an accident: the exact-cycle forcing rebuild cannot cure a
missing `OBJECT_STORE_ROOT`; routing the operator there would be the same
"repair is ineffective" trap the sidecar tier's probe-error leg already avoids.
The operator remedy is configuration, and the rejection's `unsafe_reason` says
exactly that. Production is unaffected (`infra/compose.compute.yml:63`
hard-requires `OBJECT_STORE_ROOT`). The paired positive must stay intact:
root configured + probed-absent blockers (`unsafe_reason=None`) remain
repair-eligible, and
`test_repair_authorization_accepts_both_missing_forcing_blocker_pairs`
(`tests/test_production_scheduler.py:9448`) must keep passing unweakened.

### D4 — Non-goals

- The `except (OSError, ValueError): return True, None` lane keeps
  `unsafe_reason=None` (making it distinguishable is not in the issue AC; the
  derived-key change already removes the directory-shape `ValueError` source).
- `#1367` redaction-placeholder guard on copyback, `#1203` write-side rows,
  local-path leg (`_local_artifact_path*`), node-27 lanes: untouched.

## Invariant Matrix

Governing invariant: an artifact-existence verdict of "absent"
(`unsafe_reason=None`) is only ever produced by a probe that actually ran
against a resolvable FILE key in a configured object store; every
cannot-determine outcome is fail-closed with a distinguishable reason and never
fail-open into a retry or a silent pass.

Source-of-truth identity/contract: probe tuple `(missing, unsafe_reason)` of
`_artifact_uri_missing_status`; witness key = package URI +
`_FORCING_PACKAGE_MANIFEST_FILENAME` via the single shared derivation helper.

Surfaces:
- Producers: `workers/forcing_producer/producer.py` `_directory_uri` /
  `_package_manifest_uri` (unchanged — shape authority).
- Validators/preflight: `packages/common/storage.py` `validate_object_path`
  (unchanged, closed world); `_object_manifest_is_missing` (unchanged).
- Storage/cache/query: `LocalObjectStore.exists` (unchanged).
- Public routes/entrypoints: scheduler failure-recovery decision
  (`_missing_upstream_forecast_artifact_evidence`) — tier-1/2 forcing leg
  changed, copyback leg comment-only, sidecar tier unchanged.
- Frontend/downstream consumers: `scheduler_candidates.py` blocked routing
  (same reasons/codes) AND the repair-authorization gate
  `scheduler_candidates.py:1617` (`forcing_artifact_reference_unsafe` on
  non-null `unsafe_reason`) — root-unconfigured blockers become intentionally
  non-repairable via that channel (D5); probed-absent blockers stay
  repair-eligible.
- Failure paths/rollback/stale state: root-unconfigured → blocker with
  `object_store_root_unconfigured`; probe `ValueError`/`OSError` → unchanged
  `(True, None)`.
- Evidence/audit/readiness: `forcing_provenance` gains `probe`/`probe_key` on
  the tier-1/2 leg, mirroring sidecar shape; blocker `artifact_uri` unchanged.

Regression rows:
- Tier-1/2, root configured, package present, directory URI (5-seg, trailing
  `/`) -> probe hits derived manifest key -> NOT missing, recovery proceeds.
- Tier-1/2, root configured, package absent, directory URI -> missing,
  `FORCING_PACKAGE_URI_MISSING`, `unsafe_reason=None` (determined absent).
- Root unconfigured, any object URI (incl. `s3://nhms/totally/bogus/x.json`)
  -> `(True, "object_store_root_unconfigured")`, never a silent pass.
- File-shaped 6-seg URI, root configured, present -> NOT missing (existing
  behavior preserved).
- Copyback object URI, root unconfigured -> `COPYBACK_SOURCE_MISSING` with
  `unsafe_reason="object_store_root_unconfigured"`.
- Tier-1/2, root configured, directory URI, manifest present ->
  `forcing_provenance` carries `probe == "manifest"` and `probe_key ==
  "<uri.rstrip('/')>/forcing_package.json"` (shared helper), blocker-free.
- Root unconfigured + operator-authorized repair decision ->
  `rejected("forcing_artifact_reference_unsafe")` with
  `unsafe_reason="object_store_root_unconfigured"` (D5).
- Root configured + probed-absent blocker + authorized repair -> repair path
  unchanged (`test_repair_authorization_accepts_both_missing_forcing_blocker_pairs`
  stays green unweakened).
- Sidecar tier (unchanged sibling) -> identical decisions before/after.
- Raw-manifest repair lanes (unchanged sibling, file keys) -> identical
  decisions before/after.

## Review focus

1. D2 blast radius: every existing test that leaned on root-unconfigured
   fail-open must now configure a root or assert the new blocker — no test may
   be weakened to pass.
2. Derivation helper is single-sourced (sidecar + tier-1/2 share it).
3. Evidence contract: `artifact_uri` remains the recorded package URI;
   `probe_key` is evidence, never the other way round.
4. Copyback comment matches actual behavior.
