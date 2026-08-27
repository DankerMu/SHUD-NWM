## 1. Fixture and contract

- [x] 1.1 Validate this expanded/high fixture with all risk packs classified and an independent fixture review.
- [x] 1.2 Preserve the existing `O_EXCL`, receipt schemas, clone gates, complete-success payloads, and baseline optional-flag compatibility.

## 2. Receipt control flow

- [x] 2.1 Add one failure-aware receipt-write helper shared by both transfer modes: clean write failures propagate; abort-time write failures annotate but never replace the primary exception.
- [x] 2.2 Make baseline cutover capture any basin/source failure after completed decisions, emit a partial aborted receipt only when at least one live clone exists, and re-raise the exact original exception.
- [x] 2.3 Require `--receipt` for recalibration dry-run and apply through `_REQUIRED_FLAGS_BY_MODE`; leave baseline requirements unchanged.

## 3. Requirement-driven tests

- [x] 3.1 Prove baseline A succeeds then B fails: all live rows appear in the receipt, failed location/reason is present, and the original exception propagates.
- [x] 3.2 Prove existing receipt paths cannot mask a later recalibration refusal or a single-pair mirror failure; assert primary type/message, receipt-error visibility, and no overwrite.
- [x] 3.3 Prove clean receipt-write failure still propagates and first-item/no-write failures do not create abort receipts.
- [x] 3.4 Cover missing recalibration `--receipt` for both apply and dry-run and ensure every recalibration test invocation uses a unique receipt path.
- [x] 3.5 Pin successful apply receipt persistence and baseline invocation/payload compatibility.
- [x] 3.6 Route production-script diffs to the new baseline suite and strengthen the selector contract test so future PRs cannot omit it.
- [x] 3.7 Split at the existing `# --- §6.8 --pairs resolution` marker: keep all preceding end-to-end/partial-write tests in the original module; move that marker and every following pair/registry/per-mode validation test without semantic change; keep both modules below 1000 lines and independently collectible.
- [x] 3.8 Close selector-derived authorities introduced by the split: base fixture changes select all four direct consumers, and the baseline suite's mapping-builder import is dispositioned as an independently owned `edge-consumer`.

## 4. Risk packs and evidence

- [x] 4.1 **Public API / CLI / script entry — selected:** focused parser/dispatch tests: omitted recalibration receipt → `SystemExit`; baseline legacy argv without receipt → accepted.
- [x] 4.2 **Config / project setup — selected:** targeted-CI routing must include the new baseline suite for production-script and shared-fixture diffs; selector closure/disposition tests prove the routes.
- [x] 4.3 **File IO / path safety / overwrite — selected:** existing path and injected `OSError` → no overwrite, primary error retained, write failure visible; unique path → artifact persisted.
- [x] 4.4 **Schema / columns / units / field names — selected:** complete baseline/recalibration receipt mappings retain existing fields; aborted baseline adds only explicit invocation/failure evidence.
- [x] 4.5 **Auth / permissions / secrets — not selected:** no identity or secret handling; permission errors are covered as generic receipt-write failures.
- [x] 4.6 **Concurrency / shared state / ordering — selected:** tests pin index-write-before-later-abort ordering and primary-error-before-secondary-evidence-error precedence; no new concurrency.
- [x] 4.7 **Resource limits / large input / discovery — not selected:** no new traversal, unbounded read, retry, polling, or input surface.
- [x] 4.8 **Legacy compatibility / examples — selected:** baseline no-receipt invocation and successful receipt shape remain unchanged; recalibration runbook already supplies unique receipt paths.
- [x] 4.9 **Error handling / rollback / partial outputs — selected:** partial live rows are declared, failure remains nonzero, and the code makes no false rollback claim.
- [x] 4.10 **Release / packaging / dependency compatibility — not selected:** no dependency or package metadata changes; Python 3.11 `add_note` is within the pinned runtime.
- [x] 4.11 **Documentation / migration notes — selected:** inspect §5.7; update only if wording conflicts with required receipt or dual-error behavior.
- [x] 4.12 **Geospatial / CRS / basin geometry — not selected:** basin IDs locate failures but geometry/CRS behavior is untouched.
- [x] 4.13 **Hydro-met time series / forcing windows — not selected:** no forcing or temporal-window change.
- [x] 4.14 **SHUD numerical runtime / conservation / NaN — not selected:** no solver execution or numerical behavior change.
- [x] 4.15 **PostGIS / TimescaleDB domain behavior — not selected:** node-22 file-index route is DB-free.
- [x] 4.16 **Slurm production lifecycle / mock-vs-real parity — not selected:** no scheduler/Sbatch behavior; node-22 runtime execution is not required.
- [x] 4.17 **External hydro-met providers / snapshot reproducibility — not selected:** no provider boundary.
- [x] 4.18 **Run manifest / QC provenance — selected:** persisted receipt remains bound to the exact completed clone records and failed location.
- [x] 4.19 **Published NHMS artifacts / display identity — selected:** live state-index rows and their declared receipt evidence cannot silently diverge.

## 5. Verification

- [x] 5.1 Run red proof for new behavior tests against pre-change source and leave no red-proof stash.
- [x] 5.2 Run `uv run pytest -q tests/test_state_clone_recalibration_cli.py` (and any new focused baseline module).
- [x] 5.3 Run `uv run ruff check scripts/node22_clone_direct_grid_cutover_states.py tests/test_state_clone_recalibration_cli.py` plus any new test file.
- [x] 5.4 Run `openspec validate state-clone-receipt-fail-safe --strict --no-interactive`.
- [x] 5.5 Run the selector contract test and assert a production-script-only selection includes the three pre-split owned suites.
- [x] 5.6 Run both split recalibration CLI modules, the selector contract suite, line-count guard evidence, ruff, and strict OpenSpec validation; prove production-script diffs select all four owned suites, base-fixture diffs select all four direct consumers, and CLI-helper diffs select both recalibration CLI modules.

## 6. Non-goals

- [x] 6.1 Do not alter gate admission, state row fields, index schemas, receipt schema versions, `O_EXCL`, baseline receipt optionality, or node-22 deployment/scheduling.
