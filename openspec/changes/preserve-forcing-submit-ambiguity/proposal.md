## Why

Issue #2447: forcing array49174 was accepted and completed, but an empty captured sbatch response became HTTP502 and a permanently_failed/null-id journal row. A later normal pass changed convert_cohort to forcing_cohort and submitted49309 for the same members. Both sources subsequently published2026091512; this is a duplicate-execution/state-authority defect, no longer a current publication outage.

User explicitly authorized complete independent repair and guarded recovery. Existing49174 is now superseded: it MUST NOT be adopted into live current authority.

## Triage

Issue type: bugfix
Fixture level: expanded
Upstream suggested level: absent; persisted authority, concurrency and operator CLI require expanded.
Blast radius: duplicate forcing execution, false completion, stale artifact authority, forecast reconciliation regression.
Selected risk packs: all eleven standard packs; explicit non-goals limit config/dependency rollout.
Evidence floor: real HTTP error classification plus file-journal/scheduler red-green tests; strict negative adoption tests; forecast siblings; exact deployment tree tests; node22 readonly stale-adoption refusal and later normal progression; node27 published identity/data unchanged or advancing.

## What Changes

- Persist forcing submit identity before crossing Gateway; keep response ambiguity out of retry/permanent-failure lanes.
- Prevent duplicate submission across stage-derived cohort keys and overlapping member subsets.
- Add a dry-run-first guarded completed-array adoption action with full identity/artifact verification, CAS and audit, including legacy failed rows when independently provable.
- Reject superseded attempts and unprovable legacy fields without authority writes.

## Capabilities

### New Capabilities

- `forcing-submit-recovery`: forcing-specific accepted-submit lifecycle and operator adoption, using existing journal authority mechanisms.

### Modified Capabilities

None. Forecast identity/digest semantics and #2439 pre-forecast resume behavior remain unchanged.

## Impact

Orchestrator stage submission, typed identity validation, journal reservation/inventory/CAS, scheduler selection/evidence/retry consumers and Click/argparse operator entrypoints. No new dependencies, DB on node22, output format changes or gateway capture-loop fixes.
