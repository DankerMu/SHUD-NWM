# Tasks: topology guard recognises the role terms (#3403)

Fixture level: expanded. Risk packs: Legacy compatibility / examples - selected (every existing row
of the two test files stays green unedited; the #3358 host-name rules are the compatibility
surface). Documentation / migration notes - selected (the contract's text changes with the code).
Public API / CLI, Config, File IO, Schema, Auth, Concurrency, Resource limits, Error handling,
Release - not selected: a pure text predicate with no IO of its own.

Notation: `<r>` is a role term, `<h>` a host name. "Every term" = the four English terms in
singular and plural where they have one, with a space and with a hyphen, and the four Chinese
terms.

## 1. Code

- [ ] 1.1 `topology_predicates.py`: the role terms join the pattern of names (D1, D2) and the run
  (D3). One pattern, one rewrite function; the function's name may stay.
- [ ] 1.2 The table-row rule D4 for the leg #3358 added, and the scenario-title rule D5 for both
  checks.
- [ ] 1.3 Module docstring: the family covers the role by codename, host names and role terms.

## 2. Tests

In `tests/test_entropy_audit_topology_db_boundary.py`, with its existing helpers.

- [ ] 2.1 Flagged as `production-topology-node22-db-writer`, for every term:
  - `Current NHMS production says the <r> is the active database writer.`
  - `Current NHMS production says the <r> hosts the active primary PostgreSQL.`
  - `当前生产<r>是主库，写入数据库。` (Chinese terms; CJK neighbours, no spaces)
  - `The <r> (<h> / <h>) is the active database writer.` (one finding, D3)
  - `<r> <h>（控制）+ <h> 写入生产数据库。` (one finding, D3)
  - A sentence that starts with the term in title case.
  - `slurmdbd on the <r> is the active primary PostgreSQL writer.` (the accounting exclusion does not
    launder)
  - Two lines in one file, equal except that one uses the codename and the other `<r>`: two findings
    on their own line numbers.
- [ ] 2.2 Not flagged by the database-writer check:
  - The denials the codename is allowed, with a term: `The <r> does not connect to any NHMS database.`,
    `<r>不连任何活 DB。`, `The <r> is not the current database writer.`
  - `slurmdbd on the <r> writes its accounting store.`
  - Boundaries: `compute_control is the active database writer role.`, `compute-role-x hosts the
    active primary PostgreSQL.`, `scripts/compute_node/db.py is the active database writer.`,
    `x.compute-host.y hosts the active primary PostgreSQL.`
- [ ] 2.3 Flagged as `production-topology-node22-local-postgres` (compute-host leg), for every term:
  - `The <r> uses its local PostgreSQL.` / `<r>使用本机 PG。`
  - `The <r> local PostgreSQL is archived and stopped.` (the retired host's excuse does not apply)
  - A table row whose first cell is `<r>` and another cell is `local PostgreSQL` (D4).
  - A table row whose first cell is node-27 and one cell holds both `<r>` and `local PostgreSQL` (D4).
- [ ] 2.4 Not flagged by the compute-host leg:
  - `The <r> has no local PostgreSQL.` / `<r>没有本地 PG。` / `Do not use a local PostgreSQL on the <r>.`
  - The node-27 row of the instructions' hosts table, verbatim from
    `instructions/agents/shared.md` (D4).
  - The boundary rows of 2.2 with `local PostgreSQL` in place of the writer wording.
  - D5: `#### Scenario: A <r>'s local PostgreSQL is flagged unless denied` and `#### Scenario: A
    database-writer claim about a <r> is flagged` are not findings, for a term and for `<h>`; each
    sentence without the `#### Scenario:` prefix is a finding; the title prefix with wording that
    does not say "flagged" (`#### Scenario: The <r> is the active database writer`) is a finding.
- [ ] 2.5 Mutation, recorded in the PR: remove one term from the pattern; its rows of 2.1 and 2.3 go
  red; restore.
- [ ] 2.6 Every pre-existing test of `tests/test_entropy_audit_topology_db_boundary.py` and
  `tests/test_entropy_audit_topology_authority.py` passes unedited.

## 3. Real repository

- [ ] 3.1 Hard-gate finding multiset (check id, path, line text) before the change and after it.
  Expected: identical. The seven differences measured with the vocabulary alone are gone by D4
  (three rows), D5 (two scenario titles by the rule; one body sentence once this change's delta is
  in the main spec at archive, so before the archive that one line of the main spec is the only
  expected difference) and D6 (one line). The files of this change are not findings.
- [ ] 3.2 Any other difference is listed with its line and judged; a denial the negation handling
  does not carry with a term is fixed in the predicates, not in the document.

## 4. Spec and docs (orchestrator)

- [ ] 4.1 `production-topology-contract` deltas: "Static guardrails block topology drift" (D1 to D5)
  and "Current production topology is a governed contract" (the identifier sentence).
- [ ] 4.2 The line of the umbrella design (D6); tick 1.1 to 1.4 in the umbrella `tasks.md`.
- [ ] 4.3 Archive this change in the PR's last push; `openspec validate production-topology-contract
  --type spec --strict --no-interactive`.

## Evidence Floor

- `uv run pytest -q tests/test_entropy_audit_topology_db_boundary.py
  tests/test_entropy_audit_topology_authority.py tests/test_entropy_audit_report_contract.py`
  locally and on node-27.
- `uv run ruff check --no-cache .`; `openspec validate topology-guard-compute-role-terms --strict
  --no-interactive`; the hard gate JSON before and after; `narration_count.py` before and after
  (expected: unchanged).
- CI, every shard, and the `Production Topology Hard Gate` job.
