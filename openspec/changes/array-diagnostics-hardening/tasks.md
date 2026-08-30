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
- [x] 1.5 Migrate `production_closure`'s direct-render/raw-sbatch lane to the canonical neutral path for pre-sbatch creation, bounded task-log reads, blockers, and emitted partial/QC evidence; fake, no-submit, and blocked-preflight lanes must keep zero shared-log side effects.

## 2. Required evidence

- [x] 2.1 Red proof: new requirement tests fail against pre-change source in one batched run, then `uv run pytest -q tests/test_slurm_array_contract.py tests/test_real_slurm_gateway.py tests/test_orchestration_chain.py` passes.
- [x] 2.2 `uv run ruff check .` and `openspec validate array-diagnostics-hardening --strict --no-interactive` pass; final-head regression executes the touched rows plus repository default backend test row.
- [ ] 2.3 node-22 live receipt submits a forecast array with at least two members, proves the log directory names no member, and proves two task ids map to their own model/run through the log interface.
- [x] 2.4 `uv run pytest -q tests/test_production_slurm_validation.py` proves the direct submitter creates the exact rendered neutral directory before raw `sbatch`, reads success/missing-marker/missing-log/symlink/non-regular/TOCTOU cases from that lane, and no-submit/fake/blocked paths do not create it.

## 3. Risk and invariant fixture

Selected risk packs: Public API / CLI / script entry; File IO / path safety / overwrite; Schema / columns / units / field names; Concurrency / shared state / ordering; Resource limits / large input / discovery; Legacy compatibility / examples; Error handling / rollback / partial outputs; Documentation / migration notes; Slurm production lifecycle / mock-vs-real parity; Run manifest / QC provenance.
Not selected: Config / project setup (no setting); Auth / permissions / secrets (no boundary change, existing safe readers remain mandatory); Release / packaging / dependency compatibility (no dependency); Geospatial, time-series, numerical, DB, provider, published-artifact packs (diagnostic-only, task artifacts unchanged).

Seams under test: real template rendering/submission boundary; `production_closure validate-slurm --submit` render/precreate/raw-sbatch/read/evidence boundary; `fetch_logs` response after live record and restart; `parse_sacct_array_results`; `coerce_array_aggregation`.
Must preserve: worker manifest selection and artifact ownership; existing non-empty state mapping; legacy log readability; available log content when its sibling stream or identity metadata is missing.
Non-goals: terminal reuse, Slurm comments, non-array templates, moving/deleting historical logs, changing calculation outputs.

Invariant Matrix
- Governing invariant: array diagnostics never claim member identity without the exact submission index and never abort accounting solely because state is absent.
- Source of truth: immutable `manifest_index_path` and its `task_id -> model_id/run_id` entries; accounting status domain `succeeded|cancelled|failed`.
- Producers: canonical neutral-path helper; `submit_job_array`; `production_closure` direct-render/raw-sbatch acceptance lane; four production array sbatch templates.
- Validators/preflight: workspace containment/safe filesystem helpers, bounded manifest validation, production-closure pre-sbatch directory preparation, `array_task_status`.
- Storage/query: neutral log hierarchy, immutable manifest index, production-closure bounded task-log reader/evidence paths, legacy gateway run-log fallback, bounded restart discovery.
- Public/downstream: `fetch_logs`/`SlurmLogsResponse`, `nhms-production validate-slurm --submit` partial/QC evidence, orchestrator sacct and gateway-payload accounting, operators.
- Failure/evidence: missing stream, missing/corrupt/unsafe/ambiguous index, restart without in-memory record, direct-render/raw-submit path drift, success/missing-marker/missing-log/symlink/non-regular/TOCTOU production-closure rows, fake/no-submit/blocked no-side-effect rows, empty/whitespace/missing state, node-22 receipt.
- Regression rows: two distinct members -> neutral path plus correct identities; production-closure raw submit -> exact rendered directory exists before sbatch and all task evidence reads it; production-closure non-live lanes -> no shared log directory; legacy gateway path -> content retained without false identity; bad index -> content plus incomplete identity; empty state on both legs -> failed task; existing states -> unchanged mapping.

Boundary checklist: canonical neutral-path derivation; shared manifest reader; gateway submit/render/fetch and production-closure raw-submit entrypoints; neutral and legacy gateway read paths; production-closure bounded read/blocker/evidence paths; pre-sbatch log-directory creation; manifest producer/consumer binding; restart and direct-render stale-state boundaries; fake/no-submit/blocked no-side-effect boundary; response/evidence compatibility for existing clients.
