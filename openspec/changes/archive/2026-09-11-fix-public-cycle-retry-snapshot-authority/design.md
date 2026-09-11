# Design: one retry identity per selected stage snapshot

## Diagnosis and authority

Issue #1356; base b7cdce635a62dd30badbbf17a8ed625157224ad4. User decision is recorded in .workplans/issue-1356/authorization.json. Existing evidence under .workplans/issue-2208/: concurrency-confirmed.json, concurrency-preexisting-proof.json, concurrency-diagnosis.json and second-oracle/ traces. Existing issue comment: https://github.com/DankerMu/SHUD-NWM/issues/1356#issuecomment-5630389081.

Executed red: node27 targeted test_file_journal_post_window_concurrent_public_cycles_submit_one_retry produced forecast_attempts=4 instead of3; untouched-base attempt4 reproduced the same. Passive trace kept one retry key. Forced scheduling held both second-round ID-selection calls on the same permitted attempt2, then let the first reservation commit before the second calculation: retry_2/attempt3 and retry_3/attempt4 each reserved and entered the gateway. The temporary probe changed scheduling only. Relevant orchestrator and test source diff from untouched base is empty. This is a real submit-once eligibility/identity race, not a fake-client counter race (its Lock serializes increments), and not a coverage change. Original introduction is not established; historical ace2c730 documents a similar symptom only.

## Triage and ownership

Fixture level expanded; upstream suggestion absent. High-risk review surface: submit-once, accepted-submit recovery, shared file-journal state and retry identity. Under the workflow expanded three-seat cap, seat plan is correctness+integration, invariant-state+security-perf, test-evidence+spec-compliance; all six lenses and explicit invariant coverage remain present. One serial implementer owns the coupled production/test cutover; parent owns specs, proofs, Git, reviews and node27 validation.

Selected core packs: public entry compatibility (orchestrate_cycle); schema/field identity (unchanged existing IDs and durable fields); concurrency/shared state/ordering (same-snapshot identity + existing atomic reservation); error/rollback/partial outputs (ambiguous submit, reconciliation, duplicate skip); resource limits (remove a query, preserve bounded retries and parallelism); legacy compatibility (manual retry/marker-free/operator-demotion behavior); documentation/migration notes (no deployment). File IO/path safety is not selected for mutation: existing journal lock/write/replay machinery stays untouched and is a protected integration boundary. Config, auth/secrets, packaging/dependencies are not selected: no such changes. Domain Slurm lifecycle and run identity are selected via fake gateway boundary plus actual file-journal persistence on node27; solver/numerics, geospatial, time-series fact queries, external providers and published artifacts are unchanged. No live Slurm submission or deployment is authorized by this code repair.

## Clean cutover

The minimal fix keeps identity computation and eligibility on one observation. Change the third argument of ForecastOrchestratorCycleMixin._retry_cycle_stage_job_id from the ignored single _existing_job mapping to the existing jobs sequence that its caller used to select existing_job. Preserve base_job_id derivation, context.retry_attempt short-circuit, _next_retry_attempt_for_stage's stage/prefix/malformed-suffix handling, lower bound1, and _pipeline_retry_job_id formatting. Compute the suffix from that supplied snapshot; do not query the repository again inside this method.

All three callers in chain_forecast_execution._run_cycle_chain pass the same existing_jobs snapshot used by _find_existing_stage_job: download-success-with-missing-raw, terminal-after-upstream-refresh, and terminal/manual/automatic retry. The stage loop continues refreshing its own snapshot at the existing points after a submit. The two direct file-journal test callers obtain the context-scoped jobs snapshot, select the parent from it, and pass that same snapshot. No compatibility shim/default second query; migrate every caller. LSP references is unavailable (Ruff -32601); parent scoped repository grep identified these five callers.

Keep _next_retry_attempt_for_stage unchanged: selecting only the parent row instead of its full snapshot could alter existing max-suffix behavior. Keep operator_verified_absence recovery's old job_id/idempotency key branch unchanged, as well as _terminal_stage_needs_manual_retry, _schedule_cycle_stage_retry, context marker freshness, gateway payloads, repository writes/locks/CAS and reservation result handling. Do not serialize public cycles or hold a lock across gateway submission. No new retry coordinator, shared state field, schema or dependency.

This closes the proved mixed-snapshot window: two callers deciding from the same snapshot derive the same automatic replacement key; the existing reservation permits only one owner. A caller observing a new active master follows the existing resume/duplicate path. Explicit fresh manual retry authority retains precedence. The fix is not a redesign of independently authorized retries or an assertion that every concurrent operator action shares one authority.

