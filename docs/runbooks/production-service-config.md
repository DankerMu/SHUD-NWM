# Production Service Config Template

## Preconditions

- Populate values from the production deployment system, not from local developer defaults.
- Keep credentials in workload identity, secret stores, or runtime injection. Do not place tokens, passwords, signed URLs, query strings, or fragments in these settings.
- Root and path values must not contain traversal segments, dot segments, or backslashes.

## Required Settings

```text
api: DATABASE_URL, AUTH_BACKEND, AUDIT_LOG_DESTINATION, CORS_ALLOWED_ORIGINS
orchestrator: OBJECT_STORE_PREFIX, SLURM_GATEWAY_URL, WORKSPACE_ROOT
scheduler_db_free: NHMS_SCHEDULER_*_BACKEND=file, registry/readiness/journal/state-index paths, NHMS_SCHEDULER_DB_FREE_REQUIRED=true (job-env contract, not a mode switch)
slurm_gateway: SLURM_PARTITION, SLURM_ACCOUNT, SLURM_SHARED_LOG_ROOT, SBATCH_TEMPLATE_ROOT
tile_publisher: TILE_OBJECT_PREFIX, TILE_LAYER_REGISTRY, TILE_ERROR_TOPIC
frontend: VITE_API_BASE_URL, VITE_AUTH_MODE, VITE_MAP_STYLE_URL
database: DATABASE_URL, POSTGIS_ENABLED, TIMESCALE_ENABLED, MIGRATION_LOCK
object_store: OBJECT_STORE_ROOT, OBJECT_STORE_PREFIX, OBJECT_STORE_CREDENTIAL_SOURCE
source_adapters: GFS_CONFIG, IFS_CONFIG, ERA5_CONFIG, CLDAS_RESTRICTED_REASON
workspace_roots: RUN_WORKSPACE_ROOT, SHARED_LOG_ROOT, ARTIFACT_RETENTION_POLICY
```

## Recovery Steps

1. Replace deterministic fallback values with production deployment values.
2. Fix unsafe root/path values and remove credential-shaped material from config.

## Residual Risks

This template lists required settings only. It does not prove live backend auth, live alert sink delivery, or live rollback execution.
