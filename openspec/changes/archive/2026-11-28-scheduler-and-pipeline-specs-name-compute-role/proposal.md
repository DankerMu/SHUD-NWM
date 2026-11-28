# The scheduler and pipeline specs name the compute role (#3430)

Slice 10.3 of `sweep-compute-role-narration` (#3345). Spec text only, and the removal of one
superseded active change.

## Why

Eleven capability specs of the scheduler and the pipeline still use the codename "node-22" as a
place in 67 lines: `production-scheduler-orchestration`, `compute-scheduler-operationalization`,
`scheduler-registry-refresh`, `job-retry-mechanism`, `node27-download-orchestration`,
`scheduler-stall-alerting`, `variant-activation-cutover`, `canonical-precip-copyback`,
`basin-retirement`, `forcing-domain-handoff` and `pipeline-job-persistence`. The codename stands
for the compute role, which runs on `w3` (the compute control host) and `w2` since 2026-11-23; the
original host `210.77.77.22` is retired.

A reader of these specs today is told that "node-22 runs the scheduler", that an operator
"provisions node-22", and that journals live "on node-22's local disk". A few lines are the other
case: a measurement or a receipt taken on the retired host, or its database, named "node-22"
without saying that it is the retired machine.

The active change `fix-node22-keliya-forecast-output-recompute` holds a MODIFIED block of "Backend
scheduler entrypoint", a requirement this slice rewrites, with the old sentences in it. Its own
tasks call it superseded, and it cannot be archived as written (`design.md` D6). The owner decided
on 2026-11-28 that it is removed.

## What Changes

- **64 sentences** in the eleven specs are rewritten with the glossary terms (`sentences.md` lists
  each with its text before and after and the term chosen): 41 name the compute role, 6 the compute
  control host, 8 the compute hosts, 9 are about the retired host and say so ("the retired node-22
  host").
- **2 requirement titles** of `compute-scheduler-operationalization` are renamed (RENAMED delta),
  as `appendix-titles.md` of the umbrella assigns to this slice. The title of
  `forcing-domain-handoff` that it keeps (the retired host's rollback mirror) stays.
- **6 Purpose texts** are edited in the main specs in the push that archives this change (a delta
  cannot carry Purpose text).
- **Some sentences change more than their subject**, because a present-tense statement has to be
  true of today's hosts (`design.md` D4): the Purpose of `scheduler-stall-alerting` ("the only
  alerting channel that host has", "node-22 has neither [a database nor a mail lane]"), three
  statements about where the journal and the object store are, and four dated observations.
- **Nothing is added or dropped in the eleven specs**: no requirement, no scenario, no scenario
  title. Four scenario titles use the codename for the role; `openspec` refuses a MODIFIED block
  that retitles a scenario, so they stay and are listed as residue for the last slice.
- **`openspec/changes/fix-node22-keliya-forecast-output-recompute/` is removed** (`design.md` D6).
  Four places of the umbrella that say the decision is open are amended.

Not in this change: capability names; code, tests, runbooks.

## Impact

- `openspec/specs/**` of the eleven capabilities; this change (archived in the PR's only push); the
  removed change; the umbrella's `tasks.md` (tick of 10.3) and `design.md` (the amended sentences).
- No test reads a sentence of this slice from the specs. One test holds an inlined copy of a
  sentence as corpus for the topology gate (`design.md` D8); it does not read the spec.
- The topology hard gate reads these specs and the active changes; its findings are compared before
  and after.
