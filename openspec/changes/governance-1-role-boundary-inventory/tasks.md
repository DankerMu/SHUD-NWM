## 0. Dependency gate

- [x] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or record an explicit maintainer waiver listing current red checks.

## 1. Role boundary source of truth

- [x] 1.1 Add `docs/governance/ROLE_BOUNDARY.md` with four categories: `compute_control`, `display_readonly`, `slurm_gateway`, `shared_contract`.
- [x] 1.2 For each category, list representative paths, allowed mutations, forbidden capabilities, verification oracle, and current guard tests.
- [x] 1.3 Do not link `ROLE_BOUNDARY.md` from README or `docs/governance/DOC_STATUS.md` in #360; Governance-3 owns document-status indexing.

## 2. Static boundary tests

- [x] 2.1 Add `tests/test_role_boundary_static.py` covering explicit boundary scenarios:
  - display env blockers: input `NHMS_SERVICE_ROLE=display_readonly` plus each compute-only env key -> expected blocker or static forbidden finding.
  - Slurm route registration: input display/full API route inventory -> expected no `/api/v1/slurm/*` under display, retained Slurm routes under compute/dev via existing runtime tests.
  - standalone gateway route scope: input `services.slurm_gateway.app:create_gateway_app()` -> expected slash-delimited `/api/v1/slurm` or `/api/v1/slurm/*` only, excluding forecast/model/pipeline/static/frontend business routes and mounted routes.
  - QHH diagnostic exclusion: input recursive production orchestrator Python source scan -> expected no `run_qhh_*` or diagnostic manifest-builder tokens.
  - temporary API import allowlist: input `packages/common`, `services/orchestrator`, `workers/**`, and documented shared-contract Python files such as `services/slurm_gateway/models.py` -> expected exactly these (`path`, `module`) pairs and no other `apps.api` / `apps.api.*` imports:
    - (`packages/common/model_registry.py`, `apps.api.auth`)
    - (`services/orchestrator/retry.py`, `apps.api.auth`)
    - (`workers/flood_frequency/cli.py`, `apps.api.auth`)
    - (`workers/flood_frequency/frequency.py`, `apps.api.auth`)
    - (`workers/flood_frequency/hindcast.py`, `apps.api.auth`)
    - (`workers/model_registry/basins_registry_import.py`, `apps.api.auth`)
    - (`workers/model_registry/cli.py`, `apps.api.auth`)
- [x] 2.2 Reference and run existing `tests/test_runtime_mode.py`, `tests/test_two_node_docker_runtime.py`, `tests/test_qhh_scripts_static.py`, and `tests/test_slurm_gateway_app.py` rather than duplicating their full runtime logic.
  #360 may tighten static predicates in sibling tests when they share the same boundary false negative.
- [x] 2.3 Include display retry/cancel and queue-depth compatibility evidence by running or citing focused tests from `tests/test_retry_cancel_consistency.py` and `tests/test_monitoring_api.py`.
- [x] 2.4 Verify `uv run pytest -q tests/test_runtime_mode.py tests/test_two_node_docker_runtime.py tests/test_qhh_scripts_static.py tests/test_role_boundary_static.py tests/test_slurm_gateway_app.py tests/test_retry_cancel_consistency.py tests/test_monitoring_api.py`.

## 3. Shared-policy layer inversion plan

- [x] 3.1 Confirm #361 is the focused implementation-ready issue inside this epic for moving policy evidence helpers used by CLI/workers/common out of `apps.api.auth`.
- [x] 3.2 Inventory all imports from `apps.api.auth` outside `apps/api` and classify each as shared helper vs API-only dependency.
- [x] 3.3 Add the shared auth/policy extraction issue as a dependency for any future hard-gate that fails `apps.api` / `apps.api.*` imports outside the API layer.
- [x] 3.4 Do not perform shared auth/policy extraction in #360: no helper moves, no call-site rewrites, no behavior change. #360 may only document and test the temporary #361 allowlist.
