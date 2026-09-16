## 1. Fixture and admission

- [x] 1.1 Review this expanded fixture and pass strict OpenSpec validation before production mutations.
- [x] 1.2 Bind current node-27 units, checkout/running code identity, worker count, disk capacity and connection budget to the receipt.

## 2. Pool configuration (#2346 first step only)

- [x] 2.1 Back up display.env, set only the two admitted pool keys, and perform a same-code controlled display restart.
- [x] 2.2 Record effective pool values, before/after pg_stat_activity and max_connections, HTTP smoke and unchanged read-only boundary; document rollback and remaining second step.

## 3. PNG cache pruning (#2431)

- [x] 3.1 Back up retention env and set NHMS_MVT_FILE_CACHE_DIR to the display cache root without changing other settings.
- [x] 3.2 Inspect plan-only output, execute a normal retention tick and record PNG root/skip/planned/deleted/failed evidence, separating existing #2360 canonical failure.

## 4. Upstream diagnosis (#2432)

- [x] 4.1 Execute a read-only diagnostic signal on a concrete failed forcing candidate, rank falsifiable hypotheses and trace the producer/provenance boundary.
- [x] 4.2 Record root cause with exact evidence, ruled-out alternatives and actionable next ownership; do not claim repair or trigger computation. Repair owner: #2439; captured decision-chain evidence is in `evidence/node22-decision-chain-replay.txt`.

## 5. Delivery

- [x] 5.1 Update existing runbook and write redacted live receipt with timestamps, commands, actual deployed SHA, results and rollback.
- [ ] 5.2 Complete risk-scaled cross-review, CI, single-PR merge and issue updates; leave #2346 second step open.

## Evidence Floor

- Node-27: `df -h / /home /data/GHDC`; effective systemd unit/process path and worker count; current checkout status/version versus running version. No code pull or dependency sync needed for these env-only actions.
- Capacity SQL (on node-27 only, credentials never printed): SHOW max_connections; SHOW superuser_reserved_connections; grouped pg_stat_activity by role/state before and after. Budget counts all workers plus current non-display demand and explicit reserve. Desired 2*(8+8)=32 only if workers are actually two and measured headroom admits it.
- Display runtime: the restarted process inherits NHMS_DISPLAY_DB_POOL_SIZE=8 and NHMS_DISPLAY_DB_MAX_OVERFLOW=8; current source engine consumes these settings. Actual `/health`, `/`, `/api/v1/layers`, river-network tile and published gfs/ifs discharge tiles return 200. Capture C1-C4 applicable runtime/config role and deny-write proof using existing node-27 runbook. Do not claim cold-burst isolation or latency improvement from this smoke.
- PNG: existing `NODE27_RAW_RETENTION_PLAN_ONLY=true` runner with configured env yields expected root and safe candidate paths/watermark; production service tick yields configured precip_cache_root, explicit per-lane counts and no PNG failures. Activation requires no `precip_cache_root_unconfigured`, `precip_cache_root_missing`, `precip_cache_root_unsafe`, `precip_cache_source_unsafe` or other PNG unavailable/unsafe skip. Zero expired candidates is acceptable only after both configured source directories were safely evaluated. A known canonical lock_unsafe/nonzero service exit is recorded separately, not concealed or repaired.
- Diagnosis: one executed read-only red command tied to concrete source/cycle and witness/producer; ranked hypotheses; logs/config/provenance distinguishing cause; exact SHA and timestamps; confirmed root cause or unresolved access/evidence prerequisite. No backend test suite is needed for read-only investigation.
- Local: `openspec validate configure-display-pool-cache-pruning-diagnose-forcing --strict --no-interactive`; applicable repository Markdown checks. Existing code is unchanged: do not create implementation-pinning tests. Any later code change requires separately identified red/green behavioral proof and node-27 backend oracle before merge.

## Risk packs

| Pack | Selection and mapping |
|---|---|
| Public API / CLI / script entry | Selected: real display HTTP smoke and retention entrypoint in 2.2/3.2; response contracts unchanged. |
| Config / project setup | Selected: live env/process admission, backup, effective values and rollback in 1.2/2.1/3.1. |
| File IO / path safety / overwrite | Selected: safe cache-root equality, plan inspection, retention lock preservation in 3.2; no overwrite of whole env from local. |
| Schema / columns / units / field names | Not selected: no schema or output-field change; use existing summary semantics. |
| Auth / permissions / secrets | Selected: unchanged display deny-write proof, redaction and env permissions in 2.2/5.1; no role tuning. |
| Concurrency / shared state / ordering | Selected: worker-total connection budget, sequential operations, shared canonical mutex preserved in 1.2/3.2. |
| Resource limits / large input / discovery | Selected: measured DB budget/disk and bounded diagnostic extraction in 1.2/4.1. |
| Legacy compatibility / examples | Selected: same code and unchanged API/env defaults, update operational instructions only in 5.1. |
| Error handling / rollback / partial outputs | Selected: rollback and independent lane accounting in 2.2/3.2/5.1. |
| Release / packaging / dependency compatibility | Not selected: no packages or runtime migration; prohibited node-22 environment rebuild. |
| Documentation / migration notes | Selected: receipt/runbook, #2346 stays open, #2432 diagnosis not recovery in 4.2/5.1/5.2. |
