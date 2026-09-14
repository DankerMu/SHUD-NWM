# Tasks

Batch: #2237, #2236, #2252, #2239, #2262 — one PR, one lock adjudication.

Fixture level: **broad-expanded**; repair intensity **broad-expanded**. None of
the issues carries a `Suggested fixture level` (all filed from review findings,
not pipeline Stage 5), so the level is triaged here: a shared helper
(`copyback_guard`) plus four business lanes, destructive removal on a
production NFS export shared by two hosts, rollback material, and production
config identity.

Reference convention: symbol names only (design.md). Line-citation grep in
design.md must return nothing over this change directory.

## Risk pack selection

| Pack | Status | Reason |
|---|---|---|
| concurrency/idempotency | **selected** | Cross-process, cross-host mutex primitive and lock scope changes. |
| publish/delete/rollback | **selected** | Backup retention on failure (#2237), rollback under lock (#2236), deletion under lock (#2252). |
| file/path IO safety | **selected** | Lock file open/identity, `rmtree` under shared root, backup rename. |
| shared helper behavior | **selected** | `copyback_guard` gains a primitive, a classifier, an error type, a constant. |
| production config | **selected** | node-27 unit identity decides whether canonical pruning runs (D5). |
| evidence chain | **selected** | New receipt fields; compaction must keep the lock signal; runbook closure criterion. |
| permissions/auth boundary | **selected** | Lock identity contract is kept and now fails closed for a lane that used to succeed. |
| data schema / migration | not selected | No DB schema or migration; JSON receipts gain additive fields; node-27 summary schema string v4→v5. |
| frontend contract | not selected | No `apps/frontend` or API surface. |
| numerical/scientific | not selected | No GRIB/NetCDF/unit logic. |
| Slurm scheduling | not selected | No sbatch/gateway/scheduler behaviour change. |

## Implementation

- [x] T1 `copyback_guard.resolve_copyback_lock_timeout_seconds`: explicit branch
      refuses non-finite values (D7).
- [x] T2 `copyback_guard`: `primitive` keyword (`"flock"` default, `"posix"`) on
      `acquire_copyback_batch_lock`, `release_copyback_batch_lock`,
      `copyback_batch_lock`; unknown primitive → `CopybackLockError` before any
      filesystem call; `posix` busy = `EAGAIN` or `EACCES` (D3). Docstring states
      the measured interop result and the single-threaded constraint.
- [x] T3 `copyback_guard`: `CopybackLockBudgetExhausted(CopybackLockError)`,
      `copyback_lock_failure_kind`, and
      `DEFAULT_RETENTION_COPYBACK_LOCK_WAIT_BUDGET_SECONDS = 300.0`;
      `retention.DEFAULT_COPYBACK_LOCK_WAIT_BUDGET_SECONDS` stays importable with
      the same value (D4, D6).
- [x] T4 `copyback_guard` module comment: waiter accounting lists node-22 pass,
      operator `cleanup`, node-27 raw retention; names the unbounded term (D8 iv).
- [x] T5 `run_tree_copyback._replace_tree` and `_replace_file`: D1 lifecycle and
      `OBJECT_STORE_COPYBACK_BACKUP_RETAINED`; correct the `_replace_tree`
      docstring's "no data was lost" claim.
- [x] T6 `forcing_copyback_backfill`: D2 lock scope in `--apply`,
      `observed_under_lock` per package and top level, lock failure stays
      category `copyback_lock_unavailable`, one guard poll interval between
      packages; `_copy_package` does not acquire.
- [x] T7 `node27_raw_retention.run_retention`: canonical lane under
      `posix` mutex per tree with pass budget, typed `lock_failure`,
      `copyback_lock_failures` block on every summary shape (completed, disabled,
      preflight-blocked); lock-failure entries carry `error`, `error_type`,
      `lock_failure`; `SCHEMA_VERSION` → `...production.v5`; raw and
      precip-cache unlocked (D4, D5, D6).
- [x] T8 `retention._delete_entry` / `_remove_tree_under_copyback_mutex`: raise
      `CopybackLockBudgetExhausted`, typed `lock_failure`,
      `copyback_lock_failures` in the result payload;
      `scheduler_evidence_payload._compact_retention` keeps the block (D6).
- [x] T9 Docs: `docs/runbooks/forcing-copyback-backfill.md` closure criterion
      (apply report only, plan advisory); `docs/runbooks/current-production-ops.md`
      raw-retention section: `lock_failure` meanings and the D5 identity state;
      `infra/env/node27-raw-retention.example`: the `counts.failed == 0`
      criterion is suspended while the unit is not the copyback root owner, and
      a failing tick is `Result=failed` with no `OnFailure=`;
      ADR `docs/adr/0008-cross-host-copyback-lock-primitive.md`.
- [x] T10 New tests live in new files (large-file guard): 
      `tests/test_copyback_guard_primitive.py`,
      `tests/test_run_tree_copyback_backup_lifecycle.py`,
      `tests/test_forcing_copyback_backfill_lock_scope.py`,
      `tests/test_node27_raw_retention_copyback_mutex.py`,
      `tests/test_retention_copyback_lock_signal.py`. Existing tests that pin the
      old lock scope in `tests/test_forcing_copyback_backfill.py` are updated only
      where the contract moved, keeping their assertion strength: the tests
      pinning the lock across the `rollback_log` lifetime and the tests pinning
      "the shared copy helper never acquires" move with the lock to the caller.
      Every `posix` holder and every `posix` probe runs in a separate
      subprocess: `lockf` is per process, so a second descriptor in the test
      process cannot observe the hold and its `close()` would drop it.
- [x] T11 `scripts/select_ci_tests.py` selects the new test files for diffs
      touching `copyback_guard`, `node27_raw_retention`, `run_tree_copyback`,
      `forcing_copyback_backfill`, `retention` (extend the routing only if not).
- [ ] T12 Follow-up issues: node-27 unit identity (D5); #2262 body mismatch
      comment.

## Evidence Floor

Each item names input → expected output. "Red" = fails on the pre-change source.

- [x] EF-1 guard: explicit `timeout_seconds` of `inf`, `-inf`, `nan` →
      `CopybackLockError`, lock file absent afterwards. Red for `inf`/`nan`.
- [x] EF-2 guard `posix` (holder and probe in separate subprocesses): process A
      holds `posix`, process B `posix` with 0.2 s timeout → `CopybackLockTimeout`; after A releases, B acquires. Identity
      refusals (symlink, mode ≠ 0600, nlink 2, non-owner create) raise
      `CopybackLockError` under `posix` exactly as under `flock`. Unknown
      primitive → `CopybackLockError`, no lock file.
- [x] EF-3 guard: `EACCES` from `lockf` is treated as busy (poll continues until
      deadline → `CopybackLockTimeout`), not as unsafe.
- [x] EF-4 classifier: timeout / budget-exhausted / other lock error → the three
      kinds; `CopybackLockBudgetExhausted` is caught by `except CopybackLockError`.
- [x] EF-5 `_replace_tree` and `_replace_file` each: (a) promote fails + restore
      fails → backup present, `RunTreeCopybackError` code
      `OBJECT_STORE_COPYBACK_BACKUP_RETAINED` with `backup_path`; (b) promote fails
      + temp cleanup raises → target holds the old content or the backup is
      present, never neither; (c) promote fails + restore succeeds → old content
      at target, no `.backup` residue; (d) success → new content, no `.backup`
      or `.tmp` residue. (a) and (b) red.
- [x] EF-5b `copyback_run_trees` on a re-copyback of an existing run with
      promote and restore both failing → raises `RunTreeCopybackError` code
      `OBJECT_STORE_COPYBACK_BACKUP_RETAINED`, `details["backup_path"]` exists on
      disk. Red (bare `OSError` and no backup before).
- [x] EF-6 backfill `--apply`: competitor holds the lock, promotes a
      previously-absent `forcing/<...>` tree and rolls it back (`backup_dir=None`)
      before releasing, while the backfill waits → the package is not
      `already_present` (it is copied or failed). Red.
- [x] EF-7 backfill `--apply`: during `_inspect_existing_target` a second
      `copyback_batch_lock` with a short timeout raises `CopybackLockTimeout`
      (lock held across inspection and the skip decision); exactly one
      acquisition per package, including `already_present` ones; a
      `checksum_mismatch` package takes zero. Red.
- [x] EF-8 backfill plan mode: zero acquisitions, no lock file, every package
      record `observed_under_lock: false`; `--apply` records `true`.
- [ ] EF-9 lock overhead, measured where the backfill runs: on node-22, read-only,
      with `/scratch/frd_muziyao/NWM/.venv/bin/python` (never `uv sync` or a bare
      `uv run`), time the per-package new hold — destination tree read + SHA-256
      over `/ghdc/data/nwm/object-store/forcing/**`, plus source tree SHA-256 for
      the same keys in node-22's object store — and report package count, p50,
      max seconds and the total against the 900 s deadline. If node-22 cannot be
      measured, bound it as bytes ÷ the measured ~62 MB/s and record that as a
      deviation.
- [x] EF-10 node-27 raw retention (lock holders and probes in subprocesses):
      N aged canonical cycles, lock free → N
      `posix` acquisitions and N releases, each spanning only its own `rmtree`;
      raw and precip-cache targets → zero acquisitions; summary
      `copyback_lock_failures` zeros. Red (zero acquisitions before).
- [x] EF-11 node-27 raw retention: lock held by another process past the budget →
      first canonical entry `lock_timeout`, rest `lock_budget_exhausted` with no
      attempt, trees still on disk, raw lane still deleted, `status: completed`,
      counts block matches, `main()` exit 1. Red.
- [x] EF-12 node-27 raw retention: lock file owned by another uid / euid not the
      root owner with no lock file → `lock_unsafe`, tree kept, no lock file
      created in the absent case, `main()` exit 1. Red.
- [x] EF-12b node-27 raw retention: `.nhms-copyback-batch.lock` present at the
      object-store root → absent from `planned[]` and still present after a
      `production_execute` pass.
- [x] EF-13 node-27 raw retention: dry-run, disabled, preflight-blocked → zero
      acquisitions, no lock file, `copyback_lock_failures` present with zeros.
- [x] EF-14 orchestrator retention: timeout / unsafe / budget-exhausted failures
      carry `lock_failure`; payload `copyback_lock_failures` counts;
      `_compact_retention` output keeps the block. Red.
- [x] EF-15 plan-to-delete window, in both locking lanes
      (`services/orchestrator/retention` copyback root in
      `tests/test_retention_copyback_lock_signal.py`; node-27 canonical in
      `tests/test_node27_raw_retention_copyback_mutex.py`): pass plans tree T; a
      writer acquires, replaces T with new content, commits, releases; the pass
      then removes T and the planned/deleted sets are unchanged. (Pins existing
      behaviour; not red by design.)
- [x] EF-16 existing suites green: `tests/test_copyback_guard.py`,
      `tests/test_run_tree_copyback.py`, `tests/test_forcing_copyback_backfill.py`,
      `tests/test_node27_raw_retention.py`, `tests/test_retention_copyback_mutex.py`,
      `tests/test_orchestration_chain.py`, scheduler evidence payload tests.
- [ ] EF-17 node-27 oracle at the PR head:
      `export TMPDIR=/home/nwm/tmp PATH=$HOME/.local/bin:$PATH` then
      `uv run pytest -q` over EF-16 plus the new files, and
      `uv run ruff check .` locally.
- [ ] EF-18 node-27 live receipt: one real `production_execute` pass of the
      branch code as `nwm` through `scripts/node27_raw_retention_once.sh` with
      `NODE27_RAW_RETENTION_REPO` pointing at an isolated checkout (so the
      wrapper's `flock -n` on `NODE27_RAW_RETENTION_LOCK_PATH` still serialises it
      against the timer) and a private summary/log root. Preconditions: the
      isolated checkout has `.venv` and a mode-600
      `infra/env/node27-raw-retention.env` copied from production, and
      `NODE27_RAW_RETENTION_LOCK_PATH` equals the timer's lock path → JSON with raw deletions
      unaffected and every aged canonical target in `failed[]` with
      `lock_failure: lock_unsafe`, or `canonical` not aged that day (recorded).
      Database URL is never copied into the receipt.
- [x] EF-19 cross-host receipt: `evidence/lock-interop-20260914.md` (done before
      implementation).
- [x] EF-20 `openspec validate harden-copyback-mutex-residuals --strict
      --no-interactive` passes; `scripts/select_ci_tests.py` selects the new
      files for the touched modules.

## Non-goals

- Relaxing the lock identity contract or changing node-22 writers' primitive.
- Bounding operator `cleanup` concurrency (D8 iv).
- Changing the unit identity on node-27 (follow-up ops issue).
- `review_gate.py` / `.review-gate-issues.json` (#2261's scope, misfiled into
  #2262's body).
- Publisher lanes, `canonical_precip_copyback_backfill`, retention predicates.
