# Tasks: retention-workspace-slurm-log-lane (#3142)

## Risk packs

- [x] File IO / path safety — selected: a new delete lane on a production root. → tests 2.5, 2.6.
- [x] Public API / CLI / script entry — selected: a new keyword, forwarded by the cleanup CLI and by the pass. → tests 2.8, 2.9.
- [x] Schema / field names — selected: new reasons `slurm_log_cycle_aged_out` and `slurm_log_recently_modified`; the
  schema version and fields are unchanged. → tests 2.2, 2.4b (reason, root and key asserted).
- [x] Concurrency / shared state — selected: in-flight jobs keep their manifests and logs. → tests 2.4, 2.4b, plus
  the consumer audit (task 1.4).
- [x] Resource limits — selected: the root holds thousands of entries. Selection enumerates direct children only.
  The tree walk runs only for aged entries below the frontier, and frontier-exempt entries are never walked. → test
  2.4 asserts no `size_bytes`.
- [x] Legacy compatibility — selected: parity with `slurm_log_root=None` or the gate closed; all existing
  `tests/test_retention*.py` stay green unmodified. → test 2.7.
- [x] Error handling / partial outputs — selected: a failed removal is isolated. → test 2.10.
- [x] Documentation — selected. → task 3.1.
- [x] Slurm production lifecycle / mock-vs-real parity (domain) — selected: the lane deletes directories that the
  real backend writes and reads, which the mock never creates. → task 1.4 consumer audit, tests 2.4 and 2.4b, and
  the node-22 receipt (task 4.4).
- [ ] Run manifest / QC provenance (domain) — not selected: run manifests and provenance live in the journal and
  `runs/`/object store, not in these log directories. `_ParentArrayLogCache` reads logs only for jobs still being
  provenanced, and those are frontier- or recency-protected (audit task 1.4).
- [ ] Published NHMS artifacts (domain) — not selected: published products live under the published root and
  `tiles/`, never under W.
- [ ] Config / project setup — not selected: no new env var.
- [ ] Auth / permissions / secrets — not selected.
- [ ] Release / packaging — not selected.

## 1. Implementation

- [x] 1.1 `retention.py`:
  - add the `slurm_log_root` keyword to `plan_retention` and `run_retention`;
  - add the root-level collector, with name shapes (a) and (b) (`gfs|ifs|era5` closed set) and the four-step
    adjudication from the design, including the recency guard;
  - apply the admission and alias rules: W must be an admitted additional root, not equal to the copyback root, and
    not equal to the primary root.
- [x] 1.2 `retention.py` overlap: on W, `parts[0]` matching (a) or (b) is a potential target, using the same closed
  set. Purely lexical.
- [x] 1.3 The scheduler pass call and `cli.py::_run_cleanup` pass `slurm_log_root=os.getenv("WORKSPACE_ROOT")`.
- [x] 1.4 Consumer audit. For each post-hoc consumer named in the design, write one line: what it reads and what it
  does on ENOENT for a terminal job below the frontier. Put the lines in the PR body.

## 2. Tests (red before, green after)

The lane tests go in `tests/test_retention_slurm_log_lane.py` unless a test below names another file. They use a
temporary workspace root W admitted as an additional root, with the gate open.

- [x] 2.1 Layout helper.
- [x] 2.2 These are planned as `slurm_log_cycle_aged_out` with `root=W` and `key=<name>`, and removed when not
  dry-run, for an aged cycle below the frontier with all mtimes older than the cutoff:
  - `cycle_gfs_2026010100_convert_cohort_ab12/logs/1.out`
  - `fcst_ifs_2026010100_m1/logs/2.err`
  - `gfs_2026010100/manifests/forcing_index_x.json`
  - `era5_2026010100/array_logs/x/3_1.out`
- [x] 2.3 An in-window `gfs_<cycle>` is skipped as `within_retention_window`.
- [x] 2.4 An aged cycle at or above `active_lower_bound` is skipped as `pipeline_frontier_exempt`, has no
  `size_bytes`, and still exists.
