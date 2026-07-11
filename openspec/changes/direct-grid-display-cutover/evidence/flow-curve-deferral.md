# Flow-curve cross-cutover continuity — DEFERRED-to-pilot

Task 4.3 authority: `openspec/changes/direct-grid-display-cutover/tasks.md` §4.3.
Design authority: `openspec/changes/direct-grid-display-cutover/design.md` §6
("The flow-curve cross-cutover continuity receipt is **DEFERRED**").

## Status

**DEFERRED-to-pilot.** No production basin is activated in this rehearsal;
consequently no real cross-cutover flow curve exists to sample. This
document is the recorded absence of evidence — bound to backfill at the
pilot's first real cutover — NOT a certification gap.

## Rationale

The rehearsal exercises the Change 4 display-cutover mechanisms
(state-clone hook + station-flag flip hook + supersede + activate + audit +
manifest re-publish) end-to-end on the evidence-only synthetic identity
`basin__evidence_cmfd_p02_synth__v1`. That synthetic identity is:

- not bound to any real basin hydrology;
- not producing real forecasts (`provisioning/03-seeded-forecast-run.sql`
  inserts one seeded `hydro.hydro_run` with `status='succeeded'` and no
  associated timeseries data);
- registered explicitly so no production data is written or exposed
  (`design.md` §6 phase 1 recorded bypass enumeration).

A flow-curve continuity receipt would require sampling `hydro.river_timeseries`
values before and after a cutover on a real basin whose SHUD forecasts
produce non-trivial hydrographs. There is no such data on the synthetic
identity — the seeded run has no timeseries payload — and manufacturing
synthetic hydrograph values purely to fill this receipt would be a
placeholder pass (forbidden by `design.md` §6 phase 3 and task 4.3
non-goals: "no synthetic/mocked flow curve manufactured to fake continuity").

## Certification note

The absence of a flow-curve continuity receipt is a **recorded deferral**,
not a certification gap. This receipt is:

- bound to backfill at the pilot's first real cutover (`design.md` §6, §Migration Plan step 3);
- explicitly enumerated in `evidence/README.md` §2 "Exercised-vs-bypassed";
- referenced by the change verification suite as an EXPECTED absence
  (task 4.3 evidence bar is "recorded absence of evidence", not "receipt filed").

The pilot (`direct-grid-batch-rollout`, Change 7) will register a real
basin's cross-cutover flow curve at its first live cutover, at which point
this file becomes the backfill anchor.

## Signed

- Recorded by: Epic #992 SUB-7 rehearsal receipt harness authoring
- Change: `openspec/changes/direct-grid-display-cutover`
- Window: **`<Phase B UTC window pending>`** — populated by
  `evidence/README.md` §4 timing window when Phase B lands.
