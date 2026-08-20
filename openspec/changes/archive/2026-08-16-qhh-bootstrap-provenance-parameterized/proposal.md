# Proposal: qhh-bootstrap-provenance-parameterized

## Why

Issue #1415 (#1359 fixture-review out-of-scope observation; same bug
pattern, second producer lane): `_seed_station_rows()` in
`workers/model_registry/qhh_production_bootstrap.py` hardcodes station
provenance `"source": "qhh.tsd.forc"` at :1441 and (nested
`elevation_metadata.source`) :1452, while the ENTIRE call chain is
parameterized on `project_name` (env var `QHH_PROJECT_NAME` →
`scripts/seed_qhh_forcing_stations.py:17/:21/:31` →
`seed_qhh_forcing_stations` :642-646 / `bootstrap_qhh_production` :196 /
manifest `shud_input_name` :1057), the actual asset path is
parameterized (:737 `f"{qhh_project_name}.tsd.forc"`), and the SAME dict
already parameterizes `forcing_source_identity` (:1446) and
`project_name` (:1440). The sibling river lane in the same file got it
right (:1597 `f"{project_name}.sp.riv"`) — this is an in-file
self-contradiction from the file-creating commit (35ae1b96), an
omission, not a decision.

**The defect has ALREADY FIRED in production** (fixture-review P1-1
correction to the issue's "latent" framing):
`tests/fixtures/station_series_baseline_heihe_ifs_2026060100.json:20/:31`
is a sanitized live API response for station `heihe_forc_001` carrying
exactly the predicted self-contradiction — `project_name: "heihe"`,
`forcing_source_identity: "heihe.tsd.forc:1:..."`,
`source_file: .../heihe.tsd.forc`, but `source: "qhh.tsd.forc"` and
`elevation_metadata.source: "qhh.tsd.forc"`. The key signature (`seed`,
`elevation_metadata`, `forcing_source_identity`, `source_sha256`,
x/y/z) is seed-lane-only (the #1359 handoff lane never writes those
keys), so a non-default bootstrap has already run. #1359's
"pre-existing provenance is never overwritten" scenario means the
handoff lane PRESERVES the mislabel — it does not self-heal.

## What Changes

- Issue's RECOMMENDED route: replace both literals with
  `f"{project_name}.tsd.forc"` (2 lines), mirroring the sibling lane
  :1597. Default QHH invocation produces byte-identical
  `"qhh.tsd.forc"` — zero behavior change, zero backfill, and no
  conflict with #1359's acceptance clause ":1441/:1452 保持
  qhh.tsd.forc 不变" (the VALUE is unchanged under default arguments;
  stated explicitly per the issue's instruction).
- Rejected alternative (recorded): `tsd_forc_path.name` (actually-read
  basename) — semantically stronger but diverges from :1446's
  `forcing_source_identity` prefix when path and convention disagree,
  requiring an authority ruling; 2-line route has no such split.
- New regression test PARAMETRIZED over ("qhh", "qhh.tsd.forc") and
  ("heihe", "heihe.tsd.forc") — the qhh leg gives the default path its
  FIRST real output pin (fixture-review P1-2: no existing assertion
  pins seed-lane `source`; tests :1557 is an INSERT input fixture
  inside an integration-gated pruning test, not an oracle, and skips
  locally). The heihe leg asserts `source`,
  `elevation_metadata.source` (nested — asserted on its own line),
  `forcing_source_identity`, `project_name` mutually consistent, with
  the negative `qhh`-absence check scoped to those four fields only
  (the harness's model/basin/station ids legitimately contain `qhh`).
- Backfill of the already-persisted mislabeled rows stays out of scope
  as an EXPLICIT evidence-based deferral (matching #1359's
  no-backfill precedent), routed to a follow-up issue (node-27: needs
  a live `met.met_station` GROUP BY to size the population and a
  remediation decision).

## Capabilities

- `fixed-station-forcing-production`: ADDED requirement "Bootstrap seed
  station provenance follows the parameterized project identity" (the
  #1359 handoff-lane requirement stays untouched; this is the sibling
  seed lane).

## Impact

- `workers/model_registry/qhh_production_bootstrap.py` :1441/:1452 only;
  `tests/test_qhh_production_bootstrap.py` (additive).
- Out of scope (issue boundary): `workers/forcing_producer/file_store.py`
  (#1359's lane — zero changes allowed, acceptance-checked);
  `read_qhh_tsd_forc` parsing/containment/binding; error-message
  literals mentioning qhh.tsd.forc (:345/:356/:366/:441/:449/:457/:1327
  — diagnostics, not provenance); backfill of existing rows (explicit
  deferral, see above); `packages/common/shud_forcing_contract.py`.
- Never-break-userspace pre-check: COMPLETED by fixture review — zero
  production consumers branch on the literal (`apps/api/routes/models.py`
  passthrough; frontend passthrough with no `.source` read; no SQL
  predicate on `properties_json->>'source'`; test hits are inputs or
  shape-only). Implementer records this in the PR body; only behavioral
  delta is newly seeded non-default-project rows.