## Governing invariant and surfaces

For one selected stage jobs snapshot and one automatic retry permission, concurrent public passes produce one replacement identity and at most one gateway submission. Durable attempt, cohort/hydro projections and ambiguous-submit/reconcile evidence must agree with the actual reservation owner.

| Surface | Owner / invariant | Evidence |
|---|---|---|
| Source observation | _run_cycle_chain existing_jobs and selected parent are one snapshot | deterministic delayed second ID calculation |
| Identity producer | _retry_cycle_stage_job_id consumes the snapshot; explicit context attempt retains priority | old-method red, fixed green; existing manual freshness owners |
| Atomic boundary | reservation.reserve_candidate and file-journal exact-key reserve/reclaim unchanged | real journal public cycles, one active unbound master |
| Gateway | _submit_array_stage only after ownership; same fake client as existing test | original counts2/3, no extra submission or cancellation |
| Durable projections | accepted-submit attempts/anchors/cohort membership and all18 hydro rows | existing per-round assertions plus complete journal row evidence |
| Sibling retry doors | missing raw, refreshed upstream, ordinary/manual retry and operator demotion | chain suite + direct file-journal owners, no eligibility changes |
| Failure/recovery | ambiguity -> authoritative absence -> one clean retry; next permission repeats safely | both rounds, gfs and IFS, reopen after each |
| Evidence boundary | no production Slurm/active checkout mutation; private node27 environments | receipts bound to source SHA; 20-round stressed soak |

## Tests and proof

Retain the existing two-source natural concurrent test and every assertion. Add a deterministic interleaving mode (or one companion owner reusing the same public scenario without duplicating its setup) at the already diagnosed ID-selection/reservation seam: both second-round workers reach selection with the old attempt2 snapshot; hold the slower ID calculation until the first attempt3 reservation commits. Use Barrier/Event with bounded waits, not sleeps. Do not mock computed IDs, reservation outcomes or repository reads. Only the fake Slurm boundary remains mocked. A barrier timeout is a harness failure, never accepted red evidence. If extending the existing test with a mode, preserve an unsynchronized natural mode.

After each concurrent round, assert exactly one current reserved-unbound master before indexing it; preserve cumulative gateway counts2/3, two retry submissions, clean reconciliation fields, durable attempt2/3 across all18 basins, at least one non-exception result and no cancellations. Capture query_pipeline_jobs_by_cycle's complete row list in parent proof, including job_id, idempotency_key, status, submission_attempt and reconciliation_decision, so both failure rounds are diagnosable without truncated assertion repr. Gate on observable submissions/state, not only method call counts.

Parent installs the exact old _retry_cycle_stage_job_id method from baseline in the private process (its old third argument was unused, so passing the snapshot is a valid adapter-free restoration) and runs the deterministic new public test: it must produce two different retry IDs / extra gateway submission by assertion, not import/signature/timeout errors; restored source must pass. Parent retains the existing base spontaneous red and scheduling trace, and adds full rows to the deterministic red/green receipt. A targeted explicit-manual-priority behavior check is required if existing chain tests do not observe that contract through a public invocation; do not add an implementation-only helper echo test.

## Verification, cleanup and risk

Node27 private worktree/Python3.11 environment: focused deterministic red/green; original two-source test and deterministic mode under modest bounded CPU pressure for20 consecutive rounds, every case passing; full test_orchestration_chain.py, test_file_orchestration_journal.py and test_production_scheduler.py plus actual selector-required affected pure owners. Route integration-marked cases into disposable DB stage rather than silently skipping them. Local uv run ruff check . and strict OpenSpec. No assertion weakening, skip/xfail/retry-until-green, serialized workload, widened timeout used to hide the race, or mocked ownership results. Record exactly executed suite/soak counts and SHA.

Production applicability: concurrent file-journal public passes for the same forecast cohort can reach this code; distinct retry keys bypass exact-key exclusion, so duplicate arrays are possible. The existing scenario represents18 basins, not a measured claim about every production cohort. No historical production double-submit incident has been established in this run, and no node22 live Slurm probe is required for this Python identity-only change; Slurm flags/resources/wrappers stay unchanged. Test-only branch of issue acceptance is inapplicable because two real ownership results and gateway calls were observed. Do not broaden into deployment or production-log archaeology.

After proof, archive scripts/trace/full rows/receipts and remove temporary plugins, stress processes and private worktrees. After independent reviews/CI and preauthorized merge, perform per-PR accountability and archive only this dedicated change. Never archive shared timeseries-narrow-store-expand-contract. Resume #2208 by integrating the merged fix and rerunning its unchanged full selected matrix and remaining gates.
