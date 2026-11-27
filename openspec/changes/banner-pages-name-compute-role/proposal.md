# The last two banner pages name the compute role; the banner scenario leaves the contract (#3419)

## Why

Slice 4.5 of `sweep-compute-role-narration` (#3345). Two runbooks still open with the banner that
`production-topology-contract` demands of them: `docs/runbooks/node22-control-plane-manual-recovery.md`
and `docs/runbooks/two-node-deployment-overview.md`. The banner tells the reader that "node-22" in the
page means the compute role and that a literal ssh command to `210.77.77.22` is run on `w3` instead.
Neither page holds such a command outside the banner, and after this slice neither uses the codename
for the compute role: the banner would explain a wording the pages no longer have. Every other
page that carried the banner or a pointer to it lost it in slices 3.2 to 3.6 and 4.1; the scenario
that demands it was narrowed to these two pages by `index-page-states-compute-hosts` (#3414) and now
has no page left.

## What Changes

- `docs/runbooks/node22-control-plane-manual-recovery.md`: the banner paragraph goes. The body says
  that the commands of the page run on the compute control host as the production user, links
  `w2-w3-compute-topology.md` §1, and keeps the operating rules the banner carried: no command of the
  page is run on the retired node-22 host; a rollback to that host starts by stopping the scheduler
  timer on `w3`, then copies back the journal of `w3` (or its backup under
  `operator-archives/w3-scheduler-journal/` on NFS), follows section 9 of the topology runbook, and
  needs a new operating decision. The narration of the page uses the glossary terms. The file name
  and the slug `node22-control-plane-manual-recovery` are names and stay.
- `docs/runbooks/two-node-deployment-overview.md`: the banner paragraph goes. The page is the M22
  design background and stays declared as not current, so the topology gate keeps skipping it. The
  page mixes three kinds of text, and each is handled by its kind (`tasks.md` 1.2 lists the sections):
  design sections get a lead that says "M22 设计"; current sections are written as current; lines
  that give the retired cluster as current are dated or corrected. Where the page said "22 节点",
  "22 侧", "22", "计算控制面", "27 节点" or "27", it uses the glossary terms by what the sentence is
  about: 计算角色 for who does or owns, 计算主机 for mounts and paths, 计算控制主机 for where an
  operator acts, node-27 for that host. One body sentence, outside the dated block, says that the
  M22 design intent is historical for database writes: node-27 ingest alone writes the active
  database today, and the compute role has no active database connection. The dated 2026-06-22
  block keeps its text.
- `tests/test_node22_runbook_section_markers.py`: the pinned heading of section 5.1 and the pinned
  retirement sentence follow the page.
- `production-topology-contract`: the requirement "Current production topology is a governed
  contract" loses the scenario "Runbooks that still say node-22 carry a banner". A MODIFIED block
  cannot drop a scenario, so the requirement is removed and added back as "Current production
  topology is a governed contract that names the compute hosts", with its body and its seven other
  scenarios carried over word for word.

## Impact

- Specs: `production-topology-contract` (one requirement REMOVED and ADDED under a new title; the
  archive appends it at the end of the spec).
- Docs: the two runbooks.
- Tests: two constants and two docstrings of one test file.
- No runtime behaviour, no deployed file.
- What replaces the banner rule: the requirement "Live text names the compute role and keeps the
  codename for names" of `sweep-compute-role-narration`, which reaches the main spec when that
  change is archived (#3431). That change's delta for this spec only adds a requirement, so the new
  title needs nothing from it.
