# Design: the contract specs and the specs named after the codename name the compute role

The umbrella `sweep-compute-role-narration` is the design of the sweep (`design.md` §1 vocabulary,
§3 rules, `appendix-titles.md`). This document records what this slice decides inside those rules.

## Decisions

### D1. The deltas are generated

As in slice 10.1: one table of fragments (`sentences.md`), each asserted to occur exactly once in
its spec; each changed requirement is emitted whole as a MODIFIED block from the main spec of the
day; the eight titles go through RENAMED. After `openspec archive` the three Purpose lines are
edited in the main specs and every main spec is compared with its expectation (the spec before,
with the replacements applied). The whole cycle was rehearsed on a copy of the specs tree with
this table: 16 MODIFIED blocks, 8 of them under a renamed title (`openspec archive` reports
`~ 13, → 8`: three of the blocks change in their title only), 7 of 7 specs identical to the
expectation.

Lines are re-wrapped where a rewritten sentence would otherwise break a term over a line end or
grow far beyond its neighbours, so three specs change their number of lines.

### D2. The term of each sentence

By what the sentence is about (umbrella §1):

| Term | Sentences |
|---|---|
| the compute role (who does or owns) | the scheduler ("the compute role's scheduler", the wording of 10.1), a download it would run, its scheduler examples, its env files, its oneshot units, who reaches the SMTPS shim, whom a rule restricts |
| the compute control host (the machine of the units and operator actions) | where the gateway is deployed, where its deployment receipt is captured, the host a remote probe cannot reach, probes local to it, the alert template |
| the compute hosts (the machines as a set) | the maintained Python environment, absolute paths under `/scratch` |
| the retired node-22 host | D3 |

### D3. Sentences about the retired host

17 sentences are about `210.77.77.22`: its PostgreSQL on `:55433` (stopped on 2026-06-29, receipt
`docs/runbooks/receipts/2026-06-29-node22-db-retirement-stop.md`), the writer topology it once
carried, and two observations made on it. They get the phrase "the retired node-22 host", in the
word order 10.1 used ("PostgreSQL `:55433` of the retired node-22 host").

- `node22-db-retirement-cutover` is about that database from end to end; its two requirement titles
  are kept by the umbrella. Seven of its eight sentences are retired-host statements. The eighth
  ("scheduler examples do not include active `DATABASE_URL`") is a standing guard on today's
  examples and names the compute role.
- **A normative clause is not addressed to the retired host** (the rule of 10.1 D3). One SHALL of
  this slice governs an act on it that is complete: "The system SHALL stop node-22 historical
  do-not-connect PostgreSQL `:55433` only after …". The host and the date become a record in
  brackets: "SHALL stop the historical do-not-connect PostgreSQL `:55433` (of the retired node-22
  host; stopped on 2026-06-29, #837) only after …". The scenario "Cutover preparation is
  reversible" gets the same form: "the scheduler is switched to DB-free mode (done on the retired
  node-22 host, 2026-06-28)", the date of
  `docs/runbooks/receipts/2026-06-28-node22-dbfree-scheduler-live-proof.md`.
- `production-topology-contract`, "Historical node-22 PostgreSQL retirement remains separately
  gated" (title kept): "The system SHALL treat the local PostgreSQL `:55433` of the retired node-22
  host as historical, do-not-connect, …" is a standing rule about how today's system treats that
  database; its addressee is the system.
- `node22-env-hygiene`: "one row per key the live node-22 file lacked at classification time" is an
  observation of 2026-10-16 (the table's title carries the date), made on the host of that day:
  "the live file on the retired node-22 host lacked at classification time".

### D4. Sentences that change more than their subject

A present-tense statement about a host has to be true of today's hosts. Four places could not be
rewritten by exchanging the subject:

1. `slurm-gateway-node22-deployment`, the first requirement: "SHALL be deployed on node-22, because
   the generic chain submits only via the gateway and no such service is proven there today
   (`NHMS_SERVICE_ROLE=slurm_gateway` is currently reserved/fail-fast)". With the control host as
   the subject, "no such service is proven there today" is false: the gateway runs there, and
   the role is served by the standalone gateway app (the full business API still refuses it with
   `SERVICE_ROLE_RESERVED`, `apps/api/runtime_mode.py`). The SHALL keeps its form with the new
   subject; the rationale
   becomes a dated record: "When this was written, no such service was proven on the
   retired node-22 host, and `NHMS_SERVICE_ROLE=slurm_gateway` was reserved/fail-fast (recorded no
   later than 2026-07-25)". The text is in the repository's first commit, hence "no later than".
2. `production-topology-contract`: "the source acquisition handoff between the two nodes". The
   compute role is two hosts: "between node-27 and the compute role".
3. `node22-dbfree-runtime`: "evidence that proves the scheduler pass did not depend on node-22
   PostgreSQL". The requirement's title ("… proves absence of PostgreSQL use") and its scenario (no
   PostgreSQL host, port, advisory lock or psycopg-backed dependency in the evidence) are general:
   "did not depend on PostgreSQL".
4. The two dated records of D3.

Two more are worded for what the guard does, not for a place:

