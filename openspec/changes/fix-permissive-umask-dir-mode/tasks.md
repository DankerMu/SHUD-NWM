# Tasks

## Risk triage

```text
Issue type: bugfix
Project profile: NHMS
Blast radius: high
Fixture level: expanded
Upstream suggested level: absent (hand-written issue #1513)
Repair intensity: high
Why:
- Shared helper root: packages/common/safe_fs.py, 122 production call sites (measured at HEAD; 119 at the 29892932 baseline, before the origin/master f087f08d merge)
- Mandatory expanded triggers hit: permission, path, temp, file output
- The consumer is a fail-closed security gate (provider_atomic.py:209)
- Directly governs the project's designated backend pytest oracle (node-27)
Selected risk packs:
- File IO / path safety / permission determinism  [selected]
- Shared helper contract + downstream consumers   [selected]
- Test-oracle integrity / coverage shape          [selected]
- Cross-host environment dependence (umask)       [selected]
- Geospatial / CRS / basin geometry               [not selected: no geometry touched]
- Hydro-met time series / forcing windows         [not selected: no time-series path]
- SHUD numerical runtime                          [not selected: no solver path]
- PostGIS / TimescaleDB behavior                  [not selected: db-free path by construction]
- Slurm production lifecycle                      [not selected: no sbatch/gateway change]
- External providers / snapshot reproducibility   [not selected: no provider fetch]
- Run manifest / QC provenance                    [not selected: no manifest schema change]
- Published artifacts / display identity          [not selected: no publish surface]
OpenSpec change: fix-permissive-umask-dir-mode (generated)
```

## 1. Implementation

- [x] 1.1 `packages/common/safe_fs.py:68` — `os.mkdir(part, 0o755, dir_fd=fd)`.
      No `fchmod`, no new `mode` parameter, no chmod of existing directories
      (design D1, D2).
- [x] 1.2 Add a shared test helper that creates a directory tree with an explicit
      mode on every component it creates, mirroring
      `state_manager._ensure_copyback_state_parent`. Place it where the CI
      selector still routes to real suites — **not** in `tests/conftest.py` or
      `tests/integration_helpers.py` (design D5).
- [x] 1.3 Route `_scheduler_env_roots` in `tests/test_production_scheduler.py`
      through the helper.
- [x] 1.4 Route `_set_db_free_scheduler_env` (the `db-free` / object-index
      directories and the `NHMS_SCHEDULER_JOURNAL_ROOT` mkdir) through the helper.
- [x] 1.5 Re-run `(umask 002; uv run pytest -q --tb=no -rf tests/test_production_scheduler.py)`,
      and route each still-failing site's directory creation through the helper.
      Repeat until zero failures. Do **not** blanket-rewrite the ~1398 mode-less
      `mkdir` calls in `tests/` (design D4).
