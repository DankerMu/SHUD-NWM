## 1. Scope and fixture

- [x] 1.1 Review expanded fixture and pass strict OpenSpec validation before implementation.
- [x] 1.2 Record production code drift, active interpreter, scheduler/timer/Slurm readers and safe scoped deployment contract.

## 2. Code and behavioral regression (#2439 acceptance)

- [x] 2.1 Preserve ordinary pre-forecast resumes through real candidate decision -> strict reconciliation -> witness guard; demonstrate the captured convert-only forcing path red before fix and green after.
- [x] 2.2 Keep forecast and later missing/mismatched-manifest upgrade and stable per-model missing-forcing blockers unchanged.
- [x] 2.3 Preserve matching manifests and terminal/quarantine/successor/authorized-repair siblings with existing behavioral regression coverage.
- [x] 2.4 Cover canonical stage aliases, restart_stage/restart_from_stage precedence, unknown/missing stages and downstream force-resubmit decision/stage consumers.
- [x] 2.5 Correct the existing stale-cold-manifest test's invalid forcing-stage premise while retaining its valid forecast-or-later rerun contract; no implementation-only assertions or bypass fixtures.

## 3. Review and verification

- [x] 3.1 Run local Ruff and OpenSpec, node-27 targeted red/green regression and affected scheduler/consumer suites; record source SHA/import path and commands.
- [x] 3.2 Create PR and finish expanded cross-review/fix gate; preserve immutable fixture and record reviewed deployment patch identity. PR #2445 source checkpoint clean at `c9589e0f`; live recovery and final evidence review still gate merge.

## 4. Authorized production recovery

- [x] 4.1 Publish minimal reviewed source-only backport via GitHub on measured node-22 baseline; pause timer, drain active code readers/jobs, preserve data and environment, deploy with safe rollback admission. Deployed `e7bd816` with source patch equivalence and unchanged environment/interpreter.
- [x] 4.2 Verify exact deployed decision path and run bounded normal scheduler execution; observe original GFS forcing witness, forecast success, successor state and copyback identities without bypass or tracker reset. Arrays 49028/49066/49117 each completed 38/38; node-27 received and published the corresponding GFS runs.
- [x] 4.3 Observe GFS prior completion and IFS global-prior release/progression; record any independent failures honestly. IFS convert 49156 succeeded; forcing 49174 completed 38/38 but its gateway response failed and journal became permanently_failed. Independent recovery gap tracked in #2447; no blind retry or journal mutation.
- [x] 4.4 On node-27 verify normal ingest/publication of later GFS and IFS cycles and API source/cycle/model/run identity plus readable result data; restore approved timer state and record live receipt. Both sources published `2026-09-15T12Z`, exact-run MVT decoded at 23:06Z; timer active.

GFS first recovery tick PID3339699 published 38/38 with zero failures. IFS initially encountered #2447; the next normal pass retried under a forcing-cohort identity (49309), then completed forecast49347/state-save49385. This was automatic duplicate forcing execution, not operator adoption; #2447 remains authorized follow-on work to prevent recurrence. At 23:06Z both sources selected `2026-09-15T12Z` with readable exact-run discharge tiles (GFS first feature qc_warning, IFS ok; no global QC-clean claim). See `evidence/node27-dual-source-decoded-tiles.txt` and `evidence/node22-recovered-stage-identities.txt`. Final evidence review and merge remain pending.

## 5. Delivery

- [ ] 5.1 Update narrow runbook guidance and recovery receipt, complete CI/merge/issue evidence, and archive the completed change only when code and requested recovery criteria are satisfied.

## Evidence Floor

