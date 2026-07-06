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
- [ ] 2.2 Deploy latest branch to node-22 via GitHub ff-only sync and verify the
  scheduler no longer leaves the current 13 registered basins in
  `submitted_partial` because of `basins_keliya_shud` missing forecast output.
- [ ] 2.3 Capture node-22 service/timer state after the pass and record the live
  pass id, counts, Slurm submissions, residual blockers, and whether all 13
  current registered basins are business-runnable without manual intervention.