- [x] 1.6 Apply the same treatment to any other suite implicated at umask `002`.
      **What was actually done** (the `[x]` originally overstated this): no
      full-tree umask-`002` run was performed (see §4's struck-through item);
      instead each of the named suites in §3 was measured individually and
      repaired with the same empirical loop. That first pass missed exactly one
      suite, `tests/test_publish_scheduler_file_registry.py`, which an
      independent grep-based sweep of a further 47 suites / 3786 tests found —
      not the full-tree run. It is now routed (§3) and green at both umasks.
- [x] 1.7 Record the ACL boundary (design D7) in source, so a future reader does
      not undo it by accident:
      - a comment at the `safe_fs` mkdir explaining that the explicit mode clamps
        an inherited POSIX ACL mask, so safe_fs must not be the creator of
        ACL-shared directories;
      - a comment at `services/orchestrator/run_tree_copyback.py:427` noting that
        its bare `Path.mkdir` is **ACL-mask-preserving by construction** on the
        copyback tree — the grant it preserves is currently unused (zero
        `nwm`-owned entries under `runs/`), so do not go looking for a live
        consumer — and that it must not be "fixed" into `safe_fs`.

## 2. Tests

Target files are named because CI selection depends on them: `safe_fs.py` routes
to `tests/test_safe_fs.py` (`scripts/select_ci_tests.py:187`, `:355-366`), and it
does **not** route to `tests/test_scheduler_file_provider_refresh.py`. Put the
safe_fs-mode tests in `tests/test_safe_fs.py` so this PR's diff selects them.
There is no `tests/test_provider_atomic.py`; provider_atomic coverage lives in
`tests/test_scheduler_file_provider_refresh.py`.

- [x] 2.1 `tests/test_safe_fs.py`: safe_fs creates a directory under
      `os.umask(0o002)` -> landed mode is `0o755` and `S_IMODE & 0o022 == 0`.
- [x] 2.2 `tests/test_safe_fs.py`: safe_fs creates a directory under
      `os.umask(0o077)` -> landed mode is `0o700`. This is **new** coverage, not
      a pre-existing guard: no test in the repository currently pins a directory
      mode under `0o077` (design D2), so the `fchmod` variant would have widened
      these silently.
- [x] 2.3 `tests/test_safe_fs.py`: a provider lock whose parent was created by
      safe_fs under `os.umask(0o002)` is acquired successfully — the
      permissive-side twin of
      `test_provider_atomic_publishes_shared_mode_under_private_umask`.
- [x] 2.4 Extend the existing
      `tests/test_scheduler_file_provider_refresh.py:863-877`
      (`test_provider_lock_rejects_writable_parent_and_preserves_body_errors`),
      which already pins `0o777` parent -> refused and `0o755` parent -> acquired,
      with the `0o775` case. Do **not** add a third near-twin test.
- [x] 2.5 `tests/test_safe_fs.py`: `ensure_directory_no_follow` on an existing
      `0o775` directory leaves its mode unchanged.
- [x] 2.6 Red-proof for 2.1-2.4 against pre-change source, batched.

## 3. Verification matrix

Measured blast radius (Phase 0.5 assumed one file; the orchestrator's per-file
measurement found six). All numbers below are on the local macOS host, each file
run alone. `(a)`-only = `safe_fs.py:68` explicit mode with **no** test-side
change.

| File | master `29892932`, umask 002 | `(a)`-only, umask 002 | post-fix, umask 002 | post-fix, umask 022 |
|---|---|---|---|---|
| `tests/test_production_scheduler.py` | 76 failed, 1588 passed | 74 failed, 1590 passed | **1715 passed** | 1715 passed |
| `tests/test_state_manager.py` | 19 failed | 7 failed | **137 passed** | 137 passed |
| `tests/test_run_tree_copyback.py` | 16 failed, 5 passed | 2 failed, 19 passed | **24 passed** | 24 passed |
| `tests/test_source_cycle_raw_manifest.py` | 5 failed | 1 failed | **30 passed** | 30 passed |
| `tests/test_file_orchestration_journal.py` | 4 failed | 4 failed | **343 passed** | 343 passed |
| `tests/test_scheduler_state_index_copyback_replay.py` | 26 errors | 26 passed | **29 passed** | 29 passed |
| `tests/test_scheduler_file_provider_refresh.py` | *hung* (see below) | not measured | **281 passed** | 281 passed |
| `tests/test_safe_fs.py` | n/a (new cases) | n/a | **17 passed** | 17 passed |
| `tests/test_object_store_roots.py` † | — | — | **21 passed** | 21 passed |
| `tests/test_forcing_copyback_backfill.py` † | — | — | **42 passed** | 42 passed |
| `tests/test_tile_publisher.py` † | — | — | **101 passed** | 101 passed |
| `tests/test_select_ci_tests.py` | — | — | **151 passed** | 151 passed |
| `tests/test_publish_scheduler_file_registry.py` ‡ | 17 failed, 26 passed | 4 failed, 39 passed | **43 passed** | 43 passed |

The post-fix columns are measured on the **merged** head (`origin/master`
`f087f08d` merged in), which is why they exceed the pre-fix totals: master's
`#1609/#1610` and `#1547`-family work added cases to five of these files in the
interim. † marks suites that master touched and this change does not, run as
merge-regression controls.

‡ This row was added in fix round 1 and its baseline column is **not** master
`29892932` but master `f087f08d` (the merged-in head), because the suite was
found late — by an independent grep-based sweep, not by the per-file loop that
produced the rows above. Its `(a)`-only column is likewise "this branch's head
before the round-1 test-side edits", which is exactly the `(a)`-only condition
for this file: the PR contained no edit to it. That makes it an unusually clean
**(a)-effectiveness** data point: the `safe_fs.py:68` pin alone took the file
from 17 failed to 4 failed, healing 13 of 17 with zero test-side change. The
remaining 4 needed three `mkdir` sites plus — measured, and the reason a
mkdir-only fix still left one red — the destination seed at `:1720`
(`test_manual_cli_allow_uncovered_bypasses_gate_with_warning`), which is gate 2
(`SHARED_PROVIDER_MODE`), not gate 1. The 17-failed master baseline is the
verifier's measurement, reproduced here rather than re-run.

**Interaction with master `05fcc17d`.** That commit independently hit the same
`safe_fs.py:68` root cause and worked around it by constructing its fixtures
under `os.umask(0o077)` (`private_umask_fixture`), explicitly deferring the rest
to this issue ("剩余 umask-002 敏感面是既有 #1513，只报不修"). D1 does not
invalidate that workaround: the umask may still *restrict* a safe_fs directory,
so those fixtures still land `0o700`. Verified — that suite is green at both
umasks above.

`tests/test_file_orchestration_journal.py` needed **no** edit: it imports
`_set_db_free_scheduler_env` from `tests/test_production_scheduler.py`
(`:39`, `:44`) and calls it at exactly four sites (`:6748`, `:6807`, `:6855`,
`:6915`) — matching its four failures. Task 1.4 healed them transitively.

`tests/test_scheduler_file_provider_refresh.py` has **no** pre-fix number on
purpose: under umask 002 it does not fail, it **hangs** (see §6), so no baseline
count exists to record. Post-fix it runs in 6.7 s.

| Other surface | Command | Result |
|---|---|---|
| Strict-side non-regression | `(umask 077; uv run pytest -q tests/test_safe_fs.py)` | 17 passed — `0o700` preserved, no `fchmod` widening. (An earlier revision said `11`: that was the pre-merge count, before `origin/master f087f08d` added the `#1547` undeterminable-home cases to this same file.) |
| Selector routing guard | `uv run pytest -q tests/test_select_ci_tests.py` | 151 passed |
| Lint | `uv run ruff check .` | clean |
| Spec | `openspec validate fix-permissive-umask-dir-mode --strict --no-interactive` | strict-valid |

**CI is not the oracle for this change.** GitHub runners are umask `0022`, where
every file above is green both before and after. The umask-`0002` column is the
only evidence that the bug is fixed, and it is local + (post-merge) node-27.

## 4. Evidence Floor

- [x] Baseline recorded: master `29892932`, `(umask 002)` -> `76 failed, 1588 passed`.
- [x] `(a)`-alone measured on the same baseline -> `74 failed, 1590 passed`,
      saving exactly `test_file_canonical_readiness_provider_infers_root_from_scheduler_index_path`
      and `test_nfs_raw_ready_candidate_stages_raw_before_convert_submit`, and
      breaking nothing. This is the evidence for issue #1513's acceptance
      criterion 3 ("只交付 (a) 的 PR 不得视为完成").
- [x] Post-fix, umask `002`: all **thirteen** files in the §3 table 0 failed,
      reproduced independently by the orchestrator — twelve after integrating
      the implementer's branch, and `tests/test_publish_scheduler_file_registry.py`
      after fix round 1 added it (`4 failed, 39 passed` -> `43 passed`).
- [x] Post-fix, default umask `022`: the same files, identical counts — no
      normal-side regression.
- [x] Post-fix: strict-side assertions green, specifically
      `tests/test_run_tree_copyback.py:302-303` (`0o664` / `0o775`) and
      `test_provider_atomic_publishes_shared_mode_under_private_umask`.
- [ ] ~~Full-tree umask-`002` run compared against a pre-fix full-tree run.~~
      **Not performed.** Measured at ~3 h serial (no `pytest-xdist` installed),
      and it would have required two such runs plus an implementation freeze
      across both. Substituted: per-file measurement of the six affected suites
      (§3), each with a default-umask control run to separate umask-conditional
      failures from pre-existing ones. Recoverable at any time — master
      `29892932` is immutable, so the baseline is a pure function of
      (SHA, host, umask). **Stated as a limit, not claimed as coverage.**
- [x] `uv run ruff check .` clean.
- [x] `openspec validate --strict --no-interactive` passes.
- [x] ACL enumeration recorded (design D7): the three `default:user:nwm:rwx`
      subtrees, their directory creators, and the `find`-verified zero
      `nwm`-owned entries that make `forcing/`'s exposure inert today.
- [ ] Follow-up issue filed for the latent `provider_atomic`-gate-vs-ACL
      incompatibility and for `forcing/`/`runs/`'s clamped grant. **Phase 8.**
- [x] Cross-uid boundary recorded rather than tested: no pre-merge command
      exercises the two-uid NFS root, because D7 establishes the change is a
      no-op there (empty groups on `raw/`/`models/` children; unused ACL grant on
      `forcing/`/`runs/`/`states/`) and because the shared root cannot be
      exercised from the local dev host. This is an accepted limit, stated so it
      is not mistaken for coverage.
- [ ] **node-27 terminal confirmation (post-merge, one time)**: default shell,
      no umask override, `uv run pytest -q tests/test_production_scheduler.py`
      -> 0 failed. This is issue #1513's acceptance criterion 2 and is stated
      against `master`, so it is scheduled with the merge, not before it.

## 5. Non-goals

- Relaxing the `0o022` gate at `provider_atomic.py:209`.
- Changing node-27's system umask, or pinning `umask 022` in a test wrapper as a
  substitute for the fix.
- An autouse `umask(0o022)` fixture in `tests/conftest.py` — ruled out by the
  user: it would hide the environment precondition and make the permissive side
  permanently untestable, contradicting task 2.1-2.3.
- `scheduler_lease.py:565`'s own lock channel, which does not route through this
  gate.
- The ~1398 mode-less `mkdir` calls in `tests/` that never produce a provider
  lock parent.
- The other `provider_atomic` fail-closed branches
  (`provider_lock_changed`, `provider_preimage_changed`, ...).
- Resolving the structural incompatibility between the `0o022` gate and
  ACL-mask-based cross-uid sharing (design D7). Enumerated as inert today;
  tracked as a follow-up issue.
- A `setfacl`-based unit test for D7: it would exercise kernel/ACL semantics
  rather than repository code, and cannot run on the macOS development host.
  The boundary is pinned by the D7 production enumeration plus task 1.7's source
  comments.

## 6. Report-don't-fix findings (routed at Phase 8, not fixed here)

1. **`test_provider_atomic_readers_observe_only_complete_old_or_new_json` hangs
   instead of failing.** Its writer thread has no `try/finally`, so any
   exception in `atomic_replace_provider_bytes` skips `finished.set()` and the
   main thread's `while not finished.is_set()` loop spins forever. This is why
   the suite was unrunnable rather than merely red under umask 002, and why it
   has no pre-fix baseline count. This change fixes the trigger (the `0o664`
   seed), not the trap; a `finally: finished.set()` would convert any future
   failure into a clean one.
2. **Latent umask-dependent sites left in place because `(a)` already clears
   them**: `_write_current_published_receipt` and 8 of 11
   `index_path.parent.mkdir` sites in `tests/test_state_manager.py` (measured:
   reverting all 11 leaves exactly 3 red). A future assertion added under them
   would redden only on umask-0002 hosts.
   `_write_current_published_receipt`
   (`tests/test_scheduler_file_provider_refresh.py:480`, comment at `:488`)
   now carries a comment
   saying so in place, so the next reader does not "fix" it by accident. The
   mechanism recorded there is the corrected one: the site is green **not**
   because its `exist_ok=True` mkdir is a no-op against `_config`'s pinned
   directories, but because **none of its three callers (`:1692`, `:1735`,
   `:6527`) ever takes a provider lock or publishes over these paths** — the only
   consumers are `capture_scheduler_provider_preimage`
   (`services/orchestrator/scheduler_file_providers.py:1751`) and
   `refresh.validate_current_receipt`
   (`scripts/scheduler_file_provider_refresh.py:1216`), both pure reads. The
   `_config` story cannot explain the `registry_worker_mirror` lane at all:
   `_config` creates `scheduler/registry`, `scheduler/canonical-readiness` and
   `scheduler/state-index` but never `scheduler/worker-registry`, so that lane's
   parent is created **only** in `_write_current_published_receipt`, at `0o775`
   under umask 0002. Measured landed modes there: registry `0o755`/`0o664`,
   mirror `0o775`/`0o664`, readiness `0o755`/`0o664`, state `0o755`/`0o664`.
   Also latent and untouched for the same reason: 5 of the 7
   `canonical.parent.mkdir(parents=True)` sites in
   `tests/test_publish_scheduler_file_registry.py` (post-fix `:193`, `:297`,
   `:343`, `:1634`, `:1830`) — measured green at umask 0002 because those tests
   never reach a provider publish; only the three now routed through the helper
   (post-fix `:1719`, `:1901`, `:2165`) did.
3. **The `large-file-guard` hook cannot be satisfied from a worktree.**
   `.claude/hooks/large-file-guard/large-file-guard.sh:15` reads its config at
   `CLAUDE_PROJECT_DIR` (the main checkout) while collecting staged paths via
   `git -C cwd` (the worktree), with no env disable — so a worktree agent's
   config edit is invisible to the guard that is blocking it.
4. **`scheduler/direct-grid-candidates` is `1777` with no code reference**
   (operator residue, already noted in D7). Untouched: this change never chmods
   an existing directory.
5. **Design D7's `published/` exclusion is literally true but must not be read
   as "unreachable."** `NHMS_PUBLISHED_ARTIFACT_ROOT` has no direct reference
   under `apps/`, which is what D7 asserts. It is nonetheless **transitively**
   reachable from `apps/api/routes/state_snapshots.py` via
   `StateManager.from_env` (`packages/common/state_manager.py:972`, `:2950`), so
   a future reader must not turn "no direct reference" into "the API can never
   create that tree."
6. **`e2e` / `grib` / `integration`-marked suites are unmeasured at umask
   0002.** Every number in §3 comes from an unmarked run; the marked lanes were
   never executed under umask 0002 on either host, so any provider-gate surface
   they pre-create is unmeasured, not known-green.
7. **Two "both lock parents must be private" comments are imprecise on this
   head** — `tests/test_state_manager.py:3696-3697` and
   `tests/test_run_tree_copyback.py:1376-1377`. They present the following
   `chmod(0o700)` as *required*, or `provider_lock_parent_unsafe` fires. After
   D1 that is no longer true for the safe_fs-created case: `0o755` already
   satisfies the gate (`0o755 & 0o022 == 0`), so the chmod is defensive rather
   than load-bearing. At the `test_run_tree_copyback` site it is additionally
   misleading in a second way — those chmods run *after* the publish that would
   already have raised. Reported by fix round 2 and deliberately left: both are
   in master-owned blocks this change has no other reason to touch, and neither
   invites the delete-the-wrapper edit that §6 item 2's rewrite closed.

