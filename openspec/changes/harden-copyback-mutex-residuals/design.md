# Design

Reference convention: code is named by symbol, never by line number. The check
`grep -rnE '\.(py|md|sh|json):[0-9]+|#L[0-9]+|line [0-9]+|第 ?[0-9]+ ?行' openspec/changes/harden-copyback-mutex-residuals`
must return nothing.

## Context

The copyback batch mutex (`packages/common/copyback_guard`) is one `flock` on
`<copyback root>/.nhms-copyback-batch.lock`. Acquirers today: the q_down and
canonical publisher lanes (`publisher`), `run_tree_copyback`, the two backfills,
and `services/orchestrator/retention` — all on node-22. The shared copyback root
is node-27's local `/home/ghdc/nwm/object-store`, NFS-mounted by node-22 as
`/ghdc/data/nwm/object-store`.

## Decisions

### D1 — run-tree backup lifecycle is keyed on explicit state (#2237)

`_replace_tree` and `_replace_file` track two booleans: `promoted` (temp is now
at target) and `restored` (backup moved back to target). Rules:

- Success path: after promotion the backup is removed, as today.
- Failure path, in this order: (1) if the backup exists and the target does not,
  attempt the restore and set `restored` on success; (2) best-effort temp
  cleanup whose own `OSError`/`SafeFilesystemError` is suppressed so it cannot
  pre-empt anything; (3) re-raise.
- The backup is removed only when `promoted` or `restored`. Otherwise it stays,
  and the raised error names it: a restore failure or an un-attempted restore
  raises `RunTreeCopybackError` with code `OBJECT_STORE_COPYBACK_BACKUP_RETAINED`
  and `details` holding `target`, `backup_path` and the original error text,
  chained from the original error. The original is always a non-copyback error
  here: `_copy_tree_no_symlinks` / `shutil.copy2` run before the backup rename,
  so no backup exists when they fail.
- Consumer effect, deliberate: that branch used to escape as a bare `OSError`;
  `chain_forecast_execution._copyback_stage_run_trees` catches only
  `RunTreeCopybackError`, so it now becomes that stage's recorded pipeline event
  and `OrchestratorError` carrying `backup_path`. A failure whose restore
  succeeded still raises the original error unchanged.
- The guarded predicate (`backup exists and target does not`) is unchanged: a
  target present at failure time is never overwritten by the restore.
- The `_replace_tree` docstring's "no data was lost" claim is corrected, and the
  living spec's "documented in place as a benign spurious-failure terminal
  state" clause is dropped from the run-tree scenario: #2237 shows the
  pre-change terminal state could delete the only backup, so the claim was false.

Alternative rejected: the publisher's rollback-ledger shape — much larger and
adds a full clone of every backup on NFS.

### D2 — forcing backfill `--apply` observes the destination under the mutex (#2236)

In `--apply`, each package after checksum grouping takes one
`copyback_batch_lock(copyback_root)` held across: destination inspection
(`_inspect_existing_target`), the `already_present` skip, source validation,
copy, and commit or rollback. `_copy_package` no longer acquires; its caller
holds the lock (the guard is not reentrant). A lock failure is a failed package
with the existing category `copyback_lock_unavailable` (unchanged name, still
normalised through `_normalized_failure_category`), never an aborted run.
A package that fails checksum grouping never reads the destination and takes no
acquisition.

The order inside the lock is unchanged (destination, then source): moving source
validation first would turn a package whose source was retention-swept but whose
target is present from `already_present` into `failed`.

Plan mode stays unlocked — acquiring creates the lock file, which is a write —
and every package record carries `observed_under_lock` (`true` in `--apply`,
`false` in plan). The report adds `observed_under_lock` at top level. The
runbook's closure criterion reads only an `--apply` report.

Cost, stated rather than hidden: every package now acquires once, including the
ones that are skipped, and the hold includes the destination manifest read and
SHA-256 checks plus, for a copyable package, source validation. Each hold is one
package, but the mutex is a non-fair `LOCK_NB` poll: a backfill that re-acquires
immediately after releasing can win repeatedly against a waiting writer. The
backfill therefore sleeps one guard poll interval between packages in
`--apply`, which lowers the chance a waiting writer is starved but does not guarantee it a
turn (a node-22 poll is an NFS lock RPC of comparable latency). The per-package
hold is measured on node-22 over the NFS mount, where the backfill runs
(tasks.md EF-9), against the 900 s default deadline.

