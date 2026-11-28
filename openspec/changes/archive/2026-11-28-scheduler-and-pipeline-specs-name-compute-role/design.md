# Design: the scheduler and pipeline specs name the compute role

The umbrella `sweep-compute-role-narration` is the design of the sweep (`design.md` §1 vocabulary,
§3 rules, `appendix-titles.md`). This document records what this slice decides inside those rules.

## Decisions

### D1. The deltas are generated

As in slices 10.1 and 10.2: one table of fragments (`sentences.md`), each asserted to occur exactly
once in its spec; each changed requirement is emitted whole as a MODIFIED block from the main spec
of the day; the two titles go through RENAMED. After `openspec archive` the six Purpose texts are
edited in the main specs and every main spec is compared with its expectation (the spec before,
with the replacements applied). The whole cycle was rehearsed on a copy of the specs tree with
this table: 26 MODIFIED blocks, 2 of them under a renamed title (`openspec archive` reports
`~ 25, → 2`: one block changes in its title only), 11 of 11 specs identical to the expectation, and
the strict report of each of the eleven lists the same issues before and after (no requirement
crosses the validator's 500-character note that was not over it before).

One spec changes its number of lines (`scheduler-stall-alerting`, by one, in its Purpose).

### D2. The term of each sentence

By what the sentence is about (umbrella §1):

| Term | Sentences |
|---|---|
| the compute role (who does or owns), 41 | the scheduler ("the compute role's scheduler", the wording of 10.1 and 10.2), its DB-free Orchestrator, its lanes (file-provider refresh, manual-repair marker, journal, DB-free scheduler lane), its installers, probes and entrypoints, its units, its scheduler environment and file plane, its registry manifest and dispatch set, its production, its succession, the account it writes as, a download it would perform |
| the compute control host (the machine of the units and operator actions), 6 | where production passes are business-runnable, the host an operator provisions, where the succession script and the backfill command are run, whose generic alert mails a failed unit, whose `OnFailure=` alert lane forwards it |
| the compute hosts (the machines as a set), 8 | repository, interpreter and object-store paths, the fixed topology identity `/ghdc/data/nwm/object-store`, the frozen checkout, storage private to them |
| the retired node-22 host, 9 | D3 |

Where the same thing is named in two specs it gets the same words: "the file-provider refresh lane
of the compute role" (`scheduler-registry-refresh` twice, `variant-activation-cutover`), "the
compute role's database-free scheduler environment", "storage private to the compute hosts"
(`production-scheduler-orchestration`, `canonical-precip-copyback`).

### D3. Sentences about the retired host

Nine sentences are about `210.77.77.22`:

- Its database. `node27-download-orchestration`: the preflight "rejects display_readonly-like
  credentials and historical PostgreSQL endpoints of the retired node-22 host such as `:55433`",
  and "the historical PostgreSQL of the retired node-22 host is not read or written for
  source-cycle truth". Both are standing rules on node-27's download runner; the retired host's
  database is their object, not their addressee. `forcing-domain-handoff`: the receipt proves that
  readiness "did not depend on implicit access to the DB of the retired node-22 host", and the
  mirror requirement (title kept by the umbrella) speaks of "any archived rollback forcing mirror
  path of the retired node-22 host".
- `forcing-domain-handoff`, first requirement: "without querying an active node-22 database". The
  only database that ever stood on the compute side is the one of the retired host; the compute
  role has none. "without querying the database of the retired node-22 host". The word "active" is
  dropped: that database is stopped, and the requirement is that the handoff carries enough for
  node-27 never to need it.
- Four dated observations (D4.4).

### D4. Sentences that change more than their subject

A present-tense statement about a host has to be true of today's hosts.

1. **`scheduler-stall-alerting`, Purpose.** Two statements are false of today's hosts: "the journal
   and a failed unit being the only alerting channel that host has", and "node-27 has a database and
   a mail lane, node-22 has neither". The compute control host has a mail lane: a failed unit is
   mailed by the generic unit-failure alert (`nhms-node22-unit-failure-alert@.service`; the spec's
   own first requirement says "the failed unit is what the host's `OnFailure=` alert lane
   forwards", and the stall-health unit carries that `OnFailure=`). What stays true, and is the
   point of the paragraph, is that the probe itself has no state and sends nothing. The text
   becomes: "the journal and a failed unit being the only alerting signals it gives (the generic
   unit-failure alert of the compute control host mails a failed unit)", and "The two differ where
   their sides differ: node-27 has a database and a mail lane of its own; the compute role has no
   database and the probe sends nothing itself, so this capability carries no persistent state and
   no outbound channel." The alert's capability is not named: no `name` token is added.
   In the first requirement of the same spec "the failed unit is what the host's `OnFailure=` alert
   lane forwards" loses its antecedent once the requirement's subject is the role: "what the compute
   control host's `OnFailure=` alert lane forwards".
2. **Where the journal and the object store are.** Every host, the exporting one included, reaches
   `/scratch/frd_muziyao` through an NFS client (`docs/runbooks/w2-w3-compute-topology.md`, storage
   table and the rule under it); "local" is no longer true of the journal and object-store roots.
   What the three sentences need is that those roots are the compute side's own and are not the
   NFS shared with node-27:
   - `canonical-precip-copyback`: "On node-22 the copyback root is the shared NFS mount while the
     journal and object-store roots are local" becomes "On the compute hosts the copyback root is
     the NFS mount shared with node-27 while the journal and object-store roots are on the compute
     hosts' own storage".
   - same spec: "the receipt lives in the scheduler journal on node-22's local disk, so a
     display-side reader cannot see it" becomes "on storage private to the compute hosts".
   - `production-scheduler-orchestration`: "the pass receipts and journal live on node-22 private
     storage it cannot reach" becomes "on storage private to the compute hosts that it cannot
     reach".

   The words "the local object-store root" and "the local tree" in other sentences of
   `canonical-precip-copyback` name the object-store root as against the copyback root; they are
   not rewritten. The hourly journal backup of the compute control host puts a copy of the journal
   on the NFS shared with node-27 (`operator-archives/`); that is a disaster-recovery copy, not a
   surface a display-side reader is given, and the two sentences speak of where the journal lives.
3. `production-scheduler-orchestration`: "WHEN node-22 runs the compute scheduler" becomes "WHEN the
   compute role runs its scheduler".
4. **Dated observations.** Each was made on the host of its day and gets host and date:
   - `production-scheduler-orchestration`, twice: "Measured at the live node-22 evidence root on
     2026-09-16" becomes "Measured on 2026-09-16 at the then-live evidence root on the retired
     node-22 host". The first goes on "the two counts are equal on every retained pass, so the
     boundary is presently empty in production": a present-tense statement about production that
     was not measured again on `w3`. It becomes "the two counts were equal on every retained pass,
     so the boundary was then empty in production while remaining structurally real". The second
     reports what the measurement found ("every retained pass carries …", then "The same
     measurement found …") and keeps its wording.
   - `canonical-precip-copyback`: "the node-22 receipt
     `docs/runbooks/receipts/2026-09-06-precip-copyback-backfill.md` §6.1 observed" becomes "the
     receipt `…` (taken on the retired node-22 host) §6.1 observed". §6.1 of that receipt reads the
     scheduler journal of that host.
   - same spec: "because the node-22 operation in this change's tasks invokes it without a flag"
     becomes "because the operation in this change's tasks (run on the retired node-22 host,
     2026-09-06) invoked it without a flag". The operation is §2 of the same receipt.
5. `variant-activation-cutover`: the list item "node-22 `scripts/node22_model_succession.py` runs
   the steps" becomes "on the compute control host, `scripts/node22_model_succession.py` runs the
   steps". The item before it ("node-27 `scripts/provision_…`") keeps its form.

**"The frozen checkout of the compute hosts never needs an environment built"**
(`canonical-precip-copyback`, twice) is a present-tense statement about today's hosts. Its basis:
the production checkout's environment is not synced in daily operation (root instructions, the
compute-node exception; `docs/runbooks/w2-w3-compute-topology.md`), and the property the sentence
protects is that the backfill script imports only the standard library and one in-tree module, so
running it never requires an environment to be created.

