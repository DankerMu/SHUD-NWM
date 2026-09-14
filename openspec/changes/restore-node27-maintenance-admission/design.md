## Context

Production remains `a8db554d6402bec642e9a05627eae64b2b79aec3`; target remains `415cbd1e9d0eee39ba0dfb623a586b02cbb340f2`. The 3600000 ms river107 statement timed out at 05:25:32Z; the 3900-second wrapper returned124 at05:30:32Z. Current river107 is uncompressed, status0, 643948429312 bytes. A subsequent met106 commit is inferred from catalog/log chronology, not a logged successful statement. No present maintenance backend/lock holder was observed. The original receipt remains dated Sept13.

## Goals / Non-Goals

Restore real maintenance health and new preparation admission. No reset-failed, lock deletion, shared permission change, retention-policy change, cold execution, R2 deployment, source cutover, production recovery/unstage/window or T0. Never invoke the deployed retention wrapper with --dry-run: #2355 proves it can enforce; the unexpected diagnostic invocation dropped zero chunks and is preserved as a deviation.

## Decisions

The user selected “批准临时兼容窗口”: temporary private paired budget copies, matching outer wall, bound1, necessary timer pause/resume and genuine original-service retries are authorized; formal env, active/pin and historical receipts remain protected. This is an incident exception, not resurrection of the withdrawn cold deployment recipe.

Use existing descriptor-bound paired preflight and compression-only wrapper. Private compression values: statement6000000 ms, wrapper6300 s, bound1; both copies declare service10242 s, retaining cold wrapper3901 s. Cold is validation-only, never launched. Run under a distinct user transient unit with actual RuntimeMaxSec10242 and bounded existing timeout6300; do not inject private env paths into the user manager or original unit. Its receipt is a new private path, not the scheduled receipt.

Main stops the two maintenance timers and verifies inactive state, no active maintenance/replay process or DB DDL, then stages private copies. No blanket masking or overwriting installed timer files. Record timer state before stopping and restore only those previously active; next scheduled ticks must lie beyond the bounded window. The existing shared lock remains the overlap backstop. Verify dry compression selection is exactly river107 before enforcing; default compression mode must be proven non-enforcing from its actual resolver, unlike retention.

Before enforcement: confirm cutoff/selection, capacity, no decline overlap, all overlapping runs published, and product-regeneration safety for the candidate window. A new hold, drift, selection mismatch or live regeneration blocks launch. Catch-up ends at a fresh clean receipt with exactly river107 committed plus catalog compressed status, not merely rc0. Stop on partial/indeterminate output; do not automatically repeat a large write.

Then remove only incident-owned temporary env copies, verify original config/source/pin hashes, run original compression service with defaults (expected empty selection), and run original retention service only after a readonly SQL candidate check at its actual 21-day watermark policy. Preserve previous successful retention receipt. Both original units must report success and fresh semantic receipts; refused_lock is not success evidence. Restore timer states and prove a new hash-bound prepare against415 in a never-created state directory, leaving active/pin unchanged.

## Invariant Matrix

Governing invariant: incident-owned bounded maintenance cannot replace production authority or manufacture readiness.

| Surface | Valid case | Rejection / preservation |
|---|---|---|
| Producer: private budget-copy helper | UID1005, exact OLD, staged governance, safe existing env, stopped timers -> fresh0700 directory and0600 no-clobber copies | Wrong HEAD, unsafe file, existing destination, active timer -> no launch; no formal env mutation |
| Validator: existing paired preflight | 6000000/6300/10242/bound1; actual transient wall matches declaration | Mismatch or changed origin -> refuse before DB |
| DB/runner | Only river107 eligible, no live regeneration -> one clean committed compression receipt | Partial/indeterminate/refused_lock -> no recovery claim or automatic retry |
| State/cleanup | Restore captured timer activity; original env and pin hashes equal | Foreign drift -> preserve owned artifacts and stop, never overwrite foreign state |
| Downstream admission | New prepare state, target415 and real required-unit success | Stale receipt or failed original unit -> blocked; no gate relaxation |

Boundary checklist: private files/no-clobber; exact old source and protected governance identity; fixed lifecycle lock; transient-unit process bounds; readonly DB candidate checks; immutable prior receipts; unchanged parent consumer and cold lane.

## Risks / Trade-offs

100-minute statement ceiling may still be insufficient; that yields a typed failed receipt and operator stop, not an unbounded retry. Physical compression has no automatic decompression rollback. Socket closure does not prove transaction cancellation; inspect catalog and live backends after interruption. Per-incident copies retain the currently effective budget dependency without changing either deployed env. Permanent single-lane retirement remains separate.
