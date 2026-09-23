# Tasks — Slurm submission isolation and gateway-error rendering (#1908 #1909 #2308)

## Risk Packs

- [ ] File IO / path safety — **selected**: submission-scoped index, manifest and log-dir paths under the workspace; exclusive create; cleanup bound to written paths. Covered by 1.x.
- [ ] Concurrency / shared state / ordering — **selected**: two concurrent live submissions with the same configured `run_id`; the worker reads its index after `sbatch` returns. Covered by 1.3, 1.4.
- [ ] Public API / CLI / script entry — **selected**: `nhms-production validate-slurm --submit --force`; `POST /api/v1/slurm/jobs`; `POST /api/v1/runs/{run_id}/cancel`; `GET /api/v1/queue/depth`. Covered by 1.x, 2.x, 3.x.
- [ ] Security / data exposure — **selected**: scheduler binary path and workspace root must leave the two ops response bodies. Covered by 3.x.
- [ ] Schema / columns / field names — **selected**: live-lane evidence gains submission-scoped run ids; the upstream-error response keys must not change. Covered by 1.5, 3.4.
- [ ] Legacy compatibility — **selected**: dry-run/fake-Slurm evidence byte-identical; persisted cancellation events unchanged; `submit_job_array` unchanged. Covered by 1.6, 2.3, 3.3.
- [ ] Error handling / rollback / partial outputs — **selected**: failed `sbatch` cleanup; refusal before any scheduler call. Covered by 1.4, 2.1.
- [ ] Documentation / migration notes — **selected**: `docs/validation/production-closure.md` `--force` wording, index path and artefact list. Covered by 1.7.
- [ ] Auth / permissions — not selected: no policy change (the #1909 endpoint already shares the array endpoint's bearer auth).
- [ ] Resource limits — not selected.
- [ ] Config / project setup — not selected.
- [ ] Release / packaging — not selected.

## 0. Setup

- [x] 0.1 Branch `feat/issue-1908-1909-2308-slurm-gateway-hardening` from `origin/master`. Locate every site by symbol; the issues' line numbers are from older baselines.
- [x] 0.2 The commit hook denies any staged file over 1000 lines unless it is in `.large-file-guard.json`'s `exclude`. **Six** files this change must touch are over the limit and absent from it: `services/production_closure/slurm_validation.py` (2719), `services/slurm_gateway/real_backend.py` (2363), `tests/test_real_slurm_gateway.py` (5153), `tests/test_monitoring_api.py` (3514), `tests/test_production_slurm_validation.py` (2404), `tests/test_slurm_array_contract.py` (1274). A seventh, `tests/test_retry_cancel_consistency.py` (1421), is over the limit too and must be added if the #2308 route cases land there rather than in `tests/test_monitoring_api.py`. Add every file you actually stage to the exclude list before the first commit; splitting them is out of scope. Route any NEW test module in `scripts/select_ci_tests.py` with a routing pin (batch-E1 lesson).
- [x] 0.3 Every new-behaviour test is shown red on pre-change source, then green; both outputs recorded inline below.

## 1. #1908 — submission-unique live inputs (design D1)

- [x] 1.1 One submission token per live submit: UTC timestamp matching `SAFE_IDENTIFIER_RE` (the array log dir is derived from the index stem), **claimed by an exclusive create of the index before any file that token names is written**, with a bounded retry on collision, mirroring `RealSlurmGateway.write_manifest_index`. This reverses today's order (`_write_manifest_index:685-688` writes the task manifests first).
- [x] 1.1b Every live-lane shared write goes through an exclusive-create primitive on `EvidenceWriter`, not `_write_bytes`'s overwriting path (`:172-207`, which `--force` lets skip even the exists guard at `:188`); the primitive to use is `packages/common/safe_fs.py:219 write_bytes_no_follow_exclusive`, the same one the gateway's index writer uses. `--force` and `_created_paths` keep their meaning for the `lane_dir` bundle only. Without this, 1.4 is a window, not a property.
- [x] 1.2 Index at `runs/<run_id>/input/manifest_index_<token>.json`; task run ids `<run_id>_<token>_success` / `<run_id>_<token>_controlled_fail`, so their manifests satisfy `workers/shud_runtime/cli.py`'s gate unchanged. **Do not modify the worker.**
- [x] 1.3 Red first: two live submits with the same configured `run_id` against one workspace (with `sbatch` stubbed) produce pairwise-disjoint index paths, task run ids, manifest paths and array log dirs; each submitted task resolves the manifest its own submission wrote.
- [x] 1.4 Red first: submission A succeeds, submission B's `sbatch` fails → B's cleanup removes only B's paths and A's index and manifests are byte-identical afterwards. Cleanup derives its list from the paths written, not from a `run_id` formula.
- [x] 1.5 Every site that re-derives `f"{config.run_id}_success"` / `_controlled_fail` reads the written tasks instead: `slurm_validation.py:1632`, `_blocked_partial_success` (`:1772`), `_qc_blocking_evidence` (`:1841`), `:1854`, and the cleanup formula (`:1049-1054`). `_retry_cancel_evidence` (`:1788-1816`) derives no run id — do not touch it. Assert an evidence run id that does not exist on disk cannot be produced.
- [x] 1.5b The existing suite's stable-path helpers must be rederived, not loosened: `tests/test_production_slurm_validation.py:17-21` (`_PRODUCTION_ARRAY_INDEX_STEM = "manifest_index"` and `_neutral_array_log_dir`, ~21 call sites, deliberately independent of the production helpers), `:630-633`, `:1434-1436`, `:1623-1625`, and the cleanup test at `:2121-2223` which intercepts by `path.name == "manifest_index.json"`. They must derive the stem and run ids from the index the run actually wrote. Also state what `validate_slurm:386-393` (the post-cleanup lane-dir index rewrite) does under the token scheme.
- [x] 1.6 Regression: the dry-run / fake-Slurm lane's evidence is byte-identical to master (nothing is shared there, so nothing is scoped).
- [x] 1.7 `docs/validation/production-closure.md`: `--force` governs the evidence bundle only (`:76-78`); the live index path is submission-scoped, not the fixed `<workspace_root>/runs/<run_id>/input/manifest_index.json` (`:80-90`); the artefact list names the scoped index (`:101-103`). `docs/VALIDATION.md` mentions neither `validate-slurm` nor this path — do not edit it.

## 2. #1909 — single-submit refuses every production array type (design D2)

- [ ] 2.1 Red first: `submit_job(job_type="save_state_snapshot_array")` with a **complete authorized** manifest (valid cycle identity and manifest index path, so the refusal cannot be an accident of log-dir binding) raises before any scheduler call, with `details["endpoint"] == "/api/v1/slurm/job-arrays"`; assert no sbatch invocation.
- [ ] 2.2 `ARRAY_CAPABLE_JOB_TYPES` becomes a derivation: reverse-look `PRODUCTION_ARRAY_TEMPLATE_NAMES` through `DEFAULT_JOB_TYPE_TEMPLATES` ∪ `settings.job_type_templates`, so a deployment override (`services/slurm_gateway/config.py:57,65-68`) can only widen the refused set. It has exactly one consumer (`real_backend.py:263`), so an instance-level derivation is safe. Pin the agreement between the derived set and `tests/test_slurm_array_contract.py:55-60`'s `_PRODUCTION_ARRAY_JOB_TYPES`.
- [ ] 2.3 Parametrize the existing rejection test over all four types; add a positive test that `submit_job_array("save_state_snapshot_array")` still submits with `array_spec` (not `TemplateNotFound`).
- [ ] 2.4 `uv run pytest -q tests/test_real_slurm_gateway.py -k array_capable_job_type_rejected_from_single_submit` green (the issue's named command).

## 3. #2308 — render gateway errors on responses, keep events raw (design D3)

- [ ] 3.1 Red first: with the gateway raising a command error whose `command[0]` is the configured `slurm_bin_path` and whose stderr snippet contains an absolute workspace path, `POST /api/v1/runs/{run_id}/cancel` returns a body whose failed-job entry carries `[local-path]` in the stderr snippet and no scheduler binary path in the command vector.
- [ ] 3.2 Same for the unproven-cancellation branch (blocked jobs) and for `GET /api/v1/queue/depth`'s upstream-error body.
- [ ] 3.3 **In the same tests**: the persisted `slurm_cancellation_gap` / `cancel_failed` event's `details.error` still carries the raw command vector and stderr text. The rendered payload must not be shared with the event. The reason to pin this is functional, not stylistic: `[local-path]` is deliberately persisted evidence elsewhere, but retry's runtime-root recovery reads the persisted `gateway_response.manifest` subtree.
- [ ] 3.4 The upstream-error response keys are unchanged; `openapi/nhms.v1.yaml` is not touched; `_api_error` is not touched.
- [ ] 3.5 `_unproven_slurm_cancellation_payload`'s `gateway_response` is settled, not a probe: the dumped `SlurmJobRecord.manifest` carries `workspace_dir` / `manifest_index_path` / `array_log_dir` for array submissions (`real_backend.py:390-400`). Render the **response** copy; keep the **event** copy raw, and assert that retry's runtime-root recovery can still read `("gateway_response", "manifest")` off the persisted event (`services/orchestrator/retry.py:1421`, field set `:89-95`). Do not try to settle this with a live probe — `cancel_job` only returns proven records (`real_backend.py:474-484`), so the branch is unreachable against the real backend.
- [ ] 3.6 Existing secrets-redaction assertions stay green.

## 4. Verification (Evidence Floor)

- [ ] 4.1 Local: `uv run ruff check .` clean.
- [ ] 4.2 Local: `uv run pytest -q` over the touched suites — production-closure validation, real Slurm gateway, Slurm array contract, the ops route suites, and `tests/test_select_ci_tests.py` if routing changed.
- [ ] 4.3 Local: `openspec validate slurm-submission-isolation-and-error-rendering --strict --no-interactive`.
- [ ] 4.4 **node-27 oracle** (`export PATH=$HOME/.local/bin:$PATH`, `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`): the same selection at the frozen review head; receipt (host, sha, counts) in the PR body.
- [ ] 4.5 CI green on the PR.
- [ ] 4.6 **Oracle-blocked, declared**: no live node-22 submission is performed (pre-maintenance freeze: no `uv sync`, no bare `uv run`). #1908's concurrency property is proven structurally by disjoint paths, not by a cluster run or a mocked mutex; the PR body says so plainly.
- [ ] 4.7 Issues #1908, #1909, #2308 acceptance boxes each mapped to a named test in the PR body, with any deviation recorded.

### #1908 evidence (implementer pass 1)

All commands from the worktree root with `uv`.

**RED** (`git checkout HEAD -- services/production_closure/slurm_validation.py`, test file at its new state):

```
$ uv run pytest -q tests/test_production_slurm_validation.py -k "two_live_submissions or failed_second_submission or colliding_submission_token or every_submission_token_is_taken or stale_stable_path or scoped_runtime_manifest"
E   AssertionError: expected exactly one submission index under runs/dualsubmit/input, found []
E   AssertionError: expected exactly one submission index under runs/cleanupdisjoint/input, found []
E   AssertionError: expected exactly one submission index under runs/tokencollision/input, found []
E   AttributeError: module 'services.production_closure.slurm_validation' has no attribute 'MAX_SUBMISSION_TOKEN_ATTEMPTS'
E   AssertionError: assert {'cycle_time'...ours': 6, ...} == {'old': True}   # --force overwrote the pre-existing manifest
E   AttributeError: module 'services.production_closure.slurm_validation' has no attribute 'write_bytes_no_follow_exclusive'
FAILED ...::test_two_live_submissions_with_the_same_run_id_stay_disjoint
FAILED ...::test_failed_second_submission_cleanup_leaves_the_first_submission_intact
FAILED ...::test_live_submit_retries_a_colliding_submission_token
FAILED ...::test_live_submit_fails_closed_when_every_submission_token_is_taken
FAILED ...::test_validate_slurm_live_submit_leaves_a_stale_stable_path_runtime_manifest_untouched
FAILED ...::test_validate_slurm_live_submit_refuses_to_overwrite_a_scoped_runtime_manifest_with_force
6 failed, 69 deselected in 0.49s
```

**GREEN**:

```
$ uv run ruff check .
All checks passed!

$ uv run pytest -q tests/test_production_slurm_validation.py
75 passed in 1.97s

$ uv run pytest -q tests/test_slurm_array_contract.py tests/test_real_slurm_gateway.py tests/test_job_array.py tests/test_orchestration_chain.py
866 passed, 1 skipped in 35.82s
```

**1.6 byte-identity** — the fake lane run twice into the same output root, once at `HEAD`
and once with this change, diffed: identical except `environment.json`'s `captured_at`
wall clock. Nothing is written under the shared workspace in that lane.

```
$ diff -r -I '"captured_at"' <master fake-lane artifacts> <changed fake-lane artifacts>
$ diff <master stdout.json> <changed stdout.json>
(no output)
```

**1.5b — what `validate_slurm`'s post-cleanup lane-dir rewrite does under the token scheme**:
it stays token-free and lane-local. After a failed `sbatch` whose cleanup succeeded,
`_write_manifest_index(..., use_shared_workspace_inputs=False)` re-writes
`<lane_dir>/manifest_index.json` and `<lane_dir>/runs/<run_id>_success|_controlled_fail/input/manifest.json`
through the overwriting `_write_bytes` path; the lane index is already in `_created_paths`, so
that overwrite is the documented `--force`/rerun semantics for the evidence bundle, not a
shared-input overwrite. `validate_slurm` then derives the evidence run ids from the *final*
`manifest_tasks`, which is why an evidence run id names a manifest that exists on disk whenever
cleanup fully succeeds or does not run at all: scoped ids when the submission's inputs survive,
lane-local ids after a successful cleanup. A *partially* failed cleanup (index unlink fails,
manifests unlink -- the shape of `test_validate_slurm_submit_reports_shared_input_cleanup_failure`)
leaves `shared_runtime_inputs_cleaned` false, so no lane rewrite happens and the evidence names
scoped ids whose manifests are already gone. That is pre-existing behaviour, unchanged here and
out of this change's scope.
The rendered sbatch script is **not** re-rendered, so it keeps pointing at the scoped index
that was submitted -- which is why the blocked-bundle test rederives that stem from the
submission's own recorded cleanup list rather than from a glob.

**CI routing (0.2)** — no new test module, but `tests/test_production_slurm_validation.py`
gained a top-level import of `workers.shud_runtime.cli` to assert the worker's manifest
safety gate directly. `tests/test_select_ci_tests.py`'s directory-rule importer audit red'd
on the resulting gap, so the suite was added to `scripts/select_ci_tests.py`'s
`workers/shud_runtime/**` rule and the `workers/shud_runtime/runtime.py` selection pin was
extended to match. `.large-file-guard.json` gained
`services/production_closure/slurm_validation.py` (2868) and
`tests/test_production_slurm_validation.py` (2844); `scripts/select_ci_tests.py` and
`tests/test_select_ci_tests.py` were already excluded.

**Test helpers rederived, not loosened** (`tests/test_production_slurm_validation.py`):
`_neutral_array_log_dir` gained an `index_stem` argument and still defaults to the stable
`manifest_index` for the dry-run lane; `_live_index_path` / `_live_array_log_dir` /
`_live_runtime_manifest_paths` / `_cleaned_index_stem` derive the live lane's stem and run ids
from the index the run actually wrote (or, post-cleanup, from the submission's own cleanup
record). The three hostile-leaf array-log-dir tests and the symlinked-runtime-manifest test
plant their fixtures through `_hook_index_claim`, i.e. at the instant the run claims its token,
so they still exercise the same refusal on the path the run really uses.
