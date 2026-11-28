# Sweep the compute-role narration repository-wide (#3345)

## Why

The compute role left the host "node-22" (`210.77.77.22`) on 2026-11-23 and runs on two LAN hosts,
w3 (control) and w2. The first slice of #3345 updated the agent instructions, added
`docs/runbooks/w2-w3-compute-topology.md` and put a banner on the runbooks that still say "node-22".
Everything else still narrates the old host: about 670 prose lines in docs and specs, about 160 lines
of human-read text in code, 31 literal `ssh ...@210.77.77.22` command lines in seven production-ops
pages, 16 unit descriptions, the alert mail subject, a UI hint, and the guards that pin that prose.
A reader has to carry the banner in their head for every sentence; an operator has to translate every
command.

Size (2026-11-27, history excluded): about 2500 lines in 390 files match `node-?22`; about half of
them carry the codename only inside a name and stay. `narration_count.py` in this change prints the
figures; `appendix-inventory.md` holds the classified inventory the slices work from.

## Owner decisions (grill gate, 2026-11-26 and 2026-11-27)

| Branch | Decision |
|---|---|
| Scope | repository-wide: production-ops bodies, other runbooks, specs, code comments, and the guards that pin that prose |
| Rule | change narration, keep identifiers: script names, unit names, env keys, schema ids, capability names, persisted field values, forbidden-host sets and the `COMMENT` text of an applied migration keep `node22` / `node-22` |
| What prose calls the compute side | the role term is primary (计算角色 / compute role); w3 / w2 are named only where the host matters; the topology hard gate learns the role term as a subject in the first PR |
| Deployed or visible text | all four classes change: frontend UI strings, API messages and OpenAPI descriptions, log / error / CLI help text, and the units' `Description=` lines with the alert mail subject |
| Purely historical runbooks | deleted (git keeps them), with the tests and spec text that read them |

## What Changes

Eleven task groups (order and dependencies in `tasks.md`). A group that is not atomic lands as
several PRs, first slice first; every PR goes through the issue workflow.

1. **Gate vocabulary**: the topology hard gate recognises the role terms as a subject, the way it
   recognises `node-22`, `w3` and `w2` today.
2. **Terms**: `openspec/glossary.md` defines the vocabulary; the agent instructions use it
   consistently; generated roots are regenerated.
3. **Production-ops pages**: the bodies are rewritten; the 31 literal `ssh` command lines go; the
   banners go with the pages that carried them, and the statement of which hosts carry the role moves
   into the body of the index page.
4. **Other live docs**: the remaining runbooks, `ROLE_BOUNDARY.md`, the project profile,
   `infra/env/README.md`, design docs.
5. **Historical runbooks with shared followers**: six files deleted.
6. **Historical runbooks that need a home for a residue first**: two files.
7. **Human text in scripts, services, packages, workers**: messages, help, docstrings, comments.
8. **API and UI text**: retry message and suggested action, two OpenAPI description chains, the
   monitoring hint and its live probe; node-27 deployment and live receipt.
9. **Units and alert lane**: the `Description=` lines of 16 deployed units, the mail subject and host
   fallback; manifest, a new receipt for the two receipt-pinned units, redeploy on the compute control
   host.
10. **Specs**: present-tense location statements in the remaining capability specs; requirement
    titles that state a location are renamed.
11. **Tests**: comments, docstrings and the literals that follow the earlier slices.

## Capabilities

This change carries one delta of its own: the end state, which becomes true with the last slice and
is archived then. A slice that changes a capability spec writes its own OpenSpec change, generates
its deltas from the main spec text of that day, and archives it in its PR's last push (`design.md`
§7). A capability has one owning task, or an order between the tasks that touch it (`design.md` §6). The table is the expected set; a slice's fixture is where the exact blocks are decided.

| Group | Capability | Kind | What |
|---|---|---|---|
| this change | `production-topology-contract` | ADDED | "Live text names the compute role and keeps the codename for names" |
| 1 | `production-topology-contract` | MODIFIED | "Static guardrails block topology drift": the role terms are subjects of both legs. "Current production topology is a governed contract": requirement names leave the list of identifiers that keep the codename |
| 2 | none | | the glossary and the instructions are not capability specs |
| 3 | `production-ops-readiness` | MODIFIED | "Current production ops runbook reflects verified 27-centric topology": the banner scenario goes; the scenarios that say what the runbook calls the compute side follow the page. The rule about commands is this change's own scenario and is not added again |
| 3, 4 | `production-topology-contract` | MODIFIED | the scenario "Runbooks that still say node-22 carry a banner" is narrowed by group 3 and removed by group 4 |
| 4 | none | | two spec sentences describe the table under the renamed `infra/env/README.md` heading without quoting it (`node22-unit-tracking`, `compute-scheduler-operationalization`); their narration is rewritten by the tasks that own those specs (9.2, 10.3) |
| 5 | `evidence-boundary-hardening` | MODIFIED, REMOVED | one scenario that names the deleted plan; the requirement that demands the deleted checklist |
| 6, 7 | none expected | | a spec sentence that quotes a moved recipe or a changed message is handled where found |
| 8 | `display-readonly-ops-ui` | MODIFIED | the recovery guidance requirement and scenario ("22-node", "22 compute-control") |
| 8 | `ops-monitoring-api` | MODIFIED, Purpose edit | the sentence of the retry requirement that names the manual lane; the Purpose line |
| 9 | `node22-unit-tracking` | MODIFIED, RENAMED | which receipt records which tracked file; the spec's narration and title. No spec text states the mail subject or the host fallback, so 9.1 has no delta |
| 10 | about 45 capability specs (`appendix-inventory.md` Appendix B, `appendix-titles.md`) | MODIFIED, RENAMED, Purpose edits | present-tense location statements; the renamed requirement titles (14 of the 18 that carry the codename, less the one 9.2 owns) |
| 11 | none | | comments and docstrings |

## Not in this change

- Identifiers and persisted values (the rule above). The classes are listed in `design.md` §3.
- History: `openspec/changes/archive/`, `docs/plans/`, `docs/runbooks/receipts/`, `docs/bugs.md`,
  `docs/review-loop-log.jsonl`, `docs/stage-pipeline-log.jsonl`, the 2026-10-16 entropy audit, ADRs,
  `progress.md`.
- `docs/runbooks/forcing-copyback-backfill.md` is not deleted: it calls itself historical, but its CLI
  (`services/tile_publisher/forcing_copyback_backfill.py`) is live code under test and
  `forcing-copyback-backfill` requires the operator documentation. Retiring that lane (CLI, spec,
  runbook) is a separate decision; here its narration is rewritten and its header corrected.
- Renaming anything that carries `node22` in its name (the one renamed test is in `design.md` §5).
- A guard against narration coming back (see `design.md`, "Not yet specified").

## Impact

- Docs: about 55 files rewritten, 8 deleted. Specs: about 50 capability specs touched by deltas.
- Code: human-read strings and comments only, plus the gate's vocabulary (behaviour) and the alert
  script's subject line (deployed text).
- Hosts: one redeploy of 17 unit files (16 descriptions, one comment) on the compute control host (group 9); one frontend rebuild and
  one display API restart on node-27 (group 8). Each is a production action with its own go-ahead.
