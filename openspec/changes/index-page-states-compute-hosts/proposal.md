# The index page states the compute hosts in its body (#3414)

Slice 3.6 of `openspec/changes/sweep-compute-role-narration` (#3345). Fixture level: compact (one
page, one static test over documents, two spec deltas; no runtime behaviour). Risk packs:
Documentation / migration notes - selected (a banner that two spec scenarios demand leaves a live
runbook; the statement it carried must not be lost). Legacy compatibility / examples - selected (the
scanners of `tests/test_node22_entrypoint_invariant.py` excuse blockquote lines; the execution note
of the page stays a blockquote and keeps its lines). Others - not selected.

## Why

`docs/runbooks/current-production-ops.md` opens with a banner that tells the reader that "node-22"
in the runbook and its `production-ops/` pages means the compute role, and that a literal
`ssh … 210.77.77.22` command is run on `w3` instead. Slices 3.1 to 3.5 rewrote the twelve pages
that used the codename as a place (the two others never did): they name the compute role, the compute control host and the compute hosts themselves, and none of
them holds a command addressed to the retired host. The banner now explains a wording and a kind of
command that no longer exist.

Two facts lived only inside that banner: that `210.77.77.22` is retired and a rollback archive
only, and the link to the host-layout runbook. The third, which hosts carry the compute role, was
also in the scope line, in a parenthesis after the codename. `production-topology-contract`,
scenario "The compute role names its hosts", requires all three of this page.

## What Changes

- **The page.** The banner paragraph "计算角色主机说明" goes. Its content moves into the body as
  plain paragraphs: `w3` (the compute control host) and `w2` carry the compute role since
  2026-11-23; the retired node-22 host `210.77.77.22` is a rollback archive only and no command of
  the runbook or its pages is run there (its journal copy is stale); paths, the production user,
  unit names, the Slurm partition and account did not change; steps on the compute control host run
  as the production user; the link to `w2-w3-compute-topology.md` §1 and to the rest of that
  document; one sentence that says what the codename is, where it stays (identifiers), and that in
  the dated observations of the pages "node-22" is the retired host. The sentence that
  pointed at literal `ssh` commands goes with those commands. The sentence about the shared `.venv`
  is not repeated: the execution note says it.
- **The execution note** ("计算角色执行说明": no bare `uv run` / `uv sync` in the active checkout,
  the exact interpreter) is an operating rule, not an explanation of a word. It stays, as the
  blockquote it is, with one phrase reworded ("计算侧活动操作").
- **Two more lines of the page**: the scope line names the compute role; the sentence "分册正文与
  拆分前逐字相同" says what is true today (identical at the split, revised since).
- **Two static tests** in `tests/test_node22_entrypoint_invariant.py`. Over the index page and every
  `production-ops/` page: no command (a line, or a command continued over lines) is addressed to the
  retired host (`ssh`, `scp`, `rsync`, `sftp` or a `user@210.77.77.22` address). On the index page:
  one paragraph outside any blockquote names the two hosts, the retired host as a rollback archive
  only, says that no command is run there, and links the topology runbook.
- **`production-ops-readiness`**: the requirement "Current production ops runbook reflects verified
  27-centric topology" is removed and added back as "Current production ops runbook reflects the
  verified node-27-centric topology and names the compute hosts". Three scenarios are carried over
  word for word. Three follow the page: the compute role (not "node-22") remains compute and Slurm
  Gateway only; the body, not a banner, says which hosts carry the compute role; the shared paths
  are shown on the compute hosts and on node-27.
- **`production-topology-contract`**: in the requirement "Current production topology is a governed
  contract", the scenario "Runbooks that still say node-22 carry a banner" no longer names this
  page; the two other runbooks keep their banners until slice 4.5.

## Not changed

- The rule that these pages hold no `ssh` command to the retired host is a requirement of
  `sweep-compute-role-narration` and is not added a second time here.
- The other requirements of the two capability specs (slice 10.2); the banners of
  `two-node-deployment-overview.md` and `node22-control-plane-manual-recovery.md` (slice 4.5).
- The clause about commands of the writer era. The old text read "SHALL NOT present node-22
  scheduler, orchestrator, ingest, or DB writer commands as current operational actions". Read with
  the role term it would forbid the DB-free scheduler, which is the current scheduler of the
  compute role. The clause was written against the writer topology of the retired host and now says so
  ("of the historical node-22 writer topology"). This is the one rewording beyond naming.

## Impact

- Docs: `docs/runbooks/current-production-ops.md`.
- Tests: `tests/test_node22_entrypoint_invariant.py` (two new tests).
- Specs: `production-ops-readiness`, `production-topology-contract`.
