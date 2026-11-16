## 1. Scheduler State Recovery

- [x] 1.1 Add candidate-state evidence that recomputes forecast when a downstream
  output-dependent stage fails but no durable forecast output exists.
  Evidence floor: focused test proves `state_save_qc` retry exhaustion with a
  missing forecast output produces `restart_stage=forecast`, automatic retry,
  and classifier `missing_forecast_output_recompute`.
- [x] 1.2 Preserve missing forcing-package guards before recompute submission.
  Evidence floor: existing missing-forcing scheduler tests still pass.

## 2. Verification

- [x] 2.1 Run local verification:
  `uv run pytest -q <focused scheduler tests>`;
  `uv run ruff check .`;
  `openspec validate fix-node22-keliya-forecast-output-recompute --strict --no-interactive`.
- [x] 2.2 Deploy latest branch to node-22 via GitHub ff-only sync and verify the
  scheduler no longer leaves the current 13 registered basins in
  `submitted_partial` because of `basins_keliya_shud` missing forecast output.
  - Superseded (2026-11-16 hygiene): the 13-basin registry and `basins_keliya_shud` are no longer in production. Live 2026-11-16: keliya is absent from `/ghdc/data/nwm/Basins` (62 dirs, `hetianhe` present, `keliya` absent), from node-27 `core.basin` (66 rows, 0 matching `%keliya%`), and from the 132-candidate node-22 pass `scheduler_2026111516_d110193be735` (0 occurrences). node-22 is at `b2fc70547`, which includes this fix. A recursive grep of `docs/` and `openspec/` found no record of when or why keliya left the registry.
- [x] 2.3 Capture node-22 service/timer state after the pass and record the live
  pass id, counts, Slurm submissions, residual blockers, and whether all 13
  current registered basins are business-runnable without manual intervention.
  - Superseded (2026-11-16 hygiene): the 13-basin acceptance is moot; the 66-basin db-free production replaces it. Pass `scheduler_2026111516_d110193be735` (2026-11-15T16:05Z): 132 candidates / 132 submitted for gfs+ifs 2026111500, convert→forcing→forecast→state_save_qc, `model_run_failures` 0. Its top-level status is `resource_limit_blocked`, even though `counts.submitted_count=132`. node-27 `hydro.hydro_run` shows 66 `published` per source for that cycle. `nhms-compute-scheduler.service` last triggered 2026-11-16 12:01 CST. Three November passes ended `submitted_partial` (11-07 / 11-09 / 11-10, during catch-up); none is tied to keliya.