- [x] 2.4b An aged cycle below the frontier whose descendant `logs/9.out` has an mtime at or after the cutoff is
  skipped as `slurm_log_recently_modified`, has no `size_bytes`, and still exists. One case each for
  `analysis_era5_2026010100_2026010200_m/` and `ifs_2026010100/manifests/...`.
- [x] 2.5 These names are absent from the plan and still exist (all aged):
  - `scheduler`, `slurm-logs`, `repairs`, `runs`, `onboarding-19-20260921`, `GFS_2026010100`, `gfs_2026010100x`,
    `gfs_2026013200`
  - `reset_2026070115`, `backup_2026010100`, `manual_reconvert_gfs_2026062700`,
    `manual_gfs_2026062700_recanonical_20260629T172037Z`, `cmfd_2026010100`
- [x] 2.6 A symlink `ifs_2026010100 -> <outside dir>` is not followed and its target survives. A regular file named
  `gfs_2026010100` is untouched. W's `runs/` being a symlink still skips the `runs/` lane as before, while the Slurm
  log lane on W still runs.
- [x] 2.7 Parity and isolation:
  - (i) the gate closed, or `slurm_log_root=None`, gives a plan equal key for key to the plan without the keyword;
  - (ii) discriminating case: gate open, `slurm_log_root=W`, with copyback C≠W and primary P both admitted and
    lane-shaped aged dirs at the root level of W, C and P. Targets appear only under W; C's and P's lane-shaped dirs
    still exist and are absent from the plan;
  - (iii) W equal to C after resolution gives no lane;
  - (iv) a relative or blank `slurm_log_root`, or one not among the admitted roots, gives no lane.
- [x] 2.8 `tests/test_cli_cleanup_frontier.py`: the cleanup dry-run with `WORKSPACE_ROOT=W` and extra roots enabled
  lists the lane targets with `root=W`.
- [x] 2.9 `tests/test_retention_extra_roots.py` (next to the existing pass-forwarding test): the scheduler pass
  forwards `slurm_log_root` from `WORKSPACE_ROOT`.
- [x] 2.10 An injected removal failure on one lane target is recorded in `failed`, and the other lane targets are
  still removed.
- [x] 2.11 A root nested under `W/gfs_2026010100/` is rejected as `root_overlap` with `conflicting_root=W`.
  `W/cmfd_2026010100/` is not a conflict.

## 3. Docs

- [x] 3.1 Update `docs/runbooks/two-node-deployment-overview.md` (retention paragraph) and the comment above
  `NHMS_RETENTION_EXTRA_ROOTS_*` in `infra/env/compute.scheduler-dbfree.env.example`. The workspace root also
  reclaims root-level Slurm log directories on the additional-root window. The recency guard and the frontier keep
  in-flight trees.

## 4. Verification / Evidence Floor

- [ ] 4.1 `uv run ruff check .`
- [ ] 4.2 `uv run pytest -q tests/test_retention*.py tests/test_cli_cleanup_frontier.py tests/test_production_scheduler.py tests/test_select_ci_tests.py`
  plus the selector-chosen set. Run it locally and on node-27 in a temporary worktree.
- [ ] 4.3 `openspec validate retention-workspace-slurm-log-lane --strict --no-interactive`
- [ ] 4.4 node-22 post-merge receipt, in this order:
  - (1) ff deploy at a pass boundary;
  - (2) before the first post-deploy pass deletes anything, run an out-of-pass dry-run with the timer stopped.
    Invocation: `set -a; . /scratch/frd_muziyao/NWM/infra/env/compute.scheduler-dbfree.env; set +a;`
    `/scratch/frd_muziyao/NWM/.venv/bin/python -I -m services.orchestrator.cli cleanup` (never `uv run`). Expected:
    every planned entry with `root=/scratch/frd_muziyao/nhms-prod/workspace` and reason `slurm_log_cycle_aged_out`
    has a key matching shape (a) or (b), and no key is an operational name;
  - (3) count. The backlog older than 14 days was purged on 2026-11-15, so the expected count is the number of lane
    directories older than 14 days that have been created since then, possibly zero. When it is empty, prove it by
    listing the oldest lane directory's cycle and mtime against the cutoff;
  - (4) restore the timer. In the next deleting pass's receipt, the lane entries show `failed=0`, and the listed
    directories are gone.
