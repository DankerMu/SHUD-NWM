# Sweep the compute-role narration repository-wide (#3345)

## Why

The compute role left the host "node-22" (`210.77.77.22`) on 2026-11-23 and runs on two LAN hosts,
w3 (control) and w2. The first slice of #3345 updated the agent instructions, added
`docs/runbooks/w2-w3-compute-topology.md` and put a banner on the runbooks that still say "node-22".
Everything else still narrates the old host: about 670 prose lines in docs and specs, about 160 lines
of human-read text in code, 31 literal `ssh ...@210.77.77.22` command lines in seven production-ops
pages, 14 unit descriptions, the alert mail subject, a UI hint, and the guards that pin that prose.
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
3. **Production-ops pages**: the bodies are rewritten; the 31 literal `ssh` lines go; the banners go
   with the pages that carried them.
4. **Other live docs**: the remaining runbooks, `ROLE_BOUNDARY.md`, the project profile,
   `infra/env/README.md`, design docs.
5. **Historical runbooks with shared followers**: six files deleted.
6. **Historical runbooks that need a home for a residue first**: two files.
7. **Human text in scripts, services, packages, workers**: messages, help, docstrings, comments.
8. **API and UI text**: retry message, OpenAPI description chain, the monitoring hint and its live
   probe; node-27 deployment and live receipt.
9. **Units and alert lane**: 14 `Description=` lines, the mail subject and host fallback, env template
   comments; manifest, redeploy on the compute control host, operator receipt.
10. **Specs**: present-tense location statements in the remaining capability specs; requirement
    titles that state a location are renamed.
11. **Tests**: comments, docstrings and the literals that follow the earlier slices.

## Capabilities

This change carries one delta of its own: the end state, which becomes true with the last slice and
is archived then. A slice that changes a capability spec writes its own OpenSpec change, generates
its deltas from the main spec text of that day, and archives it in its PR's last push (`design.md`
§7). The table is the expected set; a slice's fixture is where the exact blocks are decided.

| Group | Capability | Kind | What |
|---|---|---|---|
| this change | `production-topology-contract` | ADDED | "Live text names the compute role and keeps the codename for names" |
| 1 | `production-topology-contract` | MODIFIED | "Static guardrails block topology drift": the role terms are subjects of both legs |
| 2 | none | | the glossary and the instructions are not capability specs |
| 3 | `production-ops-readiness` | MODIFIED | the runbook requirement loses its banner scenario; the rule about commands is this change's own scenario and is not added again |
| 3, 4 | `production-topology-contract` | MODIFIED | the scenario "Runbooks that still say node-22 carry a banner" is narrowed by group 3 and removed by group 4 |
| 4 | specs that quote a renamed heading or anchor of `infra/env/README.md` | MODIFIED | found by search in the slice's fixture; none is known today |
| 5 | `evidence-boundary-hardening` | MODIFIED, REMOVED | one scenario that names the deleted plan; the requirement that demands the deleted checklist |
| 6, 7 | none expected | | a spec sentence that quotes a moved recipe or a changed message is handled where found |
| 8 | `display-readonly-ops-ui`, `ops-monitoring-api` | MODIFIED | the recovery guidance text; the description of the retired retry lane |
| 9 | `node22-unit-failure-alert` | MODIFIED | the mail subject and the host fallback |
| 10 | about 45 capability specs (`appendix-inventory.md`, Appendix B) | MODIFIED, RENAMED | present-tense location statements; 18 requirement titles |
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
- Renaming anything that carries `node22` in its name.
- A guard against narration coming back (see `design.md`, "Not yet specified").

## Impact

- Docs: about 55 files rewritten, 8 deleted. Specs: about 50 capability specs touched by deltas.
- Code: human-read strings and comments only, plus the gate's vocabulary (behaviour) and the alert
  script's subject line (deployed text).
- Hosts: one redeploy of 14 unit files on the compute control host (group 9); one frontend rebuild and
  one display API restart on node-27 (group 8). Each is a production action with its own go-ahead.
