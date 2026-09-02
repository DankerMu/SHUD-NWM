## Risk Triage

```text
Issue type: performance/access-path correction + wording + lock bound
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium (publish predicate on every tick; backfill runtime behaviour; shared shape oracle)
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issues; expanded forced by Timescale write path + schema field + receipt evidence)
Why:
- production write statement predicate change; shape oracle re-pin
- backfill gains a new failure channel (lock_timeout) that must be receipted live
OpenSpec change: timescale-compressed-access-paths
Evidence floor:
- uv run ruff check .
- uv run pytest -q tests/test_river_ts_text_identity_cleanup.py tests/test_display_publish_status_only.py tests/test_node27_autopipeline_handoff.py tests/test_node27_river_identity_backfill.py tests/test_node27_river_identity_backfill_receipt.py tests/test_forcing_copyback_backfill.py
- node-27 (integration marker): uv run pytest -q tests/test_display_coverage_residual_debt_integration.py tests/test_real_database_integration.py tests/test_integration_helpers_bounded_teardown.py
- node-27: EXPLAIN before/after; count(status='parsed' AND parsed_at IS NULL)=0; backfill dry-run receipt
- openspec validate timescale-compressed-access-paths --strict --no-interactive
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | new backfill env knob; autopipe tick statement |
| Config / project setup | selected | env default + assertion; example file if the lane has one |
| File IO / path safety / overwrite | not selected | receipt path handling unchanged |
| Schema / columns / units / field names | selected | receipt schema `bounds.lock_timeout_ms` (ms unit) |
| Auth / permissions / secrets | not selected | same roles |
| Concurrency / shared state / ordering | selected | lock_timeout vs statement_timeout ordering; publish vs parser race on `parsed_at` |
| Resource limits / large input / discovery | selected | the whole point of #1779 is planner cost on compressed chunks |
| Legacy compatibility / examples | selected | #1674 D2 legacy cohort must stay outside the predicate; historical receipts vs new required field |
| Error handling / rollback / partial outputs | selected | config refusal before any batch; stopped-path receipt |
| Release / packaging / dependency compatibility | not selected | none |
| Documentation / migration notes | selected | §4.6.2 caveat; #1686 AC6 note |
| PostGIS / TimescaleDB 域行为 | selected | segmentby/orderby semantics; compressed access paths |
| 其余 domain packs | not selected | not touched |

## Tasks

- [x] T1 (#1779) Predicate rewrite + docstring; MODIFIED spec delta (references → writes); `tests/test_display_publish_status_only.py` rewritten to SET/WHERE source pins (`parsed_at`/`updated_at` never in SET; `parsed_at IS NOT NULL` in WHERE; no fact table); `tests/integration_helpers.py` stamps `parsed_at` on `FORECAST_RUN_ID` only; the residual-debt suite asserts `== 1` plus hindcast still `parsed` plus legacy published untouched; the three shared-helper suites run on node-27 under T6 (not claimed here). Resolve T2's conditional by reading `tests/test_river_ts_text_identity_cleanup.py:206-211`.
- [x] T2 (#1779) Re-pin `NO_AIDS` oracle and the census for `node27_autopipeline.py`; if the oracle still asserts an "uncompressed frontier" rationale (:206-211), correct it to the measured plan shape.
- [x] T3 (#1778) Comment rewrite; marker line byte-identical (test stays green); PR body records (a)/(c) rejection; #1342 note.
- [x] T4 (#1476) Env knob + default + config assertion + `SET LOCAL lock_timeout`; receipt `bounds` on both paths; schema `required` with `SCHEMA_VERSION` unchanged (no committed receipts; cursor loader gates on version only); tests (55P03 → lock_contention, 57014 unchanged, inverted bound refuses, receipt validates).
- [x] T5 (#1476) Runbook §4.6.2 caveat (`:2204`) rewritten; env example if present.
- [ ] T6 node-27 (queued session): RO `EXPLAIN (COSTS OFF)` of the old EXISTS form and the new form, plus `EXPLAIN (ANALYZE, BUFFERS)` of the new form showing the parsed=0 short-circuit stays at single-digit shared buffers (#1779 AC2) (plans archived under the PR); count query; integration-marked run of the three shared-seed suites; backfill dry-run (non-enforce) from a detached worktree with a scratch receipt path, `bounds.lock_timeout_ms` visible, no `lock_contention`.
- [ ] T7 Comment on #1686: publish site closed, copyback pushdown still untriaged.

## Non-goals (explicit)

No aid on publish, no copyback SQL change, no #1342 work, no classification changes in the backfill.
