## 0. Risk and evidence contract

Fixture level: **expanded**. Repair intensity: **high**.

Selected core packs:

- Public API / CLI / script entry: selected — GitHub workflow triggers are the public control-plane entrypoint.
- Config / project setup: selected — `.github/workflows/ci.yml` and selector routing change.
- Concurrency / shared state / ordering: selected — group identity, running cancellation, pending replacement.
- Resource limits / large input / discovery: selected — full pytest consumes the long hosted-runner window and must reach terminal state.
- Legacy compatibility / examples: selected — PR supersession must remain unchanged.
- Error handling / rollback / partial outputs: selected — cancelled/pending-dropped runs are partial/missing evidence.
- Documentation / migration notes: selected — comments/spec must explain PR vs non-PR split and the 45-minute non-goal.

Not selected: File IO/path safety/overwrite; Schema/columns/units; Auth/permissions/secrets; Release/packaging/dependencies. No corresponding surface changes.

All NHMS domain packs are not selected: no geospatial, hydro-met, SHUD numerical, PostGIS/Timescale, Slurm lifecycle, external-provider snapshot, run-manifest/QC, or published-display identity behavior changes.

## 1. Workflow implementation

- [x] 1.1 Change CI concurrency group so pull requests use PR number and every non-PR run uses `github.run_id`.
- [x] 1.2 Make `cancel-in-progress` true only for `pull_request` events.
- [x] 1.3 Add `.github/workflows/ci.yml` to the backend paths-filter.
- [x] 1.4 Add an exact selector rule mapping `.github/workflows/ci.yml` to `tests/test_select_ci_tests.py`.
- [x] 1.5 Preserve all job conditions, names, timeouts, marker expressions, and workflow triggers outside the concurrency/self-routing change.

## 2. Contract tests

- [x] 2.1 Add a workflow-concurrency test that extracts the unique top-level block and pins PR number, `github.run_id`, conditional cancellation, and absence of `github.ref` fallback.
- [x] 2.2 Add a behavior test proving `select_tests([".github/workflows/ci.yml"])` selects exactly the selector meta-guard suite and no core-smoke fallback.
- [x] 2.3 Add a workflow filter test proving `.github/workflows/ci.yml` appears in the backend filter block, so a workflow-only PR starts targeted tests.
- [x] 2.4 Red proof: before implementation, the new tests fail on all current gaps; mutation proof separately reds on ref fallback, global-true cancellation, selector-route removal, and backend-filter removal.

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py` passes.
- [x] 3.2 `uv run ruff check .` passes.
- [x] 3.3 `openspec validate protect-master-full-ci-from-cancellation --strict --no-interactive` passes.
- [x] 3.4 `scripts/select_ci_tests.py` with a workflow-only changed-file list selects `tests/test_select_ci_tests.py` and reports `meta_guard_only=true` rather than zero files.
- [ ] 3.5 PR CI parses the workflow and runs the selector contract assertions.
- [ ] 3.6 After merge, capture the master `Unit Tests (full)` terminal receipt and duration; if it times out at 45 minutes, report a measured follow-up rather than changing the timeout in this PR.

## 4. Non-goals

- [x] 4.1 Do not add schedule, change the 45-minute timeout, alter markers, or fix #1644/#1632 in this PR.
- [x] 4.2 Do not change Governance or visual-evidence workflow concurrency.
