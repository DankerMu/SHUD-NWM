# Tile Publish Error Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_tile_publish_error_count`, layer ID, version, and run ID.
- Preserve tile report, object URI, content type, and frontend smoke evidence.

## Recovery Steps

1. Disable the bad layer version or keep it unactivated.
2. Restore the previous accepted tile layer version.
3. Republish only after tile content type, byte size, and frontend smoke checks pass.

## Residual Risks

Cached bad tiles may persist. Coordinate cache invalidation with API/frontend operators before reopening publication.
