Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Issues: #1742, #1539
Upstream suggested level: absent

## 1. Contracts and implementation

- [x] 1.1 Make all four production array templates use a safely pre-created, cohort-neutral, submission-specific log directory bound to the immutable manifest index; rendering with first task `run_id=leader_run` must contain no `leader_run` in output/error/mkdir paths.
- [x] 1.2 Extend gateway lookup to read both neutral and historical locations and join each task's exact `model_id`/`run_id` in one bounded, safe manifest read; missing/corrupt/ambiguous identity must preserve log content and report incomplete identity without guessing.
- [x] 1.3 Guard `array_task_status("")` and whitespace input as `failed`, covering empty-state sacct and status/state-missing gateway payload paths without changing non-empty mappings.
- [x] 1.4 Update owning API/contract snapshots and operational documentation where the physical array log path is stated.

## 2. Required evidence

- [x] 2.1 Red proof: new requirement tests fail against pre-change source in one batched run, then `uv run pytest -q tests/test_slurm_array_contract.py tests/test_real_slurm_gateway.py tests/test_orchestration_chain.py` passes.
- [x] 2.2 `uv run ruff check .` and `openspec validate array-diagnostics-hardening --strict --no-interactive` pass; final-head regression executes the touched rows plus repository default backend test row.
- [ ] 2.3 node-22 live receipt submits a forecast array with at least two members, proves the log directory names no member, and proves two task ids map to their own model/run through the log interface.

## 3. Risk and invariant fixture

Selected risk packs: Public API / CLI / script entry; File IO / path safety / overwrite; Schema / columns / units / field names; Concurrency / shared state / ordering; Resource limits / large input / discovery; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes; Slurm production lifecycle / mock-vs-real parity; Run manifest / QC provenance.
Not selected: Config / project setup (no setting); Auth / permissions / secrets (no boundary change, existing safe readers remain mandatory); Release / packaging / dependency compatibility (no dependency); Geospatial, time-series, numerical, DB, provider, published-artifact packs (diagnostic-only, task artifacts unchanged).

Seams under test: real template rendering/submission boundary; `fetch_logs` response after live record and restart; `parse_sacct_array_results`; `coerce_array_aggregation`.
Must preserve: worker manifest selection and artifact ownership; existing non-empty state mapping; legacy log readability; available log content when its sibling stream or identity metadata is missing.
Non-goals: terminal reuse, Slurm comments, non-array templates, moving/deleting historical logs, changing calculation outputs.

Invariant Matrix
- Governing invariant: array diagnostics never claim member identity without the exact submission index and never abort accounting solely because state is absent.
- Source of truth: immutable `manifest_index_path` and its `task_id -> model_id/run_id` entries; accounting status domain `succeeded|cancelled|failed`.
- Producers: `submit_job_array` and four production array sbatch templates.
- Validators/preflight: workspace containment/safe filesystem helpers, bounded manifest validation, `array_task_status`.
- Storage/query: neutral log hierarchy, immutable manifest index, legacy run-log fallback, bounded restart discovery.
- Public/downstream: `fetch_logs`/`SlurmLogsResponse`, orchestrator sacct and gateway-payload accounting, operators.
- Failure/evidence: missing stream, missing/corrupt/unsafe/ambiguous index, restart without in-memory record, empty/whitespace/missing state, node-22 receipt.
- Regression rows: two distinct members -> neutral path plus correct identities; legacy path -> content retained without false identity; bad index -> content plus incomplete identity; empty state on both legs -> failed task; existing states -> unchanged mapping.

Boundary checklist: shared manifest reader; submit/render/fetch entrypoints; neutral and legacy read paths; log-directory creation; manifest producer/consumer binding; restart stale-state boundary; response compatibility for existing clients.
