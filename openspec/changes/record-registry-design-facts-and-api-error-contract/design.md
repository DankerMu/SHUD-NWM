# Design: record-registry-design-facts-and-api-error-contract

Line cites are against `origin/master` `e0a655ac`; symbol names are
authoritative when lines drift.

## Context

Live receipts taken 2026-09-02 (read-only) that this design is built on:

- node-27 `core.basin_version`: 0 of 44 rows `active_flag = true`.
  `core.model_instance`: baseline `basins_*_shud` 38 true / 6 false;
  `dg_*` 0 true / 153 false. (The issue text's 20/19/35 counts are stale.)
- node-22 user systemd: `nhms-compute-scheduler.service` and
  `nhms-scheduler-evidence-retention.service` load
  `infra/env/compute.scheduler-dbfree.env` (the scheduler drop-in resets the
  list and re-adds it plus `nhms-prod/secrets/slurm-gateway.env`);
  `nhms-scheduler-file-provider-refresh.service` loads
  `compute.scheduler-provider-refresh.env`; `nhms-compute-api.service` and
  `nhms-slurm-gateway.service` load `compute.host.env` (untracked, no
  template). No unit and no running container references `compute.env`
  (receipt `.workplans/pr-1956/node22-compute-env-receipt.log`
  2026-09-02T12:36Z block: `docker ps` prints no containers;
  `nhms-compute-compose.service` is `LoadState=not-found` in both the user
  and the system manager, i.e. the tracked
  `infra/systemd/nhms-compute-compose.service` is not installed). Tracked
  references to `compute.env` are enumerated in `infra/env/README.md`
  (validators, tests, `infra/README.two-node-docker.md`).
- node-22 `compute.env` (header: "Local 22-node E2E env generated
  2026-06-01"): `NHMS_SCHEDULER_MODEL_IDS=basins_qhh_shud`,
  `NHMS_SCHEDULER_BASIN_IDS=basins_qhh`,
  `NHMS_BASINS_ROOT=/volume/data/nwm/Basins`; `/volume/data/nwm` does not
  exist on the login host nor on cn01 (same receipt log, 12:36Z and srun
  blocks). `compute.scheduler-dbfree.env`: both filters empty,
  `NHMS_BASINS_ROOT=/volume/nwm/Basins` (exists on login host and cn01).
  `/ghdc/data/nwm/Basins` also exists on the login host with different
  contents (receipt: 44 vs 33 top-level entries, 17 only under `/volume`,
  6 only under `/ghdc`; not mounted on cn01); it is the NFS root node-27
  ingest reads as `/home/ghdc/nwm/Basins`. The scheduler root is whatever
  the dbfree env says, and the README must not name either as "the" root.
- Tracked `infra/env/compute.example:178-179`:
  `NHMS_BASINS_ROOT=/volume/data/nwm/Basins`,
  `NHMS_MODEL_ASSET_ROOT=/volume/data/nwm/model-assets` — both dead on
  node-22.
- OpenAPI raise-site audit for the three lint-flagged GETs:
  - `GET /api/v1/queue/depth` (`apps/api/routes/pipeline.py:822-850`): the
    display-readonly guard raises 503 (`:201-212`, already declared);
    `gateway.list_jobs` is the only other raise path and is re-raised with
    the gateway's own status code. `services/slurm_gateway/gateway.py` has no
    `queue_depth` method; `RealSlurmGateway.list_jobs`
    (`real_backend.py:486-508`) reaches `_run_command` →
    `SlurmCommandError` (502, `SLURM_COMMAND_ERROR`) / `SlurmTimeoutError`
    (504, `SLURM_TIMEOUT`) and `_parse_sacct_list` → `SlurmParseError` (502,
    `SLURM_PARSE_ERROR`; raise sites `real_backend.py:1449` in
    `_parse_sacct_list`, `:1718` in `_parse_exit_code`, `:1734` in
    `_parse_slurm_datetime`, the latter two via `_record_from_sacct_fields`);
    `MockSlurmGateway.list_jobs`
    (`mock_backend.py:171-186`) raises nothing. No parameters (no 422), no
    auth dependency on this GET (mutation guard only). **No reachable 4XX.**
  - `GET /api/v1/slurm/health` (`services/slurm_gateway/routes.py:229-233`):
    both backends' `health()` return an unhealthy 200 instead of raising
    (`real_backend.py:552-580` catches probe errors; `mock_backend.py:222`).
    `LazySlurmGateway._get` → `create_gateway` can raise a plain
    `ValueError`/settings error, which is an unhandled 500, not a contract.
    No dependencies, no parameters. **No reachable 4XX.**
  - `GET /health` (`apps/api/startup_wiring.py:53-59`): static dict. **No
    reachable 4XX.**
  - `info.license`: no `LICENSE` file, no `[project].license` in
    `pyproject.toml`; `apps/frontend/package.json` `"license": "ISC"` is the
    npm scaffold default. The issue itself says the value needs an owner
    decision.

## Goals / Non-Goals

**Goals**

- One authoritative, linkable definition of the two `core.river_segment` row
  classes and the `2 × segment_count` invariant, plus an executable pin.
- One authoritative statement of which `active_flag` means what and who
  reads it.
- A reader on node-22 can find, from the tracked tree, which EnvironmentFile
  each unit loads, and the tracked template no longer carries a dead path.
- The OpenAPI contract declares the error responses that are reachable and
  does not declare ones that are not; the lint warning count is pinned as an
  expected, justified state.

**Non-Goals**

- Any production data change, migration, import/parser/backfill behavior
  change (#1693 out-of-scope list; the geometry duplication is by design).
- Flipping any `active_flag` in the DB (#1695 option B rejected — see D3).
- Fabricating a 4XX on any operation to satisfy `operation-4xx-response`, or
  disabling/ignoring any lint rule (#1678 hard constraints).
- Choosing a license.
- A tracked template for `compute.host.env`; deleting keys from the dormant
  compose env; touching `compute.scheduler-dbfree.env`.

## Decisions

### D1 — Glossary charter widened in place (#1693 term location)

`openspec/glossary.md` gains `## Domain terms`; the intro sentence is
rewritten so the file is the single source for governance **and** domain
ubiquitous language, matching what `CLAUDE.md` already claims for it.
Alternative rejected: a separate `docs/glossary` — two glossaries is the
exact "local synonym" the file's rules forbid, and code comments can only
point at one place.

### D2 — Counting invariant lands in runbook + import test, not the archive drill

Runbook: a new subsection immediately after the Heihe query at
`docs/runbooks/current-production-ops.md:3676-3684` explains the two groups,
states `count(*) == 2 × segment_count` is expected, gives the filtered
comparison query, notes `output_segment_count` is not an rnv column (receipt →
`resource_profile` → manifest fields), and back-links #1122/#1123. Executable pin: extend the existing
real-DB import test around `tests/test_basins_registry_import.py:3473`
(already counting reach rows for the imported rnv) with
`total_rows == 2 * segment_count` and `output_rows == reach_rows`, comment
tagged `#1693`. The issue's suggested drill file
`scripts/node27_archive_rebuild_drill.py` no longer exists and must not be
cited. Correctly filtered oracles to cite: `basins_registry_import.py:610-620`
(reach-row idempotency guard), `tests/test_real_database_integration.py:448-453`;
`workers/output_parser/parser.py` `load_river_segments` (`:820-838`) selects
the output class first but falls back to an unfiltered query when no tagged
row exists, so cite it as "output-class-first", not as an unconditional
filter.

### D3 — `active_flag`: option A, docs only, no COMMENT migration (#1695)

Facts to state (no causal story beyond what is traced):

- `core.basin_version.active_flag`: the Basins importer — the path every
  production row came from — writes a hardcoded `false`
  (`workers/model_registry/basins_registry_import.py:542-548`) and no UPDATE
  anywhere touches the column (the importer's later `UPDATE core.basin_version`
  at `:799` sets `source_uri`/`checksum` only). The internal write API
  `POST /api/v1/basins/{basin_id}/versions`
  (`packages/common/model_registry.py::_insert_basin_version`) does accept
  `active_flag` in its payload on creation; it is not used by production
  ingest, so the docs must say "importer writes false, nothing updates it,
  the internal create API could set it but nothing does", not "no code path
  can set it". Readers (round-1 review corrected the first draft's "sole
  reader" claim): backend `packages/common/model_registry.py:874` (`ORDER BY`
  tiebreak); the same query SELECTs the column (`:866`) and
  `_basin_version_public_projection` (`:3611-3617`) passes it through
  `GET /api/v1/basins/{basin_id}/versions`; the frontend maps it to
  `BasinVersionOption.active` (`overviewDataContracts.ts:854`) and uses it to
  pick the default selected version (`overviewData.ts:1285`,
  `overviewDataContracts.ts:396`, `:601`). So: no compute authority; on the
  display plane it is a default-version selector that is a no-op while every
  row is `false`, and setting any row `true` changes that basin's default
  selected version. It is not a display-membership flag.
- `met.met_station.active_flag` is a fourth, unrelated flag (station
  selection scoped by `basin_version_id`, read at
  `packages/common/forecast_store.py:1060`, flipped by
  `packages/common/station_set_flip.py`); the glossary names it once so a
  grep for `active_flag` finds every meaning accounted for.
- `core.model_instance.active_flag`: read by display — national river-network
  MVT membership `services/tiles/mvt.py:367` (also `:442, :653, :691,
  :1411`), frontend `activeModelCount`
  (`apps/frontend/src/lib/m11/overviewDataContracts.ts:408, :617`) — and by
  the lifecycle API (`model_registry.py`). As of the 2026-09-02 counts only
  baseline rows are `true` (the MVT predicate has no baseline/variant test);
  `dg_*` rows are `false` for two traceable reasons — 142/153 sit under a
  `basin_version` with an active baseline sibling, so
  `node27_autopipeline::_activate_model`'s one-active-sibling guard returns
  `rowcount == 0` for them; the other 11 have no active sibling and are
  `false` only because activation was never attempted (node-27 read-only
  check 2026-09-02, SQL echoed in the receipt) — and the production
  file-lane scheduler does not read the column (the postgres lane would).
- Compute-plane authority is the file-registry manifest
  (`NHMS_SCHEDULER_REGISTRY_BACKEND=file`, `manifest-last.json`, written by
  `scripts/publish_scheduler_file_registry.py`); the DB-free scheduler cannot
  reach either DB flag (the `core.model_instance.active_flag` read at
  `chain_repository.py:265` belongs to the postgres backend). The two planes are not synchronized by design.

Placement: `docs/spec/03_database_design.md` §5.2 and the `model_instance`
table get a short "authority" note (architecture/spec status per
`DOC_STATUS.md`); glossary terms; the existing runbook sentences at
`current-production-ops.md:1223` and `:2854` gain a link to the spec note.
`COMMENT ON COLUMN` rejected: a migration plus node-27 apply for one
sentence; recorded, can be added later without conflict.

### D4 — node-22 env authority: README table + template fix + minimal live edit (#1694)

`infra/env/README.md` gets a table: unit → EnvironmentFile(s) → tracked
template (or "untracked, no template" for `compute.host.env`), and one
sentence that `compute.env` is the compose-lane instance no node-22 unit
reads. `compute.example` header gains the same pointer;
`NHMS_BASINS_ROOT` becomes `/volume/nwm/Basins` (matches
`compute.scheduler-dbfree.env.example:84`; exists on the node-22 login host
and on compute node cn01 per srun receipt 2026-09-02) and
`NHMS_MODEL_ASSET_ROOT` becomes the template's own placeholder scheme
`/scratch/frd_muziyao/nhms-production/model-assets`, which does NOT exist on
node-22 (receipt 12:36Z block: `MISSING`; receipt 14:59Z block:
`compute.host.env:34` and `compute.scheduler-dbfree.env:35` both set
`NHMS_MODEL_ASSET_ROOT=/scratch/frd_muziyao/nhms-prod/model-assets`, which
EXISTS) and is
therefore labelled a placeholder in the template comment, never asserted to
exist.

Live `compute.env` (orchestrator ops step, not implementer): back up as
`compute.env.bak-1694-<UTC>` (mode 0600 preserved), then in place: prepend a
3-line header naming the authority files, set
`NHMS_SCHEDULER_MODEL_IDS=`, `NHMS_SCHEDULER_BASIN_IDS=`,
`NHMS_BASINS_ROOT=/volume/nwm/Basins`. Keys kept (issue option A deletes
them) so `docker compose config` on the dormant lane still renders; the
issue's acceptance ("no values contradicting production") is met either way.
Receipt: before/after `grep -nE 'BASIN|MODEL_IDS' compute.env` and
`stat -c %a`.

### D5 — OpenAPI: declare reachable 5XX, keep unreachable 4XX undeclared (#1678)

`_patch_pipeline_openapi` (`apps/api/openapi_patching.py:592-765`) adds two
components via the existing `_typed_error_response` helper —
`SlurmGatewayUpstreamError` (502; codes `SLURM_COMMAND_ERROR`,
`SLURM_PARSE_ERROR`) and `SlurmGatewayTimeout` (504; code `SLURM_TIMEOUT`) —
and injects them on `GET /api/v1/queue/depth` next to the existing 503.
`openapi/nhms.v1.yaml` is hand-edited to mirror (no regeneration script
exists; the drift test compares parsed dicts). `tests/test_api_contract.py`
`test_display_control_plane_responses_have_no_static_runtime_drift` (`:1425`)
gains the two cases; its `_response_error_codes` helper returns a list, so
the 502 case compares against the two-code list. The 4XX rule stays
unsatisfied on all three operations; `tasks.md` pins the expected lint
output as `0 errors, 4 warnings` and names the four. Every declared status
in the diff must cite its raise site in the implementer report; a reviewer
asserting a 4XX "per the issue text" must be answered with the raise-site
audit above, not the issue.

Frontend: `corepack pnpm run check:api-types` must pass; if
`openapi-typescript` output changes, regenerate `src/api/types.ts` with
`pnpm generate:api` and commit it.

## Risks / Trade-offs

- [Docs cite line numbers that drift] → symbol names sit next to every cite;
  reviewers check symbols, not lines.
- [README names a basin root that is wrong for some lane] → README states
  "the scheduler root is the value in `compute.scheduler-dbfree.env`", gives
  the live value with its date, and notes the NFS root separately.
- [Live `compute.env` edit on a production host] → file is read by nothing
  running; backup with same mode; receipt captured; rollback = `mv` the
  backup back.
- [Static yaml hand-edit diverges from runtime] → `tests/test_openapi_drift.py`
  is the full-dict oracle; run locally and in CI.
- [License stays missing] → recorded as owner-pending in tasks, PR, and the
  #1678 closure comment; the issue is left open on that one criterion.

## Migration Plan

No migration. Deploy = merge; node-27 pulls docs; node-22 live env edit is
done by the orchestrator before merge with receipt.

## Open Questions

- `info.license` value — owner decision, asked in the final report; not
  blocking anything else in this change.