- `production-topology-contract`, scenario "Active node-22 DB writer assumptions are flagged": the
  scenario is the guard's codename leg, and the next scenario's THEN refers to it ("the same finding
  it reports for the codename"). "describes node-22 as the current NHMS active DB writer or asks
  operators to use node-22 local PostgreSQL" becomes "describes the compute role, by its codename,
  as the current NHMS active DB writer or asks operators to use a local PostgreSQL named with the
  codename".
- Same spec, "Compute hosts inherit every compute-role boundary": "a rule … restricts node-22 as the
  compute role" becomes "restricts the compute role, under a role term or under its codename",
  because after the sweep the rules themselves use the terms.

The download that the scheduler must not run is "a `download_source_cycle` of its own" and "a
download by the compute role".

### D5. What stays

| Text | Why |
|---|---|
| "`node-22` and `node22` SHALL remain the codename of the compute role in identifiers …" | the identifier sentence (slice 1.4); it defines the codename |
| "The guard reports on the compute role under each of its names: the codename, …" and the paragraph after it | the gate's vocabulary (slice 3.6) |
| scenario "Retired-host material keeps its meaning" | it defines what "node-22" means next to `:55433`; the issue's acceptance refers to it |
| `production-ops-readiness`: `"node-22" means in it or in its pages` | the banner scenario (slice 4.5): a mention of the word |
| `production-ops-readiness`: "the retired node-22 host as a current NHMS production database" | already the phrase |
| the three requirement titles about the retired host's database | kept by `appendix-titles.md` |
| `node22-unit-failure-alert`: "the gateway unit and `node22/**` contain none" | a path in code font; writing the full path would add an occurrence of a name token |
| five scenario titles | below |

**Scenario titles.** Five carry the codename. `openspec validate` and `openspec archive` refuse a
MODIFIED block that retitles a scenario (rehearsed for this slice: "MODIFIED … omits scenario(s) the
current spec still has: \"Raw-ready cycle skips node-22 download\"", archive aborted). The
umbrella's rule (`design.md` §3) is that such a title stays unless its requirement has another
reason to be removed and added again; none has.

- Four are about the retired host's database and are true as they stand: "Active node-22 DB writer
  assumptions are flagged", "Guardrails block node-22 DB drift", "Node-22 local PostgreSQL is marked
  do-not-connect", "Active env templates reject node-22 DB".
- One uses the codename for the role: "Raw-ready cycle skips node-22 download". It stays and is
  handed to the last slice (#3431) as residue. Its requirement is renamed, which RENAMED carries
  without re-adding it.

`appendix-titles.md` says scenario headings "are edited inside the MODIFIED block of their
requirement"; the validator does not allow that, and §3 of the umbrella's design is the rule that
applies.

### D6. Standing requirement or completed evidence

A scenario whose WHEN is a one-off act that is complete, and whose archived evidence names the
host, is a retired-host statement (10.1 D3). Applied here:

| Scenario | Reading | Term |
|---|---|---|
| `production-topology-contract`: "Retirement stop gate is bounded by archive and DB-free proof" | the stop of 2026-06-29 | retired host |
| `node22-db-retirement-cutover`: all six scenarios about `:55433` and the cutover | the stop of 2026-06-29 and the proof of 2026-06-28 | retired host |
| `production-topology-contract`: "Full post-retirement E2E remains separately tracked" | closed once (2026-11-16, on the retired host); the sentence names who schedules, which holds for that run and for any later claim | compute role |
| `slurm-gateway-node22-deployment`: "Gateway and scheduler share an owner-only credential", "Gateway is not remotely reachable", "Live auth boundary is proven without creating a job" | **standing**: they hold for the gateway wherever it is deployed, and it was deployed again on `w3` on 2026-11-23 | compute role / compute control host |

On the last row: the only tracked receipt for these three scenarios is
`docs/runbooks/receipts/2026-08-29-node22-slurm-gateway-auth.md`, captured on the retired host. No
tracked receipt shows the same probes on `w3`. The scenarios are requirements on a deployment, not
a record of that receipt, so they name the host of today; the missing receipt is reported, not
written by this slice. "(currently measured as 8090)" is true of `w3` (the gateway's health
endpoint answered on `127.0.0.1:8090` on 2026-11-28).

### D7. Validation

`node22-db-retirement-cutover` fails `openspec validate --type spec --strict` today for one reason
this slice does not touch: its Purpose is the placeholder `openspec archive` wrote. Writing a
Purpose is authoring, not a change of narration (10.1 D6). The six others are valid under
`--strict`. Shown: the structured report of each of the seven before any edit and after the
archive lists the same issues; without `--strict` all seven validate.

### D8. The counter and the gate

- 67 lines of the seven specs are `bare` or `quoted` today. 36 lose the codename; 18 carry it inside
  "the retired node-22 host" and stay `bare`; 13 are the kept lines of D5. No `name` token moves:
  no identifier is added or removed (the installer's title loses the codename and its body is not
  given the script's name).
- The gate also reads active changes (`openspec/changes`, archives aside), and this change quotes
  the old sentences. The hard gate's "before" is therefore the base commit `cb27ddc70`, without this
  change's directory, and its "after" is the head with the change archived; no push carries this
  change unarchived.
- The topology gate reads these specs. For a role term a denial has to sit in the clause of the
  name (umbrella §3). The check was run on the specs tree alone, before and after: no finding in
  either, and three synthetic sentences appended to one of the seven specs are flagged (the role as
  database writer; a local PostgreSQL on the compute control host; a local PostgreSQL under the
  codename), so the check does read these files and these terms. None of the 213 findings of the
  hard gate is in the seven specs.
