# Tasks — fix-national-discharge-coverage-snapshot-order (issue #2087)

## Risk triage

```text
Issue type: bugfix
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium
Fixture level: expanded
Upstream suggested level: absent (hand-written follow-up issue; no upstream contract fields)
Why:
- Mandatory core expanded trigger: "concurrency, retry, cancellation, persisted/shared state
  transitions" — this is a READ COMMITTED two-statement snapshot seam.
- `_national_discharge_coverage_rows` is the declared single owner of the display-ready predicate,
  read by three consumers.
- The invariant at stake (fail-closed national intersection) is declared in `mvt-tile-contract` and
  pinned by existing tests, so a silent fail-open makes the spec wrong, not just the code.
Repair intensity: medium
- Override of the `high` trigger "shared helper behavior", recorded: the source change is the ORDER of
  two existing statements inside one function — no SQL text, no return shape, no call signature, and a
  provable behavioral no-op absent a race. The helper's three consumers are all read-only, and the
  change touches no auth/permission, file IO/publish/delete, production config, evidence-chain, money,
  or data-loss surface. (The consumer `national_discharge_cycle_coverage` IS a public symbol imported
  by `apps/api/routes/hydro_display.py`, so the override rests on the change's shape, not on the
  helper being module-private.)
Selected risk packs:
- Concurrency / shared state / ordering
- Error handling / rollback / partial outputs
- Legacy compatibility / examples
- Documentation / migration notes
OpenSpec change: fix-national-discharge-coverage-snapshot-order (generated)
Evidence floor:
- `uv run ruff check .`
- `uv run pytest -q tests/test_hydro_display_mvt_scaling.py`
- `uv run pytest -q tests/test_display_publish_status_only.py`
- `openspec validate fix-national-discharge-coverage-snapshot-order --strict --no-interactive`
- Red proof for the four new cases plus the two flipped order assertions against unmodified
  `services/tiles/mvt.py`
```

### Risk-pack disposition (every pack marked)

| Pack | Disposition | Reason |
|---|---|---|
| Public API / CLI / script entry | not selected | No route signature, parameter, or response shape changes; the two affected routes keep their contracts. |
| Config / project setup | not selected | No env var, setting, or dependency touched. |
| File IO / path safety / overwrite | not selected | No filesystem access anywhere in the diff. |
| Schema / columns / units / field names | not selected | Zero SQL text change; no column, migration, or payload field added or renamed. |
| Auth / permissions / secrets | not selected | Read-only display plane; no credential, role, or policy surface. |
| Concurrency / shared state / ordering | **selected** | The whole change: two READ COMMITTED snapshots and the order they are taken in. |
| Resource limits / large input / discovery | not selected | Same two statements, same binds, same row counts; no new scan or unbounded dimension. |
| Legacy compatibility / examples | **selected** | The no-race path must stay byte-identical for all three consumers and the existing suite. |
| Error handling / rollback / partial outputs | **selected** | The change's product IS the failure mode: fail-closed vs fail-open under a race. |
| Release / packaging / dependency compatibility | not selected | No dependency or packaging change. |
| Documentation / migration notes | **selected** | Existing docstrings/comments assert the old order and call these branches deferred; left stale they become a wrong oracle. |
| Geospatial / CRS / basin geometry (domain) | not selected | No geometry, projection, or tile-geometry code path. |
| Hydro-met time series / forcing windows (domain) | not selected | The valid-time clamp and 3-hour stride are untouched; only which rows reach the clamp. |
| SHUD numerical runtime / conservation / NaN (domain) | not selected | No solver, forcing, or numerical code. |
| PostGIS / TimescaleDB domain behavior (domain) | **selected** | READ COMMITTED per-statement snapshot semantics ARE the subject; the fix depends on PostgreSQL evaluating `mi.active_flag` inside each statement's own snapshot. Evidenced by tasks 1, 7-10 and the design.md D1 derivation against the live SQL. |
| Slurm production lifecycle / mock-vs-real parity (domain) | not selected | No sbatch, gateway, or scheduler surface; nothing in the diff runs on node-22. |
| External hydro-met providers / snapshot reproducibility (domain) | not selected | No GFS/IFS/ERA5 acquisition or provider-snapshot code; `source` stays an opaque route enum. |
| Run manifest / QC provenance (domain) | not selected | No manifest, QC result, or evidence artifact is produced, read, or bound by the diff. |
| Published NHMS artifacts / display identity (domain) | **selected** | The change decides which `(source, cycle)` identities the public catalog advertises and which the canonical tile route accepts. Evidenced by the `mvt-tile-contract` delta's four scenarios plus tasks 13-14 (unchanged no-race identity for all three consumers). |

