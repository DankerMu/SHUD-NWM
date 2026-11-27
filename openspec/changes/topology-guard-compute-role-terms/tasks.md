# Tasks: topology guard recognises the role terms (#3403)

Fixture level: expanded. Risk packs: Legacy compatibility / examples - selected (every existing row
of the two test files stays green unedited, one named exception; the #3358 host-name rules are the
compatibility surface). Documentation / migration notes - selected (the contract's text changes
with the code). Public API / CLI, Config, File IO, Schema, Auth, Concurrency, Resource limits,
Error handling, Release - not selected: a pure text predicate with no IO of its own.

Notation: `<r>` is a role term, `<h>` a host name, `<c>` the codename. "Every term" = the four English terms, singular
and plural where they have one, with a space and with a hyphen, and the four Chinese terms.

## 1. Code

- [ ] 1.1 `topology_predicates.py`: the role terms join the pattern of names (D1, D2) and its runs
  (D3). One pattern, one rewrite function; the function's name may stay.
- [ ] 1.2 The table-row rule D4 for the leg #3358 added, and the scenario-title rule D5 for the
  writer check and that leg.
- [ ] 1.3 Module docstring: the family covers the role by codename, host names and role terms.

## 2. Tests

In `tests/test_entropy_audit_topology_db_boundary.py`, with its existing helpers and constants.

