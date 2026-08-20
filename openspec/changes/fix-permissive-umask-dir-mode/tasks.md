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
- Shared helper root: packages/common/safe_fs.py, 119 production call sites
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

- [ ] 1.1 `packages/common/safe_fs.py:68` — `os.mkdir(part, 0o755, dir_fd=fd)`.
      No `fchmod`, no new `mode` parameter, no chmod of existing directories
      (design D1, D2).
- [ ] 1.2 Add a shared test helper that creates a directory tree with an explicit
      mode on every component it creates, mirroring
      `state_manager._ensure_copyback_state_parent`. Place it where the CI
      selector still routes to real suites — **not** in `tests/conftest.py` or
      `tests/integration_helpers.py` (design D5).
- [ ] 1.3 Route `_scheduler_env_roots` in `tests/test_production_scheduler.py`
      through the helper.
- [ ] 1.4 Route `_set_db_free_scheduler_env` (the `db-free` / object-index
      directories and the `NHMS_SCHEDULER_JOURNAL_ROOT` mkdir) through the helper.
- [ ] 1.5 Re-run `(umask 002; uv run pytest -q --tb=no -rf tests/test_production_scheduler.py)`,
      and route each still-failing site's directory creation through the helper.
      Repeat until zero failures. Do **not** blanket-rewrite the ~1398 mode-less
      `mkdir` calls in `tests/` (design D4).
- [ ] 1.6 Apply the same treatment to any other suite the full-tree umask-`002`
      run implicates, using the same empirical loop.
- [ ] 1.7 Record the ACL boundary (design D7) in source, so a future reader does
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
to `tests/test_safe_fs.py` (`scripts/select_ci_tests.py:187`, `:339-346`), and it
does **not** route to `tests/test_scheduler_file_provider_refresh.py`. Put the
safe_fs-mode tests in `tests/test_safe_fs.py` so this PR's diff selects them.
There is no `tests/test_provider_atomic.py`; provider_atomic coverage lives in
`tests/test_scheduler_file_provider_refresh.py`.

- [ ] 2.1 `tests/test_safe_fs.py`: safe_fs creates a directory under
      `os.umask(0o002)` -> landed mode is `0o755` and `S_IMODE & 0o022 == 0`.
- [ ] 2.2 `tests/test_safe_fs.py`: safe_fs creates a directory under
      `os.umask(0o077)` -> landed mode is `0o700`. This is **new** coverage, not
      a pre-existing guard: no test in the repository currently pins a directory
      mode under `0o077` (design D2), so the `fchmod` variant would have widened
      these silently.
- [ ] 2.3 `tests/test_safe_fs.py`: a provider lock whose parent was created by
      safe_fs under `os.umask(0o002)` is acquired successfully — the
      permissive-side twin of
      `test_provider_atomic_publishes_shared_mode_under_private_umask`.
- [ ] 2.4 Extend the existing
      `tests/test_scheduler_file_provider_refresh.py:832-847`
      (`test_provider_lock_rejects_writable_parent_and_preserves_body_errors`),
      which already pins `0o777` parent -> refused and `0o755` parent -> acquired,
      with the `0o775` case. Do **not** add a third near-twin test.
- [ ] 2.5 `tests/test_safe_fs.py`: `ensure_directory_no_follow` on an existing
      `0o775` directory leaves its mode unchanged.
- [ ] 2.6 Red-proof for 2.1-2.4 against pre-change source, batched.

## 3. Verification matrix

| Surface | Command | Expected |
|---|---|---|
| Permissive-side regression (the bug) | `(umask 002; uv run pytest -q tests/test_production_scheduler.py)` | 0 failed (from 76) |
| Normal-side non-regression | `uv run pytest -q tests/test_production_scheduler.py` | 0 failed |
| Strict-side non-regression | `uv run pytest -q tests/test_scheduler_file_provider_refresh.py tests/test_run_tree_copyback.py` | 0 failed, incl. the `0o077` and `0o775`/`0o664` assertions |
| Shared-helper consumers | `uv run pytest -q tests/test_safe_fs.py tests/test_state_manager.py` | 0 failed |
| Full-tree blast radius | `(umask 002; uv run pytest -q -m "not e2e and not grib and not integration" tests/)` | 0 failed, compared against a clean pre-fix run of the same command. Accepted limit: the marker exclusion drops exactly the suites most likely to touch real shared roots, so this row bounds the *unit* blast radius only. |
| Lint | `uv run ruff check .` | clean |
| Spec | `openspec validate fix-permissive-umask-dir-mode --strict --no-interactive` | strict-valid |

## 4. Evidence Floor

- [ ] Baseline recorded: master `29892932`, `(umask 002)` -> `76 failed, 1588 passed`.
- [ ] `(a)`-alone measured on the same baseline -> `74 failed, 1590 passed`,
      saving exactly `test_file_canonical_readiness_provider_infers_root_from_scheduler_index_path`
      and `test_nfs_raw_ready_candidate_stages_raw_before_convert_submit`, and
      breaking nothing. This is the evidence for issue #1513's acceptance
      criterion 3 ("只交付 (a) 的 PR 不得视为完成").
- [ ] Post-fix: same command -> 0 failed, with the full `-rf` output captured.
- [ ] Post-fix: default-umask run of the same file -> 0 failed (no normal-side regression).
- [ ] Post-fix: strict-side files green, specifically
      `tests/test_run_tree_copyback.py:301-302` (`0o664` / `0o775`) and
      `tests/test_scheduler_file_provider_refresh.py:823`/`:920`.
- [ ] Post-fix: full-tree umask-`002` run, compared against the pre-fix full-tree
      run captured on master.
- [ ] `uv run ruff check .` clean.
- [ ] `openspec validate --strict --no-interactive` passes.
- [ ] ACL enumeration recorded (design D7): the three `default:user:nwm:rwx`
      subtrees, their directory creators, and the `find`-verified zero
      `nwm`-owned entries that make `forcing/`'s exposure inert today.
- [ ] Follow-up issue filed for the latent `provider_atomic`-gate-vs-ACL
      incompatibility and for `forcing/`/`runs/`'s clamped grant.
- [ ] Cross-uid boundary recorded rather than tested: no pre-merge command
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
