# Production Rollback Drill Runbook

## Preconditions

- Use a production-like environment with approved operator credentials and immutable evidence storage.
- Confirm dependency summaries for #147-#151 are present under the supplied evidence root.
- Archive command output, audit rows, before/after state, and recovery state for each drill.

## Commands

```bash
nhms-admin models deactivate --model-id <model> --restore-previous-version
nhms-admin packages rollback --manifest <manifest> --quarantine-partial-objects
nhms-admin sources mark-unavailable --cycle <cycle> --use-best-available
nhms-admin jobs retry-array --job-id <job> --failed-only
nhms-admin tiles rollback --layer <layer> --previous-version
```

## Expected Evidence

- `ops/rollback_drills.json` records command, precondition, recovery result, dependency references, and live execution flags.
- `ops/audit_redaction.json` records operator action decisions with redacted lineage.
- Consumed dependency summaries resolve as `summary.json`, `<name>/summary.json`, or `object-store/summary.json`.

## Recovery Steps

1. Restore the last accepted model, package, source cycle, job output, or tile layer.
2. Confirm rejected or release-blocked actions did not mutate state.
3. Re-run `validate-ops` and attach live receipts before requesting final readiness.

## Residual Risks

The deterministic ops lane only simulates rollback drills. A requested `live_drill` scope does not prove live rollback without receipts.
