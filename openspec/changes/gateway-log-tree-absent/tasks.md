# Tasks — gateway-log-tree-absent (#3144)

- [x] 1.1 Add `log_tree_present: bool = True` to `SlurmLogsResponse`, and carry it through `mock_backend` and `gateway.py`.
- [x] 1.2 `real_backend.fetch_logs`: compute `log_tree_present` per proposal What 1, with the bound/unbound split
  decided by `_submission_metadata_complete(record)`. Reuse the candidate derivation that `_resolve_log_path` uses
  (refactor it to expose the candidates and the job-specific dirs); do not duplicate it.
- [x] 1.3 Orchestrator: `persist_gateway_logs` and `try_publish_log_for_advertise` (`chain_workspace.py`) and the legacy
  branch (`chain_stage_execution.py:1215-1227`) skip the write and the `log_uri` only when
  `logs.get("log_tree_present") is False`. `persist_gateway_logs` returns `False` on skip and `None` when written;
  update the wrapper return type at `chain_forecast_orchestrator_runtime.py:264`. Verify the refetch bound
  (proposal What 1) and report the result.
- [x] 1.4 Tests per E2, with red-on-old recorded. Do not touch `tests/test_forcing_submit_ambiguity.py` (1209 lines, not excluded from the large-file guard).
- [x] 1.5 Grep `schemas/`, the frontend types and the docs for the `SlurmLogsResponse` shape, and update any match.
