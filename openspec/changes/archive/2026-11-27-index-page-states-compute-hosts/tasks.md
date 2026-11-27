# Tasks: the index page states the compute hosts in its body (#3414)

Must preserve: every heading and anchor of the index page (inbound links and alert mails resolve
through them); the lines of the execution note that carry `uv run` / `uv sync` stay inside the
blockquote (the scanners of `tests/test_node22_entrypoint_invariant.py` excuse blockquote lines,
not these words); the three scenarios of the readiness requirement that do not change, word for
word; every other requirement of the two specs; the link set `tests/production_ops_runbook.py`
reads from the index.

## 1. Page and specs (orchestrator)

- [x] 1.1 `docs/runbooks/current-production-ops.md`: the banner paragraph goes; the body states the
  hosts, the retired host, the unchanged paths and names, where steps run, the link to
  `w2-w3-compute-topology.md` §1, and the codename sentence; the scope line and the "逐字相同"
  sentence follow.
- [x] 1.2 Spec deltas, generated from the main spec text of the day: `production-ops-readiness`
  (REMOVED and ADDED under the new title), `production-topology-contract` (MODIFIED: the `WHEN` of
  one scenario).
- [x] 1.3 Every sentence of the removed banner accounted for in the PR: moved to the body, already
  in the execution note, or dropped with its reason.

## 2. Test (implementer)

- [x] 2.1 `tests/test_node22_entrypoint_invariant.py`,
  `test_production_ops_pages_hold_no_command_to_the_retired_host`, over `production_ops_surfaces()`:
  no command names `210.77.77.22` together with `ssh`, `scp`, `rsync` or `sftp`, and no
  `<user>@210.77.77.22` address occurs anywhere. A command continued with `\` over several lines is
  read as one command. The body statement of the index page names the bare address and stays legal.
  The scan proves that it read something (more than one surface; the node-27 `ssh` form is found).
  A third test, `test_retired_host_command_classifier_reads_the_literal_examples`, runs the
  classifier on literal inputs.
- [x] 2.2 The same file, `test_index_page_states_the_compute_hosts_outside_a_blockquote`: the index
  page without its blockquote lines and fenced blocks has one paragraph (read as joined text) that
  names `` `w3` `` and `` `w2` `` (as code-font names, so that the file name of the topology runbook
  does not count) with the compute role, names `210.77.77.22` as a rollback archive, says that no
  command is run on that host, and links `w2-w3-compute-topology.md`. Each clause has its own
  assertion.
- [x] 2.3 Red runs, each on the real file, restored, sha256 equal before and after: one `ssh` line to
  the retired host restored in a `production-ops/` page; the same in the index page; a retired-host
  command split over a `\` continuation with the address on the second line; the statement
  paragraph removed; the paragraph moved inside a blockquote; each of its four clauses removed alone
  (the two host names; the retired host; the sentence that no command is run there; the link).

## 3. Evidence

- [x] 3.1 The main-spec scenario "The compute role names its hosts" met by the page body, clause by
  clause, shown in the PR.
- [x] 3.2 Hard gate: finding multiset before and after, each difference listed; the page stays in
  the scan set.
- [x] 3.3 `narration_count.py` plain and `--tokens` before and after; moved name tokens listed.
- [x] 3.4 After the archive: the three carried-over scenarios byte-identical in the main spec
  (extracted blocks compared); the diff of `production-topology-contract` is the one `WHEN`.

## 4. Last push

- [x] 4.1 `openspec archive index-page-states-compute-hosts --yes`; both main specs validate.
- [x] 4.2 Task 3.6 of `openspec/changes/sweep-compute-role-narration/tasks.md` ticked in the same push.

## Evidence Floor

- `tests/test_node22_entrypoint_invariant.py`, its python-scan sibling, `tests/test_role_boundary_static.py`
  and the structure readers of the page (`tests/production_ops_runbook.py` users), locally and on
  node-27; `uv run ruff check --no-cache .`; markdownlint on the page;
  `openspec validate index-page-states-compute-hosts --strict --no-interactive` before the archive;
  `openspec validate production-ops-readiness --type spec --strict --no-interactive`,
  `openspec validate production-topology-contract --type spec --strict --no-interactive` and
  `openspec validate sweep-compute-role-narration --strict --no-interactive` after; CI, every shard.
