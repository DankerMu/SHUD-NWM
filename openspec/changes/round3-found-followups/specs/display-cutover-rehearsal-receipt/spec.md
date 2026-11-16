## REMOVED Requirements

### Requirement: Rehearsal preconditions are provisioned under the evidence-only identity

**Reason**: The rehearsal was a one-time certification of the direct-grid display cutover. It drives the station-flag flip hook, which #3164 retires, so it can no longer be re-run as written. The flow-curve continuity deferral requirement is NOT removed: it binds a backfill to the pilot's first real cutover, which the kept cutover lifecycle can still perform.

**Migration**: None. The executed rehearsal receipt stays as immutable evidence under `openspec/changes/archive/2026-11-15-direct-grid-display-cutover/`.

### Requirement: Rehearsal runs a real cutover transaction on a synthetic identity and restores it to non-active with zero production impact

**Reason**: The rehearsal was a one-time certification of the direct-grid display cutover. It drives the station-flag flip hook, which #3164 retires, so it can no longer be re-run as written. The flow-curve continuity deferral requirement is NOT removed: it binds a backfill to the pilot's first real cutover, which the kept cutover lifecycle can still perform.

**Migration**: None. The executed rehearsal receipt stays as immutable evidence under `openspec/changes/archive/2026-11-15-direct-grid-display-cutover/`.
