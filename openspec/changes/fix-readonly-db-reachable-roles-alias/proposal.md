# Fix the reserved-word alias in the reachable-roles probe

## Why

`services/production_closure/readonly_db_probe_adapter.py:310-311` aliases `pg_roles` as `current_role` inside
`_reachable_roles`. `CURRENT_ROLE` is a PostgreSQL reserved word, so the statement cannot be parsed by any real
server. The canonical readonly-DB deny-write entrypoint
`scripts/validate_readonly_db_boundary.py` therefore always fails on a live database: on node-27 production
(PG 15.2) it returned `status=BLOCKED`, exit 2, blocker `READONLY_DB_VALIDATION_UNEXPECTED_ERROR`,
`error_type=SyntaxError` at 2026-09-16T01:06Z.

The alias has been wrong since it was introduced in `a0b6ccf9d` (#1895): the only test covering the statement
(`tests/test_readonly_db_validation_probes.py:93`) feeds it to a fake cursor that stores the SQL text and asserts on
substrings, so nothing ever hands the statement to a PostgreSQL parser.

Only the ability to *prove* the readonly boundary is broken. The boundary itself is role-level (`nhms_display_ro`
holds no write grants) and is unaffected. The missing proof is required evidence for #1987 task 5.2 (bringup
checklist C2), which gates #1988.

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: absent (compact: one statement, no validation semantics change, no shared entrypoint or
  format change; the permissions surface is read-only catalog inspection, not a grant change)
Blast radius: readonly-DB deny-write evidence only; a wrong fix silently changes which reachable roles are
  discovered and would let a mutating-capable role pass unnoticed
Selected risk packs: Auth / permissions / secrets; Error handling / rollback / partial outputs
Evidence floor: a real-PostgreSQL test that executes the reachable-roles probe and fails when the reserved-word
  alias is restored; the existing fake-cursor test still passes; `uv run ruff check`
```

`design.md` is omitted: the fixture level is `compact`.

## What Changes

- Rename the SQL alias in the single `_reachable_roles` statement so it is not a PostgreSQL reserved word. The
  recursive-reachability, `set_option`/`inherit_option` folding and the absence of a depth cap stay literally
  equivalent.
- Add a real-database regression test (`pytest.mark.integration`) that executes the probe against a live
  PostgreSQL and therefore fails on a reserved-word alias.

## Out of Scope

- Any change to readonly validation semantics or verdicts.
- The `current_role()` Python adapter methods (`readonly_db_probe_adapter.py:39`, `readonly_db_types.py:155`,
  `readonly_db_validation.py:167`, `tests/test_readonly_db_validation.py:432`). They are Python identifiers and
  unrelated to the SQL reserved word.
- The existing fake-cursor test at `tests/test_readonly_db_validation_probes.py:93`, whose "no silent depth cap"
  assertion stays.
- Producing the node-27 C2 receipt itself (that belongs to #1987 task 5.2), and grants on the production database.
