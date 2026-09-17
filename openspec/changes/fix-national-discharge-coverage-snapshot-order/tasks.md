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
- [x] 18. Report the falsified upstream premise (orchestrator-owned, Phase 8) — posted as a comment on #2087: issue #2087's
      "残留…那不是激活竞态，且现有代码同样不覆盖" is wrong — the CURRENT order does catch the
      numerator-shrink class, so the swap opens it rather than inheriting it, and the class is broader
      than DELETE (status rewrite, coverage zeroing). Post this correction on #2087 and carry it in
      the PR's `偏离记录` and the Chinese work summary.
- [x] 19. Route the residual (orchestrator-owned, Phase 8) — filed as #2457, with #2458 and #2459 for the two out-of-scope findings: the newly opened numerator-shrink fail-open
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
- [x] 24. Cross-cutting claim audit (at the time of the audit the ledger below stood at rows 1-5 of one class: a
      confidently-worded factual claim, committed to an artifact, that the code contradicts). Every factual
      claim about other code in the docstrings/comments this PR added or rewrote was opened and checked; the
      audit itself found ledger row 5 — the docstring had overstated `mark_failed`'s reachability, which
      is gated behind `create_run`'s `HYDRO_RUN_NOT_RETRIABLE` refusal. (The tick covers the audit; publishing
      its verdicts is task 27, which belongs to Phase 8 — this task was briefly ticked while claiming the
      publication too, which round 2 caught as finding D.)
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

## Round-2 cross-review (clean) — the four P2 notes and what closed them

Round 2 ran two pinned-core seats (`invariant-state` full-scope, `test-evidence+spec-compliance`) on
`9344e37e` and was recorded **clean**: no P0/P1 and no coverage gap. Its four verified findings were all
minor and all in orchestrator-owned artifacts, so under the P2-note rule they are notes, not deferrals —
they owe no issue and no reason line. Records: `.workplans/pr-2455/review/round2-summary.md` and
`verify-round2-artifact-claims.md`. Corrected anyway, because three of them were false or stale claims in
artifacts that outlive this PR:

- [ ] 27. Publish the claim-audit verdicts and the corrected evidence numbers in the PR body, close
      round-1 findings C3 (the red-proof arithmetic) and C4 (issue #2087 AC2's literal
      「无一处期望值修改」), drop the round-1 claim the audit retracted (`mark_failed` presented as an
      unqualified live writer), and fill the `Agent Review` block. Phase 8.
      **Deliberately unticked here.** The Phase 7 gap sweep caught this task ticked while unmet — in the
      very commit whose purpose was to fix task 24 being ticked while unmet, which is the same error one
      layer up. The PR body is not part of any commit, so it cannot land atomically with the tick; the
      honest state at commit time is unticked. The receipt is the `Agent Review` block in the PR body
      itself, against the frozen head.
- [x] 28. Correct matrix row 40e's reachability parenthetical. It said the pre-#2087 order made
      `covered ⊋ active` unreachable. False: the coverage statement carries `mi.active_flag` in its own
      snapshot, so under the old order a network activated between the reads holding display-ready rows
      landed in `covered` and not in `active@T1` — which is exactly how #2073's set comparison caught
      cycle K. The swap changes which race routinely produces the state, not whether it exists. The same
      cell's other half — that the untested superset mutation at `NationalCycleCoverage.complete`
      predates #2087 and is therefore routed rather than fixed — was verified CORRECT and stands.
- [x] 29. Correct two enumeration overclaims (row 40e and two test docstrings): the deactivation test is
      the only case asserting the swap relaxes a MEMBERSHIP-change outcome, not the only more-permissive
      assertion in the file (the shrink characterization is the other); and the other set-comparison
      cases are subset, equal **or incomparable**, on all of which `>=` and `==` agree — the strict
      superset is the one shape where they differ.
- [x] 30. Replace row 40d's line-number mutation instruction with identifiers. The numbers were accurate
      when the row was written and were invalidated 21 lines later by this PR's own second commit, which
      left the cited offset pointing inside the coverage SQL literal.

## Phase 7 gap sweep (final review)