## Implementation tasks

- [x] 1. Re-derive the ordering argument (design.md D1) against the actual SQL before editing. If it
      does not hold, stop and report instead of proceeding to alternative 1/2.
- [x] 2. In `services/tiles/mvt.py::_national_discharge_coverage_rows`, execute the coverage-rows
      statement first and the active-network statement second. **Zero SQL text change** in either
      statement; no isolation-level change; return tuple shape unchanged.
- [x] 3. Rewrite the helper docstring to state the trade, not a one-sided improvement (design.md D2):
      the chosen ordering and why numerator-GROWTH (activation) now fails closed; that the swap
      **newly opens** the numerator-SHRINK class and that this class has live writers
      (`mark_run_failed` in `workers/output_parser/parser.py` accepts `succeeded`/`parsed`;
      `mark_failed` in `workers/shud_runtime/runtime.py` is unguarded; `segment_count → 0` is guarded
      by #1446), with the reason it is accepted anyway; the deactivation-with-zero-coverage race-path
      delta; and the availability cost (one activation can empty the catalog for up to one cache TTL).
      Re-verify the writer facts yourself before writing them down.
- [x] 4. Fix every docstring/comment that asserts the old order or calls these branches deferred:
      `national_discharge_cycles` ("statement 1 sees … statement 2 returns …", and the paragraph
      declaring the zero-coverage branch deferred), `NationalCycleCoverage.complete`'s matrix-row
      citation. Locate them by reading, not by line number.
- [x] 4b. Flip the EXISTING assertions that pin the old statement order. These are the fixture's
      most direct oracle for the spec delta (the four behavior cases are only indirect), so they are
      updated deliberately, never weakened into set/unordered comparisons:
      - `tests/test_hydro_display_mvt_scaling.py` — the `national_discharge_cycle_coverage` case
        asserting `[params for _sql, params in helper_session.executions] == [None, {"source": "gfs",
        "cycle": _CYCLE, "since": None}]` becomes `[{...}, None]`.
      - `tests/test_hydro_display_mvt_scaling.py` — the canonical-tile case asserting
        `_statement_kinds(session) == ["digest", "active", "coverage", "tile"]` becomes
        `["digest", "coverage", "active", "tile"]`.
      - `_NationalRouteSession`'s docstring, which states "#2153 added the per-cycle coverage pair
        (active set, then coverage rows)".
      - A THIRD order-coupled site, found by the Phase 2 audit and of a different kind — implicit
        coupling through "last statement executed" rather than an explicit order assertion:
        `tests/test_hydro_display_mvt_scaling.py
        ::test_national_valid_times_use_active_basin_identity_not_transient_model_id`. Its four
        SQL-shape assertions read `session.sql`, which holds only the LAST statement, so they were
        implicitly pinned to the coverage read; after the swap the active read is last, turning the
        two positives red and — if left as-is — the two negatives (`"mi.model_id = h.model_id" not
        in …`, `"hydro.river_timeseries" not in …`) VACUOUS, since the active statement mentions
        neither. Repaired by selecting the coverage statement out of `session.executions` by content
        (the idiom already used twice in this file), keeping all four assertions unchanged in
        meaning. Not weakened, not skipped, `_Session` untouched.
      Locate all four by reading, not by line number.
- [x] 5. Correct `apps/api/routes/hydro_display.py::_default_layer_catalog`'s comment about the
      snapshot seam between its two helper calls. It is known to go stale: it currently describes the
      old order ("statement 1's active set still holds it, statement 2 drops its rows") and cites a
      covered run's status/coverage row being rewritten as a case that empties the intersection —
      which after the swap is exactly the newly opened fail-open class. Comment only; no behavior
      change, and that outer seam stays out of scope.

## Test / evidence tasks

- [x] 6. Add a race-modelling fake to `tests/test_hydro_display_mvt_scaling.py` as a **new subclass**
      of `_NationalDiscoverySession` (do not change the base class — existing tests pass an explicit
      `active_networks=` wider than their rows and rely on the base not filtering coverage by it).
      It must: flip from the pre-activation state to the post-activation state after the FIRST
      `execute()` regardless of which statement that is, and filter coverage rows by the active set
      current at that execution (what `mi.active_flag` does inside the real statement).
- [x] 7. Branch (a) × `national_discharge_cycles`: a network activated between the statements with
      zero display-ready rows → `cycles == []` and `default_cycle is None`.
- [x] 8. Branch (a) × `national_discharge_valid_times(source=…, cycle=…)` → `valid_times == []` and
      `observed_count == 0`.
- [x] 9. Branch (b) × `national_discharge_cycles`: the newly active network holds rows for cycle `K`
      only → neither `J` nor `K` is listed.
- [x] 10. Branch (b) × per-cycle valid-times for `J` → `valid_times == []`.
- [x] 11. Every one of tasks 7-10 carries non-vacuity assertions: the post-activation active set has
      at least 3 members and the coverage rows the fake serves are non-empty, so the case cannot pass
      on an empty fixture.
- [x] 12. Red proof: run the four new cases against unmodified `services/tiles/mvt.py` and capture the
      output. Use a patch file, NOT `git stash` (the stash stack is shared with other worktrees):
      `git diff services/tiles/mvt.py > /tmp/mvt.patch && git checkout -- services/tiles/mvt.py &&
      uv run pytest -q <node ids> ; git apply /tmp/mvt.patch`. Run the four new cases AND the two
      task-4b assertions in that red pass: all six must be red against the unswapped source.
- [x] 13. `uv run pytest -q tests/test_hydro_display_mvt_scaling.py` green. Oracle integrity: the ONLY
      existing expectations that may change are the order-coupled sites enumerated in task 4b, and they
      must be changed by flipping the expected order — not by relaxing the comparison, not by deleting
      or skipping a test, and not by loosening any other assertion. Any FURTHER expectation the swap
      turns red is a finding to report, not to edit. This gate held: the swap turned exactly one
      unenumerated expectation red, it was reported through the Phase 2 audit before any edit, and it
      was then added to 4b as its third bullet and repaired there — by re-pointing the assertions at
      the statement they always meant, not by weakening them. A sweep for a fourth site of the same
      kind (any test reading the `session.sql` "last statement" attribute on a session that runs
      `_national_discharge_coverage_rows`) found none: every other `.sql` read in the file targets
      `national_discharge_source_version` / `national_river_network_source_version`, neither of which
      calls that helper.
- [x] 14. `uv run pytest -q tests/test_display_publish_status_only.py` green — it pins the `h.status
      IN (...)` occurrence counts at 2/1/1/5, so it proves no sixth query shape was introduced. It
      does NOT prove "zero SQL text change"; that claim is carried by diff review, and the diff must
      show both statement literals byte-identical.
- [x] 15. `uv run ruff check .` clean.

## Documentation tasks

- [x] 16. `openspec/changes/display-v2-national-timeline-precip-overlay/invariant-matrix-i5-2009.md`:
      add mutation row **40d** (the matrix already has 40 / 40b / 40c; there is no 40a) for "the active-network statement runs AFTER the
      coverage statement", with the mutation (restore the old order) and the tests it kills; update
      decision 16's closing sentence so the two branches are no longer described as an open DEFER, and
      point it at this change / issue #2087.
- [x] 17. `openspec validate fix-national-discharge-coverage-snapshot-order --strict --no-interactive`
      passes.
- [ ] 18. Report the falsified upstream premise (orchestrator-owned, Phase 8): issue #2087's
      "残留…那不是激活竞态，且现有代码同样不覆盖" is wrong — the CURRENT order does catch the
      numerator-shrink class, so the swap opens it rather than inheriting it, and the class is broader
      than DELETE (status rewrite, coverage zeroing). Post this correction on #2087 and carry it in
      the PR's `偏离记录` and the Chinese work summary.
- [ ] 19. Route the residual (orchestrator-owned, Phase 8): the newly opened numerator-shrink fail-open
      class is a known limit of this change and needs a tracked follow-up issue (single-statement merge
      with its node-27 lane), or a recorded one-line reason why none is filed.

## Verification matrix rows consumed

| Surface | Command | Expected evidence |
|---|---|---|
| Python/shared helper | `uv run pytest -q tests/test_hydro_display_mvt_scaling.py`, `uv run pytest -q tests/test_display_publish_status_only.py`, `uv run ruff check .` | Passing tests, zero lint findings, and the only existing expectations modified are the two order assertions in task 4b, flipped rather than relaxed |
| OpenSpec | `openspec validate fix-national-discharge-coverage-snapshot-order --strict --no-interactive` | Strict-valid change |

**No node-27 oracle required**: the diff is pure in-process Python logic over a fake session, with no
DB migration, no display deployment receipt, no read-only boundary change, and no Slurm/SHUD surface.
Issue #2087 records the same conclusion (the node-27 need existed only for the rejected
REPEATABLE READ alternative).