- [ ] 2.1 Rewrite (direct calls, as the #3358 rows do), for every term:
  - `the <r> is up` and `<r>是主库` give the codename in place of the term.
  - `<r> <h>` and `<r> <h> / <h>` give one codename; `the <r> (<h> / <h>) is up` gives two.
  - A title-case and an upper-case spelling of each English term are rewritten.
  - A path segment that is exactly a hyphenated English term (`docs/<r>/x.md`) is rewritten.
  - Unchanged: `compute_control`, `compute-role-x`, `non-compute host`, `x.compute-host.y`,
    `scripts/compute_node/db.py`, `compute_node_count`, `compute  role` (two spaces), `computing
    role`, `compute hostname`, `xcompute role`, `compute-host_x`; the existing unchanged-lines test with its input "no compute host is named here"
    replaced by "no host of the role is named here", and "no compute host is named here" added as a
    rewritten case (design, Must preserve).
- [ ] 2.2 Flagged as `production-topology-node22-db-writer`, for every term:
  - `Current NHMS production says the <r> is the active database writer.`
  - `Current NHMS production says the <r> hosts the active primary PostgreSQL.`
  - `当前生产<r>是主库，写入数据库。` (Chinese terms; CJK neighbours, no spaces)
  - `The <r> (<h> / <h>) is the active database writer.`
  - A sentence that starts with the term in title case.
  - `slurmdbd on the <r> is the active primary PostgreSQL writer.` (the accounting exclusion does not
    launder)
  - A line that is only `<r>:` or `| <r> |`, followed by a line `is the active database writer.`
    (the anchor rule).
  - Two lines in one file, equal except that one uses the codename and the other `<r>`: two findings
    on their own line numbers.
- [ ] 2.3 Not flagged by the writer check, for every term:
  - Every sentence of `NODE22_EXPLICIT_NEGATIVE_DB_ACCESS_SENTENCES` and
    `NODE22_COORDINATED_NEGATIVE_DB_ACCESS_SENTENCES` with the codename replaced by the term, built
    the way `_host_versions_of_allowed_negative_sentences` builds the host versions.
  - `slurmdbd on the <r> writes Slurm accounting to its local database.` and `The <r> writes Slurm
    job records to its local MariaDB database.`
  - Boundaries: `compute_control is the active database writer role.`, `compute-role-x hosts the
    active primary PostgreSQL.`, `scripts/compute_node/db.py is the active database writer.`,
    `x.compute-host.y hosts the active primary PostgreSQL.`
- [ ] 2.4 Flagged as `production-topology-node22-local-postgres` (the leg #3358 added), for every
  term, and the D4 rows also for `<h>`:
  - `The <r> uses its local PostgreSQL.` / `<r>使用本机 PG。`
  - `The <r> local PostgreSQL is archived and stopped.` (the retired host's excuse does not apply)
  - D4: `| <r> | compute | local PostgreSQL |` (first cell is the role).
  - D4: `| node-27 | reads artifacts; the <r> local PostgreSQL | x |` (one cell holds both).
  - D4: `| notes | the <r> | local PostgreSQL |` (first cell is neither).
  - D4: `| node-27 / <r> | x | local PostgreSQL |` (first cell names the role as well).
  - D4: `| node-27-bringup-checklist.md | <r> | local PostgreSQL |` and `| not node-27 | <r> | local
    PostgreSQL |` (first cell only contains the name of node-27).
  - D4: `| node-27 | <r> \| local PostgreSQL |` (an escaped pipe does not end a cell).
- [ ] 2.5 Not flagged by that leg, for every term:
  - `The <r> has no local PostgreSQL.` / `<r>没有本地 PG。` / `Do not use a local PostgreSQL on the <r>.`
  - D4: the node-27 row of the instructions' hosts table, verbatim from
    `instructions/agents/shared.md`; `| node-27 | reads the <r> artifacts | local PostgreSQL :55432 |`
    is not a finding of that leg (and the same with `<h>`); the same row with the first cell
    written `**node-27**` and in code font.
  - The boundary rows of 2.3 with `local PostgreSQL` in place of the writer wording.
  - The other legs: `The <r> rollback mirror keeps a copy.` is not a finding.
- [ ] 2.6 D5. The first two titles run for a term and for `<h>`; the codename runs only in the
  third (the writer check). The second title with the codename is read by the retired host's leg,
  which D5 does not cover: no row asserts on it.
  - Not findings, each a line of its own:
    - `#### Scenario: A database-writer claim about a <r> is flagged`
    - `#### Scenario: A local PostgreSQL of a <r> is flagged unless denied`
    - `#### Scenario: A <c> database-writer claim is flagged`
    - one title with "is Flagged" and one with "IS FLAGGED"
  - Findings: each of those sentences without the `#### Scenario:` prefix; `#### Scenario: The <r> is
    the active database writer` (no "flagged"); a line `The <r> is the active database writer` (no
    full stop) directly above a flagged title is a finding on its own line; titles that say "is not
    flagged" (`#### Scenario: <c> writing the active primary database is not flagged`, the same
    with `<h>` and `<r>`, and `#### Scenario: <h> local PostgreSQL is not flagged when archived`).
- [ ] 2.7 Mutations, recorded in the PR, each restored: one term removed from the pattern (its rows
  of 2.1, 2.2 and 2.4 go red); the run rule off (the `<r> <h>` rewrite row goes red); D4 off (the
  instructions row of 2.5 goes red); D5 off (the titles of 2.6 go red); D4 made unconditional (the
  D4 rows of 2.4 whose first cell is the role, is neither, or names both go red; the row with both
  in one cell stays a finding); D4 by substring (the file-name row of 2.4 goes red); D5 for any
  "flagged" (the "is not flagged" titles of 2.6 go red).
- [ ] 2.8 Every pre-existing test of `tests/test_entropy_audit_topology_db_boundary.py` and
  `tests/test_entropy_audit_topology_authority.py` passes unedited, the one named input aside.

## 3. Real repository

- [ ] 3.1 The topology finding set is empty before the change. After the code, before the archive,
  the only finding is the body sentence of the main spec that this change's delta replaces (D5); that
  tree exists only locally. After the archive the set is empty again: the Evidence Floor and the
  "after" report are taken on that tree. The files of this change are not findings.
- [ ] 3.2 Because both sets are empty, the PR also lists every scanned line that the rewrite newly
  changes and that carries a database, writer or local-PostgreSQL word: path, line, the term, and
  why it is not a finding (denial, guard meta, D4, D5, no claim). A denial that the negation handling
  does not carry with a term is fixed in the predicates, not in the document.

## 4. Spec and docs (orchestrator)

- [ ] 4.1 `production-topology-contract` deltas: "Static guardrails block topology drift" (D1 to D5)
  and "Current production topology is a governed contract" (the identifier sentence).
- [ ] 4.2 The umbrella change: the line of D6; `design.md` §3 on scenario titles and on terms that
  must not break across a line end; tick 1.1 to 1.4 in its `tasks.md`.
- [ ] 4.3 Archive this change in the PR's last push; `openspec validate production-topology-contract
  --type spec --strict --no-interactive`.

## Evidence Floor

- `uv run pytest -q tests/test_entropy_audit_topology_db_boundary.py
  tests/test_entropy_audit_topology_authority.py tests/test_entropy_audit_report_contract.py`
  locally and on node-27, on the archived tree.
- `uv run ruff check --no-cache .`; `openspec validate topology-guard-compute-role-terms --strict
  --no-interactive` before the archive; `openspec validate sweep-compute-role-narration --strict
  --no-interactive` after the tick; the hard gate JSON before and after; `narration_count.py` plain
  and `--tokens` before and after, every moved `name` token listed (expected: none moves).
- CI, every shard, and the `Production Topology Hard Gate` job.
