# Tasks: fix-readonly-db-reachable-roles-alias

## 1. Alias fix and real-database regression

- [x] 1.1 Rename the `pg_roles` alias in the single `_reachable_roles` statement
  (`services/production_closure/readonly_db_probe_adapter.py:297-343`) from the PostgreSQL reserved word
  `current_role` to a non-reserved identifier, updating both references inside that statement. Nothing else in the
  statement changes: the recursive CTE, the `COALESCE(m.set_option, true)` / `COALESCE(m.inherit_option, true)`
  substitutions, the cycle guard `NOT m.roleid = ANY(reachable.path)`, the absence of a depth cap, and the
  `DISTINCT ON (reachable.roleid)` projection stay literally equivalent.
  Evidence floor: `uv run ruff check services/production_closure/readonly_db_probe_adapter.py` clean;
  `uv run pytest -q tests/test_readonly_db_validation_probes.py tests/test_readonly_db_validation.py` passes.

- [x] 1.2 Add a real-PostgreSQL regression test (`pytestmark = pytest.mark.integration`, fixture
  `integration_database_url` or `throwaway_database_url`, pattern per `tests/test_real_database_integration.py`)
  that opens a real connection and executes the reachable-roles probe through the psycopg adapter
  (`PsycopgReadonlyDbProbeAdapter.reachable_role_privileges` or `_reachable_roles`; neither swallows
  `psycopg2.Error`), asserting it returns without a database error and yields a list.

  File name MUST match the CI `database` paths filter so the `real-db-integration` job ("SQL Migration Dry Run",
  `.github/workflows/ci.yml:237-279`) actually runs the guard on a non-draft PR: use
  `tests/test_real_readonly_db_probe_integration.py` (matched by both `tests/test_real_*.py` and
  `tests/*integration*.py`, `.github/workflows/ci.yml:118-119` and `:145`).

  Evidence floor — both runs recorded verbatim in the implementer report:
  - Positive: `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://<admin-url> uv run pytest -q -rs
    -m integration tests/test_real_readonly_db_probe_integration.py` → `2 passed` (the file ships two
    tests: the public seam and both membership-column branches).
  - Negative: restore BOTH `current_role` references in the statement, rerun the same command → fails with
    `psycopg2.errors.SyntaxError: syntax error at or near "current_role"`; then revert.
  - `1 skipped` is NOT evidence in either direction: `tests/conftest.py:165-205` calls `pytest.skip` when
    `NHMS_RUN_INTEGRATION` or the integration DSN is unset, so an unconfigured run "does not fail" for both the
    fixed and the broken source. `-rs` makes a skip visible.
  - Execution preconditions: the DSN MUST be an admin/CREATEDB role, not `nhms_display_ro`, because
    `tests/conftest.py:273-298` issues `CREATE DATABASE` / `DROP DATABASE` per session. On node-27 also
    `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp` (CLAUDE.md, #1765).

## 2. Risk packs

- Auth / permissions / secrets — **selected**: the statement decides which reachable roles are inspected for
  mutating capability. Covered by 1.1's literal-equivalence constraint (the statement changes only the alias token)
  plus the existing `tests/test_readonly_db_validation.py` and
  `tests/test_readonly_db_validation_probes.py` verdict/shape tests, which must stay green.
  **Explicit non-goal:** 1.2 does not prove reachable-set *semantics*. The integration role has no role
  memberships, so the probe legitimately returns `[]`; the test proves the statement parses and executes. Seeding a
  membership is rejected on purpose: `CREATE ROLE` is cluster-global and node-27's live primary is the real-DB
  oracle, so a test that creates roles there would mutate a production cluster's global catalog. Semantics stay
  covered by 1.1's literal equivalence and by the existing DB-free verdict tests.
- Error handling / rollback / partial outputs — **selected**: the defect surfaces as a `BLOCKED` verdict with an
  unexpected-error blocker. Covered by 1.2: the probe must reach a normal result instead of raising.
- Public API / CLI / script entry — not selected: no CLI surface, flag, or output contract changes.
- Config / project setup — not selected: no configuration is read or written.
- File IO / path safety / overwrite — not selected: no file is produced by this change.
- Schema / columns / units / field names — not selected: catalog is only read; no application schema is touched.
- Concurrency / shared state / ordering — not selected: a single read-only statement on catalog tables.
- Resource limits / large input / discovery — not selected: role graphs are small and the depth behavior is
  unchanged by 1.1.
- Legacy compatibility / examples — not selected: the statement has never executed successfully, so no caller
  depends on its current behavior.
- Release / packaging / dependency compatibility — not selected: no dependency or packaging change.
- Documentation / migration notes — not selected: no operator-facing procedure changes; the runbook already
  points at the canonical entrypoint.

## 3. Change-level verification floor

- [x] 3.1 `openspec validate fix-readonly-db-reachable-roles-alias --strict --no-interactive` PASS.
- [x] 3.2 `uv run ruff check .` PASS.
- [x] 3.3 `uv run pytest -q tests/test_readonly_db_validation.py tests/test_readonly_db_validation_probes.py
  tests/test_readonly_db_validation_routes.py` PASS.
- [x] 3.4 Real-database lane: the new integration test PASS with the exact invocation in 1.2, plus the recorded
  negative run with the alias restored. The test file name is chosen so CI's `real-db-integration` job does run it
  on this non-draft PR; per `CLAUDE.md` the authoritative real-DB oracle is still node-27, so record the node-27
  run as well.

  Evidence: local disposable PostgreSQL 15.19 — positive `2 passed` (no skips, `-rs`), negative with both
  `current_role` references restored `2 failed` with `psycopg2.errors.SyntaxError: syntax error at or near
  "current_role"`, revert `2 passed`; extra PostgreSQL 18.6 `2 passed`. node-27 half at PR head
  `bed35f44d2cef7123e455dee1259982a488ac15e`, disposable production-image container
  `nwm-i8-1987-window-volume-6fb30b552` (image `sha256:ad39c4fbc5c4`): `2 passed in 0.42s`. CI
  `real-db-integration` ("SQL Migration Dry Run") pass on the same head.

- [x] 3.5 node-27 canonical-entrypoint rerun (issue #2409 acceptance criterion 4), read-only: with
  `NHMS_DISPLAY_READONLY_DATABASE_URL` built in memory from `infra/env/display.env` (role `nhms_display_ro`; DSN
  never in argv or receipts), run `scripts/validate_readonly_db_boundary.py` against the production database and
  record the JSON summary. Expected: `status` is no longer `BLOCKED` and no
  `READONLY_DB_VALIDATION_UNEXPECTED_ERROR` / `SyntaxError` blocker appears, i.e. the lane reaches its actual
  verdict stage. The final `PASS`/`FAIL` is deliberately NOT part of this acceptance: it depends on the production
  grants, which this change does not touch. This run creates no roles and mutates nothing, so it does not conflict
  with section 2's non-goal, and it is not the #1987 task 5.2 C2 receipt (that one requires `PASS`).

  Evidence: node-27 2026-09-16T01:51:24Z at PR head, worktree `/home/nwm/tmp/2409-wt`, role `nhms_display_ro`.
  `status` is `FAIL`, `blockers` is `null` — no `READONLY_DB_VALIDATION_UNEXPECTED_ERROR` and no `SyntaxError`:
  the lane now reaches its verdict stage. Permission matrix: 12 targets, 23 operations, 23 denials PASS,
  `blocked_count` 0, `failed_mutating_count` 0. `reachable_role_findings` is `[]` — the statement executed and
  `nhms_display_ro` holds no role memberships, so the seven per-role finding statements stay unexercised on a real
  server (recorded as residual risk, not closed by this change). Manual-action probes returned 409
  `CONTROL_PLANE_MANUAL_ACTION_REQUIRED` with no write executed. The `FAIL` comes from display route smoke, which
  is outside this change: `models`/`stations`/`latest-product` fail on a malformed connection option
  (`unrecognized configuration parameter "+statement_timeout"`), and the job/pipeline routes are `BLOCKED` on
  response identity plus an empty `ops.pipeline_job`. Those belong to #1987 task 5.2 and are filed separately.
