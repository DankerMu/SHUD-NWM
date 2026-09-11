# Tasks

Fixture level: **expanded**. Issue #2238 carries no `Suggested fixture level`
(it was filed by `issue-scribe` from a cross-review finding, not through the
pipeline's Stage 5 contract), so the level is triaged here and the divergence
recorded: the issue's own "预估规模: S" describes the diff, not the risk. The
subject is a destructive `rmtree` lane on a production NFS export shared by two
hosts, inside `services/orchestrator` — an expanded trigger in
`openspec/project-profile.md`. Repair intensity: **expanded**.

Reference frame for every `path:line` in this change's documents: pre-existing
code is cited at the branch base `6fdb2015`; code this change adds is cited in
the branch tree. Historical measurements are plain numbers, never citations.

## Risk pack selection

| Pack | Status | Reason |
|---|---|---|
| concurrency/idempotency | **selected** | The whole change is joining a cross-process mutex. |
| publish/delete/rollback | **selected** | The protected party is another writer's rollback material. |
| file/path IO safety | **selected** | The guarded call is a no-follow `rmtree` under a shared root. |
| production config | **selected** | Which root gets locked is decided by two env-fed call sites; node-22's live config is the reason this is not latent. |
| evidence chain | **selected** | A lock failure must land in the pass receipt's `failed[]`, not collapse the receipt. |
| shared helper behavior | **selected** | Reversed after the fixture review. `copyback_guard.py:54-67` names itself the single in-code home of the acquisition-count claim behind the 900 s budget and derives it from "at most once per cycle per scheduler pass". This change adds a per-tree acquirer, so that claim stops being true and the comment is updated (T6). The helper's behaviour is unchanged; its documented invariant is not. |
| permissions/auth boundary | **selected** | Reversed after the fixture review. This change is the first to put a **deletion** lane under `copyback_guard.py:181-188`'s root-owner uid fail-closed check; a mismatch would turn every copyback-root removal into a `failed[]` entry and silently stop reclaiming that root. Measured clear on node-22 (`design.md` D1) rather than assumed. |
| data schema / migration | not selected | No DB schema, payload schema, or migration touched. |
| frontend contract | not selected | No `apps/frontend`, OpenAPI, or display surface. |
| numerical/scientific | not selected | No GRIB/NetCDF/CRS/unit logic. |
| Slurm scheduling | not selected | No sbatch, gateway, or scheduler-behaviour change; the scheduler is touched only at the `run_retention` call's argument list. |

## Implementation

- [x] T1 `run_retention` (`services/orchestrator/retention.py`) gains a
      keyword-only `copyback_root: Path | str | None = None`. It is resolved
      against `result.extra_roots` — the sanitised, resolved, de-duplicated,
      overlap-adjudicated set `_resolve_runs_only_roots` produced — so a blank,
      unset, relative, or overlap-rejected value selects nothing and the
      parameter cannot widen the deletion surface. Default `None` keeps every
      existing caller and test byte-identical.
- [x] T2 `_delete_entry` gains a keyword-only flag (or equivalent) saying this
      entry's root is the copyback root, and when it is set, wraps **only** the
      `remove_tree_allow_symlinks` call in the copyback batch mutex, acquired on
      the resolved copyback root. One acquire and one release per entry. The
      `shutil.rmtree` branch (primary object-store root) and the workspace-root
      branch are unchanged.
- [x] T3 `run_retention` carries one pass-level acquisition-wait budget
      (`design.md` D9): a module constant defaulting to 300 s plus a keyword
      override for tests, no new environment variable. Each copyback removal is
      given the remaining budget as its `timeout_seconds`; the elapsed
      acquisition time is charged against it; when nothing is left the entry is
      recorded as a failure with no acquisition attempted. Uses
      `acquire_copyback_batch_lock` / `release_copyback_batch_lock` with the
      release in a `finally`, because the context manager gives no way to
      measure the wait separately from the hold.
- [x] T4 `CopybackLockError` joins `_delete_entry`'s `except` tuple, next to
      `SafeFilesystemError`, with the docstring extended to name it and the
      reason (both are `RuntimeError`, not `OSError`; an escape collapses the
      scheduler receipt to `{"status": "error"}` and aborts the `cleanup` CLI).
      `CopybackLockTimeout` is covered as a subclass and is asserted separately.
- [x] T5 Both call sites pass the root they already pass positionally:
      `services/orchestrator/cli.py` (`os.getenv("NHMS_OBJECT_STORE_COPYBACK_ROOT")`)
      and `services/orchestrator/scheduler_runtime.py`
      (`self.config.object_store_copyback_root`).
- [x] T6 Update the budget comment at `packages/common/copyback_guard.py:54-67`
      so its acquisition-count derivation names retention's per-tree acquirer and
      the pass budget that bounds it. Comment only — no guard behaviour changes,
      and `tests/test_copyback_guard.py` must stay green untouched.
- [x] T7 Tests in `tests/test_retention_copyback_mutex.py` (new file; the
      existing `tests/test_retention_extra_roots.py` is already 747 lines and
      this is a distinct contract), covering every clause in the Evidence Floor
      below.
- [x] T8 `uv run ruff check .` clean; the full retention suite plus
      `tests/test_copyback_guard.py` green.
- [x] T9 Route the new suite in `scripts/select_ci_tests.py` so the three
      importer pairs the gate reports as undispositioned
      (`services/orchestrator/__init__.py`, `cli.py`, `retention.py` →
      `tests/test_retention_copyback_mutex.py`) close **by rule, not by
      exclusion**: the suite is DB-free, sub-5 s, and is the requirement oracle
      for the module that changed, so excluding it would defeat the gate. Each
      pair closes at the site the derivation actually reaches — the broad
      `services/orchestrator/**` rule for the two it owns, the `cli.py` stop
      rule at its own site for the third — plus the
      `tests/retention_test_helpers.py` support-module rule, which the new suite
      top-level-imports.
- [x] T10 Record the growth in the frozen broad-rule size pin in
      `tests/test_select_ci_tests.py`. The pin's own comment defines this as the
      update protocol ("the literal stays FROZEN here … Growing the rule means
      consciously editing this list and recording the new lane wall-clock"), so
      the edit adds the new target to the still-exact expected set and extends
      the running-count narrative with the measured wall-clock. No comparison is
      relaxed, nothing is removed, nothing is skipped.

- [x] T11 Grow the two retention-partition pins in
      `tests/test_select_ci_tests.py` that were written for four partitions and
      did not grow with the corpus: `RETENTION_PARTITIONS` (a floor, so the new
      partition is simply unmentioned today) and `RETENTION_RULE_ONLY_PARTITIONS`
      (the fracture pin proving each moved partition is load-bearing in the owner
      rule). Membership in the second is verified empirically against its stated
      criterion — reached ONLY through the owner rule — not assumed. Both pins
      end up covering more, never less. This is the same "an audit a new suite
      silently invalidated" failure the file's own #1452 note describes.
## Evidence Floor

Each clause is a machine-checkable statement about the final branch tree. EF-1
through EF-16 are local and mandatory; EF-17 is post-merge ops and is a recorded
known limit, not a merge blocker. Contended cases drive the guard's own
per-acquisition timeout override down to sub-second so the suite stays fast.

- [x] **EF-1 — the copyback lane locks.** A retention pass that removes a tree
      under the copyback root acquires the mutex for that removal: a test holds
      the lock from another thread and asserts the removal does not proceed
      while it is held, and completes once it is released.
- [x] **EF-2 — the reverse direction.** While a retention removal holds the
      mutex, a second party attempting to acquire the same root's lock blocks
      and cannot proceed until the removal returns. Asserted independently of
      EF-1, because issue #2238's acceptance criteria name both directions and
      a one-directional test passes against an implementation that acquires
      after the `rmtree` instead of before it.
- [x] **EF-3 — the other two lanes do not lock.** In the same pass, a removal
      under the workspace root and a removal under the primary object-store root
      both complete while the copyback mutex is held by a competitor, and no
      `.nhms-copyback-batch.lock` exists under either of those roots afterwards.
      Asserted as a behavioural difference, not merely as "the delete worked".
- [x] **EF-4 — the dedup case locks the surviving root.** When `WORKSPACE_ROOT`
      and the copyback root resolve to the same directory, the single admitted
      root is locked.
- [x] **EF-5 — the primary-identity case locks nothing, and that is deliberate.**
      When the copyback root resolves to the same directory as the primary
      object-store root, its `runs/` entries are still removed (through the
      primary arm) and no lock file is created — the configuration in which
      `run_tree_copyback.copyback_run_trees` returns
      `copyback_root_matches_object_store_root` and no copyback writer exists.
      Asserted on both halves: the removal happens, and the lock file does not.
- [x] **EF-6 — a root the resolver dropped is neither swept nor locked.** A
      relative `copyback_root`, and one that lost an overlap adjudication to a
      different additional root, each produce zero removals and zero lock files
      at that path.
- [x] **EF-7 — a blank or unset `copyback_root` locks nothing** while the pass
      otherwise behaves exactly as it does today.
- [x] **EF-8 — per tree.** A pass removing N ≥ 3 copyback trees performs exactly
      N acquisitions and N releases, and the mutex is not held between them —
      asserted on acquisition count and on the observation that the lock is free
      at a point between two removals.
- [x] **EF-9 — a timeout is a recorded failure, and the tree survives.** With
      the lock held by a real second process, the pass returns normally; the
      blocked entry appears in `failed[]` with an `error`, is absent from
      `deleted[]`, contributes nothing to `freed_bytes`, **the tree is still on
      disk afterwards**, and the entries after it are still processed.
      `CopybackLockTimeout` does not escape `_delete_entry`.
- [x] **EF-10 — an unsafe lock file is a recorded failure too.** The same,
      including the tree-survives assertion, driven by a non-timeout
      `CopybackLockError` (for example a lock file whose mode is not `0o600`),
      so the `except` tuple is proven to catch the base class and not just the
      subclass.
- [x] **EF-11 — the pass-level wait budget bounds a stalled sweep.** With the
      lock held for the whole pass and the budget set to a small test value, the
      total time the pass spends waiting does not exceed that budget regardless
      of how many copyback entries were planned, every unattempted entry lands
      in `failed[]` with an error naming the exhausted budget, and the pass
      still returns normally.
- [x] **EF-12 — zero-write passes acquire nothing.** `dry_run=True`,
      `enabled=False`, and `extra_roots_enabled=False` each leave no lock file
      under the copyback root and perform no acquisition.
- [x] **EF-13 — the predicate is unchanged.** For one seeded tree, the
      `deleted[]`, `failed[]` and `skipped[]` key sets and their reasons are
      identical between a run with `copyback_root` supplied and uncontended and
      a run with `copyback_root=None` — the same fixture, both arms, compared
      directly. The mutex changes timing, never selection.
- [x] **EF-14 — the lock file is not a removal candidate.** With
      `.nhms-copyback-batch.lock` present at the copyback root (and at the
      workspace and primary roots), no pass selects it into `planned`,
      `deleted`, `failed` or `skipped`, on any root.
- [x] **EF-15 — both call sites name the copyback root.** The scheduler pass
      and the `cleanup` CLI each pass the copyback root into `run_retention`,
      asserted at the call site rather than inferred: this is the single place
      where the whole mutex can be lost in deployment without any test on the
      retention module itself noticing.
- [x] **EF-16 — the targeted lane routes to the new suite.** With a changed
      file list of exactly `services/orchestrator/retention.py`, and again for
      `cli.py` and for `__init__.py`, `select_tests` returns a list containing
      `tests/test_retention_copyback_mutex.py`. Asserted as routing, not as
      "the gate is green": the gate can be satisfied by an exclusion token,
      which would leave the oracle unrouted.
- [ ] **EF-17 — node-22 deployment receipt (post-merge, known limit).** After
      master is deployed to `/scratch/frd_muziyao/NWM`, one scheduler pass whose
      `deleted[]` contains `/ghdc/data/nwm/object-store` entries, with
      `retention.status=completed` and the copyback-root count not regressed to
      zero. node-22's active checkout is pre-maintenance-window (3.12 venv, no
      `uv sync`, no bare `uv run`), and `packages/common/copyback_guard.py` does
      not exist there yet, so this cannot be produced from this branch and is
      not a merge clause. Routed as a tracked issue at Phase 8.

## Known limits

- The plan-to-delete window stays open: a tree a writer promotes and commits
  after the pass planned it is still removed by that pass. This is the shape
  issue #2238's title reaches for, and it is **not** what the mutex closes.
  Stated in the spec as an explicit non-guarantee (`design.md` D4) and routed as
  a tracked follow-up at Phase 8, not silently absorbed.
- `freed_bytes` remains a planning-time estimate, measured outside the mutex.
- `scripts/node27_raw_retention.py:553` is the second unlocked deleter on the
  same NFS export. Out of scope by the issue's boundary; the spec requirement
  names it as a known-violating implementation rather than narrowing itself to
  stay true by construction (`design.md` D10).
- Lock semantics are exercised locally on APFS/ext4, not on the production
  NFSv4.2 export. The third state `copyback_guard.py:212-219` documents
  (correctly owned, no local holder, still locked) is not reproducible in the
  suite; it is the reason the pass budget exists, and the budget itself is
  tested with an ordinary local holder.
- `packages/common/copyback_guard.py` is named by **no** rule in
  `scripts/select_ci_tests.py`. It is not unrouted — same-name derivation and
  the `packages/**` supplemental routes still select nine suites for it,
  `tests/test_copyback_guard.py` among them — but the retention mutex suite is
  **not** one of them (measured: `select_tests(["packages/common/copyback_guard.py"])`
  returns nine paths and the mutex suite is absent). So a diff touching only the
  guard does not run the deleter-side oracle that depends on it.
  `packages/common` is outside `DIRECTORY_RULE_AUDIT_PATHS`, so the gate does
  not demand a rule. Pre-existing from #2035, found while wiring T9, out of
  scope here; routed as a tracked issue at Phase 8.
- Issue #2238's sixth acceptance criterion (correct #2035's "Unchanged
  downstream consumers" wording for `retention.py`) is **already satisfied at
  base `6fdb2015`** by that change's own post-ceiling sweep
  (`harden-copyback-batch-mutex-and-dir-traversal/design.md:535-546`). This
  change neither repeats nor claims it; see `design.md` D7.
