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

- "22 节点", "22 侧", "22-node", "22 compute-control", "计算控制面" are retired spellings.
- "node 22" and "node_22" are spellings of the codename and are treated like `node-22`. A hyphenated
  compound in prose ("node-22-local", "node-22-only") is narration, not a name.
- `compute_control` is the runtime service role of an API process (`NHMS_SERVICE_ROLE`), an
  identifier, and is not the compute role. The glossary states the difference.

## 2. The gate learns the role terms (group 1)

Today (`scripts/governance/entropy_audit/topology_predicates.py`): the DB-writer predicates match the
codename; `_topology_rewrite_compute_hosts` rewrites runs of `w3` / `w2` to the codename before they
run (#3358); a separate leg covers a local database named for one of the two hosts. None of the
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
- The same slice amends the sentence of `production-topology-contract` that lists the identifiers
  which keep the codename ("script names, unit names, env keys, receipt schema ids, spec and
  requirement names"): capability names stay in the list, requirement titles leave it (§3). It lands
  first, so no RENAMED delta of a later slice contradicts a SHALL in force.
- **Acceptance is on the real repository**: the hard gate's finding multiset before and after is the
  same (247 / 0 today), or every difference is listed and judged. The role terms already occur in the
  files the first slice updated, next to database words in denials ("holds no live database"); the
  existing negation handling has to carry them. If it does not, that is a finding of this group, fixed
  in the predicates, not by rewording the documents.

## 3. What an occurrence is, and what happens to it

**The counter.** `narration_count.py` (this directory) walks the tracked files, leaves history out,
and files every line that matches the codename (`node22`, `node-22`, `node 22`, `node_22`) under
the strongest class of its occurrences:

- `name`: the codename is part of a longer token (file, script, unit, module, function, env key,
  schema id, capability name, CLI flag, marker, check id, a path). Unchanged by the sweep.
- `quoted`: the codename alone, in code font. A mention of the name; stays.
- `bare`: the codename alone in running text, or in a hyphenated prose compound
  ("node-22-local"). This is what the sweep reads line by line.

It also counts literal `ssh` lines to the retired host and the bare "22" shorthand, which carry the
same stale narration without the codename. Baseline on `f5c3b487e`:

```text
area                    bare  quoted    name   total
docs/runbooks/           311       1     172     484
docs/                     28       1       1      30
openspec/specs/          195       1      36     232
openspec/                 13       0       1      14
instructions/             12       3       4      19
apps/                     12       0       9      21
packages/                 17       0       0      17
services/                 40       0      33      73
workers/                   4       0       0       4
scripts/                 135       0     274     409
infra/                    79       0      62     141
tests/                   368       1     652    1021
schemas/                   4       0       3       7
db/                        3       0       0       3
(root and other)           3       0       5       8
TOTAL                   1224       7    1252    2483
history (not swept): 3819 lines
ssh to the retired host: 44 lines
bare 22 shorthand: 41 lines
```

Every PR quotes the table before and after, and `--tokens` (the histogram of `name` tokens) before
and after. A `name` token count moves only when a file is deleted or a pointer to a deleted file is
removed; each such move is listed in the PR. The counter measures; it does not decide.

The 44 `ssh` lines are 31 commands in seven production-ops pages, 7 banner pointers, 3 banners, 1
line of the bring-up runbook and 2 lines of spec scenarios. The count does not end at zero: the
scenarios that forbid the command, this change's own among them, match it.

**Not every bare line is rewritten.** These stay, and are what the last slice lists as the residue:

| Class | Where | Rule |
|---|---|---|
| persisted or compared value | evidence keys `node22`, `node22_touched`; forbidden-host sets; the receipt `source` default (`appendix-inventory.md` §1, without the alert script's two lines, which §5 changes); the `reason` text of the entry in `config/calibration_overrides.yaml` (copied into package manifests; the entry has its own `date`) | unchanged |
| a sentence that defines the codename | the instructions' "历史代号" line, the contract's identifier sentence, the topology runbook's opening | stays; the codename is in code font there |
| a test's own input | the alert test's sender string, `--checked-by node-22-operator`, synthetic repositories | unchanged |
| text in an applied migration: the `COMMENT ON` string and the `--` comments | `db/migrations/000035_*.sql`, `db/migrations/000061_*.sql` | unchanged: `tests/test_migrations.py` pins every applied migration file byte for byte (#2634) |
| gate vocabulary and the synthetic sentences its tests feed it | `scripts/governance/entropy_audit/topology_*.py`, `tests/test_entropy_audit_*` | unchanged; new vocabulary is added next to it (§2) |
| statements about the retired host | wherever the sentence is about `210.77.77.22`, port `:55433` or the Python 3.12.7 environment | reworded to "the retired node-22 host" / "原 node-22 主机" where they do not say so yet |
| dated observations inside a live page | "2026-06-22 现场验证 …" | keep their text, and the tests that pin them stay: they say what was seen on that host on that day. Where the page gives them without a date, the date is added rather than the host renamed |

**Everything else is rewritten**, by these rules:

- **A term stays on one line.** The topology gate reads lines, and it recognises a role term only
  inside one line (#3403). Rewritten prose does not break a term across a line end ("compute" at the
  end of a line, "role" at the start of the next).
- **A compound of the codename becomes an open phrase.** The gate reads a hyphenated longer name
  as an identifier, not as a name of the role (#3403), so "node-22-local" is not rewritten to a
  term plus "-local" but to words ("local to the compute hosts").
- **A denial stays with the name it denies.** For a role term or a host name the gate's
  local-database leg reads one clause: a denial on the next line or in the next sentence does not
  cover it, where with the codename a following line could. The gate ends a clause at a comma,
  a semicolon, a full stop (English or Chinese), a spaced hyphen and at "while", "whereas",
  "but". A rewritten sentence that denies keeps the denial in the clause of the name.
- **Sentences.** The new sentence is written from a read of the whole page or function, not by
  substitution: the subject is chosen from §1 by what the sentence is about (who does it, which
  machine, the set of machines). The PR's reviewer checks each rewritten sentence against §1.
- **Human text in code** (messages, help, finding texts, docstrings, comments): rewritten; where a
  test asserts the text, the test follows in the same PR.
- **Requirement titles.** 18 titles carry the codename (`appendix-titles.md` lists each with its
  class). The 4 about the retired host or its database are kept. The other 14 use the codename for the
  role, or as an adjective for something the role owns, and are renamed with a RENAMED delta (e.g.
  "Node-22 Slurm submission path"). Capability names stay.
- **Scenario titles.** The validator refuses a MODIFIED block that omits a scenario of the current
  spec, so a MODIFIED block can change the text under a scenario but cannot retitle or drop it. A
  slice that has to retitle or drop a scenario removes the requirement and adds it back under a new
  title (REMOVED and ADDED in one change, as `retire-docker-compose-lane` did), and its PR shows
  that every other clause is carried over. A scenario title that carries the codename and is
  otherwise true stays when its requirement has no other reason to be re-added, and is listed in
  the final residue.
- **Spec Purpose lines.** A delta cannot carry Purpose text. A slice edits the Purpose of a main spec
  directly, in the push that archives its change (as `2bdc7a71d` did for five specs).
- **Headings that are anchors.** Three headings are link targets, one of them also a test's anchor:
  `infra/env/README.md` "node-22 unit → EnvironmentFile authority (#1694)" (two tests, three inbound
  links), `known-issues-pipeline.md` §8.5 (five inbound links), `recalibration-and-archive.md` §5.8
  (one inbound link from `current-production-ops.md`). The PR that renames a heading updates every
  inbound link, in whichever page it sits, and shows a repository-wide search for the old slug
  returning nothing. The repository has no link checker.
- **Commands.** A literal `ssh -p 32099 frd_muziyao@210.77.77.22` line is removed. The step says where
  it runs (the compute control host, as the production user) and the page links once to §1 of
  `w2-w3-compute-topology.md` ("主机与角色"), which says how the two hosts are reached. No operator
  account, sudo policy or proxy endpoint is written into a runbook.
- **The host statement stays.** `production-topology-contract`, "The compute role names its hosts",
  requires the instructions, `ROLE_BOUNDARY.md` and `current-production-ops.md` to name `w3` and `w2`,
  to say that `210.77.77.22` is retired and a rollback archive only, and to link the topology runbook.
  On `current-production-ops.md` two of the three are inside the banner today. The rewrite moves the
  statement into the body; the scenario stays as it is.
- **Banners.** A page loses its banner in the PR that rewrites its body. The scenarios of
  `production-ops-readiness` and `production-topology-contract` that demand the banners are narrowed
  by the slice that rewrites the pages they name and removed with the last of those pages (group 3 for
  the production-ops pages, group 4 for the rest). Narrowing changes the text under a scenario;
  removing one re-adds its requirement under a new title (see "Scenario titles"). The rule that replaces them is this change's own
  requirement and is not added a second time by a slice.
- **Scenarios bound to a page's wording.** `production-ops-readiness`, "Current production ops runbook
  reflects verified 27-centric topology", has scenarios that say what the runbook calls the compute
  side ("node-22 remains compute and Slurm Gateway only", "shared artifact paths use both node
  perspectives"). The slice that rewrites `current-production-ops.md` rewrites them in the same
  change, so the page and the spec change together.
- **Pages the gate skips.** `forcing-copyback-backfill.md` and `two-node-deployment-overview.md` are
  not scanned by the topology gate because their opening declares the whole document not current.
  They keep that declaration, and the words "historical" / "归档" a test requires. The PR shows the
  gate's skip decision for both paths before and after.

## 4. Deletions

`appendix-deletions.md` has the per-file followers.

| File | Lines | Followers in the same PR |
|---|---|---|
| `qhh-continuous.md` | 486 | a section-marker entry in `tests/test_production_scheduler.py`; two inventory rows |
| `qhh-mvp-smoke-evidence.md` | 56 | inventory rows |
| `qhh-mvp-production-like-e2e-checklist.md` | 1197 | a section-marker entry; the requirement of `evidence-boundary-hardening` that demands the checklist |
| `qhh-controlled-failure-retry-evidence.md` | 94 | none outside the family |
| `two-node-production-e2e-plan.md` | 893 | two tests of `tests/test_readonly_db_validation_routes.py`; a scenario of `evidence-boundary-hardening`; three pointer sentences that name it as the authority for the display env; links in `two-node-deployment-overview.md` and one governance inventory |
| `node22-db-retirement-runbook.md` | 151 | one test that walks the runbook and its receipt (the runbook leaves the tuple, the receipt stays) |
| `qhh-22-business-bringup.md` | 510 | group 6: script header comments, a front-matter test, an authority-invariant test that forbids its name, three citations in a forcing note, pointers in two production-ops pages, a link in a test fixture's README, an inventory row |
| `qhh-backend-smoke.md` | 254 | group 6: about 12 lines move to `scripts/diagnostic/qhh/README.md`; a freshness anchor in the governance schema, the synthetic instruction files that mirror it, and `apps/api/AGENTS.md`; a citation in a forcing note; rows of both governance inventories |

**Tests that protect something real get a successor, not a deletion:**

- `tests/test_readonly_db_validation_routes.py` pins the plan's snippet twice: "check mode 0600, then
  `source`, then run the validator", and the validator command's evidence arguments (a parent
  directory, `--evidence-root`, `--run-id`, no doubled run-id directory). The rule is in
  `infra/env/README.md`; the whole snippet, validator command included, moves there, and both
  assertions read it there.
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
| monitoring hint "失败诊断用于交给 22 compute-control 节点处理；…" (`JobsTable.tsx`, `BasinFailures.tsx`, both variants each), the fake page's text, three tests, the two "22-node" lines of `display-readonly-ops-ui`, the frontend comment lines | "…交给计算控制主机处理…" | frontend rebuild on node-27 |
| the C4 probe `c4DisplayEvidence/dom.ts` | its main test is the element `ops-manual-recovery-guidance`, which does not change; its fallback text ("需要恢复时在 22 compute-control") follows the fake page | with the frontend; the probe passes on the old and on the new page, so the C4 live receipt can be taken at any time after the deployment |
| retry 409 message and the two `suggested_action` texts of the control-plane refusal ("Ask a node 22 operator …"; the retry one is asserted by `tests/test_retry_cancel_consistency.py`) | name the compute role | display API restart for the suggestion; the message is returned by a `compute_control` / `dev_monolith` API only, and none is deployed since #3394 |
| two OpenAPI description chains (the retired retry lane; the "compute_control runbook on node 22" sentence), each patching code → `openapi/nhms.v1.yaml` → generated types | name the compute role | display API restart; `check:api-types` |
| messages and help of scripts and services (one asserted by two tests) | §1 | next sync of the production checkout |
| `Description=` of 16 manifest units: 14 say "NHMS node-22 …", and the scheduler and gateway units under `infra/systemd/node22/` end with "(node 22 user)"; unit comments (one more manifest unit, `nhms-w3-journal-backup.service`, changes by a comment only: 17 files) | "NHMS compute …"; "(compute role user)" | manifest shas; redeploy on the compute control host at a pass boundary; `daemon-reload`, no restart (a description is metadata); drift probe before and after; operator receipt in the PR (the pattern of #3401) |
| the example unit `infra/systemd/nhms-slurm-gateway.service` (two comments, its `Description=`) | §1 | none: not installed |
| alert mail subject `[NHMS node-22] unit failed: …` and the `HOST` fallback `node-22` | `[NHMS compute] unit failed: …`; fallback `compute` | next sync of the production checkout (the script runs from it; it is not an installed file); a mail filter on the old subject stops matching (the owner chose to change it) |

- The alert test's sender string is the test's own input and stays.
- The two units under `infra/systemd/node22/` are pinned byte-exactly to the read-only receipt of
  2026-10-16 by `tests/test_node22_unit_tracking.py`, and `node22-unit-tracking` says which receipt
  records which file. Receipts are history. The redeploy therefore writes a new receipt for the two
  files (as #3359 did for the gateway drop-in). Unlike that drop-in, the two paths are already in the
  2026-10-16 receipt, so the test's rule that no path is in two receipts becomes: the latest receipt
  that records a path holds its sha. The spec sentence follows in the same slice.

## 6. Order and why

```text
1 gate vocabulary ──┬─> 5 deletions ──> 6 deletions with residue ──┬─> 3 production-ops pages ──┐
2 terms ────────────┤                                              ├─> 4 other live docs ───────┼─> 10 specs ──> 11 tests
                    │                                              └─> 7 code text              │
                    ├─> 8 API and UI text ──────────────────────────────────────────────────────┤
                    └─> 9 units and alert lane ─────────────────────────────────────────────────┘
```

- 1 before any prose: a sentence rewritten to the role term must be visible to the gate on the day it
  lands.
- 2 before any prose: the PRs cite the glossary instead of arguing wording.
- 5 and 6 before the pages that point at the deleted files: a pointer to a deleted file is removed
  once, not rewritten and then removed.
- 10.2 after 3.6 and 4.5: they edit the same two contract specs.
- 11 last: comments follow the text they describe; its archive needs the receipts of 8.3 and 9.2.

The diagram is the general shape. The edges that bind are per task, and each comes from a shared
file:

| Later task | After | Shared |
|---|---|---|
| 5.2 | 5.1 | `evidence-boundary-hardening`; both governance inventories |
| 6.1 | 5.1, 5.2 | one bullet of each governance inventory names files of all three |
| 6.2 | 5.1 | one row of `LEGACY_DEAD_CODE_INVENTORY.md` names the bring-up runbook and `qhh-continuous.md` |
| 3.4 | 6.2 | the bring-up pointer in `stuck-detection.md` |
| 3.5 | 3.4 | the §8.5 link lines in `file-provider-refresh.md` and `gateway-and-services.md` |
| 3.6 | 3.1 to 3.5, 6.2 | the banner explains every production-ops page; the §5.8 and §8.5 link lines and the bring-up pointers in `current-production-ops.md`; the `ssh` test is green only when the last command is gone |
| 4.2 | 6.1, 6.2 | `scripts/diagnostic/qhh/README.md`; the forcing note |
| 4.3 | 5.2 | the snippet 5.2 adds to `infra/env/README.md` |
| 4.4 | 5.2 | the pointer sentences of `display-readonly-live-mvt.md` |
| 4.5 | 3.4, 3.6, 4.3, 5.2 | the §8.5 link in the manual-recovery runbook; the banner scenario; the README anchor inside the sentence `tests/test_node22_runbook_section_markers.py` pins; the plan links in the overview |
| 7.2 | 6.2 | the script header comments |
| 8.3 | 8.1, 8.2 | deploys both |
| 10.2 | 3.6, 4.5 | `production-topology-contract`, `production-ops-readiness` |
| 10.3 | the owner's decision on `fix-node22-keliya-forecast-output-recompute` | that change holds a MODIFIED block of "Backend scheduler entrypoint", which 10.3 rewrites; archived later, it would put the old text back |
| 11.1 | every other task | leftover literals |

Tasks not named in the left column (3.1, 3.2, 3.3, 4.1, 4.6, 5.1, 7.1, 8.1, 8.2, 9.1, 9.2, 10.1)
depend only on groups 1 and 2. A capability spec has one owning task (`tasks.md`, group 10 lists
the exclusions), so no two slices generate a block for the same requirement from the same text.

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
- **Deviation, stated in the PRs of 8.1 and 8.2.** The repository's rule is that a display or frontend
  change has its node-27 live receipt before it counts as passed. The production checkout on node-27
  only fast-forwards to master, so the receipt of these two can only follow their merge (8.3). The
  change is not archived before that receipt and the receipt of 9.2 exist.
- Until this change is archived, the 13 bare lines of the other active change
  (`fix-node22-keliya-forecast-output-recompute`) are that change's own text. They are reported in
  the final residue with the owner's decision on it.

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
- A static test (group 3, in `tests/test_node22_entrypoint_invariant.py`, which already scans the
  commands of these pages) that the production-ops pages and `current-production-ops.md` hold no
  `ssh` command addressed to the retired host, and that `current-production-ops.md` keeps the host
  statement outside a banner: the scenarios a test can state exactly.
- The generated instruction roots: the byte-exact regeneration test, because the roots are generated
  and a hand edit is the failure it catches.
- The OpenAPI drift test and `check:api-types` for group 8: the two description chains end in
  checked-in generated files. The C4 probe's unit test and the live receipt: the probe is the
  production evidence for the page.
- `tests/test_node22_unit_tracking.py` and the drift probe for 9.2: they compare tracked bytes,
  manifest, receipts and installed copies, which is what a redeploy can get wrong.
  `tests/test_node22_unit_failure_alert.py` for 9.1: it renders the mail.
- Review, not a test, for the code-font clause of "Names keep the codename" and for the choice of
  term in each rewritten sentence.
- `narration_count.py` per PR: evidence in the PR body, not a test.

## Not yet specified

- Whether a guard against the codename coming back as a place is worth having, and what it would
  check. The residue after the last slice decides it: if the remaining bare lines can be told from
  narration by form (the retired-host phrase, the gate's vocabulary files, a date), a guard is cheap;
  if not, it would be a list that only grows. Until then the requirement's scenario "live text does
  not use the codename as a place" is evidenced by the counter and by review, not by a test.
