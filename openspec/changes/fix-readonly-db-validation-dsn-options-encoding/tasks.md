# Tasks: fix-readonly-db-validation-dsn-options-encoding

## 1. Encoding fix and regressions

- [x] 1.1 In `_bounded_database_url` (`services/production_closure/readonly_db_route_smoke.py:320-348`) build
  the query string with `urlencode(query_items, quote_via=quote)` instead of `urlencode(query_items)`. `quote`
  is already imported and used at `:309`. Nothing else in the function changes: the `parse_qsl` of the incoming
  DSN, the `{"connect_timeout", "options"}` strip set, the appended pair order, and the
  `urlunsplit((scheme, netloc, path, query, fragment))` reassembly stay literally as they are.

  Note the consequence to keep in mind while reviewing, not to act on: with `safe=''` (urlencode's default for
  `quote_via`), the `=` inside each `-c name=value` is emitted as `%3D` and the space as `%20`. libpq
  percent-decodes both, so the server sees the original string. That is the point of the fix — the previous
  output already contained `%3D`, which is why the existing assertion at
  `tests/test_readonly_db_validation_routes.py:344` passed on the broken code.

  Evidence floor: `uv run ruff check services/production_closure/readonly_db_route_smoke.py` clean.

- [x] 1.2 Add a unit test that **can go red on the current code**. In
  `tests/test_readonly_db_validation_routes.py`, assert on the emitted `options` value directly:
  parse the rebuilt URL, take the raw `options` query value, decode it with `urllib.parse.unquote`
  (percent-decoding **only** — never `unquote_plus`, and never `parse_qsl`, both of which turn `+` into a
  space and would keep the test blind), and assert it equals `_validation_pgoptions()` verbatim. Assert
  separately that the raw emitted value contains no `+`.

  The existing assertion `"statement_timeout%3D10000" in database_url`
  (`tests/test_readonly_db_validation_routes.py:344`) holds for both the broken and the fixed output, so it is
  not a regression guard and must not be treated as one. Leave it in place; add the new assertions alongside.

  Evidence floor — recorded verbatim in the implementer report: the new test **fails** against the unmodified
  `urlencode(query_items)` with the mismatch shown, and passes after 1.1. A test that passes both ways is a
  failed task, not a passed one.

- [x] 1.3 Add a real-PostgreSQL integration test (`pytestmark = pytest.mark.integration`, fixture
  `integration_database_url` or `throwaway_database_url` per `tests/conftest.py:165-205`, pattern per
  `tests/test_real_readonly_db_probe_integration.py`) that feeds the **output of `_bounded_database_url`** to
  `psycopg2.connect` — the libpq-direct consumer, not SQLAlchemy, because SQLAlchemy's `unquote_plus` decoding
  is precisely what hid the defect — then asserts the connection opens and that the three timeouts are the
  ones the lane configured.

  Read them with `SELECT setting FROM pg_settings WHERE name = %s`, not with `SHOW`. `pg_settings.setting`
  carries the GUC's base unit, which is `ms` for all three, so `int(setting)` compares directly against
  `VALIDATION_STATEMENT_TIMEOUT_MS` / `VALIDATION_LOCK_TIMEOUT_MS` / `VALIDATION_IDLE_TIMEOUT_MS`
  (`services/production_closure/readonly_db_types.py:59-63`). `SHOW` renders a time GUC in its largest whole
  unit — `'10s'`, `'2s'` — so an assertion written against `SHOW` would fail on the *fixed* code, and
  formatting the constant back into `f"{ms // 1000}s"` breaks the moment a value stops being a whole second.
  Compare against the constants, not against literals, so the test tracks a future retune.

  File name MUST match the CI `database` paths filter (`.github/workflows/ci.yml:118-119`, `:145`) so the
  `real-db-integration` job ("SQL Migration Dry Run") runs it on a non-draft PR: put it in
  `tests/test_real_readonly_db_route_smoke_integration.py`.

  Residual risk, accepted: `services/production_closure/**` is **not** in that `database` paths filter
  (`.github/workflows/ci.yml:85-109`), so this file opens the real-DB lane on *this* PR only because
  `tests/test_real_*.py` matches its own addition. A future source-only edit to `readonly_db_route_smoke.py`
  would not rerun it. The durable CI guard against this defect class is therefore 1.2's unit test, which
  `scripts/select_ci_tests.py:2212,2233` selects via the `services/production_closure/**` →
  `READONLY_DB_VALIDATION_TESTS` rule and which goes red on the same mutation. The integration test's value
  is proving the claim against a real libpq once, not guarding it on every future PR.

  Evidence floor — both runs recorded verbatim:
  - Positive: `NHMS_RUN_INTEGRATION=1 NHMS_INTEGRATION_DATABASE_URL=postgresql://<admin-url> uv run pytest -q
    -rs -m integration tests/test_real_readonly_db_route_smoke_integration.py` passes with no skips.
  - Negative: restore `urlencode(query_items)`, rerun the same command → fails with
    `FATAL:  unrecognized configuration parameter "+statement_timeout"`; then revert.
  - `1 skipped` is NOT evidence in either direction: `tests/conftest.py:165-205` skips when
    `NHMS_RUN_INTEGRATION` or the integration DSN is unset, so an unconfigured run "does not fail" for the
    broken source too. `-rs` makes a skip visible.
  - Execution preconditions: the DSN MUST be an admin/CREATEDB role, not `nhms_display_ro`, because
    `tests/conftest.py:273-298` issues `CREATE DATABASE` / `DROP DATABASE` per session. On node-27 also
    `mkdir -p /home/nwm/tmp && export TMPDIR=/home/nwm/tmp` (CLAUDE.md, #1765).

## 2. Risk packs

- Auth / permissions / secrets — **selected**: the value being rebuilt is a credential-bearing DSN, and the
  bounded `options` are the lane's own safety limits. Covered by 1.2 (the emitted value is exactly the intended
  option string, no more and no less) and 1.3 (the server actually applies the three timeouts). The DSN must
  not appear in any test name, assertion message, or captured output — assert on the decoded `options` value,
  not on the whole URL.
