# Phase B rehearsal summary — Epic #992 SUB-7 (Issue #999)

Executed 2026-07-11 on node-27 against
`postgresql://nhms:*****@127.0.0.1:55432/nhms`. `rehearse.py` returned rc=0.

## Timing window

- `REHEARSAL_WINDOW_UTC_START` = `2026-07-11T14:24:11.623292Z`
- `REHEARSAL_WINDOW_UTC_END`   = `2026-07-11T14:24:42.309507Z`
- Between-boundary rationale: no fast-cadence NWM scheduler is running on
  node-27 as a systemd unit or crontab; only OS maintenance timers
  (`phpsessionclean`, `logrotate`, `certbot`, `apt-daily`, etc.) are
  present. `_capture_max_hydro_run_created_at` recorded MAX(created_at)
  before the window, and the post-restore assertion confirmed 0 new
  `hydro.hydro_run` rows for any `model__evidence%` model created during
  the window.

## Zero-impact anchor (production `active_flag=true` count)

| Observation point            | Count (SQL: `SELECT count(*) FROM met.met_station WHERE active_flag=true AND basin_version_id NOT LIKE 'basin__evidence%'`) |
|------------------------------|---------------------------------------------------------------------------------------------------------------------------|
| pre-rehearsal (baseline)     | **6290** across 13 basin_version_ids                                                                                      |
| during-window (post-flip)    | **6290** (unchanged; only synth basin rows moved) — see `production-scoped-assertions.during.log::per_basin_active_station_count` |
| post-restore                 | **6290** — see `production-scoped-assertions.after-restore.log`                                                            |

Anchor HOLDS at all three observation points.

## MVT source-identity diff

- BEFORE: `met-stations:2bfc915b79ad9dbe:basin__evidence_cmfd_p02_synth__v1:3` (from `mvt-source-identity.before.txt`)
- AFTER:  `met-stations:f03703b827fc1462:basin__evidence_cmfd_p02_synth__v1:3` (from `mvt-source-identity.after.txt`)

The 3-station cardinality is stable (before/after both = 3 rows), but the
station-id / role / grid_snapshot_id checksum changes from
`2bfc915b79ad9dbe` to `f03703b827fc1462` — the flip re-pointed the display
set from the 3 legacy `synth-station-*` rows onto the 3 M1 mirror
`synth-mip-m1-v2::cell:cell-*` rows, so `_station_source_version`
self-invalidates.

## Screenshot

**Captured: NO.** Playwright hit its 30-second test timeout with
`page.screenshot: Target page, context or browser has been closed` before
the retention-empty-state page finished rendering. The failure is
independent of the DB transaction; `rehearse.py` returned rc=0 before the
Playwright wait completed. Per the Phase B brief, this recorded gap does
not block certification — the DB / audit-trail evidence is the
load-bearing certification. Backfilling the screenshot needs a
Playwright-only retry against a freshly re-provisioned seeded run
(`03-seeded-forecast-run.sql`) but does not require re-doing the Change 4
transaction.

## Restoration status

**FULLY RESTORED**. All four restore steps in `rehearse.py::_restore_synthetic_state`
succeeded (see the four `RESTORE step N ok:` lines in
`rehearse.node-27.pass.log`). Post-restore DB state:

- `core.model_instance`: 13 active / 15 total. The 15 = 13 production + 1
  M0 baseline `model__evidence_cmfd_p02_synth__v1` (`active_flag=false`,
  `lifecycle_state='inactive'`, retained as evidence) + 1 M1 target
  `dg_10d27a62b35b39cb5a6f9d10f7fff6e9` (`active_flag=false`,
  `lifecycle_state='inactive'`, retained per `restore/README.md`).
- `met.met_station`: 6 rows on the synth basin, all `active_flag=false`
  (3 legacy synth-station rows + 3 M1 mirror rows). Production 6290
  unchanged.
- `met.canonical_grid_snapshot`: 3 rows (IFS, gfs, `synth-grid-p0.2-m1-v2`);
  the rehearsal snapshot is retained (matches `restore/README.md`).
