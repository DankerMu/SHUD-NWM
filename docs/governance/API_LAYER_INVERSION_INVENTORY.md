# API Layer Inversion Inventory

This inventory is the #417 source of truth for the pre-cleanup non-API
references to the API layer. It is evidence-only: #417 did not fix imports,
change runtime behavior, update role-boundary policy, or enable a hard gate.

## Evidence Commands

Evidence was collected on 2026-06-11 with:

- `rg -n "from apps\\.api|import apps\\.api|apps\\.api\\." . -g '!apps/api/**'`
- `PYTHONDONTWRITEBYTECODE=1 uv run --no-sync python scripts/governance/audit_repo_entropy.py --format json`

The focused search is intentionally broader than the entropy finding. It
returns runtime service imports, test imports, governance text, runbook command
examples, OpenSpec text, deployment entrypoint strings, and scan implementation
text. Only the AST-based entropy audit decides what is counted by
`apps-api-layer-inversion`.

## #417 Active Service Import Baseline

The #417 entropy-counted layer inversion baseline was two files and three
deduplicated audit findings:

- `services/tiles/mvt.py`: imports `apps.api.errors`, counted once by the audit
  even though six local import statements appear in the file.
- `services/production_closure/readonly_db_validation.py`: imports
  `apps.api.main` and `apps.api.routes`, counted as two audit findings.

| File | Line | Import statement | Imported module | Owner area | Counted by `apps-api-layer-inversion` | Follow-up |
|---|---:|---|---|---|---|---|
| `services/tiles/mvt.py` | 83 | `from apps.api.errors import ApiError` | `apps.api.errors` | Tiles / MVT helper boundary | Yes, deduped into the `services/tiles/mvt.py` `apps.api.errors` finding | #418 |
| `services/tiles/mvt.py` | 94 | `from apps.api.errors import ApiError` | `apps.api.errors` | Tiles / MVT helper boundary | Yes, same deduped finding as line 83 | #418 |
| `services/tiles/mvt.py` | 168 | `from apps.api.errors import ApiError` | `apps.api.errors` | Tiles / MVT helper boundary | Yes, same deduped finding as line 83 | #418 |
| `services/tiles/mvt.py` | 192 | `from apps.api.errors import ApiError` | `apps.api.errors` | Tiles / MVT helper boundary | Yes, same deduped finding as line 83 | #418 |
| `services/tiles/mvt.py` | 1180 | `from apps.api.errors import ApiError` | `apps.api.errors` | Tiles / MVT helper boundary | Yes, same deduped finding as line 83 | #418 |
| `services/tiles/mvt.py` | 1486 | `from apps.api.errors import ApiError` | `apps.api.errors` | Tiles / MVT helper boundary | Yes, same deduped finding as line 83 | #418 |
| `services/production_closure/readonly_db_validation.py` | 1894 | `from apps.api.main import create_app` | `apps.api.main` | Production closure readonly validation | Yes, deduped into the `services/production_closure/readonly_db_validation.py` `apps.api.main` finding | #419 |
| `services/production_closure/readonly_db_validation.py` | 1895 | `from apps.api.routes import pipeline as pipeline_routes` | `apps.api.routes` | Production closure readonly validation | Yes, counted as the `services/production_closure/readonly_db_validation.py` `apps.api.routes` finding | #419 |
| `services/production_closure/readonly_db_validation.py` | 2970 | `from apps.api.main import create_app` | `apps.api.main` | Production closure readonly validation | Yes, same deduped finding as line 1894 | #419 |

No import was fixed in #417. #418 owned moving or adapting the tile helper
boundary. #419 owned replacing the readonly validation API probe boundary with
an API-owned adapter or injected requester. #420 owns zero-baseline enforcement
prep after #418 and #419 removed these findings.

## Current Zero Baseline

