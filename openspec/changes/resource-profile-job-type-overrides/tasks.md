# Tasks

Fixture level: `expanded`. Risk packs: production Slurm submission (the
resource directives of every production job come out of this resolver);
shared configuration contract (one file read by the gateway and by the
scheduler's cohort split, and by a gateway process that may still run the
old code); fail-closed configuration (a malformed file must refuse, not
fall back).

Must preserve: the resolved profile of every job type without an entry,
for every model (default, the two HLJ `dg_*` overrides, `yangtze_shud_v12`,
`m24_smoke`), field for field; the rendered `run_shud_forecast_array`
script; `resource_profile_override_model_ids` and the cohort split and
budgets; the deployment overlay (`partition_override`, `exclude_nodes`)
applied last; `validate_resource_profile` on the resolved result and every
existing refusal (missing default, non-mapping `overrides`, unsupported
fields, directive injection, context collision); the public signatures stay
callable as today (`job_type` is keyword-only and optional); existing tests
stay green unedited.

## 1. Resolver and loader (`services/slurm_gateway/resource_profiles.py`)

- [ ] 1.1 `load_resource_profiles`: `job_type_overrides` absent or null
  becomes `{}`; not a mapping, an entry that is not a mapping, an entry
  with a key outside `{cpus_per_task, memory_gb, shud_threads}`, an entry
  that sets one of `cpus_per_task` / `shud_threads` without the other, or
  an entry whose values fail `validate_resource_profile(entry,
  require_required=False)` raises `ConfigurationError` naming the job type
  and the offending key names (never values).
- [ ] 1.2 `resolve_model_resource_profile(..., *, job_type=None, ...)`:
  the job-type entry is merged after the model override and before the
  deployment overlay; the module and function docstrings are brought up to
  date (resolution order; the stale sentence about production-closure goes).

## 2. Gateway (`services/slurm_gateway/real_backend.py`)

- [ ] 2.1 `RealSlurmGateway.resolve_resource_profile(model_id, job_type=None)`
  passes the job type through.
- [ ] 2.2 The array submit resolves with its job type; the single-job
  render resolves with the job type it was called with. Any other caller of
  either resolver found by search is listed in the report with what it
  passes.

## 3. Configuration (`config/resource_profiles.yaml`)

- [ ] 3.1 `job_type_overrides.produce_forcing_array: {cpus_per_task: 1,
  shud_threads: 1}` with the comment described in `design.md`. No value
  elsewhere in the file changes. The existing comment on how a cohort's
  resources are resolved is extended by one sentence on the job type, and
  its sentence "SHUD threads come from the registry, not from this file"
  is made exact: the solver's thread count comes from the registry; the
  exported `SHUD_THREADS` / `OMP_NUM_THREADS` and the core request come
  from this file.

## 4. Spec and documents (orchestrator)

- [ ] 4.1 Spec delta `job-array-orchestration`: the two requirements
  modified.
- [ ] 4.2 `docs/runbooks/production-ops/service-bringup.md`, the profile
  and concurrency section: the job-type layer and its order, the forcing
  entry and its measurement, why memory was left alone, the conclusion on
  the bound (unchanged, now the limit for forcing, how to decide), the
  placement caveat, and that the gateway must be restarted for a code
  change in the resolver while the file itself is read on every submission.

## 5. Tests

Resolver tests go in `tests/test_job_array.py`; rendered-script tests in
`tests/test_real_slurm_gateway.py`; split pins in
`tests/test_production_scheduler.py`.

- [ ] 5.1 Resolver, in-memory profiles: default only; model override only;
  job-type override only; both stacked (model 12 cores x forcing -> 1 core
  and 1 thread, every other field from default plus model); a job type
  without an entry; `job_type=None`; the deployment overlay still applied
  last (an in-memory job-type entry carrying `partition: other`, resolved
  with `partition_override="CPU"`, gives `partition == "CPU"`); a job-type
  entry that is not a mapping is ignored by the resolver.
