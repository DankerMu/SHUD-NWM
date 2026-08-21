# qhh-fallback-parity-frozen-oracle

Issue: #1414 (origin: PR #1413 / #1120 cross-review, verifier CONFIRMED, deferred P3). Fixture level: **compact** (test-only, S; no production code; the issue predates the `Suggested fixture level` field — triage recorded here, not diverged from).

## Why

`openspec/specs/qhh-latest-display-product/spec.md:57` ("Result parity": the pushdown fallback and the previous single-statement fallback return identical rows) has no repeatable oracle in the repo. The only row-level assertion, `tests/test_display_coverage_residual_debt_integration.py:94-112`, compares the new two-statement fallback with the FAST path — and the FAST path's coverage cache is produced by the same pushdown idiom (`packages/common/forecast_store.py` ≡ `packages/common/display_coverage.py`, same `scan_* IS NULL OR ...` guards, same 7 header scalars). A shared-idiom bug cancels out on both sides and the assertion stays green. The PR #1413 `PARITY: IDENTICAL` receipt (old 532.98 s vs new 7.27 s) was one-off and never landed in the repo.

## What Changes

- Freeze the pre-#1413 single-statement fallback (`packages/common/forecast_store.py` at `90dc4a7e`, first parent of the #1413 merge `6f12117b`) as `tests/fixtures/legacy_qhh_fallback_pre_1413.sql`, exposed in the test module as `_LEGACY_FALLBACK_SQL` with a "frozen — do not update with production" header. Positional `%s` binding exactly as the legacy call site bound it: `(QHH_LATEST_EXPECTED_HORIZON_HOURS, basin_id, source_id, candidate_limit, list(MVP_STATION_VARIABLES), len(MVP_STATION_VARIABLES))` with `identity_sql` rendered empty (identity=None).
- Add real-database parity tests in `tests/test_display_coverage_residual_debt_integration.py` under the forced fallback (`_run_display_coverage_available → False`), comparing `_fetch_latest_qhh_display_candidates` with the frozen statement **row-by-row, ordered, on the projected column set** — the current final SELECT is `cr.*` and the #1442 candidate CTE carries `run_key`/`basin_version_key`/`river_network_version_key`, which the frozen text lacks; the helper pops exactly that literal set (asserted) and then requires equal column sets, values and order — on one snapshot (same `store._transaction()` cursor), with station rows seeded by a module-private helper because `seed_issue_126_data` seeds none: (i) the seeded covered state; (ii) a NULL-identity candidate (`hydro_run.forcing_version_id IS NULL`, nullable per `db/migrations/000006_hydro.sql:7`) that drives the `scan_forcing_version_id IS NULL` guard branch while river coverage stays non-trivial; (iii) the empty-header short-circuit state (no candidate) — both sides empty; (iv) an in-test negative control that monkeypatches `forecast_store._QHH_LATEST_CANDIDATE_RUNS_SQL` with a result-changing mutant (display horizon `'1 hour'` → `'1 second'`; `'1 minute'` is inert on the seed's 2-hour window) and asserts parity FAILS, proving the frozen oracle is independent of the module's SQL.
- Spec delta: MODIFIED requirement "Fallback candidate query scan discipline" — the "Result parity" scenario now names the frozen pre-pushdown statement as the comparison baseline (not the fast path) and requires the comparison to be reproducible in-repo; plus one ADDED scenario pinning that the oracle is independent of the production SQL (mutant → parity fails).

## Non-goals

- No change to `packages/common/forecast_store.py` / `packages/common/display_coverage.py` (issue AC: no production code).
- Strict-identity (`identity is not None`) parity; the sibling pushdown copy in `display_coverage.py` (its parity also rests on a one-off receipt — separate issue if wanted).
- Performance baselines; FAST-path ≡ fallback equivalence (existing test keeps that).
- Re-aligning the frozen text with #1442's surrogate-key join (the divergence is absorbed by the asserted projection, design D2).
- Retiring the frozen statement when #1342 drops the text identity columns — the frozen text joins `hydro.river_timeseries` on text columns and WILL break then; routing comment on #1342 is part of this change's evidence, the retirement itself is #1342's.

## Must-preserve

- The three existing tests in the file stay byte-identical in behaviour (the FAST-vs-fallback test remains; it answers a different question).
- `tests/test_river_ts_text_identity_cleanup.py` census/register untouched: the test module is not a registered source and no repo-wide sweep exists (`REGISTERED_SOURCES` is a closed tuple), so the frozen text-join SQL in a fixture file is not an oracle violation — verified by running that test.
- CI selector (`scripts/select_ci_tests.py:796` comment) already treats the integration file as DSN-gated; no selector change.

## Risk triage

- Risk surface: display-plane data correctness oracle (silent wrong/missing rows on `/` latest-QHH). Change itself is test-only → low blast radius; the value is regression protection.
- Seams under test: `forecast_store._run_display_coverage_available` (forced miss), `forecast_store._QHH_LATEST_CANDIDATE_RUNS_SQL` (mutant injection point), `PsycopgForecastStore._fetch_all` (positional vs mapping binding), `tests/integration_helpers.seed_issue_126_data` (state).
- Reviewer packs: correctness/test-design (is the parity non-vacuous, is the mutant real), spec-conformance/oracle-integrity. Not selected: security, performance (no production path changes).
