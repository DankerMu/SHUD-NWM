# Tasks

Fixture level: **expanded**. Issue #2238 carries no `Suggested fixture level`
(it was filed by `issue-scribe` from a cross-review finding, not through the
pipeline's Stage 5 contract), so the level is triaged here and the divergence
recorded: the issue's own "预估规模: S" describes the diff, not the risk. The
subject is a destructive `rmtree` lane on a production NFS export shared by two
hosts, inside `services/orchestrator` — an expanded trigger in
`openspec/project-profile.md`. Repair intensity: **expanded**.

Reference convention: code is named by symbol, never by line number (design.md
"Reference convention"). One grep enforces it, and it must return nothing:

```
grep -rnE '[`A-Za-z_)]:[0-9]+|(#L|::|:L)[0-9]+|line [0-9]+|第 ?[0-9]+ ?行' openspec/changes/route-retention-deletes-through-copyback-mutex
```

It covers the common spellings, not every spelling that exists; design.md names
the ones it misses rather than claiming the stronger thing.

## Risk pack selection

| Pack | Status | Reason |
|---|---|---|
| concurrency/idempotency | **selected** | The whole change is joining a cross-process mutex. |
| publish/delete/rollback | **selected** | The protected party is another writer's rollback material. |
| file/path IO safety | **selected** | The guarded call is a no-follow `rmtree` under a shared root. |
| production config | **selected** | Which root gets locked is decided by two env-fed call sites; node-22's live config is the reason this is not latent. |
| evidence chain | **selected** | A lock failure must land in the pass receipt's `failed[]`, not collapse the receipt. |
| shared helper behavior | **selected** | Reversed after the fixture review. `copyback_guard`'s budget comment is the single in-code home of the acquisition-count claim behind the 900 s default and derives it from "at most once per cycle per scheduler pass". This change adds a per-tree acquirer and the comment is updated (T6). |
| permissions/auth boundary | **selected** | Reversed after the fixture review. This change is the first to put a lane **whose purpose is deletion** under `copyback_guard._require_lock_identity`'s root-owner uid fail-closed check; a mismatch would turn every copyback-root removal into a `failed[]` entry and silently stop reclaiming that root. Measured clear on node-22 (design.md D1) rather than assumed. |
| data schema / migration | not selected | No DB schema, payload schema, or migration touched. |
| frontend contract | not selected | No `apps/frontend`, OpenAPI, or display surface. |
| numerical/scientific | not selected | No GRIB/NetCDF/CRS/unit logic. |
| Slurm scheduling | not selected | No sbatch, gateway, or scheduler-behaviour change; the scheduler is touched only at the `run_retention` call's argument list. |

## Implementation

- [x] T1 `run_retention` (`services/orchestrator/retention.py`) gains a
      keyword-only `copyback_root: Path | str | None = None`, resolved against
      `result.extra_roots` so a blank, unset, relative, or overlap-rejected
      value selects nothing and the parameter cannot widen the deletion surface.
      Default `None` leaves the pass byte-identical for every caller that does
      not pass it.
- [x] T2 `_delete_entry` gains a keyword-only flag saying this entry's root is
      the copyback root, and when it is set, wraps **only** the
      `remove_tree_allow_symlinks` call in the copyback batch mutex, acquired on
      the resolved copyback root. One acquire and one release per entry. The
      `shutil.rmtree` branch (primary object-store root) and the workspace-root
      branch are unchanged.
- [x] T3 `run_retention` carries one pass-level acquisition-wait budget
      (design.md D9): a module constant defaulting to 300 s plus a keyword
      override for tests, no new environment variable. Each copyback removal is
      given the remaining budget as its `timeout_seconds`; the elapsed
      acquisition time is charged against it; when nothing is left the entry is
      recorded as a failure with no acquisition attempted. Uses
      `acquire_copyback_batch_lock` / `release_copyback_batch_lock` with the
      release in a `finally`, because the context manager gives no way to
      measure the wait separately from the hold.
- [x] T4 `CopybackLockError` joins `_delete_entry`'s `except` tuple, next to
      `SafeFilesystemError`, with the docstring extended to name it and the
      reason. `CopybackLockTimeout` is covered as a subclass and is asserted
      separately.
- [x] T5 Both call sites pass the root they already pass positionally:
      `services/orchestrator/cli.py` (`os.getenv("NHMS_OBJECT_STORE_COPYBACK_ROOT")`)
      and `services/orchestrator/scheduler_runtime.py`
      (`self.config.object_store_copyback_root`).
- [x] T6 Update `copyback_guard`'s budget comment so its acquisition-count
      derivation names retention's per-tree acquirer and the pass budget that
      bounds it. Comment only — no guard behaviour changes, and
      `tests/test_copyback_guard.py` stays green untouched.
- [x] T7 Tests in `tests/test_retention_copyback_mutex.py` (new file; the
      existing `tests/test_retention_extra_roots.py` is a distinct contract),
      covering every clause in the Evidence Floor below.
- [x] T8 `uv run ruff check .` clean; the full retention suite plus
      `tests/test_copyback_guard.py` green.
- [x] T9 Route the new suite in `scripts/select_ci_tests.py` so the three
      importer pairs the gate reports as undispositioned
      (`services/orchestrator/__init__.py`, `cli.py`, `retention.py` →
      `tests/test_retention_copyback_mutex.py`) close **by rule, not by
      exclusion**: the suite is DB-free, sub-5 s, and is the requirement oracle
      for the module that changed. Each pair closes at the site the derivation
      actually reaches — the broad `services/orchestrator/**` rule for the two
      it owns, the `cli.py` stop rule at its own site for the third — plus the
      `tests/retention_test_helpers.py` support-module rule.
- [x] T10 Record the growth in the frozen broad-rule size pin in
      `tests/test_select_ci_tests.py`. The pin's own comment defines this as the
      update protocol, so the edit adds the new target to the still-exact
      expected set and extends the running-count narrative with the measured
      wall-clock. No comparison is relaxed, nothing is removed or skipped.
- [x] T11 Grow the two retention-partition pins in
      `tests/test_select_ci_tests.py` that were written for four partitions:
      `RETENTION_PARTITIONS` (a floor) and `RETENTION_RULE_ONLY_PARTITIONS`
      (the fracture pin proving each moved partition is load-bearing in the
      owner rule). Membership in the second is verified empirically against its
      stated criterion — reached ONLY through the owner rule — not assumed.
      Both pins end up covering more, never less. The fracture pin is the one
      that can red; extending the floor row keeps the corpus inventory honest.

## Evidence Floor

Each clause is a machine-checkable statement about the final branch tree. EF-1
through EF-16 are local and mandatory; EF-17 is post-merge ops and is a recorded
known limit, not a merge blocker. Contended cases set
`copyback_lock_wait_budget_seconds` to sub-second values so the suite stays
fast.

**No clause below rests on reading a test for the presence of an assertion.**
The record is `evidence/mutation-sweep.md` in this change directory, and it
carries two evidentiary sweeps: 18 mutants built independently by a review seat
at `3854b596`, one of which survives; and 27 mutants re-measured at `4fe059f7`,
which found no over-claimed clause. Both sweeps re-ran the suite under CPU
oversubscription with no flake.

Five legs are not redded by any single-edit mutant of sweep B's 18. Each is
named here rather than folded into the headline:

- EF-14 admits no removal mutation at all: `retention.py` never references
  `COPYBACK_BATCH_LOCK_NAME`, and the planner's walk excludes the root-level
  lock file by construction, so there is no line whose deletion admits it.
- EF-6's **relative** leg is excluded the same way — `_sanitize_root_candidate`
  returns `None` before membership is consulted, so no lock site exists to
  mutate.
- EF-5 and EF-6's **overlap** leg are different: they survive a *single*
  mutation of the membership check, because `_delete_entry`'s
  `containment_root` nesting makes that check a redundant second line of
  defence. The double mutant that also adds a pass-level acquire reds both.
  That is the one survivor among the 18.
- EF-11's third case pins a guarantee the code already had at `3854b596`; what
  came later is the clause and its test. No row of that sweep targets it, which
  means that sweep had a real gap — a mutant charging acquire and hold as a
  single span would have survived it with nothing to red. That mutant was built
  and measured at `4fe059f7` instead, and reds exactly that one test.

EF-4 and EF-12's `extra_roots_enabled=False` parameter are covered, but were not
named in the sweep table's non-exhaustive "incl." lists; both were re-measured
at `4fe059f7` and the covering mutants are recorded there.

- [x] **EF-1 — the copyback lane locks.** A retention pass that removes a tree
      under the copyback root acquires the mutex for that removal: a test holds
      the lock from another thread and asserts the removal does not proceed
      while it is held, and completes once it is released.
- [x] **EF-2 — the reverse direction.** While a retention removal holds the
      mutex, a second party attempting to acquire the same root's lock blocks
      and cannot proceed until the removal returns. Asserted independently of
      EF-1, because a one-directional test passes against an implementation that
      acquires after the `rmtree` instead of before it.
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
      primary arm) and no lock file is created: the root is not a member of
      `result.extra_roots`, and membership is the whole rule (design.md D3).
      Asserted on both halves — the removal happens, and the lock file does not.
- [x] **EF-6 — a root the resolver dropped is not reached by the copyback
      lane.** A relative `copyback_root`, and one that lost an overlap
      adjudication to a different additional root, each produce zero removals
      through that lane and zero lock files at that path. The overlap case
      claims nothing about the *winning* root's own sweep, which in that
      geometry can still reach the same path unlocked (design.md D3).
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
      still returns normally. Two further pins: a wait that *succeeds* is
      charged against the budget, and a long *uncontended hold* is not.
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
      Alone among these clauses EF-14 has **no removal mutation**, and that is a
      structural exclusion rather than a coverage gap: `retention.py` never
      references `COPYBACK_BATCH_LOCK_NAME` at all, and neither planner enumerates
      root-level entries (design.md D6 states where each walk starts). There is
      no line whose deletion admits it. The case is kept as a regression guard against a
      future widening of the enumeration.
- [x] **EF-15 — both call sites name the copyback root.** The scheduler pass
      and the `cleanup` CLI each pass the copyback root into `run_retention`,
      asserted at the call site: this is the single place where the whole mutex
      can be lost in deployment without any test on the retention module itself
      noticing.
- [x] **EF-16 — the targeted lane routes to the new suite.** For each of
      `services/orchestrator/retention.py`, `cli.py` and `__init__.py`, a test
      reds if `select_tests` stops returning
      `tests/test_retention_copyback_mutex.py` for that module. Asserted as
      routing, not as "the gate is green": the gate can be satisfied by an
      exclusion token, and two of its token classes (`runtime-budget`,
      `fn-gated`) have no machine check at all, which would leave the oracle
      unrouted while every test stayed green.
      The three legs are not asserted the same way, and the difference is
      recorded rather than glossed: `retention.py` has a direct assertion on a
      changed-file list of exactly that module, plus the per-partition fracture
      pin; `cli.py` has a direct at-site assertion, proven to red when the
      at-site target is removed; `__init__.py` has no assertion naming it and
      rides the broad `services/orchestrator/**` rule's frozen exact-output
      literal instead. That is accepted for this leg specifically because the
      literal is an equality pin **on the selector's own output**, and
      `select_ci_tests` never reads the disposition/exclusion table at all:
      measured, deleting the suite from the broad rule's member list reds that
      assertion, and no exclusion token can mask it. It would not be accepted
      for `cli.py`, whose route is a stop rule the broad literal never
      exercises.
      The counterfactual was run rather than argued: removing the route entirely
      reds **five** tests, all of them the disposition-audit family, and a
      single `runtime-budget` token restores all five to green with the route
      still gone — which is precisely why EF-16 asserts routing directly.
- [ ] **EF-17 — node-22 deployment receipt (post-merge, known limit).** Two
      halves, because the first alone proves nothing about this change:
      1. *Non-regression, labelled as such.* After master is deployed to
         `/scratch/frd_muziyao/NWM`, one scheduler pass whose `deleted[]`
         contains `/ghdc/data/nwm/object-store` entries, with
         `retention.status=completed` and the copyback-root count not regressed
         to zero. A build with the mutex removed satisfies all three criteria
         byte for byte, so this half is a regression guard, not evidence of
         acquisition.
      2. *The discriminating probe.* Hold the copyback root's batch lock from a
         second process across one pass, and require that pass's copyback-root
         entries to appear in `failed[]` carrying the lock error while the
         workspace-root and primary-root entries still appear in `deleted[]`. A
         mutex-less build deletes straight through a held lock. The lock file's
         mere presence does **not** substitute (design.md D8).
      Two conditions on running the probe, both from design.md D8: it blocks
      every #2035 copyback writer on that root for the window it holds the lock,
      so it runs when no promotion is owed and never during a live forecast
      cycle; and it reads per-entry `error` text, which
      `scheduler_evidence_payload._compact_retention` replaces with `*_count`
      scalars under `pre_write_size_pressure` — a compacted receipt is a void
      run of the probe, not a failed one.
      node-22's active checkout is pre-maintenance-window (3.12 venv, no
      `uv sync`, no bare `uv run`), and `packages/common/copyback_guard.py` does
      not exist there yet, so neither half can be produced from this branch and
      EF-17 is not a merge clause. Routed at Phase 8.

## Known limits

- The plan-to-delete window stays open: a tree a writer promotes and commits
  after the pass planned it is still removed by that pass. This is the shape
  issue #2238's title reaches for, and it is **not** what the mutex closes.
  Stated in the spec as an explicit non-guarantee (design.md D4). Routed at
  Phase 8.
- `freed_bytes` remains a planning-time estimate, measured outside the mutex.
- `node27_raw_retention.run_retention` is the second unlocked deleter on the
  same directory tree — measured: node-27's
  `NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store` is what
  node-22 mounts as `/ghdc/data/nwm/object-store`, and its per-cycle `canonical`
  lane is the keyspace the canonical-precip writers promote into. Out of scope
  by the issue's boundary; the spec requirement names it as a known-violating
  implementation rather than narrowing itself to stay true by construction
  (design.md D10). Filed as issue #2252, which leads with the question this
  change could not settle: node-27 is the NFS *server*, so its `flock` is
  local-ext4 and node-22's is client-side, and whether those exclude each other
  has to be measured before that script simply calls the same acquire.
- Retention's copyback acquisition deadline is **not operator-tunable in
  production**. `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` reaches every
  other acquirer but not this one, because the guard reads the environment only
  when the caller passes no explicit timeout and this lane always passes the
  remaining pass budget (design.md D9). Accepted rather than fixed, and the two
  stuck states are kept apart: against a live hung holder no deadline reclaims
  anything; against the finite NFS lease-expiry hold a longer deadline would
  ride it out, and this change does not know that lease, so 300 s may simply be
  shorter than it. What the budget does then is deferred reclamation, not lost
  reclamation.
- Lock semantics are exercised on a local filesystem, never through an NFSv4.2
  client. No available host closes that gap for this branch: node-27 is the
  export's **server**, so `flock` there is local ext4 too (design.md D11), and
  node-22 — the only client — is pre-maintenance-window. The third state
  `copyback_guard.acquire_copyback_batch_lock` documents (correctly owned, no
  local holder, still locked) is therefore not reproducible in the suite. That
  state is where the budget imposes its known **cost**, not a reason it exists;
  the budget itself is tested with an ordinary local holder.
- `packages/common/copyback_guard.py` is named by **no** rule in
  `scripts/select_ci_tests.py`. It is not unrouted — same-name derivation and
  the `packages/**` supplemental routes still select nine suites for it,
  `tests/test_copyback_guard.py` among them — but the retention mutex suite is
  **not** one of them (measured: `select_tests(["packages/common/copyback_guard.py"])`
  returns nine paths and the mutex suite is absent). So a diff touching only the
  guard does not run the deleter-side oracle that depends on it.
  `packages/common` is outside `DIRECTORY_RULE_AUDIT_PATHS`, so the gate does
  not demand a rule. Pre-existing from #2035, found while wiring T9, out of
  scope here. Routed at Phase 8.
- A persistently contended copyback root reclaims **nothing**, quietly. Every
  entry lands in `result.failed` with the lock error, the pass reports
  `completed`, and nothing downstream distinguishes "failed to acquire on every
  tree" from "had nothing to delete": `failed[]` drives no metric and no alert,
  and `scheduler_evidence_payload._compact_retention` replaces the list with a
  `failed_count` scalar under size pressure, erasing the error text entirely.
  The disk fills at retention's normal rate while the receipt looks healthy.
  Pre-existing shape, newly reachable through the mutex. Routed at Phase 8.
- The retention **lane** can contribute two waiters, not one. `run_retention`
  has two production entry points — the scheduler pass and the operator
  `cleanup` CLI, which takes no scheduler lease and no cross-process guard of
  its own — so an operator cleanup overlapping a scheduled pass queues two
  retention waiters on this lock, each carrying its own independent 300 s
  budget. **Two is an assumption, not a ceiling**: nothing serialises two
  concurrent operator cleanups, so every further one adds another waiter. The
  ~24-queued-acquisition figure the guard's derivation rests on is therefore
  conditional on at most one concurrent cleanup, and nothing enforces that.
  Routed at Phase 8.
- The budget bounds waiting, never holding. Only acquisition elapsed is charged,
  so an uncontended pass takes an unbounded number of holds (the removal loop
  has no cap) while charging ~0, and no single hold is bounded either —
  `safe_fs.remove_tree_allow_symlinks` takes no deadline. What protects a writer
  is the measured shape (one waiter per pass, short `rmtree` holds), which is
  scale-dependent rather than structural: a retention pass growing well past
  today's 48-54 trees is a real way for a writer to exhaust its own 900 s
  deadline. Not fixed here: bounding a hold means giving
  `remove_tree_allow_symlinks` a deadline, which is a change to shared
  filesystem-safety code and to every one of its callers — out of this issue's
  boundary. Routed at Phase 8.
- `copyback_guard.resolve_copyback_lock_timeout_seconds` validates its
  **explicit-argument** branch less than its environment branch: it rejects only
  `value <= 0` there, while the env branch also rejects `NaN` and `inf`.
  Measured with a watchdog: `acquire_copyback_batch_lock(root,
  timeout_seconds=float("inf"))` and `...=float("nan")` against a held lock both
  ran past 8 s and had to be killed, contradicting the guard's own "never a
  hang" comment. Pre-existing and behaviourally untouched here; this change is
  the first production caller on that branch but passes only the `300.0` module
  constant, and `copyback_lock_wait_budget_seconds` is a test seam. Out of scope
  by the issue's boundary. Routed at Phase 8.
- A diff touching only `services/orchestrator/scheduler_runtime.py` does not
  select this suite: `select_ci_tests` returns 22 files for it, none of them a
  retention partition, so EF-15's scheduler-side leg is not routed to the PR
  that could break it. The pins in `tests/test_select_ci_tests.py` claim only
  the `retention.py`, `cli.py`, `__init__.py` and
  `tests/retention_test_helpers.py` legs, so nothing is over-claimed — and
  `tests/test_retention_extra_roots.py` has the same gap against the
  `runs_only_roots=` wiring it guards, which makes this the retention corpus's
  existing shape rather than a regression this change introduces. The
  full-suite master run catches it post-merge. Routed at Phase 8.
- Issue #2238's sixth acceptance criterion (correct #2035's "Unchanged
  downstream consumers" wording for `retention.py`) is **already satisfied at
  base `6fdb2015`** by that change's own post-ceiling sweep. This change neither
  repeats nor claims it; see design.md D7.
