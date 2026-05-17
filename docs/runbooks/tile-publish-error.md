# Tile Publish Error Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_tile_publish_error_count`, layer ID, version, and run ID.
- Preserve tile report, object URI, content type, and frontend smoke evidence.

## Commands

```bash
uv run nhms-production validate-scale --evidence-root artifacts/production-closure --run-id tile-publish-error-check
uv run pytest -q tests/test_production_scale_validation.py
```

## Expected Evidence

- `scale/tile_evidence.json` records content type, byte limits, and blocker status.
- `e2e/stage_manifest.json` links tile publication to upstream QC.
- `ops/monitoring_alerts.json` records this critical alert.

## Recovery Steps

1. Disable the bad layer version or keep it unactivated.
2. Restore the previous accepted tile layer version.
3. Republish only after tile content type, byte size, and frontend smoke checks pass.

## Residual Risks

Cached bad tiles may persist. Coordinate cache invalidation with API/frontend operators before reopening publication.