- `hydro.hydro_run`: seeded run row `run__evidence_cmfd_p02_synth__rehearsal_pre_cutover_v1`
  DELETED by restore step 4. Zero new evidence-model runs created during
  the window.

## Files added / updated under `evidence/`

Populated by Phase B execution:

| Path                                                                             | Description |
|----------------------------------------------------------------------------------|-------------|
| `rehearse/baseline.node-27.pre.log`                                               | Pre-rehearsal DB snapshot captured just before `run-on-node27.sh` launched, plus a retry-2 header appended after the mid-run Phase-A fix cycle. |
| `rehearse/rehearse.node-27.pass.log`                                              | Full `rehearse.py` pass log — activate result JSON, station-flip state, during-window assertion, restore trace, post-restore assertion. |
| `rehearse/mvt-source-identity.before.txt`                                         | `_station_source_version` string on the legacy synthetic station set. |
| `rehearse/mvt-source-identity.after.txt`                                          | `_station_source_version` string after the flip re-points onto the M1 mirror set. |
| `rehearse/production-scoped-assertions.during.log`                                | JSON record: production per-basin `active_flag=true` counts (13-basin vector) + transient global active = 14 + M1 target snapshot during the window. |
| `rehearse/production-scoped-assertions.after-restore.log`                         | JSON record: production per-basin counts + global active = 13 + evidence-model check post-restore. |
| `rehearse/scheduler-manifest.post-restore.json`                                   | Derived-from-DB `publish_scheduler_registry_manifest` equivalent (13 production models, 0 evidence models). |
| `rehearsal-summary.md`                                                            | This summary. |

Static Phase-A files updated during Phase B iteration to close live-execution
gaps (each with an inline comment recording the rationale):

| Path                                                                              | Reason updated |
|-----------------------------------------------------------------------------------|----------------|
| `provisioning/00-baseline-and-stations.sql`                                       | Idempotent INSERT of `core.mesh_version` placeholder row (`mesh__evidence_cmfd_p02_synth__v1`) required by `_fetch_model_lifecycle_row`'s INNER JOIN — archived readiness change inserted `core.model_instance` without a matching mesh_version row. |
| `provisioning/01-canonical-grid-snapshot.sql`                                     | Snapshot `applicable_source_ids` aligned from `cmfd` to `gfs` — `normalize_source_id` only accepts GFS/ERA5/IFS. |
| `provisioning/02-register-direct-grid-variant.py`                                 | Contract `applicable_source_ids` aligned to `["gfs"]`; script switched from psycopg v3 to psycopg2 to match `register_direct_grid_variant._json`'s `psycopg2.extras.Json` requirement. |
| `provisioning/synthetic-package/package/binding-manifest.json`                    | `applicable_source_ids` aligned to `["gfs"]` for parity with the parser-supported contract. |
| `rehearse/rehearse.py`                                                            | `COVERED_SOURCE_IDS = ("gfs",)`. `_production_baseline_assert` exclusion predicate switched from `model_id NOT LIKE 'model__evidence%'` to `basin_version_id NOT LIKE 'basin__evidence%'` so the SHA-minted M1 target `dg_<hex>` id is correctly excluded. Post-restore evidence-model check widened to also filter by basin_version_id. Post-restore `hydro_run` scan uses parameterized LIKE (psycopg v3 doesn't allow bare `%` in query text alongside `%s` binds). |
| `README.md`                                                                       | Timing-window section §4 filled with real UTC start/end + rationale; §3 exclusion predicate updated to basin_version_id; screenshot recorded as an accepted gap. |

## Phase A design gaps closed during Phase B

Each of the file updates above closed a Phase A gap discovered only on
live execution. All rationale is inline in the updated files; the pattern
mirrors the `subagent-workflow` "implementer closes gaps discovered by
verifier during live rehearsal" case. None of the closures relaxed any
zero-impact invariant.

## Commit SHA + PR

- Branch: `feat/issue-999-node27-rehearsal-receipt`.
- PR: `#1048` (transitioned draft -> ready-for-review by Phase B).
- Commit: recorded in the final Phase B commit message on the branch.
