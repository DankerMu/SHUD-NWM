## 0. Dependency gate

- [ ] 0.1 Confirm `governance-0-ci-contract-baseline` is merged and green, or record an explicit maintainer waiver listing current red checks.

## 1. Role boundary source of truth

- [ ] 1.1 Add `docs/governance/ROLE_BOUNDARY.md` with four categories: `compute_control`, `display_readonly`, `slurm_gateway`, `shared_contract`.
- [ ] 1.2 For each category, list representative paths, allowed mutations, forbidden capabilities, verification oracle, and current guard tests.
- [ ] 1.3 Link `ROLE_BOUNDARY.md` from README or `docs/governance/DOC_STATUS.md` once Governance-3 creates that status document.

## 2. Static boundary tests

- [ ] 2.1 Add `tests/test_role_boundary_static.py` covering display env blockers, Slurm route registration, standalone gateway route scope, and QHH diagnostic token exclusion.
- [ ] 2.2 Extend or reference existing `tests/test_runtime_mode.py`, `tests/test_two_node_docker_runtime.py`, and `tests/test_qhh_scripts_static.py` rather than duplicating their full logic.
- [ ] 2.3 Verify `uv run pytest -q tests/test_runtime_mode.py tests/test_two_node_docker_runtime.py tests/test_qhh_scripts_static.py tests/test_role_boundary_static.py`.

## 3. Shared-policy layer inversion plan

- [ ] 3.1 Create a focused implementation-ready issue inside this epic for moving policy evidence helpers used by CLI/workers/common out of `apps.api.auth`.
- [ ] 3.2 Inventory all imports from `apps.api.auth` outside `apps/api` and classify each as shared helper vs API-only dependency.
- [ ] 3.3 Add the shared auth/policy extraction issue as a dependency for any future hard-gate that fails `apps.api.*` imports outside the API layer.
- [ ] 3.4 Do not perform the extraction in the inventory PR unless the change remains small and all affected tests are local to one ownership boundary.