### D3 — node-27 acquires with a POSIX record lock (#2252, #2239)

Measured (`evidence/lock-interop-20260914.md`): node-27 `flock` does not exclude
node-22 NFS-client `flock`; node-27 `lockf` does, both directions. The guard
gains `primitive: Literal["flock", "posix"] = "flock"` on
`acquire_copyback_batch_lock`, `release_copyback_batch_lock` and
`copyback_batch_lock`. `posix` uses `fcntl.lockf(LOCK_EX | LOCK_NB)` in the same
poll loop; a busy result is `OSError` with `EAGAIN` **or** `EACCES` (both mean
held). Release is `lockf(LOCK_UN)` then `close`. Identity checks, the
refuse-before-create rule, the deadline and the error types are identical.

`lockf` rather than OFD: node-27's Python 3.11 `fcntl` does not expose
`F_OFD_SETLK`, and hand-packing `struct flock` is ABI-specific. The cost of
`lockf` is per-process semantics — closing any descriptor on the lock file drops
the lock, and threads of one process do not contend. The `posix` primitive is
therefore documented for single-threaded acquirers that open the lock file
nowhere else; the one caller, `node27_raw_retention`, is a single-threaded CLI.

Constraint recorded, not enforced: a `posix` holder on node-27 does not exclude a
`flock` acquirer on node-27. No node-27 process acquires the mutex today (every
acquirer listed in Context runs on node-22; on node-27 the object-store root is
the primary store, so a publisher there takes the same-root skip). ADR 0008.

Default stays `flock` for every existing caller: node-22 code cannot deploy
before #1831, and node-22 `flock` already excludes node-27 `posix` without it.

### D4 — which node-27 lanes lock

Only the canonical lane. The mutex protects promote windows; the canonical key
space is promoted under it by `publisher._copyback_canonical_precip` and
`scripts/canonical_precip_copyback_backfill.py`. No acquirer promotes under
`raw/` (`grep` of every mutex acquirer: no `raw/` key), so locking the raw lane
buys no exclusion and — under the D5 identity outcome — would stop raw pruning.
The precip-cache lane is a different root. The deleter requirement is narrowed
accordingly to key spaces a mutex writer promotes into (`runs/`, `forcing/`,
`canonical/`), with the enumeration as its evidence.

On node-27 the object-store root **is** the shared copyback root, so the
node-22 retention exemption "copyback root equals own primary store → no lock"
does not transfer: that exemption exists because on node-22 such a root is
unshared.

Per tree, never per pass; the planning walk is unlocked. Acquisition happens
immediately before `shutil.rmtree`, release in `finally`. One pass-level wait
budget (300 s, the same constant `retention` uses, moved into `copyback_guard`
as `DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS` and re-exported by
`retention` under its existing name) is charged with acquisition time only; once
spent, remaining canonical entries fail with `lock_budget_exhausted` without an
attempt. Disabled, dry-run and preflight-blocked runs acquire nothing and create
no lock file.

### D5 — identity outcome on node-27 (owner decision 2026-09-14)

The lock file is `0600` owned by uid 1103; the unit runs as uid 1005. The
identity contract is not relaxed: a group-shared `0660` lock would be refused by
the node-22 code live until #1831, breaking every writer. Consequence accepted
by the owner: until the unit runs as the root owner, every aged canonical cycle
fails with `lock_failure: lock_unsafe` (open `EACCES`, or refuse-before-create
when the file is absent) and is not removed. Raw and precip-cache are
unaffected. How visible that is, precisely: on every tick with an aged canonical
cycle `main()` returns 1, so the unit ends `Result=failed`; the unit has no
`OnFailure=`, so nothing pages. It shows in `systemctl --user --failed`, in the
documented `jq` check (non-zero on any `failed[]` entry) and in
`copyback_lock_failures.lock_unsafe`. The #2100 operator criterion
`counts.failed == 0` in `infra/env/node27-raw-retention.example` is suspended
for that period and the example says so. Follow-up ops issue: run the unit as
the copyback root owner.