### D5. What stays

| Text | Why |
|---|---|
| scenario titles "bounded node-22 live pass", "concurrent node-22 business pass" (`production-scheduler-orchestration`) | below |
| scenario title "NFS raw manifest handoff is complete before node-22 scheduling" (`node27-download-orchestration`) | below |
| scenario title "The node-22 removal has not finished" (`basin-retirement`) | below |
| requirement title "Archived/stopped compatibility-only node-22 rollback mirror is explicit-DSN, allow-flagged, and sunset/removal-bound" (`forcing-domain-handoff`) | kept by `appendix-titles.md`: it is about the retired host |
| `scripts/node22_model_succession.py`, `scripts/node22_publish_merged_scheduler_registry.py`, `nhms-node22-unit-failure-alert@.service`, `N22_DSN` | identifiers |

**Scenario titles.** `openspec validate` and `openspec archive` refuse a MODIFIED block that
retitles a scenario (rehearsed in 10.2). The umbrella's rule (`design.md` §3) is that such a title
stays unless its requirement has another reason to be removed and added again; none has. All four
use the codename for the role. They stay and are handed to the last slice (#3431) as residue, with
the one of 10.2.

### D6. The superseded active change

`openspec/changes/fix-node22-keliya-forecast-output-recompute/` is removed (the owner's decision of
2026-11-28, recorded on #3430). The umbrella left it to the owner (`design.md` Non-Goals, §6, §7).

- What it was: six files, last touched by `cc4624b9c`; all five tasks ticked, two of them with the
  note "Superseded (2026-11-16 hygiene)"; two MODIFIED deltas, "retry classification" of
  `job-retry-mechanism` and "Backend scheduler entrypoint" of `production-scheduler-orchestration`.
- It was never archived, and it could not be archived as written: `job-retry-mechanism` has no
  requirement titled "retry classification", and its block of "Backend scheduler entrypoint"
  carries two scenarios of its own and omits the six the main spec has, which `openspec` refuses
  (the refusal 10.2 rehearsed for a dropped scenario).
- Why now: this slice rewrites "Backend scheduler entrypoint". That block quotes the requirement's
  old first paragraph; made to fit and archived later, it would put the old sentences back.
- It is removed before the deltas are generated. No other active change holds a delta of a
  requirement this slice modifies (the umbrella's own delta adds one requirement to
  `production-topology-contract`).
- What the removal alone moves, measured on the base commit with only that directory removed:
  the hard gate's findings are the same multiset (213, none was in that change);
  `narration_count.py`, row `openspec/`: 12 / 1 / 1 becomes 2 / 1 / 0 (bare / quoted / name), total
  673 / 11 / 1247 becomes 663 / 11 / 1246. **One `name` token leaves: the change's own name.**
- Reported, not acted on: the three scenarios that change wrote are in no main spec, before or
  after the removal. "downstream retry is widened when upstream forecast output is absent" (reason
  `missing_forecast_output_recompute`) and "downstream output stage cannot mask missing forecast
  output" (`restart_stage=forecast`; exhausted downstream retries do not block permanently)
  describe behaviour that is in the code and its tests
  (`services/orchestrator/scheduler_state_failure.py`); the third ("all registered basins
  business-ready on node-22") is the 13-basin acceptance its tasks call superseded. The text is in
  the repository's history at `78c3ba631`.

Four places of the umbrella call the decision open. Each gets the decision and its date; nothing
else in them changes:

| Place | Added or replaced |
|---|---|
| `tasks.md` 10.3: "**Stop: needs the owner's decision on the active change `fix-node22-keliya-forecast-output-recompute`** (`design.md` §6)." | "The owner's decision on the active change `fix-node22-keliya-forecast-output-recompute` (`design.md` §6) was given on 2026-11-28: it is removed in this slice." |
| `design.md` Non-Goals: "… whether it is removed is the owner's call, asked separately." | appended: "(Decided on 2026-11-28: removed in slice 10.3.)" |
| `design.md` §6, the row of 10.3: "the owner's decision on `fix-node22-keliya-forecast-output-recompute`" | appended in the cell: "(given on 2026-11-28: removed in 10.3)" |
| `design.md` §7: "… They are reported in the final residue with the owner's decision on it." | appended: "(Removed in slice 10.3 on the owner's decision of 2026-11-28; the lines went with it.)" |

The umbrella's `appendix-inventory.md` snapshots that list the change stay for the last slice.

### D7. Validation

`canonical-precip-copyback` and `forcing-domain-handoff` fail `openspec validate --type spec
--strict` today for one reason this slice does not touch: their Purpose is the placeholder
`openspec archive` wrote. Writing a Purpose is authoring, not a change of narration (10.1 D6). The
nine others are valid under `--strict`. Shown: the structured report of each of the eleven before
any edit and after the archive lists the same issues; without `--strict` all eleven validate.

### D8. The counter, the gate and the one quote

- 67 lines of the eleven specs are `bare` today. 53 lose the codename; 9 carry it inside "the
  retired node-22 host" and stay `bare`; 5 are the kept titles of D5. No identifier is added or
  removed in the specs, so no `name` token moves through them; the only one that moves is the
  removed change's name (D6).
- The gate also reads active changes (`openspec/changes`, archives aside), and this change quotes
  the old sentences. The hard gate's "before" is therefore the base commit `78c3ba631`, and its
  "after" is the head with this change archived and the superseded change removed; no push carries
  this change unarchived.
- The topology gate reads these specs. For a role term a denial has to sit in the clause of the
  name (umbrella §3): "the compute role has no database" does. The check was run on the specs tree
  alone, before and after: no finding in either, and three synthetic sentences appended to one of
  the eleven specs are flagged (the role as database writer; a local PostgreSQL on the compute
  control host; a local PostgreSQL under the codename), so the check does read these files and
  these terms. None of the 213 findings of the hard gate is in the eleven specs.
- **One quote outside `openspec/`.** Each old title and each line of an old fragment that carries
  the codename was searched outside `openspec/` (65 searches). One hit:
  `tests/test_entropy_audit_topology_authority.py` holds the whole first paragraph of "Out-of-pass
  deletion surfaces respect the pipeline frontier or fail closed" inlined as one case of a
  parametrized test (a line the mirror-fallback check must not flag). The test does not read the
  spec, and its comment already cites a line number the spec left long ago; after this slice the
  inlined text is the wording before it. It stays a valid case of that test. Test text is slice
  11.1's.
