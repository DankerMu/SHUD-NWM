# The contract specs and the specs named after the codename name the compute role (#3429)

Slice 10.2 of `sweep-compute-role-narration` (#3345). Spec text only.

## Why

Seven capability specs still use the codename "node-22" as a place in 67 lines: the two contract
specs (`production-topology-contract`, `production-ops-readiness`) in the requirements the earlier
slices left, and the five specs whose own name carries the codename (`node22-db-retirement-cutover`,
`node22-dbfree-runtime`, `node22-env-hygiene`, `node22-unit-failure-alert`,
`slurm-gateway-node22-deployment`). The codename stands for the compute role, which runs on `w3`
(the compute control host) and `w2` since 2026-11-23; the original host `210.77.77.22` is retired.

A reader of these specs today is told that "node-22 SHALL reach the SMTPS shim", that a gateway
"SHALL be deployed on node-22", and that "the node-22 scheduler" runs DB-free. About a third of the
lines are the other case: they are about the retired host and its database (`:55433`), and say
"node-22" without saying that it is the retired machine.

## What Changes

- **43 sentences** in the seven specs are rewritten with the glossary terms (`sentences.md` lists
  each with its text before and after and the term chosen): 18 name the compute role, 5 the compute
  control host, 1 the compute hosts, 17 are about the retired host and say so ("the retired node-22
  host"), 2 no longer need a subject.
- **8 requirement titles** are renamed (RENAMED delta), as `appendix-titles.md` of the umbrella
  assigns to this slice. The 3 titles of these specs that it keeps (about the retired host's
  database) stay.
- **3 Purpose lines** are edited in the main specs in the push that archives this change (a delta
  cannot carry Purpose text).
- **Four sentences change more than their subject**, because a present-tense statement has to be
  true of today's hosts (`design.md` D4): the gateway's rationale ("no such service is proven there
  today"), "between the two nodes", "node-22 PostgreSQL" in the DB-free evidence requirement, and
  the two sentences of the database cutover that describe an act completed in 2026-06.
- **Nothing is added or dropped**: no requirement, no scenario, no scenario title. Five scenario
  titles carry the codename; `openspec` refuses a MODIFIED block that retitles a scenario, so they
  stay (four are about the retired host's database; one is listed as residue for the last slice).

Not in this change: capability names; the identifier sentence, the gate's vocabulary paragraph, the
scenario "Retired-host material keeps its meaning" and the banner scenario, which earlier slices
wrote; code, tests, runbooks.

## Impact

- `openspec/specs/**` of the seven capabilities; this change (archived in the PR's last push); the
  umbrella's `tasks.md` (tick of 10.2).
- No test reads a sentence of this slice. The topology hard gate reads these specs; its findings
  are compared before and after.
