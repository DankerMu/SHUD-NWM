# National discharge intersection closure (batch G2: #2457 #2458 #2459)

## Why

The national discharge intersection ("every active river network covers this identity") is answered by one helper, `_national_discharge_coverage_rows` in `services/tiles/mvt.py`, returning `(coverage rows, active-network set)`. It is judged at three sites, and the three issues are three gaps around that helper:

- **#2457 (needs-decision)** — PR #2455 (#2087) swapped the two READ COMMITTED statements (coverage first, active second). That closed numerator GROWTH (activation between the reads) and opened numerator SHRINK (a covered run stops being display-ready between the reads; live writers `mark_run_failed`, `node27_refresh_coverage.py --force`, runtime `mark_failed`). Whether to merge the two statements was never argued on a criterion.
- **#2458** — the no-argument branch of `national_discharge_valid_times` discards the helper's active set (`rows, _active_networks = ...`) and derives its denominator from the rows (`latest_by_network`), the exact union degradation the helper docstring forbids; a zero-coverage active network is invisible there. Reachable on the public route `GET /api/v1/layers/discharge/valid-times` with no arguments (frontend and catalog do not use it).
- **#2459** — `NationalCycleCoverage.complete` (`covered == active`, feeding the per-cycle valid-times branch and the canonical national tile's 424 `MVT_NATIONAL_IDENTITY_INCOMPLETE`) has no `covered ⊋ active` oracle: relaxing it to `>=` survives every related test (issue baseline 352 passed / 0 failed).

One PR because the three share the helper and its set-comparison rule; the user asked not to split them.

## What Changes

- **#2457** — ruling: **keep the two statements, accept the numerator-shrink residual** (design D1, with the frequency / harm / cost criteria). Code: the helper docstring cites #2457 and the ruling; the characterization test stays a characterization (its docstring points at the ruling). Matrix: new row recording the ruling; row 40d's trade text points at #2457.
- **#2458** — the no-argument branch keeps the helper's active set and fails closed to `[]` unless the set of networks in `latest_by_network` EQUALS the active set (same set rule as the per-cycle branch, not cardinality). Docstring of `national_discharge_valid_times` updated to match. node-27 read-only pre-check (2026-09-23T12:18:57Z, `nhms_display_ro`): active 48, covered 48, both difference sets empty → a production no-op today.
- **#2459** — test-only: `covered-superset` case added to the four-case parametrize, a route-level superset case (424 `MVT_NATIONAL_IDENTITY_INCOMPLETE`, no tile statement), and a superset case on the #2458 no-argument site, so all three judging sites carry the superset oracle. Mutation receipt `complete` → `>=` must go red. Matrix row 40e's "Site gap, recorded not fixed" becomes fixed with the measured counts.

## Triage

```text
Issue type: bugfix (1 logic, #2458) + test enforcement (#2459) + ruling (#2457)
Fixture level: standard
Upstream suggested level: absent
Risk axes: Legacy compatibility (a live route's answer can flip to [] when an active network has no display-ready run); Concurrency (two-statement snapshot seam, ruling only)
```

## Impact

- `services/tiles/mvt.py` (no-arg branch logic; docstrings), tests under `tests/test_hydro_display_mvt_scaling_*.py` (+ shared fakes in `tests/hydro_display_mvt_helpers.py` only if a case needs it — issue #2459 says neither fake needs extending).
- `openspec/specs/mvt-tile-contract` — ADDED requirement for the no-argument branch.
- `openspec/changes/display-v2-national-timeline-precip-overlay/invariant-matrix-i5-2009.md` — rows 40d/40e updated, rows 40f/40g added.
- No API schema/OpenAPI change; no SQL change; no migration.
