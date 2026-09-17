## Context

Captured active decision chain from #2441: succeeded shared convert job 48756 -> resume forcing -> strict manifest mismatch forecast rewrite -> FORCING_VERSION_ROW_ABSENT. This is not a forcing producer failure.
Change surface: scheduler_candidates._upgrade_retry_for_strict_warm_start_manifest; canonical restart-stage helper; existing scheduler tests; controlled compute deployment and publication receipts.
Must preserve: strict warm-start lineage/manifest checks at forecast and later, per-model forcing witnesses, stage retry budgets, explicit operator markers, terminal/quarantine/successor behavior, DB-free compute boundary and current Python 3.12.7 active environment.
Governing invariant: strict manifest reconciliation must never skip recognized unfinished pre-forecast work, and it must never weaken the forecast-stage forcing/identity guard.
Sibling surfaces: candidate state projection/decision, stage alias normalization, warm-start upgrade, witness guard, authorized forcing repair, terminal/quarantine/successor retries, chain force-resubmit consumers, file journal/cohort state, Slurm worker source paths, state-save/copyback, node-27 ingest and display.

## Goals / Non-Goals

Goals: fix #2439; keep identity and retry protections; restore real source-cycle progression and observe new downstream publications.
Non-goals: global retry redesign, missing-forcing bypass, fabricated witnesses, manual journal mutation, circuit tracker clearing, model re-registration, unrelated old Slurm reconciliation faults, role/schema changes, environment recreation, or full backlog-clear guarantee.
The user's current recovery request explicitly authorizes normal production scheduler/deployment actions beyond the original issue's no-production clause; it does not authorize bypasses.

## Decisions

Use the existing canonical stage normalization/precedence. Exempt only recognized stages before forecast; absent/unknown stages must not accidentally gain a pre-forecast exemption.
Keep forecast and downstream stale/missing-manifest reruns and guard outcomes unchanged, including stage aliases. Prefer a narrow helper-level phase gate over duplicated call-site checks.
Existing test test_retry_with_stale_cold_manifest_is_upgraded_to_warm_forecast_rerun currently chooses forcing as input and encodes the diagnosed defect. Move its stale-manifest rerun scenario to a real forecast/later stage; separately prove forcing/convert preservation via observable action/reason/restart/guard output. This is an explicit issue-contract correction, not weakening an oracle to hide a failure.
Node-27 is the backend-test oracle. Use an isolated checkout and its source path with an existing compatible environment; never replace production checkout for tests.
Node-22 active SHA is initially 7b38bcb8bebd65ce051dae0771c896e1ca96c0fc; master contains broad unrelated changes. Prepare a GitHub-delivered source-only backport descendant of the measured active SHA, preserving runtime pin and all other source/config bytes. Bind reviewed patch equivalence and deployment SHA in evidence; do not silently pull master.
Stop the scheduler timer and allow an active oneshot to settle; do not change checkout while scheduler or in-flight Slurm workers can still read it. Preserve untracked .nhms-work and all ignored data. Fetch the dedicated branch, verify clean tracked state and expected parent, then fast-forward/switch without stash.
Verify exact active interpreter exists and remains 3.12.7. Never uv sync, bare uv run or connect any node-22 live DB. Keep terminal stage forecast_state_save_qc and require warm start. Existing wrappers/exact interpreter only.

## Risks / Trade-offs

A resumed cohort can reveal an independent failure after this fix. Trace actual failing stage, preserve evidence and fail-closed behavior; never claim end-to-end recovery from submit alone.
The no-progress circuit tracks the exact GFS candidate and old unrelated jobs. Observe normal behavior after fix; do not erase tracker state or assume it is a separate submission gate.
A source-only backport avoids unrelated deployment but differs from the master review SHA: capture patch identity and run the exact deployment variant's decision-chain smoke before releasing work.

## Migration Plan

Local edit -> node-27 red/green tests -> commit/push/PR/review -> publish reviewed minimal deployment branch via GitHub -> pause timer, drain active readers/jobs, record old SHA/process/env fingerprints -> fast-forward/switch pinned compute checkout -> exact-runtime read-only decision smoke -> one bounded normal scheduler pass -> inspect true forcing/forecast/state/copyback outcomes -> restore original scheduler timer state after admission. No manual stage markers.
Rollback before submitting new work: restore recorded code SHA while readers are quiescent and restore original timer state. After submission, never roll code beneath active workers; fence new submissions, wait for safe terminal state, preserve journal/artifacts, then revert code only. No blanket scancel or journal rollback.
Seams under test: real candidate-decision/strict-upgrade/witness chain; actual scheduler + Slurm; published source/cycle/model/run API identity.
Recovery acceptance: the original GFS blocked cohort produces forcing, successful forecast and successor state, and copied outputs; node-27 publishes a later GFS cycle than 2026-09-14T00Z. Observe IFS global-prior release and a later published IFS cycle than 2026-09-14T12Z. Match exact run/model/cycle identities and readable data, not old-cache HTTP200. Do not require all historical backlog cleared.

## Open Questions

Current in-flight readers, deployment-patch applicability and later-stage failures are measured execution gates. An unresolved real blocker stops unsafe actions and is reported with evidence, without narrowing the requested recovery completion.
