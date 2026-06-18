## Context

Issue #495 is part of #492. Business production should only admit UTC
`00` and `12` forecast cycles, but the current scheduler and GFS/IFS source
window logic can discover `00/06/12/18`. Even when later stages avoid
submitting some cycles, the extra cycles pollute backfill gap accounting,
candidate evidence, and readiness checks.

Risk triage:

- Issue type: feature / production remediation
- Project profile: NHMS
- Blast radius: high
- Fixture level: expanded
- Repair intensity: high
- Why: scheduler candidate selection, backfill gap semantics, runtime config
  evidence, production env config, and downstream readiness/submit side effects.

## Goals

- Add a production scheduler allowed-cycle-hours configuration with env
  `NHMS_SCHEDULER_ALLOWED_CYCLE_HOURS_UTC`.
- Default production examples to UTC `00,12` while keeping parsing
  deterministic and fail-closed for invalid configured values.
- Ensure scheduler discovery filters disallowed `06/18` cycles before dedupe,
  completion status lookup, gap accounting, candidate/blocked-candidate
  selection, readiness checks, forcing production, or submit.
- Emit evidence for excluded disallowed cycles with
  `selection_status=excluded` and
  `selection_reason=cycle_hour_not_allowed`.
- Make source-cycle window flooring respect the allowed cycle hours, so windows
  near `06/18` still align to the nearest allowed `00/12` boundary.
- Update compute env examples with the production allowed-cycle configuration.

## Non-Goals

- No adapter-level default change in this issue; #497 owns adapter probe
  reduction and docs consolidation.
- No strict warm-start successor enforcement; #496 owns forecast warm-start
  hardening.
- No DB schema migration or production backfill execution.
- No frontend change.
