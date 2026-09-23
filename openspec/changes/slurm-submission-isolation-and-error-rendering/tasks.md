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

- [x] 2.1 Red first: `submit_job(job_type="save_state_snapshot_array")` with a **complete authorized** manifest (valid cycle identity and manifest index path, so the refusal cannot be an accident of log-dir binding) raises before any scheduler call, with `details["endpoint"] == "/api/v1/slurm/job-arrays"`; assert no sbatch invocation.
- [x] 2.2 `ARRAY_CAPABLE_JOB_TYPES` becomes a derivation: reverse-look `PRODUCTION_ARRAY_TEMPLATE_NAMES` through `DEFAULT_JOB_TYPE_TEMPLATES` ∪ `settings.job_type_templates`, so a deployment override (`services/slurm_gateway/config.py:57,65-68`) can only widen the refused set. It has exactly one consumer (`real_backend.py:263`), so an instance-level derivation is safe. Pin the agreement between the derived set and `tests/test_slurm_array_contract.py:55-60`'s `_PRODUCTION_ARRAY_JOB_TYPES`.
- [x] 2.3 Parametrize the existing rejection test over all four types; add a positive test that `submit_job_array("save_state_snapshot_array")` still submits with `array_spec` (not `TemplateNotFound`).
- [x] 2.4 `uv run pytest -q tests/test_real_slurm_gateway.py -k array_capable_job_type_rejected_from_single_submit` green (the issue's named command).

### #1909 evidence (implementer pass 2)

Pre-implementation checks the design asks for: `ARRAY_CAPABLE_JOB_TYPES` had exactly one
consumer (`real_backend.py:263`, `submit_job`) and no other module, test or doc referenced it;
`tests/test_slurm_array_contract.py:55-60`'s `_PRODUCTION_ARRAY_JOB_TYPES` already enumerated
the four, so the derived set is pinned against that enumeration rather than a new literal.

The union is **pairwise over `(job_type, template)` pairs**, not a dict merge: merging
`settings.job_type_templates` over `DEFAULT_JOB_TYPE_TEMPLATES` would let a deployment
*narrow* the refused set by remapping an array job type to another template — and would
have silently un-refused `run_shud_forecast_array` in `tests/test_slurm_route_contract.py:134`
and `tests/test_real_slurm_gateway.py`'s `_gateway`, both of which remap it to a stub template.

**RED** (tests at their new state, `services/slurm_gateway/real_backend.py` unmodified):

```
$ uv run pytest -q tests/test_real_slurm_gateway.py tests/test_slurm_array_contract.py \
    -k "array_capable_job_type_rejected_from_single_submit or deployment_mapped_array_template \
        or submit_job_array_still_submits_save_state or refusal_set_is_derived or override_only_widens"
E       AssertionError: subprocess.run must not be called for array-capable single submit
E       AssertionError: subprocess.run must not be called for array-capable single submit
E       AssertionError: subprocess.run must not be called for array-capable single submit
E       AttributeError: module 'services.slurm_gateway.real_backend' has no attribute 'array_capable_job_types'
E       AttributeError: module 'services.slurm_gateway.real_backend' has no attribute 'array_capable_job_types'
FAILED ...::test_array_capable_job_type_rejected_from_single_submit[save_state_snapshot_array]
FAILED ...::test_array_capable_job_type_rejected_from_single_submit_despite_template_override
FAILED ...::test_deployment_mapped_array_template_is_refused_from_single_submit
FAILED ...::test_single_submit_refusal_set_is_derived_from_the_production_array_templates
FAILED ...::test_deployment_job_type_template_override_only_widens_the_refusal_set
5 failed, 4 passed, 358 deselected in 0.57s
```

The three `subprocess.run` failures are the proof the refused request was **fully authorized**:
the submit reached `_submit_rendered_script` (`real_backend.py:285` -> `:1123` -> `:1216`), i.e. it
passed manifest validation, the production array log-dir binding and template rendering. The
other three parametrizations (`produce_forcing_array`, `run_shud_forecast_array`,
`parse_output_array`) are among the 4 passed — they were already in the old literal.

**GREEN**:

```
$ uv run ruff check .
All checks passed!

$ uv run pytest -q tests/test_real_slurm_gateway.py -k array_capable_job_type_rejected_from_single_submit
5 passed, 280 deselected in 0.22s

$ uv run pytest -q tests/test_real_slurm_gateway.py tests/test_slurm_array_contract.py
367 passed in 6.88s

$ uv run pytest -q tests/test_slurm_route_contract.py tests/test_production_slurm_validation.py \
    tests/test_job_array.py tests/test_orchestration_chain.py tests/test_gateway.py \
    tests/test_slurm_route_security_contract.py
647 passed, 1 skipped in 29.86s

$ uv run pytest -q tests/test_slurm_route_contract.py -k array_capable
1 passed, 23 deselected in 0.08s

$ openspec validate slurm-submission-isolation-and-error-rendering --strict --no-interactive
Change 'slurm-submission-isolation-and-error-rendering' is valid
```

`tests/test_real_slurm_gateway.py`'s parametrization is spelled out from
`DEFAULT_JOB_TYPE_TEMPLATES` x `PRODUCTION_ARRAY_TEMPLATE_NAMES` rather than copied, so a fifth
array template parametrizes the refusal test without an edit; the contract file's hand list stays
the pin that must be updated (and fails loudly if it is not).

**CI routing (0.2)** — no new test module; `scripts/select_ci_tests.py` already selects
`tests/test_real_slurm_gateway.py`, `tests/test_slurm_array_contract.py` and
`tests/test_slurm_route_contract.py` for a `services/slurm_gateway/real_backend.py` diff
(52 files selected), so no routing change. `.large-file-guard.json` gained
`services/slurm_gateway/real_backend.py` (2379), `tests/test_real_slurm_gateway.py` (5287)
and `tests/test_slurm_array_contract.py` (1300).

## 3. #2308 — render gateway errors on responses, keep events raw (design D3)

- [x] 3.1 Red first: with the gateway raising a command error whose `command[0]` is the configured `slurm_bin_path` and whose stderr snippet contains an absolute workspace path, `POST /api/v1/runs/{run_id}/cancel` returns a body whose failed-job entry carries `[local-path]` in the stderr snippet and no scheduler binary path in the command vector.
- [x] 3.2 Same for the unproven-cancellation branch (blocked jobs) and for `GET /api/v1/queue/depth`'s upstream-error body.
- [x] 3.3 **In the same tests**: the persisted `slurm_cancellation_gap` / `cancel_failed` event's `details.error` still carries the raw command vector and stderr text. The rendered payload must not be shared with the event. Correction folded in from the implementation pass: retry's runtime-root scan filters `event_type == "submission"` (`services/orchestrator/retry.py:1382-1386`), so it never reads these two event types — its readers are used as the honest oracle for "a real root, not a placeholder", not as a consumer of these rows. The binding reason for this route is operator diagnosis.
- [x] 3.4 The upstream-error response keys are unchanged; `openapi/nhms.v1.yaml` is not touched; `_api_error` is not touched.
- [x] 3.5 `_unproven_slurm_cancellation_payload`'s `gateway_response` is settled, not a probe: the dumped `SlurmJobRecord.manifest` carries `workspace_dir` / `manifest_index_path` / `array_log_dir` for array submissions (`real_backend.py:390-400`). Render the **response** copy; keep the **event** copy raw, and assert that retry's runtime-root recovery can still read `("gateway_response", "manifest")` off the persisted event (`services/orchestrator/retry.py:1421`, field set `:89-95`). Do not try to settle this with a live probe — `cancel_job` only returns proven records (`real_backend.py:474-484`), so the branch is unreachable against the real backend.
- [x] 3.6 Existing secrets-redaction assertions stay green.

### #2308 evidence (implementer pass 3)

The split is three call sites in `apps/api/routes/pipeline.py`, one raw shape and one
rendered shape:

- `_raw_slurm_gateway_error(error)` (new) builds the secrets-only dict once per
  `SlurmGatewayError`; `cancel_run` hands it straight to both `insert_event` calls
  (`details["error"]`) and hands the SAME dict to `_slurm_cancellation_gap_payload`,
  which returns the wire entry with `message` through `_public_error_message` and
  `details` through `_public_evidence`. `_public_evidence` rebuilds every mapping and
  list, so the two payloads share no mutable object.
- `_unproven_slurm_cancellation_payload` now renders (`_public_evidence(_safe_redacted_payload(response))`);
  the `insert_event` beside it keeps `_safe_redacted_payload(cancellation)` verbatim.
- `queue_depth`'s two `SlurmGatewayError` arms go through the new
  `_public_slurm_gateway_api_error`. `_api_error` (RetryError) is untouched, and
  `openapi/nhms.v1.yaml` is untouched — the `SlurmGatewayUpstreamError` keys do not move.

**Response-shape note (3.4, disclosed):** the shared renderer collapses a whole mapping
under a *sensitive* key to the scalar `"[redacted]"` instead of recursing (it is
`_sanitize_public_field`'s existing rule, the same one `runtime_root_resolution` gets).
So in `test_unproven_cancel_gateway_response_redacts_response_and_event_details` the
response's `gateway_response.auth` subtree becomes the string `"[redacted]"` while the
persisted event keeps the structured, key-wise-redacted mapping — the split showing
itself. No real `SlurmCommandError` detail key (`command`, `returncode`, `stdout`,
`stderr`, `timeout_seconds`) is sensitive, so production bodies only lose host paths.

**RED** (tests at their new state, `apps/api/routes/pipeline.py` unmodified):

```
$ uv run pytest -q tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py \
    -k "renders_gateway_host_paths or renders_manifest_roots_on_the_wire or unproven_cancel_gateway_response_redacts"
E   AssertionError: assert {'errors': [{'status': '[redacted]'}], 'issuer_url': 'https://idp.example.invalid/auth', ...} == '[redacted]'
E   AssertionError: assert 'Slurm comman...cancel_paths.' == 'Slurm comman...[local-path].'
E   AssertionError: assert 'Slurm comman...locked_paths.' == 'Slurm comman...[local-path].'
E   AssertionError: assert {'array_log_d...t-store', ...} == {'array_log_d...l-path]', ...}
      {'workspace_dir': '/srv/nhms/workspace'} != {'workspace_dir': '[local-path]'}
      {'manifest_index_path': '/srv/nhms/workspace/runs/run_array_roots/input/manifest_index_20260522T010203040506.json'} != {'manifest_index_path': '[local-path]'}
E   AssertionError: assert 'Slurm comman...rkspace/runs.' == 'Slurm comman...[local-path].'
E   AssertionError: assert 'Slurm comman...rkspace/runs.' == 'Slurm comman...[local-path].'
FAILED tests/test_retry_cancel_consistency.py::test_unproven_cancel_gateway_response_redacts_response_and_event_details
FAILED tests/test_retry_cancel_consistency.py::test_cancel_failure_response_renders_gateway_host_paths_while_the_event_stays_raw
FAILED tests/test_retry_cancel_consistency.py::test_cancel_blocked_response_renders_gateway_host_paths_while_the_event_stays_raw
FAILED tests/test_retry_cancel_consistency.py::test_unproven_cancellation_renders_manifest_roots_on_the_wire_and_keeps_them_raw_in_the_event
FAILED tests/test_monitoring_api.py::test_queue_depth_direct_method_error_renders_gateway_host_paths
FAILED tests/test_monitoring_api.py::test_queue_depth_list_jobs_error_renders_gateway_host_paths
6 failed, 129 deselected in 0.37s
```

**GREEN**:

```
$ uv run ruff check .
All checks passed!

$ uv run pytest -q tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py \
    -k "renders_gateway_host_paths or renders_manifest_roots_on_the_wire or unproven_cancel_gateway_response_redacts"
6 passed, 129 deselected in 0.37s

$ uv run pytest -q tests/test_monitoring_api.py tests/test_retry_cancel_consistency.py \
    tests/test_api_contract_pipeline_ops.py tests/test_retry.py \
    tests/test_openapi_response_conformance.py tests/test_api_contract.py
411 passed in 7.54s

$ uv run pytest -q tests/test_production_slurm_validation.py tests/test_real_slurm_gateway.py \
    tests/test_slurm_array_contract.py        # passes 1-2 still green
442 passed in 9.34s

$ printf 'apps/api/routes/pipeline.py\ntests/test_retry_cancel_consistency.py\ntests/test_monitoring_api.py\n' \
    | uv run python scripts/select_ci_tests.py | xargs uv run pytest -q
# the 15 suites CI selects for this diff: test_api, test_api_contract,
# test_api_contract_pipeline_ops, test_api_contract_resources,
# test_file_journal_read_blocked_consumers, test_monitoring_api,
# test_node27_connection_attribution(+_delegated), test_openapi_response_conformance,
# test_path_canonicalization_family_guard, test_pipeline_logs_artifacts,
# test_pipeline_ops_identity_envelope, test_retry_cancel_consistency,
# test_river_segment_write_surface_scan, test_select_ci_tests
1269 passed in 321.75s (0:05:21)

$ uv run pytest -q tests/test_select_ci_tests.py && uv run pytest -q tests/test_orchestration_chain.py \
    tests/test_gateway.py tests/test_slurm_route_contract.py tests/test_api.py \
    tests/test_api_errors_logging.py tests/test_runtime_mode.py
775 passed in 284.14s (0:04:44)

$ openspec validate slurm-submission-isolation-and-error-rendering --strict --no-interactive
Change 'slurm-submission-isolation-and-error-rendering' is valid
```

**CI routing (0.2)** — **corrected in review fix pass 1: this claim was wrong.**
`tests/test_retry_cancel_consistency.py` was in this PR's 1269-test selection only
because the test file itself was in the diff; measured on the route alone,
`echo apps/api/routes/pipeline.py | uv run python scripts/select_ci_tests.py`
returned 11 suites and **not** that one — so a later diff touching only
`apps/api/routes/pipeline.py` would never have run the sole oracle for #2308's
cancel-side rendered-on-wire / raw-in-event invariant. The narrow
`apps/api/routes/pipeline.py` rule in `scripts/select_ci_tests.py` now names it,
with a routing pin in `tests/test_select_ci_tests.py`
(`test_select_tests_routes_the_cancel_route_to_its_rendered_versus_raw_oracle`).
`.large-file-guard.json` gained `tests/test_retry_cancel_consistency.py` (1620) and
`tests/test_monitoring_api.py` (3590); `apps/api/routes/pipeline.py` was already excluded.

**Fixture-rationale deviation (for 4.7):** design D3 and spec say the event must stay raw
because "retry's runtime-root recovery reads `("gateway_response", "manifest")` off
persisted events". The path tuple at `retry.py:1421` is real, but the scan around it
(`_event_runtime_root_candidates`) filters `PipelineEvent.event_type == "submission"`, so
it never reaches the `slurm_cancellation_gap` / `cancel_failed` rows this route writes.
The required behaviour is unaffected (events stay raw; operator diagnosis is the reason
that does bind this route). Task 3.5's assertion is therefore written with retry's real
readers — `_mapping_at(event.details, ("gateway_response", "manifest"))` plus
`_has_runtime_root_field` — over the persisted event, with the rendered copy asserted as
the shape those readers could not use; no `submission` event is planted and no end-to-end
retry is run, since either would be staged rather than observed.

**Wire-shape deviation (3.6 / 4.7):** #2308's acceptance says the existing secrets
assertions stay green, and they do — but seven response-side `gateway_response.auth.*`
assertions in `test_unproven_cancel_gateway_response_redacts_response_and_event_details`
were rewritten into a single `auth == "[redacted]"`, with the structured, key-wise
assertions moved to the event side. Coverage is preserved on both copies and the wire is
strictly safer (the whole subtree collapses instead of being walked), but this **changes
the JSON type of `gateway_response.auth` on the response** from object to string. It is a
behaviour change of the public body, disclosed here rather than only in the PR body:
`SlurmGatewayUpstreamError`'s keys are unchanged, so `openapi/nhms.v1.yaml` is untouched,
and the branch that produces it is unreachable against the real backend
(`cancel_job` only returns proven records, `real_backend.py:474-484`).

## Review fix pass 1 (cross-review round 1 findings)

- [x] F1 (P1) The rendered sbatch script was still submitted from the stable, overwritable
  `lane_dir` path, so an interleaved second submission with `--force` could replace it in
  place and submission A would hand `sbatch` **B's** `NHMS_MANIFEST_INDEX`. The submitted
  copy is now submission-scoped at
  `runs/<run_id>/input/rendered_run_shud_forecast_array_<token>.sbatch`, derived once from
  the claimed index in `_write_rendered_script` and carried into `_real_accounting` instead
  of being recomputed at submit time; it is created exclusively and is part of
  `_shared_runtime_input_paths`, so failed-`sbatch` cleanup covers it. The stable `lane_dir`
  copy stays as evidence. Placement under the workspace (not `lane_dir`) is required:
  `_cleanup_shared_runtime_inputs` refuses any path outside `workspace_root`, and keeping it
  out of `lane_dir` keeps `--force`/`_created_paths` confined to the evidence bundle.
  Discriminating test: `test_interleaved_live_submissions_submit_their_own_rendered_script`
  re-enters `validate_slurm` for B **inside A's `sbatch` stub**; the sequential disjointness
  test cannot see this defect. Spec, design D1's artefact table and
  `docs/validation/production-closure.md` now name the script.
- [x] F2 (P2) `apps/api/routes/pipeline.py` did not select
  `tests/test_retry_cancel_consistency.py`; measured, fixed and pinned — see the corrected
  #2308 CI-routing note above.
- [x] F3 (P3) The "fully authorized" claim for #1909 was asserted by construction. Added the
  mutation control `test_authorized_array_manifest_reaches_sbatch_once_the_refusal_is_removed`:
  with `services.slurm_gateway.real_backend.array_capable_job_types` stubbed to an empty set,
  the same four requests reach the `sbatch` stub, so the manifest really was authorizable.
- [x] F4 (P3) A claimed-then-refused submission leaves its index behind.
  `test_validate_slurm_live_submit_refuses_to_overwrite_a_scoped_runtime_manifest_with_force`
  now pins that post-state as intentional — retention/GC of per-submission inputs is a
  declared non-goal (design D1's disclosed residual) — together with the fact that nothing
  else of that submission, including the submitted script, was written.
- [x] F5 (P3) Deviation recorded above.
- [x] F6 (P3, cross-review round 2) The routing comment added by F2 stated a false reason —
  it claimed `tests/test_retry_cancel_consistency.py`'s imports are function-local, but
  `tests/test_retry_cancel_consistency.py:17` is a module-level
  `from apps.api.routes import pipeline as pipeline_routes`, on master too. The importer
  index does hold that edge; the gate is on the **query** side
  (`scripts/select_ci_tests.py:5374` only consults it when the changed path is itself a
  suite), and `apps/api/routes/pipeline.py` is in neither `GUARDED_MODULE_CLOSURES` nor
  `DIRECTORY_RULE_AUDIT_PATHS`, so no closure guard would have forced the suite on. Comment
  and docstring corrected; no rule entry, assertion or behaviour changed.

### Fix-pass test receipts (rounds 1–2)

```text
$ uv run pytest -q tests/test_production_slurm_validation.py   # F1, before the source fix
FAILED tests/test_production_slurm_validation.py::test_interleaved_live_submissions_submit_their_own_rendered_script
E  assert 'export NHMS_MANIFEST_INDEX=.../runs/interleaved/input/manifest_index_<Ta>.json'
   in '#!/usr/bin/env bash\n#SBATCH --job-name=nhms_run_shud_forecast_array\n...'
1 failed, 75 deselected in 0.41s

$ uv run pytest -q tests/test_select_ci_tests.py -k cancel_route   # F2, before the rule entry
E  AssertionError: assert 'tests/test_retry_cancel_consistency.py' in [...]
1 failed, 775 deselected in 6.17s

$ uv run pytest -q tests/test_production_slurm_validation.py      # after
77 passed in 2.23s
$ uv run pytest -q tests/test_select_ci_tests.py                  # after
776 passed in 432.39s
$ uv run pytest -q tests/test_production_slurm_validation.py tests/test_real_slurm_gateway.py \
    tests/test_slurm_array_contract.py tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py
583 passed in 12.74s
$ uv run pytest -q tests/test_select_ci_tests.py::test_select_tests_routes_the_cancel_route_to_its_rendered_versus_raw_oracle
1 passed in 5.79s                                                 # F6, comment-only change
$ echo apps/api/routes/pipeline.py | uv run python scripts/select_ci_tests.py | grep retry_cancel
tests/test_retry_cancel_consistency.py
```

## 4. Verification (Evidence Floor)

- [x] 4.1 Local: `uv run ruff check .` clean.
- [x] 4.2 Local: `uv run pytest -q` over the touched suites — production-closure validation, real Slurm gateway, Slurm array contract, the ops route suites, and `tests/test_select_ci_tests.py` if routing changed.
- [x] 4.3 Local: `openspec validate slurm-submission-isolation-and-error-rendering --strict --no-interactive`.
- [x] 4.4 **node-27 oracle** (`export PATH=$HOME/.local/bin:$PATH`, `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp`): the full CI selection (73 suites, `git diff --name-only f48f599c6...a04703bdf | select_ci_tests.py`) at the frozen review head `a04703bdf`, in a detached worktree at `/home/nwm/tmp/wt-e2r2`:

  ```text
  nwm@210.77.77.27  a04703bd  uv run pytest -q -p no:cacheprovider <73 suites>
  4347 passed, 8 skipped, 1 warning in 1342.60s (0:22:22)   exit 0
  ```

  The three fix-pass tests are inside that run (`tests/test_production_slurm_validation.py`,
  `tests/test_real_slurm_gateway.py`), as is `tests/test_retry_cancel_consistency.py`.
- [x] 4.5 CI: **all 4347 tests pass on the runner too** — the "Unit Tests" job printed
  `4347 passed, 8 skipped` (identical counts to node-27) and then the pytest process died at
  interpreter shutdown, four seconds after the summary, with the same selection failing as
  `SIGSEGV` at `a04703bdf` and `SIGABRT` at `9ff2b03b8`. Zero test failures in either run; the
  nondeterministic signal and the clean node-27 exit on the identical 73-suite list at the
  identical commit place the fault in the runner's native teardown, not in this change. This
  PR's diff is the first to route the pyproj/PROJ-loading `tests/test_production_*` family
  into the targeted job (the merged batch-E1 selection, 117 files / 6524 tests, did not
  include it and exited cleanly), so the trigger is the selection's composition, not its size.
  Filed as a follow-up rather than worked around here.
- [x] 4.6 **Oracle-blocked, declared**: no live node-22 submission is performed (pre-maintenance freeze: no `uv sync`, no bare `uv run`). #1908's concurrency property is proven structurally by disjoint paths, not by a cluster run or a mocked mutex; the PR body says so plainly.
- [x] 4.7 Issues #1908, #1909, #2308 acceptance boxes each mapped to a named test in the PR body, with any deviation recorded.

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
