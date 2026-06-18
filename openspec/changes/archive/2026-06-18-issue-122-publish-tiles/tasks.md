## 1. Contract and Implementation

- [x] 1.1 Replace the placeholder `publish-tiles` CLI with a real implementation that returns zero only when delivery evidence exists.
- [x] 1.2 Implement or connect an importable tile publisher module that discovers publishable cycle/run products and writes or registers delivery metadata.
- [x] 1.3 Record publish lineage using `cycle_id`, run/source/scenario where available, layer/artifact IDs, and published/excluded basin counts.
- [x] 1.4 Preserve failure behavior for missing cycle/product/database inputs with non-zero CLI exit and clear `error_code` / `error_message`.
- [x] 1.5 Make repeat publish idempotent for the same cycle: no duplicate logical layer rows, no conflicting cache rows, and deterministic JSON identifiers.
- [x] 1.6 Bound product discovery to requested cycle/run lineage and configured workspace/object-store prefix.

## 2. Slurm and Orchestrator Integration

- [x] 2.1 Ensure `infra/sbatch/publish_tiles.sbatch` calls the implemented CLI with required environment and does not mask non-zero failures.
- [x] 2.2 Ensure mock M3 orchestration can complete through publish success when products exist.
- [x] 2.3 Ensure publish failure maps to `failed_publish` and records job/event/log observability.
- [x] 2.4 Preserve partial basin publish metadata for successful basins and excluded failed basins.
- [x] 2.5 Preserve existing final status semantics: full M3 success ends `complete`; partial upstream basin success remains `parsed_partial`.

## 3. Tests and Documentation

- [x] 3.1 Add CLI success test with input `cycle_id=GFS_2026050100` plus a minimal publishable run/product fixture; expect exit code 0, JSON `status="published"`, `cycle_id`, non-empty `layers`, and a verified `map.tile_layer` / `map.tile_cache` row or documented object-store artifact.
- [x] 3.2 Add CLI missing-product test with input `cycle_id=missing_cycle`; expect non-zero exit, JSON `status="failed_publish"`, stable error code/message, and no successful layer metadata.
- [x] 3.3 Add CLI idempotency test that runs publish twice for the same fixture; expect stable layer/artifact identifiers and no duplicate logical delivery rows.
- [x] 3.4 Add environment/config tests for local and Slurm-equivalent invocation: valid workspace/object-store/database env succeeds, missing or invalid required env fails with `failed_publish`.
- [x] 3.5 Add file safety test proving mismatched object-store prefixes or unsafe publish roots do not create success metadata.
- [x] 3.6 Add or update mock gateway/orchestrator tests proving a full Forecast M3 cycle reaches final success only after publish evidence exists.
- [x] 3.7 Add or update failure tests proving publish errors do not complete the cycle.
- [x] 3.8 Update tile publisher or operations docs to name the implemented delivery table/artifact format and release limits.

## 4. Required Evidence

- [x] 4.1 `openspec validate issue-122-publish-tiles --strict --no-interactive` passes.
- [x] 4.2 `uv run pytest -q tests/test_slurm_array_contract.py tests/test_orchestration_chain.py tests/test_e2e_m3.py` passes.
- [x] 4.3 `uv run pytest -q tests/test_api.py tests/test_gateway.py` passes.
- [x] 4.4 `uv run ruff check .` passes.

## Risk Pack Evidence Mapping

- Public API / CLI / script entry: tasks 1.1, 3.1, 3.2, evidence 4.2.
- Config / project setup: tasks 1.2, 2.1, 3.4, evidence 4.2.
- File IO / path safety / overwrite: tasks 1.2, 1.4, 1.5, 3.3, 3.5.
- Schema / columns / units / field names: tasks 1.2, 1.3, 3.1, 3.4.
- Time series / forcing / temporal boundaries: task 1.3.
- Resource limits / large input / discovery: tasks 1.2, 1.4, 1.6.
- Legacy compatibility / examples: tasks 2.1, 2.2, 2.5, evidence 4.2.
- Error handling / rollback / partial outputs: tasks 1.4, 2.3, 2.4, 3.2, 3.3, 3.5.
- Release / packaging / dependency compatibility: task 1.2, evidence 4.4.
- Documentation / migration notes: task 3.8.

## Non-Goals

- Full vector-tile rendering pipeline beyond the selected minimal release artifact.
- Frontend production data-source migration, OpenAPI drift cleanup, Slurm Analysis/Hindcast unification, or real database integration matrix work.
