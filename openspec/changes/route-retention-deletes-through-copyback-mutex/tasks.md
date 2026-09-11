# Tasks

Fixture level: **expanded**. Issue #2238 carries no `Suggested fixture level`
(it was filed by `issue-scribe` from a cross-review finding, not through the
pipeline's Stage 5 contract), so the level is triaged here and the divergence
recorded: the issue's own "预估规模: S" describes the diff, not the risk. The
subject is a destructive `rmtree` lane on a production NFS export shared by two
hosts, inside `services/orchestrator` — an expanded trigger in
`openspec/project-profile.md`. Repair intensity: **expanded**.

Reference frame for every `path:line` in this change's documents — **one frame,
with symbol anchors**. Every citation names a line in this branch's final tree,
whether or not this change added that line, and carries the symbol it points
into: `copyback_guard.resolve_copyback_lock_timeout_seconds:124-155`, not a bare
`copyback_guard.resolve_copyback_lock_timeout_seconds:124-155`. Historical measurements stay plain numbers, never
citations.

This replaces a two-frame rule (pre-existing code at base `6fdb2015`, added code
in the branch tree) that rounds 2, 3 and 4 each shipped broken citations under.
The rule was unworkable for the files this change itself edits: the comment
block added to `packages/common/copyback_guard.py` sits *above* most of what the
documents cite there, so every base line below it drifts by a fixed amount that
is invisible to a reader, and the same function ended up cited under the base
frame in one section and the branch frame in another. A reader could not tell
from a citation which frame it used. The symbol anchor is what makes the
remaining drift recoverable: if a later edit moves a line, the name still finds
it, and a citation that never named the right symbol — round 4 found two — is
exposed when the anchor is written rather than three rounds later.

Both halves are mechanically checked, not read: `.workplans/pr-2245/review/check_citations.py`
parses every anchor in these four documents and fails if the named symbol does
not enclose the cited lines, or if a bare `path:line` appears at all. It found
one wrong range in the very edit that introduced this note. A second scratch
pass flagged sentences asserting an absolute with nothing in the same sentence
that settles it; its precision is low — it merges bullets and reads ordinary
prose uses of "every" as claims — so its output was triaged by hand rather than
applied, and three sentences were changed as a result. That triage is a
judgement call and is recorded as one.

## Risk pack selection

| Pack | Status | Reason |
|---|---|---|
| concurrency/idempotency | **selected** | The whole change is joining a cross-process mutex. |
| publish/delete/rollback | **selected** | The protected party is another writer's rollback material. |
| file/path IO safety | **selected** | The guarded call is a no-follow `rmtree` under a shared root. |
| production config | **selected** | Which root gets locked is decided by two env-fed call sites; node-22's live config is the reason this is not latent. |
| evidence chain | **selected** | A lock failure must land in the pass receipt's `failed[]`, not collapse the receipt. |
| shared helper behavior | **selected** | Reversed after the fixture review. `copyback_guard.module:54` names itself the single in-code home of the acquisition-count claim behind the 900 s budget and derives it from "at most once per cycle per scheduler pass". This change adds a per-tree acquirer, so that claim stops being true and the comment is updated (T6). The helper's behaviour is unchanged; its documented invariant is not. |
| permissions/auth boundary | **selected** | Reversed after the fixture review. This change is the first to put a lane **whose purpose is deletion** under `copyback_guard._require_lock_identity:213-221`'s root-owner uid fail-closed check — the copyback writers already remove backup trees inside the mutex, but as a step of a promotion, not as the lane's product; a mismatch would turn every copyback-root removal into a `failed[]` entry and silently stop reclaiming that root. Measured clear on node-22 (`design.md` D1) rather than assumed. |
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
      parameter cannot widen the deletion surface. Default `None` leaves the
      pass byte-identical to its pre-#2238 behaviour for any caller that does
      not pass it — every existing test, and every caller except the two this
      change updates in T5.
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
- [x] T6 Update the budget comment at `copyback_guard.module:54`
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
      What each buys is stated rather than implied: the fracture pin is the one
      that can red, because it asserts the partition is reached *only* through
      the owner rule. Extending the floor row catches nothing the broad rule's
      equality literal does not already catch — it keeps the corpus inventory
      honest, so the next reader of that row is not told there are four
      retention partitions when there are five.
## Evidence Floor

Each clause is a machine-checkable statement about the final branch tree. EF-1
through EF-16 are local and mandatory; EF-17 is post-merge ops and is a recorded
known limit, not a merge blocker. Contended cases drive the guard's own
per-acquisition timeout override down to sub-second so the suite stays fast.

**Every clause below was mutation-checked, not read for the presence of an
assertion.** Round 3 found two clauses (EF-11's total-wait bound, and T3's
"release in a `finally`") that a reader would have called covered and that no
mutation of `services/orchestrator/retention.py` could red; the Review Failure
Retro's corrective action was to stop trusting inspection. What backs the floor
is an archived, reconstructable sweep — `.workplans/pr-2245/review/round-4-mutation-sweep.md`
— and not this sentence: 18 mutants, built independently by the round-4
test-evidence seat, each deleting or inverting the behaviour a clause names,
each redding the clause that names it, plus 42 suite runs under 28-way CPU
oversubscription with no flake. An earlier 22-mutant sweep is recorded there too
and is explicitly marked unverifiable, because its enumeration was not kept.
Two caveats, recorded rather than smoothed over:

- EF-5 and EF-6's overlap leg survive a *single* mutation of
  `_resolve_copyback_lock_root`'s membership check, because `_delete_entry`'s
  `containment_root` nesting makes that check a redundant second line of
  defence: in the primary-identity configuration the entry's root is not in
  `extra_roots`, so the unlocked `shutil.rmtree` arm is taken before the lock
  could matter. A double mutant that also adds a pass-level acquire reds both.
  Defence in depth, not an over-claimed clause.
- EF-14 has no removal mutation at all; see its own entry for why.

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
      Alone among these clauses EF-14 has **no removal mutation**, and that is a
      structural exclusion rather than a coverage gap: `retention.py` never
      references `COPYBACK_BATCH_LOCK_NAME` at all, and the planner enumerates
      only the directory entries under `runs_root = root / RUNS_PREFIX`
      (`retention._collect_run_targets:415-463`), so the root-level lock file is outside the walk
      by construction. There is no line whose deletion admits it. The case is
      kept as a regression guard against a future widening of the enumeration.
- [x] **EF-15 — both call sites name the copyback root.** The scheduler pass
      and the `cleanup` CLI each pass the copyback root into `run_retention`,
      asserted at the call site rather than inferred: this is the single place
      where the whole mutex can be lost in deployment without any test on the
      retention module itself noticing.
- [x] **EF-16 — the targeted lane routes to the new suite.** For each of
      `services/orchestrator/retention.py`, `cli.py` and `__init__.py`, a test
      reds if `select_tests` stops returning
      `tests/test_retention_copyback_mutex.py` for that module. Asserted as
      routing, not as "the gate is green": the gate can be satisfied by an
      exclusion token — and two of its token classes, `runtime-budget` and
      `fn-gated`, have no machine check at all
      (`test_select_ci_tests._disposition_offenders:9014-9056` checks only an
      invalid token, an underived gap, a stale exclusion, an orphan
      `edge-consumer` and a dead `redirect`) — which would leave the oracle
      unrouted while every test stayed green.
      The three legs are not asserted the same way, and the difference is
      recorded rather than glossed:
      `retention.py` has a direct assertion on a changed-file list of exactly
      that module, plus the per-partition fracture pin; `cli.py` has a direct
      at-site assertion added in round 1, proven to red when the at-site target
      is removed; `__init__.py` has no assertion naming it, and rides the broad
      `services/orchestrator/**` rule's frozen exact-output literal instead.
      That is accepted as equivalent for this leg specifically because the
      literal is an equality pin **on the selector's own output**, and
      `select_ci_tests` never reads the disposition/exclusion table at all:
      measured, deleting the suite from the broad rule's member list reds that
      assertion, and no exclusion token can mask it. It would not be accepted
      for `cli.py`, whose route is a stop rule the broad literal never
      exercises.
      The counterfactual behind this clause was run rather than argued: removing
      the route entirely reds **five** tests, not one, and all five are the
      disposition-audit family
      (`test_directory_rule_importer_gaps_are_dispositioned` and its three
      guard-of-the-guard tests, plus
      `test_mapping_builder_joins_the_directory_audit_without_new_gaps`). A
      single `runtime-budget` token restores all five to green with the route
      still gone — which is precisely why EF-16 asserts routing directly instead
      of resting on a green gate.
- [ ] **EF-17 — node-22 deployment receipt (post-merge, known limit).** Two
      halves, because the first half alone proves nothing about this change:
      1. *Non-regression, labelled as such.* After master is deployed to
         `/scratch/frd_muziyao/NWM`, one scheduler pass whose `deleted[]`
         contains `/ghdc/data/nwm/object-store` entries, with
         `retention.status=completed` and the copyback-root count not regressed
         to zero. A build with the mutex removed satisfies all three criteria
         byte for byte — the mutex-less build running on node-22 today already
         does — so this half is a regression guard, not evidence of acquisition.
      2. *The discriminating probe.* Hold the copyback root's batch lock from a
         second process across one pass, and require that pass's copyback-root
         entries to appear in `failed[]` carrying the lock error while the
         workspace-root and primary-root entries still appear in `deleted[]`. A
         mutex-less build deletes straight through a held lock, so this is the
         observation that separates the two builds. The lock file's mere
         presence does **not** substitute: the same deploy brings #2035's
         writers, which acquire the identical file on the identical root earlier
         in the pass (`design.md` D8).
      Two conditions on running the probe, both from `design.md` D8. It blocks
      every #2035 copyback writer on that root for the window it holds the lock,
      not only retention, so it is run when no promotion is owed and never
      during a live forecast cycle. And it reads per-entry `error` text, which
      `scheduler_evidence_payload._compact_retention` (`:767-802`) replaces with
      `*_count` scalars under `pre_write_size_pressure` — a compacted receipt is
      a void run of the probe, not a failed one.
      node-22's active checkout is pre-maintenance-window (3.12 venv, no
      `uv sync`, no bare `uv run`), and `packages/common/copyback_guard.py` does
      not exist there yet, so neither half can be produced from this branch and
      EF-17 is not a merge clause. Routed as a tracked issue at Phase 8.

