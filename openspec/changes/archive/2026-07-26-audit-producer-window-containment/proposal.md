# Inventory-audit producer provenance: window fields use containment, not equality (#1158)

## Why

The node-27 storage-inventory audit fail-closed blocks retention with
`EVIDENCE_BLOCKED` ("product archive producer end_time differs from DB
inventory for forc_gfs_2026062600_basins_heihe_shud"). Live diagnosis
(node-27, 2026-07-26): the archived package's producer window is
`2026-06-26T00:00:00Z → 2026-07-03T00:00:00Z` while the DB row (and its
actual `met.forcing_station_timeseries` max `valid_time`, 574 224 rows)
ends at `2026-07-02T21:00Z` — the incident-era ingest of that one cycle
was truncated by 3 h for all 13 models, so the archive is a strict
SUPERSET of DB coverage. Adjacent cycles (2026062500/2026062700) match
exactly; the skew is a one-off, not a fencepost convention.

`_verify_product_producer_provenance`
(`scripts/node27_storage_inventory_audit.py:610-630`) compares ALL
producer fields — identity (`kind`, `subject_id`, `manifest_path`,
`model_id`, `basin_version_id`) AND coverage (`start_time`, `end_time`)
— with one equality loop. The neighbouring forcing-manifest range check
(`:700-702`) already uses the correct containment direction
(`manifest_start > subject.start or manifest_end < subject.end` →
blocked). Equality on coverage misclassifies a legitimately-superset
archive as corrupted evidence and deadlocks the retention chain: the
archive can never be "fixed" (it is complete), and the DB row is
truthful (data genuinely ends at 21:00).

## What Changes

- `_verify_product_producer_provenance`: split the expected-field loop —
  identity fields keep equality; `start_time`/`end_time` switch to
  containment (`producer_start <= subject.start AND producer_end >=
  subject.end`, after `_parse_time`; unparseable → blocked). Archive ⊂
  DB window still blocks (that IS a completeness gap) with a NEW stable
  message ("product archive producer window does not contain DB
  inventory window for <subject>"); identity mismatch keeps the current
  per-field message. `manifest_sha256` binding unchanged. No upper bound
  on producer-window size is added: the embedded/outer producer equality
  binding (`node27_product_archive.py:1948-1959`) and the mover's
  `start <= cycle <= end` check already pin the window to the real
  source package declaration.
- Tests (red-provable) in the existing audit suite: equality (green
  today, stays green), superset (RED today → green — the incident
  shape), subset (must STAY blocked — fail-closed invariant), identity
  field mismatch (must stay blocked).
- ADDED spec requirement (new title) in `timeseries-product-archive`
  pinning containment semantics for producer-window verification (the
  umbrella requirement in live change `tier-node27-timeseries-storage`
  says only "bind that provenance … to the DB subject"; equality was an
  implementation choice, and same-title ADDED would collide at archive).

## Impact

- Affected specs: `timeseries-product-archive` — ADDED "Producer
  provenance window verification SHALL use containment semantics".
- Affected code: `scripts/node27_storage_inventory_audit.py` (one
  function); tests `tests/test_node27_storage_inventory_audit.py` (the
  only inventory-audit test module; test seam is
  `audit.verify_product_archive`).
- Must preserve: fail-closed on subset/identity-mismatch/missing
  provenance/missing digest; receipt schema 1.1 and outcome vocabulary
  untouched; no receipt field changes; `AuditBlocked` flow and stable
  reason codes unchanged; the shared archive-path binding (`:571`) and
  the state lane (`:574-582`, producer check is gated to
  forcing/runs at `:572-573`) untouched.
- Known residual (disclosed, out of scope — fixture-review P2-3): the
  runs-lane HOT check `_verify_run_hot` (`:753-772`) still compares
  `start_time`/`end_time` by EQUALITY against the same
  `input/manifest.json`; a runs subject with the same window skew and a
  surviving hot copy would still block the receipt (`run manifest row
  identity mismatch`), and the audit main loop has no per-subject
  isolation — this is the possible NEXT block point after this fix and
  is handled by a follow-up issue only if node-27 actually hits it.
- Non-goals: any other audit check (incl. `_verify_run_hot` equality);
  ingest repair of the truncated cycle (the DB stays truthful);
  retention/mover/compression code; receipt schema.
- Ops note (post-merge, node-27): rerun
  `nhms-node27-storage-inventory-audit.service` → expect the receipt to
  pass this subject (or surface the next real gap); then the retention
  chain (dry-run → ENFORCE=1 → enable timer) proceeds under ADR 0002
  gating.
