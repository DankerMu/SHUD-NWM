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
- [ ] 3.2 Disable node-22 production `download_source_cycle` stage after node-27
  ownership is live.
  Evidence floor: node-22 no longer submits download jobs for production cycles;
  27 produces GFS/IFS raw manifest evidence for a new cycle.

## 4. Node-22 DB-Free Compute

- [ ] 4.1 Remove business `DATABASE_URL` inheritance from node-22 Slurm job
  templates and compute env.
  Evidence floor: rendered sbatch text and live Slurm process env contain no
  business `DATABASE_URL`; required artifact inputs are explicit.
- [ ] 4.2 Move DB-mutating post-compute steps to node-27 or receipt apply.
  Evidence floor: parse/publish/status writes happen on node-27, or node-22
  writes object-store receipts that node-27 applies idempotently.

## 5. Node-27 Orchestration Of Node-22 Slurm

- [ ] 5.1 Make node-27 hold pipeline job state and submit compute through
  node-22 Slurm Gateway.
  Evidence floor: with node-22 scheduler stopped, node-27 can submit, observe,
  and ingest a compute job via node-22 gateway using shared NFS artifacts.
- [ ] 5.2 Record live GFS and IFS end-to-end receipts.
  Evidence floor: public latest-product advances for both sources from
  node-27-downloaded raw cycles through node-22 compute artifacts and node-27
  display readiness.

## 6. Retire Node-22 Historical PostgreSQL

- [ ] 6.1 Archive/dump node-22 `:55433` and record checksum/path without secrets.
  Evidence floor: archive receipt is stored outside gitignored volatile paths
  or referenced by stable operator evidence.
- [ ] 6.2 Stop node-22 historical PostgreSQL and remove active compute env DB use.
  Evidence floor: `ss -ltnp` shows no `:55433`, compute services remain healthy,
  and two post-retirement cycles complete through node-27.
- [ ] 6.3 Add/update topology guardrails and docs.
  Evidence floor: static guard fails on active node-22 `:55433`/business
  `DATABASE_URL` writer assumptions, while allowing historical archived context;
  OpenSpec, ruff, focused tests, docs lint, and live receipts pass.
