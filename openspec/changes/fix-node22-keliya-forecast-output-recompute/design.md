## Design

The scheduler already supports downstream resume after a successful upstream
forecast. The missing invariant is the inverse: downstream resume is unsafe when
the forecast output never became durable.

The fix adds a candidate-state branch before permanent failure handling:

- trigger only when the failed stage is downstream of forecast and durable SHUD
  output is absent;
- limit the branch to transient/runtime-like error codes such as
  `NODE_FAILURE` and `STATE_SAVE_QC_TASK_FAILED`;
- force `restart_stage=forecast` with an explicit
  `missing_forecast_output_recompute` classifier;
- run the existing missing-forcing artifact guard before submitting the
  recompute.

This keeps genuine malformed downstream input failures from being widened, while
making the #882 node-22 residual recoverable without operator intervention.