- Error handling / rollback / partial outputs — **selected**: the defect's signature is a connection-time
  `FATAL` that the lane reports as a route `FAIL`/HTTP 500 rather than as a misconfiguration. Covered by 1.3's
  negative run reproducing the exact FATAL text, and by the existing route-smoke tests staying green.
- Legacy compatibility / examples — **selected**: the broken encoding has been the emitted shape since #234,
  and `tests/test_readonly_db_validation_routes.py:344` encodes an assumption about it. Covered by 1.1's
  literal-equivalence constraint on the rest of the function, by keeping that assertion green, and by 1.2
  adding the guard it never was.
- Public API / CLI / script entry — not selected: no flag, subcommand, or output contract changes.
- Config / project setup — not selected: no configuration file or environment contract changes; `PGOPTIONS`
  keeps being exported exactly as today.
- File IO / path safety / overwrite — not selected: no file is produced by this change.
- Schema / columns / units / field names — not selected: no schema is read or written.
- Concurrency / shared state / ordering — not selected: a pure function over a string.
- Resource limits / large input / discovery — not selected: the timeout constants are unchanged.
- Release / packaging / dependency compatibility — not selected: `quote` is already imported in the module.
- Documentation / migration notes — not selected: no operator-facing procedure changes; the runbook already
  names the canonical entrypoint and the DSN is built in memory either way.

## 3. Change-level verification floor

- [x] 3.1 `openspec validate fix-readonly-db-validation-dsn-options-encoding --strict --no-interactive` PASS.
- [x] 3.2 `uv run ruff check .` PASS.
- [x] 3.3 `uv run pytest -q tests/test_readonly_db_validation_routes.py tests/test_readonly_db_validation.py
  tests/test_readonly_db_validation_probes.py` PASS, including the new case.
- [x] 3.4 Real-database lane: 1.3's positive run passes and its recorded negative run fails with the FATAL.
- [x] 3.5 node-27 canonical rerun, read-only: with `NHMS_DISPLAY_READONLY_DATABASE_URL` built in memory from
  `infra/env/display.env` (role `nhms_display_ro`; DSN never in argv or receipts), run
  `scripts/validate_readonly_db_boundary.py` against the production database and record the JSON summary.
  Expected: `stations`, `latest_product` and `models` no longer report `unrecognized configuration parameter`
  or `OperationalError`.

  The lane's overall `PASS`/`FAIL` is deliberately NOT part of this acceptance. The same run still carries the
  `ops.pipeline_job` producer gap (#2420), which keeps `jobs` / `pipeline_status` / `pipeline_stages` /
  `job_logs` from binding response identity. That is a separate defect and this change does not touch it, so
  the C2 receipt reaching PASS is #1987 task 5.2's gate, not this one's.

  Evidence: node-27 2026-09-16T05:40:48Z, detached worktree `/home/nwm/tmp/2413-wt` at PR head
  `e7e2d322b805afd5a16c01a6c3e6bca3a0bc8eb2` (the executing bytes were confirmed to carry `quote_via=quote`
  before the run), role `nhms_display_ro`, `validation_provenance` `{"mode": "live", "live_readonly_proof":
  true, "injected_components": []}`, DSN only in `NHMS_DISPLAY_READONLY_DATABASE_URL` and redacted to
  `postgresql://127.0.0.1:55432/nhms` in the receipt. Pins re-derived live: the QHH latest-product surface
  serves `fcst_ifs_2026091412_dg_9ccb261a39d51c24f4de9173fb4461b6`, cycle `2026-09-14T12:00:00Z`.
  Receipt archived at `evidence/node27-c2-20260916T054048Z.json`.

  **`unrecognized configuration parameter` and `OperationalError` each occur 0 times** in stdout and stderr.
  All five DB-reading routes now PASS — `health`, `runtime_config`, `models`, `stations`, `latest_product`.
  On the pre-fix run at 2026-09-16T01:51:24Z all three of `models`, `stations` and `latest_product` were
  `FAIL`, the latter two carrying `connection to server at "127.0.0.1", port 55432 failed: FATAL:
  unrecognized configuration parameter "+statement_timeout"` verbatim. The lane's status moved
  `FAIL` → `BLOCKED`: no route fails to connect any more, and every remaining blocker is a data condition.
  Permission matrix unchanged and still clean: 12 targets, 23 operations, 23 denials
  PASS, `blocked_count` 0, `failed_mutating_count` 0. `validation_timeouts` reports
  `statement_timeout_ms` 10000, `lock_timeout_ms` 2000, `idle_in_transaction_session_timeout_ms` 10000,
  `connect_timeout_seconds` 5 — the bounded options this change emits are the ones in force.

  The lane's overall status stays `BLOCKED`, and every remaining blocker is #2420: `jobs`,
  `pipeline_status` and `pipeline_stages` are `display_read_route_response_identity_invalid`, and `job_logs`
  is `source_cycle_run_model_job_required_for_job_log_smoke`. No encoding-related blocker remains.

  A first attempt at 05:39:57Z with a different pin (`dg_0e611766f8d1edb6e99a2ba1892e48e1`, the newest
  published IFS run) left `latest_product` `BLOCKED` on HTTP 404 `QHH_LATEST_PRODUCT_UNAVAILABLE` — already
  proof the connection succeeded and the database answered, but the wrong pin: that run has no QHH display
  product. The pin was re-derived from the live API per the runbook rather than accepted as a result.
