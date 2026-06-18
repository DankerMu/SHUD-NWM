## Context

Issue #496 is part of #492 and depends on #495. Business production forecast
cycles must not cold start or silently fall back to an older usable state. The
current forecast warm-start selection can fall back from an exact
`valid_time == cycle_time` miss to `get_latest_usable_state()`, and cohort
orchestration skips warm-start selection when scheduler-provided
`init_state_uri` is already present.

Risk triage:

- Issue type: feature / production remediation
- Project profile: NHMS
- Blast radius: high
- Fixture level: expanded
- Repair intensity: high
- Why: forecast state-machine gating, run manifest mutation, Slurm submit
  side effects, state lineage/QC evidence, scheduler-provided warm-start
  fields, and legacy non-production compatibility.

## Goals

- Add an explicit strict forecast warm-start mode with env
  `NHMS_REQUIRE_FORECAST_WARM_START`.
- In strict mode, forecast cycles must use an exact successor checkpoint whose
  `valid_time == cycle_time`.
- For production UTC `00/12` business cycles, the selected checkpoint must be
  the previous allowed cycle's `lead_hours == 12` state.
- Strict mode must block before run manifest write, hydro_run create/update, or
  Slurm submit when the exact successor checkpoint is missing, unusable,
  QC-failing, lineage-incompatible, or has the wrong lead.
- Scheduler-prefilled `init_state_*` fields must be validated by the same strict
  policy and cannot bypass exact-successor checks.
- Preserve existing non-strict forecast/analysis behavior unless a caller
  explicitly enables strict mode.

## Non-Goals

- No adapter cycle-hour default change; #497 owns adapter probe reduction.
- No scheduler allowed-cycle gate changes; #495 owns scheduler candidate
  filtering.
- No DB schema migration unless implementation discovers an existing metadata
  field cannot carry required evidence.
- No production execution.
