# Design: compute-role narration sweep

## Goals / Non-Goals

**Goals**

- A sentence about who does what names the role; a sentence about a machine names the machine. No
  sentence needs a banner to be read correctly.
- An operator can run every command of a live runbook as written.
- The topology hard gate sees the sentences the sweep writes.
- The next host move changes the topology runbook, the glossary entry and the instructions, not the
  repository.

**Non-Goals**

- Renaming identifiers, changing persisted values, rewriting history (lists in §3).
- Changing what any code does, except the gate's vocabulary (§2) and the text of the alert mail (§5).
- Retiring the forcing copyback backfill lane (CLI, spec and runbook).
- Correcting facts in the swept documents that have nothing to do with where compute runs (found
  ones are reported per PR).
- `openspec/changes/fix-node22-keliya-forecast-output-recompute/`: an active change that its own
  tasks call superseded. It is not a runbook and is outside the deletion decision; whether it is
  removed is the owner's call, asked separately.
- `docs/runbooks/feat-reach-geom-oq-findings.md`, `docs/runbooks/reach-geom-rollout-receipt.md` and
  `docs/governance/GOVERNANCE_8_FINAL_EVIDENCE_MAP.md`: historical with no inbound reference, and
  they do not concern the compute role. Reported to the owner, not deleted here.
- ADR 0008's text is host-specific ("按主机选择"). Whether it needs a superseding ADR is a decision
  about the ADR ledger. ADRs are history here and are not edited.

## 1. Vocabulary

`openspec/glossary.md` becomes the single source (group 2). No entry exists today; the files updated
by the first slice of #3345 use three vocabularies side by side (`appendix-inventory.md` §6).

| Term (zh / en) | Means | Used when |
|---|---|---|
| 计算角色 / compute role | what runs the scheduler, the Slurm gateway, Slurm and SHUD and writes artifacts to NFS; holds no live database | the sentence says who does or owns something |
| 计算控制主机 / compute control host | the host of the Slurm controller, the scheduler timers, the gateway and the journal; today `w3` | the sentence is about that machine: where a unit is installed, where a command is run |
| 计算主机 / compute hosts | the hosts jobs run on; today `w3` and `w2` | the sentence is about the machines as a set: mounts, exports, cores |
| 计算节点 / compute node | a node of the Slurm cluster; today `w3` and `w2` are the two | Slurm statements (a job lands on a node), and the instructions' existing "计算节点（w3 / w2）" lines |
| `w3`, `w2` | host names | only where the particular host matters: the two exports, SMT on `w2`, a command that must run on one of them |
| `node22`, `node-22` inside a name | the codename of the compute role in identifiers | never as a place in prose; when prose has to mention a name it is in code font |
| 原 node-22 主机 / the retired node-22 host | `210.77.77.22`, out of the compute role since 2026-11-23, rollback archive | history and rollback statements only |

- "22 节点", "22 侧", "22 compute-control", "计算控制面" are retired spellings.
- `compute_control` is the runtime service role of an API process (`NHMS_SERVICE_ROLE`), an
  identifier, and is not the compute role. The glossary states the difference.

## 2. The gate learns the role terms (group 1)

