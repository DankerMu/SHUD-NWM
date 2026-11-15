# Design: retention-workspace-slurm-log-lane

Change surface:
- `services/orchestrator/retention.py`:
  - a new keyword `slurm_log_root` (default `None`) on `plan_retention` and `run_retention`;
  - a root-level collector for the new lane;
  - overlap classification (`_parts_lie_in_potential_target` / `_find_conflicting_root`) learns the lane.
- Call sites: the pass call in `services/orchestrator/scheduler_runtime.py` (around line 2153) and
  `services/orchestrator/cli.py::_run_cleanup` (around line 203). Both pass `slurm_log_root=os.getenv("WORKSPACE_ROOT")`.
- Docs: `docs/runbooks/two-node-deployment-overview.md` (retention paragraph) and the comment near
  `NHMS_RETENTION_EXTRA_ROOTS_*` in `infra/env/compute.scheduler-dbfree.env.example`.

Must preserve:
- `slurm_log_root=None`, which covers every existing direct caller and test, or a closed gate: the plan is identical
  key for key.
- Unchanged: the `runs/` lane on every root (including its symlinked-`runs/` refusal), primary cycle lanes, protected
  prefixes, and published-root protection.
- Unchanged admission: blank, relative, and unexpandable roots, `root_overlap`, and `conflicting_root`.
- The copyback batch mutex still covers only the copyback root.
- The receipt keeps its schema version and fields. Only one target reason value and one skip reason value are added.
- The additional-root block keeps its attribution through evidence compaction.

Must add/change:
- Lane admission. The lane runs only when all of these hold:
  - the resolved `slurm_log_root` W equals an admitted additional root;
  - W is not equal to the resolved copyback root. If they alias, there is no lane, so the copyback root never gets
    it;
  - W is not equal to the primary root. Equality there already deduplicates W out of the additional roots.
  
  In every other case there is no lane, and no skip entry is added beyond what admission already records. The lane
  runs independently of W's `runs/` state: a missing or symlinked `runs/` affects only the `runs/` lane.
- The collector enumerates W's direct children, with no recursion for selection. Symlinks and non-directories are
  never selected and never followed, the same as `_iter_dirs`.
- Name shapes. A child is in the lane only if:
  - (a) `parse_run_cycle(name)` is not `None` (canonical `fcst_|cycle_|analysis_` run ids, the same looseness already
    accepted for `runs/`, `run_identity.py:11-14`); or
  - (b) `name` fully matches `^(gfs|ifs|era5)_\d{10}$` and the digits parse as `%Y%m%d%H`. This is the closed set
    that `cycle_id_for` can emit: `normalize_source_id` accepts only GFS, IFS and ERA5
    (`packages/common/source_identity.py:19-32`), and the output is lower-cased.
  
  Every other name is outside the lane and is ignored, with no skip entry.
- Adjudication order (the same as `_collect_run_targets`, plus one recency guard):
  1. `cycle >= extra cutoff` → `within_retention_window`.
  2. `cycle >= active_lower_bound` → `_frontier_exempt_entry`, with no size walk.
  3. Walk the tree once to get its size and newest mtime. The newest mtime covers the directory itself and every
     non-symlink descendant, using lstat and never following links. If the newest mtime is at or after the extra
     cutoff → skip with `slurm_log_recently_modified`, without `size_bytes`. Any `OSError` during the walk (an
     unreadable subtree, an entry vanishing mid-walk) is skipped the same way, fail-closed. The walk stops at the
     first recent entry; the verdict is the same as computing the true newest mtime.
  4. Otherwise it becomes a target with reason `slurm_log_cycle_aged_out`, `key=<name>`, `root=W`, and `size_bytes`.
