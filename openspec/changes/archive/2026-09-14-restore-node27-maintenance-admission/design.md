## Context

Production remains `a8db554d6402bec642e9a05627eae64b2b79aec3`; target remains `415cbd1e9d0eee39ba0dfb623a586b02cbb340f2`. The 3600000 ms river107 statement timed out at 05:25:32Z; the 3900-second wrapper returned124 at05:30:32Z. Current river107 is uncompressed, status0, 643948429312 bytes. A subsequent met106 commit is inferred from catalog/log chronology, not a logged successful statement. No present maintenance backend/lock holder was observed. The original receipt remains dated Sept13.

The first authorized catch-up also timed out after6000 seconds (backend78398,10:42:51Z), returning `partial/failed_before_mutation`; catalog confirmed river107 unchanged and the private env copies were cleaned. Retention's original service recovered at10:49:52Z with zero dropped chunks and its timer was restored.

## Goals / Non-Goals

Restore real maintenance health and new preparation admission. No reset-failed, lock deletion, shared permission change, retention-policy change, cold execution, R2 deployment, source cutover, production recovery/unstage/window or T0. Never invoke the deployed retention wrapper with --dry-run: #2355 proves it can enforce; the unexpected diagnostic invocation dropped zero chunks and is preserved as a deviation.

## Decisions

The user selected “批准临时兼容窗口”: temporary private paired budget copies, matching outer wall, bound1, necessary timer pause/resume and genuine original-service retries are authorized; formal env, active/pin and historical receipts remain protected. This is an incident exception, not resurrection of the withdrawn cold deployment recipe.

Use existing descriptor-bound paired preflight and compression-only wrapper. The separately approved third window uses statement21600000 ms, wrapper21900 s, bound1; both private copies declare service25842 s, retaining cold wrapper3901 s. Cold is validation-only, never launched. Run under a distinct user transient unit with actual RuntimeMaxSec and TimeoutStartSec25842 and bounded existing timeout21900; do not inject private env paths into the user manager or original unit. Its receipt is a new private path, not the scheduled receipt. These are upper limits, not predicted completion times.

Historical second attempt: after the first failure the user separately approved one scan-strategy comparison at the same100-minute ceiling, after small-data validation and plan review. Only its private compression copy gained `PGOPTIONS='-c timescaledb.enable_compression_indexscan=off'`; it also timed out, before committed mutation. The now-approved third window retains scan-off and changes only the private time budget. Formal env, DSN, global/role GUCs, memory settings and cold behavior remain unchanged. The manifest binds scan mode `off` and exact current budget, rejecting old/mismatched manifests. Main must prove the actual private-env connection resolves `off` before launch. The frozen runner passes libpq options through and only sets statement timeout; the existing paired preflight retains the private env setting. No general tuning API or automatic retry is introduced.

Third-window authority is explicit in `receipts/extended-window-authorization.json`: the user selected “批准一次 6 小时窗口” after both100-minute attempts failed. At most one new enforcing attempt is permitted. The private arithmetic is21600+300=21900; outer25842=21900+3901+40+1. Fresh capacity and next-schedule checks must cover the full25842-second outer envelope. No automatic fourth attempt is authorized.

The user additionally directed “修复压缩后立刻恢复压缩timer”: compression scheduling remains paused until repair; immediately after catch-up proof, owned-copy cleanup and genuine original compression service success, Main restores its timer. Retention is already healthy: preserve its successful receipt and restore its timer after the temporary comparison window, without rerunning a successful retention tick merely for evidence.

Main stops the two maintenance timers and verifies inactive state, no active maintenance/replay process or DB DDL, then stages private copies. No blanket masking or overwriting installed timer files. Record timer state before stopping and restore only those previously active; next scheduled ticks must lie beyond the bounded window. The existing shared lock remains the overlap backstop. Verify dry compression selection is exactly river107 before enforcing; default compression mode must be proven non-enforcing from its actual resolver, unlike retention.

Before enforcement: confirm cutoff/selection, capacity, no decline overlap, all overlapping runs published, and product-regeneration safety for the candidate window. A new hold, drift, selection mismatch or live regeneration blocks launch. Catch-up ends at a fresh clean receipt with exactly river107 committed plus catalog compressed status, not merely rc0. Stop on partial/indeterminate output; do not automatically repeat a large write.

After successful catch-up, remove only incident-owned temporary env copies, verify original config/source/pin hashes, and run original compression service with defaults after a fresh eligibility check (expected empty selection). Immediately restore the compression timer after genuine original-service success. Preserve the already recovered retention service and its historical/new success receipts; restore its previously active timer without an unnecessary enforcing rerun. Both original units must report success with valid semantic receipts; refused_lock is not success evidence. Then prove a new hash-bound prepare against415 in a never-created state directory, leaving active/pin unchanged. On failure, clean only owned copies, retain failed evidence, restore retention scheduling and keep compression scheduling paused under the user's explicit instruction; no automatic fourth attempt is authorized.

## Invariant Matrix

Governing invariant: incident-owned bounded maintenance cannot replace production authority or manufacture readiness.

| Surface | Valid case | Rejection / preservation |
|---|---|---|
| Producer: private budget-copy helper | UID1005, exact OLD, staged governance, safe existing env, stopped timers -> fresh0700 directory and0600 no-clobber copies | Wrong HEAD, unsafe file, existing destination, active timer -> no launch; no formal env mutation |
| Validator: existing paired preflight | 21600000/21900/25842/bound1; actual transient wall matches declaration | Mismatch or changed origin -> refuse before DB |
| Scan strategy | Private connection reports compression_indexscan=off; isolated on/off compression preserves identical rows | Unapplied or altered scan option -> no launch; no global/role/formal-env tuning |
| DB/runner | Only river107 eligible, no live regeneration -> one clean committed compression receipt | Partial/indeterminate/refused_lock -> no recovery claim or automatic retry |
| State/cleanup | Original env/pin hashes equal; restore retention scheduling; restore compression scheduling immediately after repair | Foreign drift -> preserve and stop; failed compression remains paused under explicit user disposition |
| Downstream admission | New prepare state, target415 and real required-unit success | Stale receipt or failed original unit -> blocked; no gate relaxation |

Boundary checklist: private files/no-clobber; exact old source and protected governance identity; fixed lifecycle lock; transient-unit process bounds; readonly DB candidate checks; immutable prior receipts; unchanged parent consumer and cold lane.

## Risks / Trade-offs

Both100-minute attempts were insufficient. The separately authorized six-hour statement ceiling may also be insufficient; failure yields a typed receipt and operator stop, not an unbounded retry. Physical compression has no automatic decompression rollback. Socket closure does not prove transaction cancellation; inspect catalog and live backends after interruption. Per-incident copies retain the effective budget dependency without changing either deployed env. Permanent single-lane retirement remains separate.

TimescaleDB2.10.2 selects a matching btree when compression index scans are enabled, without a planner cost comparison (`tsl/src/compression/compression.c:349-454,489-547`). The primary key matches; the actual compression-role session reports indexscan=on, maintenance_work_mem2047MB and shared_buffers32197MB. The candidate has an estimated1.06billion rows, heap295GB and indexes349GB. Current RAID1 is healthy. The index-on trial had a sampled DataFileRead wait; the off trial had a sampled active backend with no wait event and logged182.426GiB of sort temp files at cancellation. Neither wait event nor temp volume identifies completion percentage. Both strategies exceeded100minutes, so no speedup is claimed. Small-data validation proves correctness/control only. Temp files use pg_default on md0; recheck capacity before the extended window and preserve the existing memory settings.