Production relevance (#2239 ask): the node-27 unit does delete canonical cycles
today — its 2026-09-14 receipt has 2 `canonical/` entries in `deleted[]`, and the
#2100 installed-unit tick deleted 24.

### D6 — typed lock failures (#2262 ii)

Vocabulary, shared by both retention lanes via
`copyback_guard.copyback_lock_failure_kind(error)`:

| Error | `lock_failure` |
|---|---|
| `CopybackLockTimeout` | `lock_timeout` |
| `CopybackLockBudgetExhausted` | `lock_budget_exhausted` |
| any other `CopybackLockError` | `lock_unsafe` |

`lock_unsafe` covers every non-timeout lock refusal: a tampered or foreign-owned
lock file, an unopenable one (`EACCES`), and configuration refusals (non-finite
timeout, unknown primitive, unreadable root owner). The retention lanes pass
constants, so configuration refusals are not reachable in production; the
runbook text says "unsafe, unopenable or misconfigured" rather than "tampered".

`CopybackLockBudgetExhausted` subclasses `CopybackLockError`, so existing
`except CopybackLockError` sites still catch it. A `failed[]` entry from a lock
error carries `error` (text), `error_type` (exception class name) and
`lock_failure`; one from a removal `OSError` carries `error` and `error_type` as
today and no `lock_failure`. Each retention receipt carries
`copyback_lock_failures` with all three keys (zeros when nothing failed).
`_compact_retention` keeps that block verbatim, so the signal survives when
per-entry `failed[]` detail is dropped. The node-27 summary schema string moves
from `nhms.node27_raw_retention.production.v4` to `.v5`, following the v3→v4
precedent of bumping on new content.

The backfill (D2) keeps its single existing category
`copyback_lock_unavailable`; it has no budget, and the timeout/unsafe split is
already visible in the failure `reason` text.

### D7 — non-finite explicit timeout (#2262 i)

`resolve_copyback_lock_timeout_seconds` refuses `NaN`, `+inf` and `-inf` on the
explicit branch with `CopybackLockError`, as the env branch already does.
Resolution happens before any filesystem call, so a refusal creates no lock
file.

### D8 — plan-to-delete window and waiter count (#2262 iii, iv)

(iii) The tolerated window — a writer commits into a planned target before the
pass acquires — gets a test in each lane that removes under the mutex
(`services/orchestrator/retention` copyback root and node-27 canonical) that
pins the current contract: the tree is removed after acquisition and the
removal predicate is not re-evaluated. No behaviour change.

(iv) Accepted limit, not fixed. Waiters on the mutex from retention are one per
concurrent pass: node-22 scheduler pass, each concurrent operator `cleanup`
(no cross-invocation guard), and node-27 raw retention (at most one per checkout
when started through its wrapper, which holds `flock -n` on
`NODE27_RAW_RETENTION_LOCK_PATH`; a direct CLI call bypasses that). The
`copyback_guard` budget comment is corrected to list all three and to state that
the operator-cleanup count is the unbounded term. A cross-host single-waiter
guard would need a second lock file under the shared root with the same
identity problem D5 records; not justified by an operator-concurrency risk.

## Invariant Matrix

Governing invariant: every promotion, removal, or destination-dependent decision
under the shared copyback root that a lane reports as authoritative happens while
holding a lock that every other acquirer on that root provably contends with,
from either host; and no failure path destroys a destination tree's only copy.

Source-of-truth identity: the lock file `<copyback root>/.nhms-copyback-batch.lock`
(`0600`, owner = copyback root owner), `primitive` per host (node-22 `flock`,
node-27 `posix`).

Surfaces:
- Producers: `run_tree_copyback._replace_tree`, `_replace_file`;
  `forcing_copyback_backfill._plan_or_apply_packages`, `_copy_package`;
  unchanged `publisher` lanes and `canonical_precip_copyback_backfill`.
- Validators/preflight: `copyback_guard.resolve_copyback_lock_timeout_seconds`,
  `_require_lock_identity`, refuse-before-create in
  `acquire_copyback_batch_lock`.
- Storage/cache/query: lock file on the NFS export; `canonical/<S>/<cycle>`,
  `runs/<run_id>`, `forcing/**` trees.
- Public routes/entrypoints: `scripts/node27_raw_retention.py` CLI and its
  wrapper; `forcing_copyback_backfill` CLI; `cli cleanup`; scheduler pass.
- Frontend/downstream consumers: node-27 display reads canonical mirror and
  `runs/`; unchanged.
- Failure paths/rollback/stale state: D1 retained backup; D2 lock failure as
  failed package + rollback; D4/D6 lock failure as `failed[]`; budget
  exhaustion.
- Evidence/audit/readiness: node-27 retention summary JSON; scheduler pass
  receipt `retention` block and its compaction; backfill report;
  runbooks `forcing-copyback-backfill.md`, `current-production-ops.md`.

Regression rows:
- node-27 canonical removal, lock free → one `posix` acquire/release per tree,
  tree removed, `copyback_lock_failures` all zero.
- node-27 canonical removal while another process holds the lock → blocks until
  release, then removes; with a holder past the budget → `lock_timeout` then
  `lock_budget_exhausted`, trees kept, pass `completed`.
- node-27 canonical removal, lock file not owned by euid / unopenable / absent
  with non-owner euid → `lock_unsafe`, tree kept, no lock file created.
- node-27 raw and precip-cache removals → no acquisition, no lock file.
- node-27 dry-run / disabled / preflight-blocked → zero acquisitions, no lock file.
- `posix` holder vs `posix` attempt from a second process → attempt blocks
  (local test); cross-host `posix`↔`flock` → receipt only.
- guard explicit timeout `inf`/`nan`/`-inf`/`0` → `CopybackLockError`, no file.
- node-27 lock file sitting at the object-store root → never planned, still
  present after a `production_execute` pass.
- `copyback_run_trees` with promote and restore both failing → raises
  `RunTreeCopybackError` `OBJECT_STORE_COPYBACK_BACKUP_RETAINED` (consumer
  `_copyback_stage_run_trees` catches it).
- `_replace_tree` / `_replace_file`: promote fails + restore fails → backup
  exists, `RunTreeCopybackError.details["backup_path"]`; promote fails + temp
  cleanup raises → restore still happens (target has old content, no backup
  left); promote fails, restore succeeds → target has old content, nothing
  deleted after restore; success → no `.backup` residue.
- backfill `--apply`, competitor promotes a new tree then rolls it back while
  the backfill waits on the lock → package not reported `already_present`.
- backfill `--apply`, destination read happens with the lock held (second
  acquire times out during inspection).
- backfill plan mode → zero acquisitions, `observed_under_lock: false`.
- orchestrator retention lock timeout / unsafe / budget exhausted → typed
  `lock_failure`, counts block, compaction keeps the block.
- unchanged sibling: node-22 retention on non-copyback roots → no acquisition
  (existing tests stay green); publisher lanes → default `flock` unchanged.

## Boundary-surface checklist

- Shared helper roots: `copyback_guard` (primitive, classifier, budget constant,
  non-finite refusal).
- Write/delete/overwrite: `_replace_tree`, `_replace_file`,
  `node27_raw_retention.run_retention` removal loop, `retention._delete_entry`.
- Staging/publish/rollback: `_copy_package` commit/rollback under caller lock.
- Producer/consumer evidence: retention summary fields, `_compact_retention`,
  backfill report fields, runbooks.
- Stale-state/idempotency: plan-to-delete window (D8), plan-mode advisory
  (D2), NFS host-death lease (unchanged, guard docstring).
- Unchanged downstream: `publisher` lanes, `canonical_precip_copyback_backfill`,
  state-snapshot index merge, display API.

## Known limits

- The cross-host exclusion is proven by receipt only; CI and node-27 tests
  exercise `posix` vs `posix` on one host.
- node-27 canonical pruning is paused until the unit identity follow-up lands
  (D5).
- Retention waiter count remains unbounded in operator `cleanup` concurrency
  (D8 iv).
- `/home/nwm/yd-NWM` runs its own raw-retention unit against
  `/home/ghdc/yd-nwm/object-store`; whether that root is any node-22 copyback
  root is not established here. It pulls its own checkout and is not changed.
- node-22 receipts for #2237/#2236/retention signal wait for #1831.
- `services/orchestrator/retention` has no notion of host: its "copyback root
  equals primary store → no lock" exemption is correct only because the
  scheduler and `cleanup` run on node-22 (deployment topology, not code).
- After an NFS server restart, nfsd's grace period does not constrain a local
  `lockf` on node-27, so node-27 retention could take the lock while node-22 is
  still reclaiming its own. Low probability; not mitigated.