## Round-1 cross-review fix pass (added after the verification gate)

Round 1 ran three seats (`correctness`, `invariant-state`, `test-evidence+spec-compliance`), produced no
P0/P1, and was recorded **not clean** because one verified finding was a coverage gap on behaviour this
change introduces — those never downgrade to a note. Verdict tables: `.workplans/pr-2455/review/verify-*.md`.

- [x] 20. Pin the deactivation-with-zero-coverage delta (`design.md` D2's recorded race-path delta):
      `test_national_cycles_list_a_cycle_when_a_zero_coverage_network_is_deactivated`, red under a partial
      revert that keeps growth closed but restores the old deactivation strictness.
- [x] 21. Pin its companion that must stay fail-closed in BOTH orders (a deactivated network that HAS
      coverage rows): `test_national_cycles_close_when_a_covered_network_is_deactivated`, red under the
      superset-containment mutation (`not covered_networks >= active_networks`). Recorded as matrix row 40e.
- [x] 22. Characterize the numerator-SHRINK residual the swap newly opens:
      `test_national_cycles_still_list_a_cycle_whose_covered_run_stopped_being_display_ready`, on a new
      `_CoverageRowVanishesBetweenStatementsSession`. It documents the ACCEPTED residual, not desired
      behaviour; the fix stays out of scope (task 19's follow-up).
- [x] 23. Correct the false claim in the helper docstring that `segment_count -> 0` is not a live writer:
      #1446's guard is bypassed by `force=True`, exposed as `--force` by `scripts/node27_refresh_coverage.py`
      as the intended operator remediation. Same correction applied to `design.md` D2.
- [x] 24. Cross-cutting claim audit (this PR's review history produced FIVE instances of one class: a
      confidently-worded factual claim, committed to an artifact, that the code contradicts). Every factual
      claim about other code in the docstrings/comments this PR added or rewrote was opened and checked; the
      audit itself found the fifth instance — the docstring had overstated `mark_failed`'s reachability, which
      is gated behind `create_run`'s `HYDRO_RUN_NOT_RETRIABLE` refusal. Verdicts recorded in the PR body.
- [x] 25. Widen the spec delta's antecedent: "without a concurrent activation or deactivation ... MUST NOT
      change any result" was falsified by the shrink class, which involves no membership change at all. Now
      conditioned on no concurrent write to `core.model_instance` / `hydro.hydro_run` /
      `hydro.run_display_coverage`, matching the wording the commit message, proposal and docstring already used.
- [x] 26. Correct matrix row 40d's Measured cell (it claimed `200 passed / 1 failed` "as delivered" with the
      `:170` site unrepaired, while that repair is in the delivered commit; and it undercounted the kill list
      as "5 assertions" when 9 node ids need six test names). Now `204 passed / 0 failed`, eight names, eleven
      node ids, with row 40e added for the superset mutation.

### Routed out of scope (report, don't fix)

- `national_discharge_valid_times`' no-argument branch discards the active set and derives its intersection
  from the rows — the "union over whoever happens to have data" the helper's own docstring forbids.
  Pre-existing, untouched by the swap; found independently by two seats.
- `NationalCycleCoverage.complete` has no `covered ⊋ active` oracle, so the superset mutation survives at
  that second comparison site. Predates #2087 (row 40b's site, from #2073); recorded in row 40e.
