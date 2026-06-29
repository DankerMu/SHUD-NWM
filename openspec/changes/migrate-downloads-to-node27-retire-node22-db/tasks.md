## 1. Current-State Evidence

- [x] 1.1 Capture node-22 scheduler, Slurm Gateway, compute API, historical
  PostgreSQL `:55433`, and sanitized runtime env evidence.
  Evidence floor: receipt identifies active processes, ports, scheduler status,
  `DATABASE_URL` host/port without credentials, and current Slurm queue.
- [x] 1.2 Capture node-27 ingest/display DB identity, cron status, public
  latest-product GFS/IFS identity, and object-store roots.
  Evidence floor: receipt proves node-27 active DB is `:55432`, display runtime
  remains readonly, and public API reports the latest GFS/IFS cycle.
  Evidence: `docs/runbooks/receipts/2026-06-27-node27-download-migration-phase0.md`
  records node-22 scheduler/Slurm/API/PostgreSQL state, sanitized compute env,
  node-27 ingest/display env separation, cron, local health, and public
  latest-product identity for GFS/IFS at `2026-06-26T12:00:00Z`.

## 2. Node-27 Download Runner

- [x] 2.1 Add node-27 download env template and wrapper/runner with preflight.
  Evidence floor: preflight fails before mutation when `DATABASE_URL`,
  `OBJECT_STORE_ROOT`, `WORKSPACE_ROOT`, GRIB tools, bbox/cycle config, lock, or
  log roots are missing/unsafe; output is credential-safe JSON.
- [x] 2.2 Run focused tests for node-27 download preflight and summary evidence.
  Evidence floor: tests cover writer-vs-readonly DB classification, node-22
  `:55433` rejection, display env separation, path safety, lock behavior, and
  redaction.
  Evidence: `scripts/node27_download_cycles.py`,
  `scripts/node27_download_once.sh`, `infra/env/node27-download.example`, and
  `tests/test_node27_download_cycles.py` implement and cover the bounded
  node-27 download runner. Verification passed:
  `uv run pytest -q tests/test_node27_download_cycles.py` (6 passed),
  `uv run ruff check scripts/node27_download_cycles.py tests/test_node27_download_cycles.py`,
  and `bash -n scripts/node27_download_once.sh`.
- [x] 2.3 Produce node-27 live proof for one safe GFS or IFS cycle.
  Evidence floor: node-27 writes/verifies raw manifest and `met.forecast_cycle`
  in `:55432` without any node-22 DB access.
  Evidence: `docs/runbooks/receipts/2026-06-27-node27-download-gfs-live-proof.md`
  records node-27 GFS `2026-06-26T12:00:00Z` download success after installing
  user-local GRIB tools at `/home/nwm/nhms-grib` and opening the shared NFS
  `object-store/raw` write surface. The live run wrote 58 physical raw bundle
  files, a manifest with 397 entries, and a node-27 DB
  `met.forecast_cycle` row with `source_id=gfs`, `status=raw_complete`, and
  `manifest_uri=s3://nhms/raw/gfs/2026062612/manifest.json`.
  `docs/runbooks/receipts/2026-06-27-node27-download-ifs-live-proof.md` records
  the matching IFS proof for the same cycle: 54 physical raw bundle files,
  424 manifest entries, and node-27 DB `source_id=IFS`, `status=raw_complete`,
  `manifest_uri=s3://nhms/raw/IFS/2026062612/manifest.json`.

## 3. Production Download Ownership

- [ ] 3.1 Promote node-27 download into cron/autopipeline source-cycle ownership.
  Evidence floor: bounded production pass selects allowed UTC `00,12` cycles,
  handles already-complete cycles idempotently, and records per-source status.

## 4. Node-22 NFS Raw Manifest Scheduler Bridge

- [x] 4.1 Add node-22 scheduler support for node-27 NFS raw manifest readiness.
  Evidence floor: scheduler can materialize source-cycle readiness from
  `raw/<source>/<cycle>/manifest.json` on shared NFS even when the local
  node-22 repository has no matching `met.forecast_cycle` row.
  Evidence: `services/orchestrator/source_cycle_raw_manifest.py` and
  `services/orchestrator/chain_repository_state.py` validate NFS manifest
  source/cycle identity, URI suffix, entry list, and physical raw files before
  creating raw-ready candidate state.
- [x] 4.2 Skip node-22 production download when node-27 raw is ready.
  Evidence floor: with raw-ready NFS manifest evidence and absent canonical
  rows, scheduler submits a downstream restart from `convert` with fresh
  ingestion disabled.
  Evidence: `services/orchestrator/scheduler_candidates.py` maps raw-ready NFS
  evidence to `restart_stage=convert`, `fresh_ingestion.required=false`, and
  `raw_manifest_reuse.source=node27_nfs_raw_manifest`.
- [x] 4.3 Block node-22 fallback download when required NFS raw is missing.
  Evidence floor: when `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true`, missing
  or invalid NFS raw evidence blocks the candidate instead of submitting
  `download_source_cycle`.
  Evidence: focused tests cover ready GFS, ready uppercase IFS storage, missing
  raw files, synthetic candidate state without DB rows, restart-from-convert,
  and required-manifest blocking. Verification passed:
  `uv run pytest -q tests/test_source_cycle_raw_manifest.py tests/test_chain_repository_nfs_raw_manifest.py tests/test_production_scheduler.py::test_fresh_zero_canonical_with_nfs_raw_ready_restarts_at_convert tests/test_production_scheduler.py::test_required_nfs_raw_manifest_missing_blocks_fresh_download_fallback`
  plus adjacent scheduler regression coverage (11 passed total), and focused
  `ruff check`.
