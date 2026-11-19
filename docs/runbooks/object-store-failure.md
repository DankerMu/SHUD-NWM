# Object Store Failure Alert Runbook

## Preconditions

- Confirm the alert payload includes `nhms_object_store_write_failures`, object prefix, manifest path, and run ID.
- Stop new imports for the affected prefix before cleanup.

## Recovery Steps

1. Verify prefix containment and credential source for the failed write.
2. Quarantine partial objects using the cleanup report.
3. Re-run publication after permissions and object-store health are confirmed.

## Residual Risks

Partial objects may remain externally visible until lifecycle cleanup completes. Registry activation must remain unchanged until manifest checksums pass.
