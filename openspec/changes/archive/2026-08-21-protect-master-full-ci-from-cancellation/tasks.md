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
- [x] 1.6 Make `dorny/paths-filter` expose a catch-all JSON changed-file list and make targeted selection parse it with runner-provided `jq` into the existing `--changed-file` seam; do not recompute PR changes with `--base-ref` or assume `uv` is installed in CI.

## 2. Contract tests

- [x] 2.1 Add a workflow-concurrency test that extracts the unique top-level block and pins PR number, `github.run_id`, conditional cancellation, and absence of `github.ref` fallback.
- [x] 2.2 Add a behavior test proving `select_tests([".github/workflows/ci.yml"])` selects exactly the selector meta-guard suite and no core-smoke fallback.
- [x] 2.3 Add a workflow filter test proving `.github/workflows/ci.yml` appears in the backend filter block, so a workflow-only PR starts targeted tests.
- [x] 2.4 Red proof: before implementation, the new tests fail on all current gaps; mutation proof separately reds on ref fallback, global-true cancellation, selector-route removal, and backend-filter removal.
- [x] 2.5 Pin the exact group expression and add run_id-first / branch-inversion mutation tests.
- [x] 2.6 Pin `list-files: json`, catch-all `all_files` job output, env-only JSON transport, runner-provided `jq -r '.[]'` conversion, selector use of `--changed-file`, and absence of `uv run`/`--base-ref` in the targeted selection step; removing any leg must red.

## 3. Verification

- [x] 3.1 `uv run pytest -q tests/test_select_ci_tests.py` passes.
- [x] 3.2 `uv run ruff check .` passes.
- [x] 3.3 `openspec validate protect-master-full-ci-from-cancellation --strict --no-interactive` passes.
- [x] 3.4 `scripts/select_ci_tests.py` with a workflow-only changed-file list selects `tests/test_select_ci_tests.py` and reports `meta_guard_only=true` rather than zero files.
- [x] 3.5 Final-head PR CI run [32461229539](https://github.com/DankerMu/SHUD-NWM/actions/runs/32461229539) on `5a52d2161064d6d51e8a56524bbd24158ab2ba2f` parsed the workflow, carried all nine PR paths through `all_files`, executed hosted-runner `jq`, selected `tests/test_select_ci_tests.py`, ran 172 assertions, and collected the 13,258-test tree.
- [x] 3.6 Post-merge receipt: merge SHA `4524ceefb955f79d6afc5887b17d46ae4bac57f7` run [32464770825](https://github.com/DankerMu/SHUD-NWM/actions/runs/32464770825), job [96718991932](https://github.com/DankerMu/SHUD-NWM/actions/runs/32464770825/job/96718991932), ran for 45m15s and reached 97% before GitHub annotated `The job has exceeded the maximum execution time of 45m0s`; overlapping backend master run [32467042826](https://github.com/DankerMu/SHUD-NWM/actions/runs/32467042826), job [96725776125](https://github.com/DankerMu/SHUD-NWM/actions/runs/32467042826/job/96725776125), independently repeated the 45m16s / 97% timeout. Later master pushes overlapped the first run without cancelling it, so the concurrency repair held; measured timeout remediation is routed to #1671 rather than changing the bound or coverage here.

## 4. Non-goals

- [x] 4.1 Do not add schedule, change the 45-minute timeout, alter markers, or fix #1644/#1632 in this PR.
- [x] 4.2 Do not change Governance or visual-evidence workflow concurrency.
