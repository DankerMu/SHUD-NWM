## Why

Issue #2447 records a real duplicate forcing execution: response failure after accepted49174 became an unbound permanent failure, then a normal pass changed convert_cohort to forcing_cohort and submitted49309 for the same work. Both sources subsequently published2026091512. This is a recurrence-prevention fix, not a current publication outage.

The user explicitly requested the smallest fix after questioning over-defensive scope. Generic historical adoption/reconstruction/migration and new operator CLI are removed from scope.

## Triage

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent; existing persistent submission state and concurrency require expanded.
Blast radius: duplicate forcing execution or a new permanent scheduler wedge.
Selected risk packs: persistent identity, shared state/concurrency, errors, compatibility, bounded existing IO and production deployment; no new operator API or configuration.
Evidence floor: real HTTP502→file journal→renamed-cohort/restart no duplicate; confirmed original completion→normal forecast continuation; explicit rejection, ordinary/unrelated work, concurrent overlap and forecast siblings.

## What Changes

- Reuse existing submission/reservation/reconciliation mechanisms for new forcing attempts, retaining only identity needed to resolve the accepted execution.
- Keep gateway-crossed ambiguity distinct from rejection/permanent failure; prevent intersecting members from submitting again despite changed cohort run keys.
- Resolve confirmed executions through existing trustworthy accounting/status and normal completion consumers; fail closed only while genuinely unproven. Do not replace repeated execution with a permanent unresolved-state trap.
- Preserve historical successful products and forecast identity/digest semantics; do not generically fence sparse legacy failed rows.

## Capabilities

### New Capabilities

- `forcing-submit-recovery`: bounded extension of the existing forcing submit lifecycle to preserve and resolve response ambiguity without repeated execution.

### Modified Capabilities

None; existing forecast and #2439 contracts remain unchanged.

## Impact

Existing orchestrator submission row/identity, reservation, query/reconciliation and scheduler consumers. No new dependencies, CLI, data migration, raw journal edits, legacy adoption framework, capture-loop fix or live49309/49174 authority changes.