- [x] 31. Propagate the row-40e correction to the two production docstrings that had not received it.
      `national_discharge_cycles` and `_national_discharge_coverage_rows` still flattened "the set
      comparison could not see branch (b)" with no per-cycle scoping. The truth, re-derived against the
      PRE-swap tree rather than from any artifact: under the old order the coverage statement applied
      `mi.active_flag` in its own (later) snapshot, so a partial-coverage newcomer's rows for cycle K
      landed in the covered set while the stale active set still lacked it — `covered ⊋ active`, unequal,
      so #2073 DID catch K. Only the uncovered cycle J escaped. Both docstrings now scope the invisible
      cases to "the cycles the newcomer has NO rows for". Left unchanged after review: the
      `NationalCycleCoverage.complete` comment (its preceding sentence already scopes it correctly) and
      the "same predicates, one snapshot" shorthand (terse, not false).
- [ ] 32. See task 27 — the PR-body publication is a Phase 7 finding and is the same item as 27.

**Failure-class ledger.** One class dominated this PR's entire review history: a confidently-worded
factual claim, committed to an artifact, that the code contradicts. The count is enumerated rather than
asserted — an earlier draft of this very paragraph said "Ten" while listing nine, which is instance 13.

| # | Caught by | The claim, and what the code said |
|---|---|---|
| 1 | fixture review r1 | `design.md` D2 stated the shrink residual's direction backwards ("现有代码同样不覆盖") — the OLD order caught that class |
| 2 | fixture review r2 | `_TERMINAL_HYDRO_STATUSES` cited as a global state-machine guard; it only stops the cancel endpoint overwriting a terminal row |
| 3 | cross-review r1 | the spec delta's antecedent excluded only membership writes, so the shrink class falsified its MUST-NOT-change clause |
| 4 | cross-review r1 | the docstring called `segment_count -> 0` "NOT a live writer"; `--force` bypasses #1446 and is the documented operator remediation |
| 5 | the r1 fix pass's own audit | the docstring overstated `mark_failed`'s reachability, which sits behind `create_run`'s `HYDRO_RUN_NOT_RETRIABLE` |
| 6 | cross-review r2 | matrix row 40e: "the pre-#2087 order made this unreachable" — the superset state was reachable by activation, which is how #2073 caught cycle K |
| 7 | cross-review r2 | "the ONLY case asserting the swap makes something MORE permissive" — the shrink characterization is another |
| 8 | cross-review r2 | "every other set-comparison case is `covered ⊆ active`" — several are incomparable |
| 9 | cross-review r2 | row 40d's mutation instruction cited line numbers its own PR's second commit shifted into the SQL literal |
| 10 | cross-review r2 | task 24 ticked while its stated deliverable did not exist |
| 11 | Phase 7 pass 1 | task 27 ticked while unmet — in the commit whose purpose was to fix instance 10 |
| 12 | Phase 7 pass 1 | two production docstrings never received instance 6's correction |
| 13 | Phase 7 pass 2 | this ledger asserted a total of ten while enumerating nine |
| 14 | Phase 7 pass 2 | the PR body reported the #2087 upstream correction in the done tense while it was unposted |
| 15 | Phase 7 pass 3 | a superseded "Seven instances" paragraph survived directly above this ledger — pass 2 replaced one of the file's two competing totals with the ledger and left the other, in a file it had open |
| 16 | pre-merge self-audit | the PR body's `Key findings addressed` wrote "round 2 四条 P2" while `verify-round2-artifact-claims.md:10-15` records five CONFIRMED; the count was taken from 偏离记录 bullets, not from the verdict table, and 偏离记录 12 omitted finding D entirely |

Averted before they were written, and therefore absent from the ledger: the round-2 repair's implementer rejected the
orchestrator's suggested replacement wording as itself false (the file's EQUAL pairs have `>=` True and
survive the mutation because `>=` and `==` agree, not because both are False), and the Phase 7 repair's
implementer re-derived the K/J behaviour from the pre-swap tree instead of copying the matrix row.

Every instance was authored by someone reasoning from a plausible mental model without opening the cited
file; every one was caught by someone who opened it. Instances 10, 11, 13 and 15 are the sharpest: each was
written *while fixing the previous one*. That is the transferable lesson from this issue, and it is why
the docstrings here carry their citations — so the next reader can check them the cheap way.