Today (`scripts/governance/entropy_audit/topology_predicates.py`): the DB-writer predicates match the
codename; `_topology_rewrite_compute_hosts` rewrites runs of `w3` / `w2` to the codename before they
run (#3358); a separate leg flags a compute host named together with a local PostgreSQL. None of the
terms of §1 is a subject, so a line that makes the role term the subject of a database-writer claim
is not seen by the gate.

Change: the four role and host-set terms of §1 (compute role, compute control host, compute hosts,
compute node, each in both languages, singular and plural) join the rewrite, with the same token
discipline as the host names (not inside a longer identifier: `compute_control`, `compute-role-x`,
a path). Both legs (DB writer, local PostgreSQL) read them.

- "计算节点" is included because the instructions already use it for `w3` / `w2`
  (`instructions/agents/shared.md`, pinned by `tests/test_node22_entrypoint_invariant.py`), and no
  compute node of this cluster may hold or write an NHMS database.
- The finding ids (`production-topology-node22-db-writer`, `production-topology-node22-local-postgres`)
  are identifiers and stay.
- **Acceptance is on the real repository**: the hard gate's finding multiset before and after is the
  same (247 / 0 today), or every difference is listed and judged. The role terms already occur in the
  files the first slice updated, next to database words in denials ("holds no live database"); the
  existing negation handling has to carry them. If it does not, that is a finding of this group, fixed
  in the predicates, not by rewording the documents.

## 3. What an occurrence is, and what happens to it

**The counter.** `narration_count.py` (this directory) walks the tracked files, leaves history out,
and files every line that matches `node-?22` under the strongest class of its occurrences:

- `name`: the codename is part of a longer token (file, script, unit, module, function, env key,
  schema id, capability name, CLI flag, marker, check id, a path). Unchanged by the sweep.
- `quoted`: the codename alone, in code font. A mention of the name; stays.
- `bare`: the codename alone in running text. This is what the sweep reads line by line.

It also counts literal `ssh` lines to the retired host and the bare "22" shorthand, which carry the
same stale narration without the codename. Baseline on `f5c3b487e`:

```text
area                    bare  quoted    name   total
docs/runbooks/           310       1     173     484
docs/                     28       1       1      30
openspec/specs/          194       1      36     231
openspec/                 13       0       1      14
instructions/             11       3       5      19
apps/                      8       0       9      17
packages/                 17       0       0      17
services/                 39       0      34      73
workers/                   4       0       0       4
scripts/                 132       0     277     409
infra/                    73       0      62     135
tests/                   363       1     656    1020
schemas/                   4       0       3       7
db/                        3       0       0       3
(root and other)           2       0       5       7
TOTAL                   1201       7    1262    2470
history (not swept): 3794 lines
ssh to the retired host: 44 lines
bare 22 shorthand: 39 lines
```

Every PR quotes the table before and after, and `--tokens` (the histogram of `name` tokens) before
and after. A `name` token count moves only when a file is deleted or a pointer to a deleted file is
removed; each such move is listed in the PR. The counter measures; it does not decide. A compound
such as `node-22-only` is filed under `name` although it is narration, and is found by reading.

**Not every bare line is rewritten.** These stay, and are what the last slice lists as the residue:

| Class | Where | Rule |
|---|---|---|
| persisted or compared value | evidence keys `node22`, `node22_touched`; forbidden-host sets; the receipt `source` default (`appendix-inventory.md` §1) | unchanged |
| `COMMENT ON` text in an applied migration | `db/migrations/000035_*.sql` | unchanged |
| gate vocabulary and the synthetic sentences its tests feed it | `scripts/governance/entropy_audit/topology_*.py`, `tests/test_entropy_audit_*` | unchanged; new vocabulary is added next to it (§2) |
| statements about the retired host | wherever the sentence is about `210.77.77.22`, port `:55433` or the Python 3.12.7 environment | reworded to "the retired node-22 host" / "原 node-22 主机" where they do not say so yet |
| dated observations inside a live page | "2026-06-22 现场验证 …" | keep their text: they say what was seen on that host on that day. Where the page gives them without a date, the date is added rather than the host renamed |

**Everything else is rewritten**, by these rules:

- **Sentences.** The new sentence is written from a read of the whole page or function, not by
  substitution: the subject is chosen from §1 by what the sentence is about (who does it, which
  machine, the set of machines). The PR's reviewer checks each rewritten sentence against §1.
- **Human text in code** (messages, help, finding texts, docstrings, comments): rewritten; where a
  test asserts the text, the test follows in the same PR.
- **Requirement titles.** A title that states where compute runs (18 found, e.g. "node-22 remains
  compute and Slurm Gateway only") is renamed with a RENAMED delta; a title in which `node-22` is part
  of a name stays. Capability names stay.
- **Headings that are anchors.** Two headings are link targets and one is a test's anchor
  (`infra/env/README.md` "node-22 unit → EnvironmentFile authority (#1694)", `known-issues-pipeline.md`
  §8.5): renamed with their inbound links and tests in the same PR.
- **Commands.** A literal `ssh -p 32099 frd_muziyao@210.77.77.22` line is removed. The step says where
  it runs (the compute control host, as the production user) and the page links once to the access
  section of `w2-w3-compute-topology.md`. No operator account, sudo policy or proxy endpoint is
  written into a runbook.
- **Banners.** A page loses its banner in the PR that rewrites its body. The scenarios of
  `production-ops-readiness` and `production-topology-contract` that demand the banners are narrowed
  by the slice that rewrites the pages they name and removed with the last of those pages (group 3 for
  the production-ops pages, group 4 for the rest). The rule that replaces them is this change's own
  requirement and is not added a second time by a slice.

## 4. Deletions

`appendix-deletions.md` has the per-file followers.

| File | Lines | Followers in the same PR |
|---|---|---|
| `qhh-continuous.md` | 486 | a section-marker entry in `tests/test_production_scheduler.py`; two inventory rows |
| `qhh-mvp-smoke-evidence.md` | 56 | inventory rows |
| `qhh-mvp-production-like-e2e-checklist.md` | 1197 | a section-marker entry; the requirement of `evidence-boundary-hardening` that demands the checklist |
| `qhh-controlled-failure-retry-evidence.md` | 94 | none outside the family |
| `two-node-production-e2e-plan.md` | 893 | `tests/test_readonly_db_validation_routes.py`; a scenario of `evidence-boundary-hardening`; three pointer sentences that name it as the authority for the display env |
| `node22-db-retirement-runbook.md` | 151 | two tests whose subject is the file |
| `qhh-22-business-bringup.md` | 510 | group 6: script header comments, an authority-invariant test that forbids its name, three citations in a forcing note |
| `qhh-backend-smoke.md` | 254 | group 6: about 12 lines move to `scripts/diagnostic/qhh/README.md`; a freshness anchor in the governance schema and in `apps/api/AGENTS.md` |

**Tests that protect something real get a successor, not a deletion:**

- `tests/test_readonly_db_validation_routes.py` pins the snippet "check mode 0600, then `source`, then
  run the validator". The rule is in `infra/env/README.md`; the snippet moves there and the test reads
  it there.
- `tests/test_qhh_scripts_static.py` pins the detached-root recipe of `qhh-backend-smoke.md`; the same
  recipe is in `scripts/diagnostic/qhh/README.md`, already pinned. The second pin and the freshness
  anchor move to the README.
- `tests/test_qhh_entrypoint_authority_invariant.py` forbids script headers from naming the bring-up
  runbook as their authority. With the file gone the rule becomes: a script header names no deleted
  runbook; the headers that cite it are corrected.

**Kept**: `forcing-copyback-backfill.md` (see the proposal), `two-node-deployment-overview.md`,
`display-readonly-live-mvt.md`, `LEGACY_DEAD_CODE_INVENTORY.md` (each is the authority for something
live or is read by a spec).

## 5. Deployed and visible text

| What | New text | Deployment |
|---|---|---|
| monitoring hint "需要恢复时在 22 compute-control 节点…" (`JobsTable.tsx`, `BasinFailures.tsx`), the C4 probe `c4DisplayEvidence/dom.ts`, the fake page, three tests, `display-readonly-ops-ui` | "…在计算控制主机…" | frontend rebuild on node-27; the C4 live receipt is taken with the new probe against the new page |
| retry 409 message; the OpenAPI description of the retired retry lane and the generated types | names the compute role | display API restart; `check:api-types` |
| messages and help of scripts and services (two asserted by tests) | §1 | next sync of the production checkout |
| 14 `Description=` lines | "NHMS compute …" in place of "NHMS node-22 …" | manifest shas; redeploy on the compute control host at a pass boundary; drift probe before and after; operator receipt in the PR (the pattern of #3401) |
| alert mail subject `[NHMS node-22] unit failed: …` and the `HOST` fallback `node-22` | `[NHMS compute] unit failed: …`; fallback `compute` | with the units; a mail filter on the old subject stops matching (the owner chose to change it) |

The alert test's sender string is the test's own input and stays.

The probe and the page change in one PR and are deployed in one step: between the merge and the
node-27 deployment the old page is served and the old probe text is still in the deployed bundle, so
no receipt is taken in that window.

## 6. Order and why

```text
1 gate vocabulary ──┬─> 3 production-ops pages ──┐
2 terms ────────────┼─> 4 other live docs ───────┼─> 10 specs ──> 11 tests
                    ├─> 5 deletions ──> 6 deletions with residue
                    ├─> 7 code text
                    ├─> 8 API and UI text
                    └─> 9 units and alert lane
```

- 1 before any prose: a sentence rewritten to the role term must be visible to the gate on the day it
  lands.
- 2 before any prose: the PRs cite the glossary instead of arguing wording.
- 5 before 4 where they meet: a pointer to a deleted file is rewritten once.
- 10 after 3 and 4: the banner scenarios change with the pages; the remaining specs do not depend
  on a page.
- 11 last: comments follow the text they describe.
- 3 to 9 are independent of each other apart from the files named above and may land in any order.

## 7. How the slices use OpenSpec

- This change carries the design, the task ledger, the appendices, the counter and one ADDED
  requirement that states the end state. It stays active until the last slice and is archived in that
  slice's final push, when the requirement is true.
- A slice that changes a capability spec writes its own change. Its MODIFIED and RENAMED blocks are
  generated from the main spec text of that day with asserted substitutions, and the change is
  archived in the slice PR's last push. A block written here today would replace the whole
  requirement at archive time, after other issues have edited it.
- A slice with no spec delta (docs, comments, deletions without a spec follower) has its issue body
  as the fixture, as the repository's rule for docs-only and test-only issues says.
- Every slice ticks its tasks in this change's `tasks.md` in its PR's final push. A task that runs
  after its PR is merged (a production deployment) is ticked in the next slice's PR.

## Sketch seams under test

- `topology_predicates`: synthetic lines per role term × predicate leg × negation, and the token
  boundaries (`compute_control`, a path, an upper-case spelling). The predicates are the unit the
  group changes.
- The real-repository hard gate (`tests/test_entropy_audit_report_contract.py`): finding multiset
  unchanged per PR. It is the highest seam: it reads the documents the sweep rewrites.
- Tests that pin prose (`test_role_boundary_static`, `test_node22_runbook_section_markers`,
  `test_node22_entrypoint_invariant*`, the two `AUTHORITY_HEADING` users): each follows its sentence in
  the PR that rewrites it, with a red run on the old sentence.
- Structure readers of rewritten pages (section markers, fences, tables, HTML comment markers): green
  unedited, or named when a heading they read changes.
- A static test that the production-ops pages and `current-production-ops.md` hold no `ssh` command
  addressed to the retired host (group 3): the one scenario of this change's requirement that a test
  can state exactly.
- The generated instruction roots: byte-exact regeneration test.
- The OpenAPI drift test and `check:api-types` for group 8; the C4 probe's unit test and the live
  receipt.
- `tests/test_node22_unit_tracking.py`, the drift probe and `tests/test_node22_unit_failure_alert.py`
  for group 9.
- `narration_count.py` per PR: evidence in the PR body, not a test.

## Not yet specified

- Whether a guard against the codename coming back as a place is worth having, and what it would
  check. The residue after the last slice decides it: if the remaining bare lines can be told from
  narration by form (the retired-host phrase, the gate's vocabulary files, a date), a guard is cheap;
  if not, it would be a list that only grows. Until then the requirement's scenario "live text does
  not use the codename as a place" is evidenced by the counter and by review, not by a test.