## Known limits

- The plan-to-delete window stays open: a tree a writer promotes and commits
  after the pass planned it is still removed by that pass. This is the shape
  issue #2238's title reaches for, and it is **not** what the mutex closes.
  Stated in the spec as an explicit non-guarantee (`design.md` D4) and routed as
  a tracked follow-up at Phase 8, not silently absorbed.
- `freed_bytes` remains a planning-time estimate, measured outside the mutex.
- `node27_raw_retention.run_retention:553` is the second unlocked deleter on the
  same directory tree — measured: node-27's
  `NODE27_RAW_RETENTION_OBJECT_STORE_ROOT=/home/ghdc/nwm/object-store` is what
  node-22 mounts as `/ghdc/data/nwm/object-store`, and its per-cycle `canonical`
  lane is the keyspace the canonical-precip writers promote into. Out
  of scope by the issue's boundary; the spec requirement names it as a
  known-violating implementation rather than narrowing itself to stay true by
  construction (`design.md` D10). **Filed as issue #2252**, which leads with the
  question this change could not settle: node-27 is the NFS *server*, so its
  `flock` is local-ext4 and node-22's is client-side, and whether those exclude
  each other has to be measured before that script simply calls the same
  acquire.
- Retention's copyback acquisition deadline is **not operator-tunable in
  production**. `NHMS_OBJECT_STORE_COPYBACK_LOCK_TIMEOUT_SECONDS` reaches every
  other acquirer but not this one, because the guard reads the environment only
  when the caller passes no explicit timeout and this lane always passes the
  remaining pass budget (`design.md` D9). Accepted rather than fixed, and the
  two stuck states are kept apart: against a live hung holder no deadline
  reclaims anything; against the finite NFS lease-expiry hold a longer deadline
  would ride it out, and this change does not know that lease — the export's
  server-side setting was not read — so 300 s may simply be shorter than it.
  What the budget does then is deferred reclamation, not lost reclamation: the
  blocked trees land in `failed[]` and the next pass retries. Recorded here so
  the next person to reach for that variable learns it from the fixture rather
  than from a pass that ignores them.