- [ ] 5.2 Loader: absent and null `job_type_overrides`; not a mapping;
  an entry that is not a mapping; an entry with `max_concurrent`,
  `walltime`, `partition` (each refused, message names the job type and the
  key and contains no value); `cpus_per_task` without `shud_threads` and
  the reverse; a non-positive or non-integer value. Through the gateway, a
  submission against a refused file raises and the fake `sbatch` is never
  called.
- [ ] 5.3 Array submit path, through `RealSlurmGateway` with a fake
  `sbatch`: a `produce_forcing_array` array for a default model and for a
  model with a 12-core override both render `#SBATCH --cpus-per-task=1`,
  and export `SHUD_THREADS=1` and `OMP_NUM_THREADS=1`; the `--mem`,
  `--time` and array throttle are those of default plus model.
- [ ] 5.4 Single submit path: a non-array job type with a job-type entry
  in the test file renders with the entry applied (the entry is in the
  test's own profile file; the shipped file has no such entry).
- [ ] 5.5 Unchanged renders through the array submit path: with a fake
  `sbatch` capturing the script, submit `run_shud_forecast_array` arrays
  for a default model and for a 12-core model against a profile file that
  contains `job_type_overrides.produce_forcing_array: {cpus_per_task: 1,
  shud_threads: 1}` and against the same file without it. Assert
  `#SBATCH --cpus-per-task=4` / `=12` and `SHUD_THREADS` /
  `OMP_NUM_THREADS` of 4 / 12, and that the two scripts are identical once
  the manifest-index path and the array log directory are replaced by a
  placeholder.
- [ ] 5.6 The shipped file: loading `config/resource_profiles.yaml` and
  resolving every job type of `DEFAULT_JOB_TYPE_TEMPLATES` for every model
  key in the file plus one unlisted model gives, for
  `produce_forcing_array`, one core and one thread and otherwise default
  plus model; for every other job type, exactly default plus model.
- [ ] 5.7 Split pins: `resource_profile_override_model_ids` of a file with
  and without `job_type_overrides` is the same set; a pass built from each
  gives the same cohorts and budgets.

## 6. Evidence

- [ ] 6.1 Red first, in `.workplans/3344/red-runs.log`: 5.1 (stacked),
  5.3 and 5.6 fail against the unmodified source.
- [ ] 6.2 Mutations, by an exact-replacement script under
  `.workplans/3344/`, each red on the named test, restored and compared:
  - M1 job type merged before the model override -> 5.1 stacked, 5.3 red;
  - M2 the array submit does not pass the job type -> 5.3 red;
  - M3 the single-job render does not pass the job type -> 5.4 red;
  - M4 the job-type entry merged after the deployment overlay -> 5.1
    overlay case red;
  - M5 the loader accepts `max_concurrent` in an entry -> 5.2 red;
  - M6 the loader accepts a non-mapping `job_type_overrides` -> 5.2 red;
  - M7 the shipped entry without `shud_threads` -> 5.6 red;
  - M8 `resource_profile_override_model_ids` also returns job types ->
    5.7 red;
  - M9 the shipped entry keyed `run_shud_forecast_array` -> 5.6 red;
  - M10 the array submit resolves with the literal job type
    `produce_forcing_array` -> 5.5 red;
  - M11 the loader accepts `cpus_per_task` without `shud_threads` -> 5.2
    red;
  - M12 the loader does not validate entry values -> 5.2 red.
- [ ] 6.3 Local: `tests/test_job_array.py`, `tests/test_real_slurm_gateway.py`,
  `tests/test_orchestration_chain.py`, the profile and split tests of
  `tests/test_production_scheduler.py` (`-k "resource_profile or cohort"`);
  `uv run --no-sync ruff check --no-cache .`; `openspec validate
  resource-profile-job-type-overrides --strict --no-interactive`; markdown
  lint of the edited documents; the repository entropy hard gate at its
  baseline.
- [ ] 6.4 node-27, temporary detached worktree of the PR head: the four
  test files of the issue's `Verification:` in full. Receipt on the PR.

Owed on #3344 after merge and deployment, not a merge gate: the gateway
restart inside the deployment window (no `nhms_*` job in `squeue`), and
from the next submitting pass the `sacct` receipt of `design.md`
"Deployment" step 3, then the decision on the bound.
