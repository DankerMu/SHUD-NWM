# retention-workspace-slurm-log-lane (#3142)

## Why

Slurm jobs write their logs to `<WORKSPACE_ROOT>/<run_id>/logs/<job>.{out,err}` (the sbatch `--output/--error`
directives in `infra/sbatch/*.sbatch:11-14`). The gateway writes per-cycle array manifests and array logs to
`<WORKSPACE_ROOT>/<cycle_id>/{manifests,array_logs}` (`write_manifest_index`, `real_backend.py:685`;
`_ensure_array_log_directory`, `real_backend.py:375`), where `cycle_id = cycle_id_for(source, cycle)` =
`(gfs|ifs|era5)_<YYYYMMDDHH>`.

End-of-pass retention admits `WORKSPACE_ROOT` as an additional root (#1318) but sweeps it `runs/`-only
(`services/orchestrator/retention.py:21`, `_collect_run_targets`). As a result these root-level directories are never
reclaimed.

On node-22 on 2026-11-15 there were about 2,700 of them, dating back to 2026-05-30:

| shape | count | older than 14 d |
|---|---|---|
| `cycle_*` | 1328 | 1060 |
| `fcst_*` | 845 | 845 |
| `<source>_<cycle>` | 558 | 424 |

Together they take about 1.6 GB. They grow without bound, slow every listing of the workspace root, and lead operators
to read stale logs.

Owner request (2026-11-15): "核查后立issue解决".

## What

- The pass and the out-of-pass `cleanup` CLI both pass the scheduler workspace root to retention a second time, as a
  named argument, so it can be recognised as the Slurm log root. This follows the same pattern as `copyback_root`
  (#2238).
- On that root only, and only when it was admitted as an additional root (so `NHMS_RETENTION_EXTRA_ROOTS_ENABLED` and
  the #1616/#1617 admission rules apply), retention gains a lane for root-level Slurm log directories. The lane
  covers two name shapes:
  - canonical run ids (`parse_run_cycle`);
  - gateway cycle ids `(gfs|ifs|era5)_<YYYYMMDDHH>`, the closed set `cycle_id_for` can emit.
- A directory is selected by the same conjunction as `runs/`: its cycle is older than the additional-root cutoff
  (`NHMS_RETENTION_EXTRA_ROOTS_DAYS`) **and** below the pass frontier `active_lower_bound`, **and** nothing in its
  tree was modified within the window. The recency guard protects in-flight analysis, retry and forced-resubmit jobs
  that sit below the forecast frontier.
- Every other root-level name is outside the lane and is never touched. This covers `runs`, `scheduler`,
  `slurm-logs`, `repairs`, and operational and probe directories.
- The copyback root and the primary object-store root never get the lane, even if they alias the workspace root.
- The receipt keeps schema `nhms.production_scheduler.retention.v2`. Lane entries carry `root=<workspace root>`, a
  root-relative `key=<dirname>`, a new target reason `slurm_log_cycle_aged_out`, and a new skip reason
  `slurm_log_recently_modified`. Frontier-exempt and in-window entries reuse the existing skip reasons.

Non-goals:
- No new environment variable. The lane reuses the existing additional-root gate and window.
- No change to `runs/` behaviour, to primary-root lanes, or to copyback mutex scope. The workspace root stays unlocked.
- No one-off backlog purge. The node-22 backlog older than 14 days was already removed manually during the
  2026-11-15 cleanup; this change prevents it from coming back.

## Triage

Issue type: bugfix
Fixture level: expanded (new file-delete behaviour on a production root; touches the retention receipt reasons and
the CLI cleanup surface).
Upstream suggested level: absent.
Blast radius: a wrong admission rule deletes an operational directory under the workspace root (for example
`scheduler/` with the journal, or `slurm-logs`). A wrong frontier comparison deletes logs or array manifests of an
in-flight cycle, which breaks gateway log discovery and restart reconcile.
Selected risk packs: File IO / path safety; Public API / CLI / script entry; Schema / field names (receipt reasons);
Concurrency / shared state (in-flight cycles); Resource limits (thousands of root entries); Legacy compatibility
(existing retention plan and tests); Error handling; Documentation; Slurm production lifecycle / mock-vs-real parity
(domain).
Evidence floor: `uv run pytest -q tests/test_retention*.py tests/test_cli_cleanup_frontier.py tests/test_production_scheduler.py tests/test_select_ci_tests.py`
together with the scheduler retention wiring tests found by `scripts/select_ci_tests.py` (run on node-27 and locally);
`uv run ruff check .`; `openspec validate retention-workspace-slurm-log-lane --strict --no-interactive`; node-22
post-merge receipt.