- Why step 3 exists. `_retention_active_lower_bound` (`scheduler_runtime.py:2031-2079`) only covers the forecast
  pass's candidates and discovery window. Three kinds of in-flight jobs sit below it:
  - analysis lanes `analysis_era5_<start>_…` / `era5_<start>`, whose cycle is the start time
    (`chain_analysis.py:40-45`);
  - API `retry_run` (`apps/api/routes/pipeline.py:546`);
  - forced resubmits.

  Their `<cycle_id>/manifests` are runtime inputs of array tasks (`NHMS_MANIFEST_INDEX`, `real_backend.py:1041`),
  and their log files are being written by Slurm. A running job touches its tree within the window. Using the
  additional-root cutoff as the mtime threshold keeps the guard on the same time base as the window.
  Accepted residual risks (review round 1 corrected the earlier "parity with `runs/`" wording: `fetch_logs` reads
  `W/<run_id>/logs` and `W/<cycle_id>/array_logs`, never `W/runs/`, so these exposures are new, though narrow):
  - a non-array retry of an old run still queued in Slurm. Its `<run_id>/logs` tree only has old mtimes, so a pass
    during the queue wait can delete it. On node-22 (Slurm 23.11.4) Slurm creates the `--output` directory when the
    job starts, so the job does not fail; only earlier attempts' logs in that directory are lost;
  - a walk-to-delete TOCTOU within one pass: a resubmit of an aged, below-frontier cycle that writes a new manifest
    index in that seconds-to-minutes gap. Locking W stays a non-goal;
  - `persist_gateway_logs` (`chain_workspace.py:152-169`) for a terminal job whose logs were never persisted and
    whose tree was reclaimed: it records an empty log and never re-fetches. Reachable only for orphaned jobs resumed
    after more than the additional-root window; tracked as a follow-up issue.
- Deletion goes through the same `_delete_entry` path as every other target on W. A failure is recorded and never
  aborts.
- Overlap. On W only, `parts[0]` matching (a) or (b) is a potential target. The same closed set is used for (b).
  A root nested under such a directory is rejected as `root_overlap`, with `conflicting_root=W`.

Governing invariant: a direct child of the scheduler workspace root is deleted only when all of these hold: its name
is a canonical run id or a `gfs|ifs|era5` cycle id; its cycle is outside the additional-root window and below the
frontier; and no file in it was modified within the window. Nothing else under any root changes.

Sibling surfaces:
- Producers (paths that create the deleted trees):
  - sbatch `--output/--error={{workspace_dir}}/{{run_id}}/logs/...` and `{{array_log_dir}}`, in
    `infra/sbatch/*.sbatch:11-14`;
  - `write_manifest_index` (`real_backend.py:685`);
  - `_ensure_array_log_directory` (`real_backend.py:375-376`).
- Runtime consumers of in-flight jobs:
  - array tasks read `<cycle_id>/manifests/<stage>_index_*.json` through `NHMS_MANIFEST_INDEX`. These are the
    `produce_forcing_array`, `run_shud_forecast_array`, `parse_output_array`, `save_state_snapshot_array` and
    `hindcast` sbatch scripts, which call `workers/forcing_producer/cli.py` and `packages/common/state_cli.py:497`;
  - Slurm writes the logs of pending and running jobs.
  
  Both are protected by the frontier (step 2) plus the recency guard (step 3).
- Post-hoc consumers:
  - gateway `fetch_logs` (`_resolve_log_path`, `_collect_array_task_logs`, `_array_log_binding`,
    `_identities_from_record`, `_discover_*`; `real_backend.py:1530-1660`, `1913-2130`);
  - `persist_gateway_logs` (`services/orchestrator/chain_workspace.py:152-164`);
  - `_ParentArrayLogCache` (`services/orchestrator/pipeline_job_provenance.py:132-140`).
  
  For terminal jobs below the frontier, the implementer audits each one's ENOENT behaviour and records it in the PR;
  every consumer degrades without raising (see the residual risks above for `persist_gateway_logs`). `/jobs/{id}/logs` is unaffected because it reads
  the persisted `log_uri` (`apps/api/routes/pipeline.py:494-530`).
- Entry points: the scheduler pass, and the cleanup CLI (dry-run by default; a frontier fail-closed forces dry-run).
- Validators: root admission and overlap.

Seams under test:
- `plan_retention` / `run_retention` on temporary roots: `tests/test_retention_extra_roots.py`, or a new
  `tests/test_retention_slurm_log_lane.py`;
- cleanup CLI: `tests/test_cli_cleanup_frontier.py`;
- pass forwarding: `tests/test_retention_extra_roots.py` (the existing 5.3 forwarding test) or
  `tests/test_production_scheduler.py`.

Required evidence: see `tasks.md` §2. Every new-behaviour test is red against the pre-change source.

Non-goals:
- new environment knobs;
- recursion for selection;
- locking W;
- the legacy backlog (purged on node-22 on 2026-11-15);
- the pre-existing gateway `MAX_DISCOVERY_ENTRIES=256` discovery limit (`real_backend.py:108`), which this change
  only relieves.

Review focus:
- the closed name set cannot match operational directories;
- the order of window, frontier and recency checks;
- `None`/gate parity, and the copyback and primary alias rules;
- overlap and collector consistency;
- the consumer ENOENT audit.