- Lock semantics are exercised on a local filesystem, never through an NFSv4.2
  client. No available host closes that gap for this branch: node-27 is the
  export's **server**, so `flock` there is local ext4 too (`design.md` D11), and
  node-22 — the only client — is pre-maintenance-window. The third state
  `copyback_guard.acquire_copyback_batch_lock:244-251` documents (correctly owned, no local holder, still
  locked) is therefore not reproducible in the suite. That state is where the
  budget imposes its known **cost** — 300 s may be shorter than the lease, and
  the pass then defers rather than loses the reclamation — not a reason it
  exists; the budget itself is tested with an ordinary local holder.
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
- A persistently contended copyback root reclaims **nothing**, quietly. Every
  entry lands in `result.failed` with the lock error, the pass reports
  `completed`, and nothing downstream distinguishes "failed to acquire on every
  tree" from "had nothing to delete": `failed[]` drives no metric and no alert,
  and `scheduler_evidence_payload._compact_retention` (`:767-802`) replaces the
  list with a `failed_count` scalar under size pressure, erasing the error text
  entirely. The disk fills at retention's normal rate while the receipt looks
  healthy. Pre-existing shape (`failed[]` has never had an operator signal),
  newly reachable through the mutex; routed as a tracked issue at Phase 8, with
  the rest of this change's deferred findings.
