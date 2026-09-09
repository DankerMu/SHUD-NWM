# Guard MVT tile instants against out-of-range UTC normalization (#2033)

## Why

`services/tiles/mvt.py::canonical_mvt_time` calls `.astimezone(UTC)` unguarded on both of its
branches. CPython raises **`OverflowError`** — not `ValueError` — when normalization leaves
`datetime.min/max`, and nothing on the path catches it. Two public, unauthenticated legacy tile
routes still feed a user-controlled instant straight into that helper via `_format_time`:

- `apps/api/routes/hydro_display.py::hydro_mvt_tile`
- `apps/api/routes/hydro_display.py::hydro_national_mvt_tile`

Baseline measured on the public reverse proxy (`https://test.nwm.ac.cn`, 2026-09-08):

| URL instant | status |
|---|---|
| `not-an-instant` | 422 |
| `9999-12-31T23:59:59-08:00` | **500** |
| `0001-01-01T00:00:00+08:00` | **500** |

Same route, same class of bad client input, two different status codes. The 500s also arrive
*after* SQL: the national route pays a `national_discharge_source_version` round trip (the
`source_version=` kwarg is evaluated before `valid_time=`), and the single-run route pays
`_require_display_ready` + `_require_hydro_mvt_source_identity`. Every such request pollutes the
5xx error-rate signal with a client-input defect and burns DB round trips.

This is **pre-existing on `origin/master`** (helper unchanged since root commit `35ae1b96`); it was
found out-of-scope during PR #2027 review. PR #2027's own new `{source}/{cycle}` route already
closed the hole locally in `_require_seconds_precision_instant`; this change lifts that guard into
the shared helper and the two legacy routes instead of growing a third sibling copy.

## What Changes

1. `canonical_mvt_time` raises a typed `MvtTimeOutOfRangeError(ValueError)` instead of letting a
   bare `OverflowError` escape — both the `datetime` branch and the string branch.
2. `hydro_display.py` extracts `_require_representable_instant(value, field_name)` from the existing
   guard inside `_require_seconds_precision_instant`, which then calls it. One implementation, three
   routes.
3. The two legacy routes call `_require_representable_instant` immediately after `validate_xyz`,
   before any SQL, and keep passing the original `valid_time` object downstream.

Non-goals: no change to in-range canonicalization (`Z` / `+00:00` / `.000Z` collapse, sub-second
round-trip), no tile SQL change, no cache-key composition change, no `*_QUERY_VERSION` bump, no
business-validity check on `valid_time`.

`design.md` is required at this fixture level (expanded / high repair intensity) and is present.
