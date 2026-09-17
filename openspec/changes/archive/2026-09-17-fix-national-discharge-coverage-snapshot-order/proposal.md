## Why

`_national_discharge_coverage_rows` (`services/tiles/mvt.py`) answers one question — "which
`(network, cycle)` pairs are display-ready, and which networks must be covered" — with **two**
statements. Under READ COMMITTED each takes its own snapshot: statement 1 reads the active-network
denominator from `core.model_instance`, statement 2 reads the coverage numerator. PR #2073 upgraded
the callers' test from `len(covered) == len(active)` to `covered == active` as SETS, which closed the
*reorder-during-activation* branch — but the denominator is still the OLDER snapshot, so two
fail-**open** branches survive by construction (issue #2087):

- **zero-coverage activation** — a network activated between the statements with no display-ready row
  never appears in statement 2 at all, so no comparison of statement-2 output can see it;
- **partial-coverage activation** — a network activated between the statements with rows for cycle K
  but not J leaves J's covered set equal to the stale active set, so J stays listed.

Either one advertises a cycle a just-activated basin cannot render. The frontend paints that basin
colourless, which a user reads as "no flow" rather than "no data" — exactly what the fail-closed
intersection exists to prevent — and the answer is served from the display catalog cache for up to
`DISPLAY_CATALOG_STALE_MAX_SECONDS` (600 s) on the stale-while-revalidate path.

## What Changes

- Read the coverage rows FIRST and the active-network set SECOND inside
  `_national_discharge_coverage_rows`. The coverage statement carries `mi.active_flag` itself, so
  `covered ⊆ active@T1`; an activation only grows the set, so `active@T2 ⊇ active@T1` and any
  activation between the statements forces `covered != active@T2` — both branches fail **closed**.
- No SQL text changes, no isolation-level change, no migration, no new query. Absent a race the
  result is byte-identical (same predicates, `covered ⊆ active` either way).
- Document the trade honestly in the helper and caller docstrings (which today describe the old order
  and call these branches deferred): the swap closes the numerator-growth class and **newly opens** a
  numerator-shrink class (a covered row ceasing to be display-ready between the reads). That class has
  live writers — `mark_run_failed` accepts `succeeded`/`parsed`, and `shud_runtime`'s `mark_failed` is
  unguarded — so it is accepted on the argument that no two-statement design can close both classes
  and that closing both means the plan-changing single-statement merge, not on it being unreachable.
  Also document the cost: one normal activation can empty the catalog for at most one cache TTL.
- Four new behavior tests — two branches × (`national_discharge_cycles`, per-cycle
  `national_discharge_valid_times`) — driven by a race-modelling fake session.

## Capabilities

### New Capabilities

None.

### Modified Capabilities

- `mvt-tile-contract`: the national discharge intersection's two reads gain an ordering requirement —
  the active-network denominator must be at least as fresh as the coverage numerator, so a network
  activated between them fails the intersection closed instead of being invisible to it.

## Impact

- `services/tiles/mvt.py` — `_national_discharge_coverage_rows` (statement order + docstring),
  `national_discharge_cycles` and `NationalCycleCoverage` docstrings/comments (they describe the old
  order and declare these branches deferred).
- `apps/api/routes/hydro_display.py` — `_default_layer_catalog`'s comment about the third-layer
  snapshot seam between its two helper calls: re-checked for accuracy, no behavior change.
- `tests/test_hydro_display_mvt_scaling.py` — new race-modelling fake subclass + four cases, plus two
  existing assertions that pin the old statement order (`executions` binds, `_statement_kinds`), which
  are flipped rather than relaxed.
- `openspec/changes/display-v2-national-timeline-precip-overlay/invariant-matrix-i5-2009.md` —
  new mutation row and decision-16 closure note.
- No DB, API shape, migration, or frontend change. `design.md` is authored because the fixture level
  is `expanded` (mandatory concurrency trigger).