- The retention **lane** can contribute two waiters, not one. `run_retention`
  has two production entry points — the scheduler pass and the operator
  `cleanup` CLI, which takes no scheduler lease and no cross-process guard of
  its own — so an operator cleanup overlapping a scheduled pass queues two
  retention waiters on this lock. Each carries its own independent 300 s budget.
  **Two is an assumption, not a ceiling** — that is what the guard's budget
  comment says, and this bullet used to contradict it: nothing serialises two
  concurrent operator cleanups, so every further one adds another waiter. The
  ~24-queued-acquisition figure the guard's derivation rests on is therefore
  conditional on at most one concurrent cleanup, and nothing enforces that.
  Filed as a tracked issue at Phase 8 with the other residuals — the earlier
  decision not to file rested on the ceiling reading, which no longer stands.
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
  boundary. Routed as a tracked issue at Phase 8.
- `resolve_copyback_lock_timeout_seconds` validates its **explicit-argument**
  branch less than its environment branch: `copyback_guard.resolve_copyback_lock_timeout_seconds:124-155` rejects
  only `value <= 0`, while the env branch at `:150` also rejects `NaN` and
  `inf`. Measured in round 3 with a watchdog: `acquire_copyback_batch_lock(root,
  timeout_seconds=float("inf"))` and `...=float("nan")` against a held lock both
  ran past 8 s and had to be killed, contradicting the guard's own "never a
  hang" docstring at `:200-203`. Pre-existing and behaviourally untouched here;
  this change is the first production caller on that branch but passes only the
  `300.0` module constant, and `copyback_lock_wait_budget_seconds` is a test
  seam. Out of scope by the issue's boundary; routed as a tracked issue at
  Phase 8.
- A diff touching only `services/orchestrator/scheduler_runtime.py` does not
  select this suite: `select_ci_tests` returns 22 files for it, none of them a
  retention partition (measured round 3), so EF-15's scheduler-side leg
  (`tests/test_retention_copyback_mutex.py`, the scheduler call-site case) is
  not routed to the PR that could break it. The pins in
  `tests/test_select_ci_tests.py` claim only the `retention.py`, `cli.py`,
  `__init__.py` and `tests/retention_test_helpers.py` legs, so nothing is
  over-claimed — and `tests/test_retention_extra_roots.py` has the same gap
  against the `runs_only_roots=` wiring it guards, which makes this the retention
  corpus's existing shape rather than a regression this change introduces. The
  full-suite master run catches it post-merge. Routed at Phase 8.
- Issue #2238's sixth acceptance criterion (correct #2035's "Unchanged
  downstream consumers" wording for `retention.py`) is **already satisfied at
  base `6fdb2015`** by that change's own post-ceiling sweep
  (`harden-copyback-batch-mutex-and-dir-traversal/design.md:535-546`). This
  change neither repeats nor claims it; see `design.md` D7.
