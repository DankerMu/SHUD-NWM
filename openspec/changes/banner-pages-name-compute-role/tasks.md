# Tasks: the last two banner pages name the compute role (#3419)

Must preserve:

- **The gate's skip decision for `two-node-deployment-overview.md`.** The topology gate skips the
  page while its first 80 lines hold three things together: a whole-document subject ("本文档", today
  only inside the dated block), a non-current term ("不是当前", lines 4 to 5), and a design-intent or
  historical term; and while those lines hold neither "当前生产值守" nor "current production
  operations". The dated block is load-bearing for the skip. The PR shows the decision before and
  after.
- **The dated 2026-06-22 block of that page** (from "⚠️ 2026-06-22 当前部署事实 vs 本文档设计意图的差异" to
  "不反映当前生产 host 分配"), byte for byte, with the pin of `tests/test_role_boundary_static.py` on it.
- **What `evidence-boundary-hardening` reads from that page** (scenario "deployment overview
  describes node-27"): `/` as the single-map entry and `/ops` as the operational path.
- **The operating rules of the manual-recovery banner**: no command on the retired node-22 host; a
  rollback to it stops the scheduler timer on `w3` first, copies back the journal of `w3` or its
  backup under `operator-archives/w3-scheduler-journal/`, follows section 9 of
  `w2-w3-compute-topology.md`, and needs a new operating decision; the journal copy on the retired
  host stopped at 2026-11-23T02:34Z.
- The HTML comment markers `reentry-refusal-reasons` and every command line of the manual-recovery
  page; the slug `node22-control-plane-manual-recovery` (an API payload value).
- The body and the seven other scenarios of the contract requirement, word for word; every other
  requirement of the contract.

## 1. Pages and spec (orchestrator)

- [x] 1.1 `docs/runbooks/node22-control-plane-manual-recovery.md`: the banner paragraph goes; the
  body states where the commands run, links `w2-w3-compute-topology.md` §1, forbids commands on the
  retired node-22 host and keeps the rollback rule with its four parts; title, the heading "执行纪律"
  and the narration lines use the glossary terms; undated observations of the retired host get
  their date.
- [x] 1.2 `docs/runbooks/two-node-deployment-overview.md`, by kind of text:
  - **Banner** (the paragraph "计算角色主机说明"): removed. The two body references to "banner"
    (§1, §5.1) point to `current-production-ops.md` and `docs/governance/ROLE_BOUNDARY.md` for the
    facts of today.
  - **Dated**: the 2026-06-22 block, unchanged.
  - **Design sections, each under a lead that says "M22 设计"**: §1; the flow of §2; §3.1; the
    design sentences of §3.2; §3.4; §4; the env paragraph of §5.1; §6.1; §7.1; §7.2; §9. One body
    sentence near the top of §1 states that the M22 design intent is historical for database
    writes (today: node-27 ingest only; no active database connection on the compute role).
  - **Current sections, written as current**: the #2420 note; the route authority and the publish
    paths of §2; §3.3; the retirement sentence and the published-root line of §5.1; §5.2; §5.3;
    §6.2; §6.3; §7.3; §7.4; §8.
  - **Corrected or dated**: the live-probe paragraph of §3.2 (the mount fact was true of the
    retired cluster; today both compute hosts mount `/ghdc/data`, source: the export table of
    `w2-w3-compute-topology.md`); "只读 DB 副本" in §3.4 (the page's own §4 and the instructions say a
    readonly account); the `DATABASE_URL` line of §8.2 (`compute-api` is retired, #3394; source:
    `infra/env/README.md`); the node range `cn01-24` of §8.2 (the retired cluster: labelled, with
    "not re-read on the compute hosts", no source for a correction exists).
  - **Terms**: 计算角色 for who does or owns; 计算主机 for mounts, paths and the checkout; 计算控制主机
    for where an operator acts; node-27 for that host. "计算控制面" leaves with "22 节点" and "22 侧".
    `compute_control` stays as the value of `NHMS_SERVICE_ROLE` and is not described as an API-only
    identifier.
- [x] 1.3 Spec delta, generated from the main spec text of the day: `production-topology-contract`,
  REMOVED and ADDED under the new title, without the banner scenario.
- [ ] 1.4 Every sentence of the two removed banners accounted for in the PR: moved to the body,
  stated elsewhere on the page, or dropped with its reason.

## 2. Test (implementer)

- [x] 2.1 `tests/test_node22_runbook_section_markers.py`: `SECTION_HEADING` and
  `RETIREMENT_SENTENCE` follow the page (the sentence stays one line of at most 300 characters; the
  test compares three lines exactly); the docstrings that describe the section follow.
- [x] 2.2 Red runs, each on the real file, restored, sha256 equal before and after: the page
  rewritten and the constants not yet; the new heading changed in the page; the new sentence
  changed in the page.

## 3. Evidence

- [x] 3.1 The gate's skip decision (`_topology_document_is_non_current`) for
  `two-node-deployment-overview.md`, before and after: skipped both times.
- [ ] 3.2 Hard gate: finding multiset before (master) and after (the tree after the archive, where
  the requirement sits at the end of the main spec and this change is no longer scanned), each
  difference listed and judged; the fixture directory itself has no finding while it is open.
- [ ] 3.3 `narration_count.py` plain and `--tokens` before and after; moved name tokens listed; the
  count of ssh lines to the retired host explained.
- [ ] 3.4 A repository-wide search for the old slug of every renamed heading returns nothing outside
  history and the umbrella change.
- [ ] 3.5 After the archive: the body and the seven carried-over scenarios byte-identical in the main
  spec (extracted blocks compared); no other requirement of the contract changed.

## 4. Last push

- [ ] 4.1 `openspec archive banner-pages-name-compute-role --yes`; the main spec validates.
- [ ] 4.2 Task 4.5 of `openspec/changes/sweep-compute-role-narration/tasks.md` ticked in the same push.

## Evidence Floor

- `tests/test_node22_runbook_section_markers.py`, `tests/test_role_boundary_static.py`,
  `tests/test_operator_reentry_confirmation.py`, `tests/test_node22_scheduler_env_drift_table.py`,
  `tests/test_monitoring_api.py`, `tests/test_node22_entrypoint_invariant.py` and the topology
  authority tests green locally and on node-27.
- `openspec validate banner-pages-name-compute-role --strict --no-interactive` before the archive;
  `openspec validate sweep-compute-role-narration --strict --no-interactive` and the main spec after.
- `uv run ruff check --no-cache .` clean; markdownlint clean on the two pages; CI green on every shard.
- Hard gate multiset, `narration_count.py` and the skip decision quoted in the PR.

Fixture level: none (documentation and one spec requirement; no runtime behaviour).
Suggested fixture level: none - documentation; pinned sentences are followed.
Minimal mergeable slice: atomic - the two pages, their two pinned constants and the scenario that names them change together.
