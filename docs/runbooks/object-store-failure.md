# Object Store Failure Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_object_store_write_failures`, object prefix, manifest path, and run ID.
- Stop new imports for the affected prefix before cleanup.

## Commands

```bash
uv run nhms-production validate-object-store --evidence-root artifacts/production-closure --run-id object-store-failure-check
uv run pytest -q tests/test_production_object_store_validation.py
```

## Expected Evidence

- `object-store/package_manifest.json` records written object checksums and URI scope.
- `object-store/cleanup_rollback.json` records quarantined partial writes.
- `ops/monitoring_alerts.json` links this runbook and records the critical alert.

## Recovery Steps

1. Verify prefix containment and credential source for the failed write.
2. Quarantine partial objects using the cleanup report.
3. Re-run publication after permissions and object-store health are confirmed.

## Residual Risks

Partial objects may remain externally visible until lifecycle cleanup completes. Registry activation must remain unchanged until manifest checksums pass.