- [x] 4.4 Stage node-27 NFS raw into compute-visible object-store before
  downstream submit.
  Evidence floor: because Slurm compute nodes may not read `/ghdc`, scheduler
  copies the ready NFS manifest's raw files into `OBJECT_STORE_ROOT` on the
  node-22 side before calling Slurm, and writes the manifest last.
  Evidence: `services/orchestrator/source_cycle_raw_manifest.py` implements
  `stage_nfs_raw_manifest_to_object_store`, and
  `services/orchestrator/scheduler_execution.py` calls it in the pre-submit
  path when `NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE=true`. Verification:
  focused tests cover direct staging and scheduler pre-submit staging.
- [x] 4.5 Enable the NFS raw-manifest gate on node-22 production scheduler.
  Evidence floor: node-22 runtime env sets
  `NHMS_SCHEDULER_REQUIRE_NFS_RAW_MANIFEST=true` and points
  `NHMS_SCHEDULER_NFS_RAW_MANIFEST_ROOT` at the shared NFS object-store, plus
  `NHMS_SCHEDULER_STAGE_NFS_RAW_TO_OBJECT_STORE=true` and a compute-visible
  `NHMS_SCHEDULER_NFS_RAW_STAGE_ROOT`.
  Evidence: `docs/runbooks/receipts/2026-06-27-node22-nfs-raw-stage-live-proof.md`
  records node-22 runtime env activation, Slurm compute-node read proof
  (`/ghdc` fails, `/scratch` succeeds), GFS/IFS raw staging smoke checks, and
  the restarted compute API / scheduler pass.
- [ ] 4.6 Record live GFS and IFS end-to-end receipts through the NFS handoff.
  Evidence floor: public latest-product advances for both sources from
  node-27-downloaded raw cycles after node-22 scheduler observes the NFS raw
  manifest, stages raw to compute-visible object-store, and starts downstream
  stages without submitting node-22 download.
  Latest status: the 2026-06-27 node-22 scheduler pass exited successfully but
  produced `candidate_count=0`, so it did not yet satisfy the end-to-end
  handoff evidence floor.
- [x] 4.7 Retire the active node-22 production download stage and Slurm job
  mapping.
  Evidence floor: active forecast stages and gateway defaults no longer expose
  `download_source_cycle`; fresh zero-canonical cycles without node-27 raw are
  blocked instead of converted into full-chain download submissions.
  Evidence: `services/orchestrator/chain_stages.py` starts production forecast
  orchestration at `convert`, `services/slurm_gateway/config.py` no longer maps
  `download_source_cycle`, `infra/sbatch/download_source_cycle.sbatch` is
  removed, and focused tests assert missing node-27 raw blocks scheduling.

## 5. Node-27 Raw NFS Retention

- [x] 5.1 Add an independent raw NFS retention runner for node-27-owned source
  bundles.
  Evidence floor: runner only targets
  `<object-store-root>/raw/<source>/<YYYYMMDDHH>`, always executes after
  safety preflight, rejects unsafe roots, skips symlinks/non-cycle names, and
  writes JSON evidence.
  Evidence: `scripts/node27_raw_retention.py`,
  `scripts/node27_raw_retention_once.sh`,
  `infra/env/node27-raw-retention.example`, and focused tests.
- [x] 5.2 Install and run the node-27 raw retention systemd timer.
  Evidence floor: node-27 has `nhms-node27-raw-retention.timer` enabled for
  user `nwm`, a live summary JSON exists under the retention log root, and the
  first live run records planned/deleted/skipped/failed counts.
  Evidence: `docs/runbooks/receipts/2026-06-27-node27-raw-retention-live-proof.md`.
- [x] 5.3 Upgrade the node-27 raw retention timer to production execute-only
  semantics.
  Evidence floor: repo and node-27 runtime no longer expose
  `NODE27_RAW_RETENTION_DRY_RUN`, retention summaries use production execution
  mode, and a live node-27 run records planned/deleted/skipped/failed counts.
  Evidence: `docs/runbooks/receipts/2026-06-27-node27-raw-retention-production-proof.md`.

## 6. Later Node-22 Scheduler-State Reduction

- [x] 6.1 Design the replacement for node-22 scheduler DB responsibilities.
  Evidence floor: separate change documents lock state, candidate state, job
  state, retry semantics, rollback, and live verification before removing
  node-22 scheduler DB dependencies.
  Latest status: #837 completed this in `node22-db-free-scheduler-state`.
  Post-stop evidence shows node-22 scheduler runtime has no `DATABASE_URL`,
  file-backed locks/selectors are active, and `:55433` is stopped.
  Retirement gate: `docs/runbooks/node22-db-retirement-runbook.md`.

## 7. Retire Node-22 Historical PostgreSQL

- [x] 7.1 Archive/dump node-22 `:55433` and record checksum/path without secrets.
  Evidence floor: archive receipt is stored outside gitignored volatile paths
  or referenced by stable operator evidence.
- [x] 7.2 Stop node-22 historical PostgreSQL only after scheduler-state
  responsibilities are replaced.
  Evidence floor: `ss -ltnp` shows no `:55433`, compute services remain healthy,
  and post-retirement cycles complete through node-27 download, node-22
  NFS-gated scheduling, downstream compute, node-27 ingest, and public display.
- [x] 7.3 Add/update topology guardrails and docs.
  Evidence floor: static guard fails on active node-22 `:55433`/business
  `DATABASE_URL` writer assumptions, while allowing historical archived context;
  OpenSpec, ruff, focused tests, docs lint, and live receipts pass.
  Evidence: #837 receipt
  `docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`.
