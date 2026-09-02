# Proposal: record-registry-design-facts-and-api-error-contract

Issues: #1693 #1694 #1695 #1678 — one PR, one implementer, serial.

## Why

Four independent "the truth exists only in someone's head or a code comment"
gaps have each already cost at least one full investigation cycle:

- #1693: `core.river_segment` stores two row classes under one
  `river_network_version_id` by design, so `count(*) == 2 × segment_count`.
  #1122 and #1123 both misreported this as duplicate seed rows; #1123 got as
  far as preparing a production delete. No glossary term, no runbook sentence,
  and a stale comment in `basins_geometry.py` that still describes the
  pre-PR-2 counting basis.
- #1694: node-22 `infra/env/compute.env` looks like the production scheduler
  config but no systemd unit reads it; its basin values are dead
  (`/volume/data/nwm` does not exist, `basins_qhh`-only allow-list). The
  tracked template `compute.example` carries the same dead path. Nothing in
  `infra/env/README.md` says which EnvironmentFile each node-22 unit loads.
- #1695: `core.basin_version.active_flag` is `false` on every row (0/44 live)
  while the node-22 file-registry manifest marks every model active, and
  `core.model_instance.active_flag` is true on the baseline rows that do not
  run and false on the `dg_*` rows that do. Which flag is authoritative for
  what is written nowhere.
- #1678: `openapi/nhms.v1.yaml` has four `redocly lint` warnings that CI does
  not fail on: three GETs without a 4XX response and a missing
  `info.license`. Investigation for this change shows none of the three
  operations has a reachable 4XX; `GET /api/v1/queue/depth` does have
  reachable, undeclared 5XX codes (502 command/parse, 504 timeout).

## What Changes

- Glossary charter widened: `openspec/glossary.md` gains a `Domain terms`
  section (the file's own Usage Rules require an OpenSpec change to do this;
  this is that change). Terms: SHUD input reach row, SHUD output river row,
  `segment_count` vs `output_segment_count`, and the three `active_flag`
  meanings (file-registry manifest, `core.model_instance`,
  `core.basin_version`).
- Runbook and spec text: `docs/runbooks/current-production-ops.md` gets the
  counting invariant plus a copy-paste hygiene query next to the existing
  Heihe `shud_output_river` query; `docs/spec/03_database_design.md` gets the
  `active_flag` authority statement; `infra/env/README.md` gets the node-22
  unit → EnvironmentFile authority table.
- Code comments only: `workers/model_registry/basins_geometry.py` stale
  `segment_count` comment corrected; `basins_reingest.py` two-row-class
  comment points at the glossary. No behavior change.
- Executable pin for #1693 (the issue's optional item, taken): the existing
  real-DB import test in `tests/test_basins_registry_import.py` additionally
  asserts physical row count == 2 × `segment_count` for the imported rnv.
- `infra/env/compute.example`: dead `/volume/data/nwm/*` values replaced;
  header comment states the file is the compose-lane template and names the
  live node-22 authority files.
- node-22 live `compute.env` (gitignored, edited on the host by the
  orchestrator, backed up first): header comment added, the three
  contradicting basin/scheduler values aligned with
  `compute.scheduler-dbfree.env`. Keys are kept so the dormant compose lane
  still renders.
- OpenAPI: `apps/api/openapi_patching.py` injects 502 and 504 typed error
  responses on `GET /api/v1/queue/depth`; `openapi/nhms.v1.yaml` mirrors them;
  `tests/test_api_contract.py` pins static == runtime for the new codes. No
  4XX is declared on any of the three operations because none is reachable;
  the three `operation-4xx-response` warnings and the `info-license` warning
  are retained with recorded justification.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `basins-registry-import`: ADDED requirement — the two row classes and the
  `2 × segment_count` counting invariant are documented and pinned by the
  real-DB import test.
- `api-contract-convergence`: ADDED requirement — every error response
  declared on an operation cites a reachable raise site; `GET
  /api/v1/queue/depth` declares its reachable 502/504; operations without a
  reachable 4XX declare none and the retained lint warning is recorded.
- `compute-scheduler-operationalization`: ADDED requirement — node-22
  EnvironmentFile authority per unit is documented and the tracked compute
  template carries no dead basin path.
- `doc-status-alignment`: ADDED requirement — the three `active_flag`
  meanings and their authority are declared in the architecture spec and the
  glossary.

## Impact

- Docs/spec: `openspec/glossary.md`, `docs/runbooks/current-production-ops.md`,
  `docs/spec/03_database_design.md`, `infra/env/README.md`,
  `infra/env/compute.example`.
- Code (comments only): `workers/model_registry/basins_geometry.py`,
  `workers/model_registry/basins_reingest.py`.
- Code (contract): `apps/api/openapi_patching.py`, `openapi/nhms.v1.yaml`,
  `apps/frontend/src/api/types.ts` if `openapi-typescript` output changes.
- Tests: `tests/test_api_contract.py`, `tests/test_basins_registry_import.py`.
- Live hosts: node-22 `infra/env/compute.env` (ops edit, receipt required);
  node-27 read-only receipts only.
- Not done here, recorded with reason: `info.license` (owner legal decision,
  no LICENSE file, no `pyproject` license; frontend `ISC` is an npm scaffold
  default, not a project decision); DB `COMMENT ON COLUMN` for
  `basin_version.active_flag` (would need a migration and a node-27 apply for
  one sentence that the spec now carries); a tracked template for node-22
  `compute.host.env` (discovered here, out of #1694's scope, reported).
