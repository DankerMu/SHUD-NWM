# Production Service Config Template

## Preconditions

- Populate values from the production deployment system, not from local developer defaults.
- Keep credentials in workload identity, secret stores, or runtime injection. Do not place tokens, passwords, signed URLs, query strings, or fragments in these settings.
- Root and path values must not contain traversal segments, dot segments, or backslashes.

## Required Settings

```text
api: DATABASE_URL, AUTH_BACKEND, AUDIT_LOG_DESTINATION, CORS_ALLOWED_ORIGINS
orchestrator: PIPELINE_DATABASE_URL, OBJECT_STORE_PREFIX, SLURM_GATEWAY_URL, WORKSPACE_ROOT
slurm_gateway: SLURM_PARTITION, SLURM_ACCOUNT, SLURM_SHARED_LOG_ROOT, SBATCH_TEMPLATE_ROOT
tile_publisher: TILE_OBJECT_PREFIX, TILE_LAYER_REGISTRY, TILE_ERROR_TOPIC
frontend: VITE_API_BASE_URL, VITE_AUTH_MODE, VITE_MAP_STYLE_URL
database: DATABASE_URL, POSTGIS_ENABLED, TIMESCALE_ENABLED, MIGRATION_LOCK
object_store: OBJECT_STORE_ROOT, OBJECT_STORE_PREFIX, OBJECT_STORE_CREDENTIAL_SOURCE
source_adapters: GFS_CONFIG, IFS_CONFIG, ERA5_CONFIG, CLDAS_RESTRICTED_REASON
workspace_roots: RUN_WORKSPACE_ROOT, SHARED_LOG_ROOT, ARTIFACT_RETENTION_POLICY
```

## Validation Command

```bash
NHMS_RUN_PRODUCTION_CLOSURE=1 uv run nhms-production validate-ops \
  --evidence-root artifacts/production-closure \
  --run-id production-service-config-check
```

## Expected Evidence

- `ops/config_validation.json` records each service, required settings, template source, blockers, and this template reference.
- `ops/summary.json` remains `release_blocked` until live auth, alert, rollback, and dependency evidence are accepted.

## Recovery Steps

1. Replace deterministic fallback values with production deployment values.
2. Fix unsafe root/path values and remove credential-shaped material from config.
3. Re-run ops validation and review blockers before release approval.

## Residual Risks

Config validation proves shape and safety only. It does not prove live backend auth, live alert sink delivery, or live rollback execution.