- Regression proof: existing/captured GFS convert-only state, strict ready and absent forcing => old code promotes forcing to forecast then blocks; patched real decision chain retains forcing retry. Execute red and green on node-27, not on node-22 active environment. Parent orchestrates verification; implementer must not run project-wide tests mid-flight.
- Boundary proof: canonical convert/forcing aliases remain pre-forecast; nonempty restart_stage precedence beats restart_from_stage; absent/unknown stage gets no new bypass; forecast/later mismatch and missing forcing still fail closed. Matching strict manifest, terminal/quarantine/successor and explicit authorized repair retain observable outcomes, including downstream forced-resubmit semantics.
- The existing test at tests/test_production_scheduler.py::test_retry_with_stale_cold_manifest_is_upgraded_to_warm_forecast_rerun encodes forcing-stage skipping. Changing its premise to forecast/later is explicitly required by #2439; preserve stale-manifest rerun coverage and add separate pre-forecast regression rather than silently weakening expectations.
- Local commands: `uv run ruff check <changed Python files>`; `openspec validate preserve-pre-forecast-retry-stages --strict --no-interactive`; applicable Markdown checks. Backend test oracle: node-27 isolated checkout, `TMPDIR=/home/nwm/tmp`, measured `df -h / /home /data/GHDC`, focused pytest new regression plus existing affected scheduler/consumer tests. Record exact collected/running test paths and outcomes; do not claim collect-only as test pass.
- Deployment oracle: node-22 current git status/head, exact .venv/bin/python -V (3.12.7 retained), scheduler unit/process and squeue/sacct identities, paused timer/no readers, GitHub-delivered descendant patch equivalence, no unrelated master rollout, no hidden environment synchronization. Untracked .nhms-work and ignored data remain untouched.
- Recovery oracle: normal node-22 scheduler pass and Slurm terminal outcomes bound to actual source/cycle/model/run, forcing manifest/witness, forecast outputs, successor checkpoint, copied artifacts. submitted>0 alone is insufficient. No manual file-state edits, circuit reset or manufactured forcing witness.
- Display oracle: node-27 existing ingest lane publishes later GFS than 2026-09-14T00Z and later IFS than 2026-09-14T12Z; API selects those new exact identities and serves result data. Existing HTTP200 on old cycles does not pass. Record readonly boundary unchanged; no display code deployment is planned.
- Long-running external scheduler/Slurm observation may use one read-only monitor per job; it must not mutate or poll in the user chat. User explicitly requests silent waiting while agents execute.
- A real later-stage blocker is not a code regression assumption: preserve its red evidence, diagnose before proposing changes, and report requested recovery as incomplete until resolved or user explicitly changes scope.

## Risk packs

| Pack | Selection and verification |
|---|---|
| Public API / CLI / script entry | Selected: existing scheduler CLI production entry plus published identity API smoke, 4.2/4.4; signatures unchanged. |
| Config / project setup | Selected: pinned runtime/env/unit and narrow deployment admission in 1.2/4.1; no config broad rollout. |
| File IO / path safety / overwrite | Selected: preserve journal/object store/readonly witness behavior and scoped Git deployment, 4.1/4.2; no artifact deletion or fabrication. |
| Schema / columns / units / field names | Selected: decision reason/restart-stage/identity contracts and aliases in 2.1-2.4; no schema migration. |
| Auth / permissions / secrets | Selected: DB-free node22, readonly node27 observation and credential redaction in 4.1/4.4; no privilege changes. |
| Concurrency / shared state / ordering | Selected: stage order, retry budgets, shared cohort, timer/worker quiescence in 2.1-2.4/4.1-4.3. |
| Resource limits / large input / discovery | Selected: unchanged concurrency limits, bounded initial pass and explicit source frontier evidence in 4.2/4.3; no backlog-clear claim. |
| Legacy compatibility / examples | Selected: aliases, special retry lanes and active older production baseline in 2.3/2.4/4.1. |
| Error handling / rollback / partial outputs | Selected: fail-closed witness preserved, no submit-only success, rollback/no-active-reader gate in 2.2/4.1-4.4. |
| Release / packaging / dependency compatibility | Selected: old active interpreter retained, dependency-free source-only patch equivalence in 4.1; no environment migration. |
| Documentation / migration notes | Selected: fixture, explicit test-oracle correction, live recovery receipt and scoped deployment notes in 2.5/5.1. |