After #418/#419, the live entropy audit is expected to report
`metadata.summary_counts.by_check_id.get("apps-api-layer-inversion", 0) == 0`.
Issue #420 keeps that zero baseline under static and entropy-audit tests while
preserving `apps-api-layer-inversion` as a standalone future hard-gate
candidate. Governance CI remains report-only and does not pass
`--mode hard-gate`.

## Entropy Audit Extraction

The audit JSON reported `metadata.summary_counts.by_check_id["apps-api-layer-inversion"] == 3`.

Extracted findings:

| Audit id | Evidence path | Normalized imported module from description | Module | Owner area | Role | Severity | Priority | Budget counted | Gate eligible | Follow-up |
|---|---|---|---|---|---|---|---|---|---|---|
| `ENT-0001` | `services/production_closure/readonly_db_validation.py` | `apps.api.main` | `services/production_closure` | `layering` | `shared_contract` | `high` | `P1` | `true` | `false` | #419 |
| `ENT-0002` | `services/production_closure/readonly_db_validation.py` | `apps.api.routes` | `services/production_closure` | `layering` | `shared_contract` | `high` | `P1` | `true` | `false` | #419 |
| `ENT-0003` | `services/tiles/mvt.py` | `apps.api.errors` | `services/tiles` | `layering` | `shared_contract` | `high` | `P1` | `true` | `false` | #418 |

The matching high-spread pattern is `apps-api-layer-inversion` with
`occurrence_count: 3`, `module_count: 2`, modules
`services/production_closure` and `services/tiles`, role `shared_contract`, top
priority `P1`, and top severity `high`.

## Focused Search Classification

The focused #417 `rg` command also returned non-baseline text hits outside the
two active service files. These were classified separately and were not
implementation scope for #417.

| Class | Current examples | Classification |
|---|---|---|
| Tests | `tests/test_api.py`, `tests/test_role_boundary_static.py`, `tests/test_readonly_db_validation.py`, API route tests | Test imports, monkeypatch targets, and static-scan fixtures. Not counted by the service/shared entropy scan. |
| Governance docs and runbooks | `docs/governance/ROLE_BOUNDARY.md`, `docs/governance/entropy-burndown-triage.md`, `docs/governance/API_LAYER_INVERSION_INVENTORY.md`, `docs/runbooks/*` | Policy text, triage notes, command examples, and this inventory's evidence rows. Not active service imports. |
| OpenSpec changes | `openspec/changes/governance-1-role-boundary-inventory/*`, `openspec/changes/governance-4-entropy-automation/*`, `openspec/changes/governance-5-e4-layer-inversion-hardgate-prep/*`, milestone worklogs | Historical and active spec text. Not active service imports. |
| Infra and deployment entrypoints | `infra/compose.compute.yml`, `infra/compose.display.yml`, `infra/docker/Dockerfile.app`, `infra/docker/entrypoint.sh`, `infra/systemd/nhms-slurm-gateway.service` | ASGI entrypoint strings or comments such as `apps.api.main:app`. Not Python layer imports. |
| Scripts | `scripts/governance/audit_repo_entropy.py`, `scripts/validate_two_node_docker_runtime.py` | Scan implementation and expected-entrypoint validation text. Not counted as layer inversion findings. |
| Shared package comment | `packages/common/met_store.py` | Comment referencing an API route constant for alignment. Not an import statement and not counted by the audit. |
| Other service text | `services/slurm_gateway/app.py` | Docstring/comment reference to the full business API. Not an import statement and not counted by the audit. |
| Repo-root commands and local instructions | `AGENTS.md`, `Makefile`, `README.md` | Developer command examples. Not active service imports. |

There were no additional #417 `apps-api-layer-inversion` audit findings beyond
`services/tiles/mvt.py` and
`services/production_closure/readonly_db_validation.py`. If a future entropy run
adds another `apps-api-layer-inversion` finding after the #420 zero baseline, it
should be filed as a separate follow-up candidate instead of silently expanding
issues #418 or #419.
