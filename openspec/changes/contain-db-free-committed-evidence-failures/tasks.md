Fixture level: expanded
Repair intensity: high
Project profile: NHMS
Issues: #1796, #1568
Upstream suggested level: absent
Minimal mergeable slice: the shared committed-decision containment invariant across generic reservation/reclaim and duplicate-skip evidence; either entrance alone leaves the invariant false.

## 1. Generic reservation commit boundary

- [x] 1.1 Generalize the explicit post-append projection containment policy so plain file-journal reservation and both automatic/operator reclaim calls return the committed row after direct/inventory/latest failure, without changing unrelated writer contracts.
- [x] 1.2 Emit one bounded non-secret committed-warning signal per failed projection, attempt remaining independent projections, and ensure any secondary evidence failure cannot reverse the committed result.
- [x] 1.3 Preserve pre-append fail-closed behavior with byte-identical authority on injected append failure and preserve clean reserve/reclaim/bind behavior.

## 2. Public automatic-absence recovery

- [x] 2.1 Add a real public-cycle regression for `absence_retry_permitted` with a one-shot post-append direct or reconcile-inventory fault; prove fresh replay sees the committed attempt, the winning path performs at most one sbatch, and no `FILE_JOURNAL_WRITE_FAILED` escapes.
- [x] 2.2 Run a second independent public cycle and prove zero additional sbatch and no `PIPELINE_ALREADY_ACTIVE`; retain automatic retry-suffixed identity and immutable attempt/cohort contracts.
- [x] 2.3 Add concurrency/ownership or equivalent deterministic control evidence proving containment does not let two passes own the same attempt.

## 3. Duplicate-skip evidence boundary

- [x] 3.1 Align the submission skip handler with its sibling by containing exactly `OrchestratorError` and `FileOrchestrationJournalError`, without catching arbitrary exceptions.
- [x] 3.2 Add parameterized red regressions at the real submit/skip seam: either expected exception from `insert_pipeline_event` still returns `skipped_duplicate_submission`, preserves bounded skip evidence, and invokes zero sbatch.
- [x] 3.3 Inventory `services/orchestrator/` for other `except OrchestratorError` handlers around repository evidence writes and record whether a third same-intent site exists; report but do not fix unrelated sites.

## 4. Compatibility and evidence floor

- [x] 4.1 Prove normal plain reserve, automatic reclaim, operator old-ID reclaim, bind, warning cleanliness, and duplicate-skip event behavior remain compatible; PostgreSQL and unrelated file-journal writers stay unchanged.
- [x] 4.2 Produce one batched red proof against pre-change source for all new-behavior tests, restore source immediately, and leave no `red-proof` stash.
- [x] 4.3 Run focused pytest for the new repository/public-cycle/skip regressions, `uv run ruff check .`, `openspec validate contain-db-free-committed-evidence-failures --strict --no-interactive`, and `git diff --check`; the issues are local-only and require no node-22/node-27 live receipt.

## Selected Risk Packs and Required Evidence

- Public API / CLI / script entry: selected — real forecast public cycle and typed `StageRunResult` inputs map to exact status and zero-sbatch outputs in tasks 2.1-2.3 and 3.2.
- Config / project setup: not selected — no config/setup changes.
- File IO / path safety / overwrite: selected — append/direct/inventory/latest fault matrix, fresh replay, pre-commit byte identity, and bounded warning evidence in tasks 1.1-1.3 and 2.1.
- Schema / columns / units / field names: selected — stable warning/event tokens and reservation identity assertions in tasks 1.2, 2.1, and 4.1; no external schema change.
- Auth / permissions / secrets: selected — secret-shaped injected exception must not appear in warnings/events in task 1.2.
- Concurrency / shared state / ordering: selected — append commit order and single-owner proof in tasks 2.1-2.3.
- Resource limits / large input / discovery: not selected — no discovery or new input surface.
- Legacy compatibility / examples: selected — operator, generic/legacy, PostgreSQL, and normal-path controls in task 4.1.
- Error handling / rollback / partial outputs: selected — pre-append zero-commit versus post-append committed-warning matrix in tasks 1.1-1.3.
- Release / packaging / dependency compatibility: not selected — no dependency/package changes.
- Documentation / migration notes: selected — this fixture is the normative delta; no runtime migration/runbook change in task 4.3.
- Geospatial / CRS / basin geometry: not selected — untouched.
- Hydro-met time series / forcing windows: not selected — untouched.
- SHUD numerical runtime / conservation / NaN: not selected — untouched.
- PostGIS / TimescaleDB domain behavior: not selected — db-free-only behavior; PostgreSQL is an unchanged control.
- Slurm production lifecycle / mock-vs-real parity: selected — exact zero/one submit assertions at the real orchestrator seam in tasks 2.1-2.3 and 3.2.
- External hydro-met providers / snapshot reproducibility: not selected — untouched.
- Run manifest / QC provenance: not selected — no manifest/QC content changes.
- Published NHMS artifacts / display identity: not selected — untouched.

## Non-Goals

- No #1116 decision change, new scheduler status, cross-layer exception hierarchy refactor, global best-effort writer policy, Slurm gateway change, PostgreSQL behavior change, #1592 fix, or live production fault injection.
