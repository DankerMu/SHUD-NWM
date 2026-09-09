# Design: guard-mvt-instant-range-2033

## Risk triage

```text
Issue type: bugfix
Project profile: NHMS (openspec/project-profile.md)
Blast radius: high
Fixture level: expanded
Upstream suggested level: absent (hand-written issue from a PR #2027 out-of-scope review finding)
Repair intensity: high (shared helper behavior + validation shared by three public routes)
Why:
- public API / routing / entrypoint trigger: two unauthenticated tile routes
- legacy compatibility trigger: legacy routes must keep accepting in-range sub-second instants
- error handling / partial outputs trigger: new failure branch on a shared helper
- shared helper root: canonical_mvt_time backs cache keys and cache-row comparison for all five layers
Selected risk packs:
- Public API / CLI / script entry
- Error handling / rollback / partial outputs
- Legacy compatibility / examples
- Hydro-met time series / forcing windows (domain)
- Published NHMS artifacts / display identity (domain)
OpenSpec change: guard-mvt-instant-range-2033 (generated)
Evidence floor:
- uv run ruff check .
- uv run pytest -q tests/test_hydro_display_mvt_scaling.py
- openspec validate guard-mvt-instant-range-2033 --strict --no-interactive
- node-27 isolated-worktree live receipt (five layers bytes/ETag/cache unchanged; out-of-range 422 sql=0)
```

### Risk-pack selection ledger

| Pack | Selected | Reason |
|---|---|---|
| Public API / CLI / script entry | yes | two public tile routes change status code for one input class |
| Config / project setup | no | no config, env, or dependency touched |
| File IO / path safety / overwrite | no | no filesystem surface; file tile cache is read/written by unchanged code |
| Schema / columns / units / field names | no | no column, bind name, or property schema changes |
| Auth / permissions / secrets | no | routes were already unauthenticated; no authz decision changes |
| Concurrency / shared state / ordering | no | guard is pure and per-request; no shared mutable state |
| Resource limits / large input / discovery | no | strictly reduces work (fails before SQL) |
| Legacy compatibility / examples | yes | legacy routes must NOT inherit the new route's sub-second rejection |
| Error handling / rollback / partial outputs | yes | this change *is* an error-path change |
| Release / packaging / dependency compatibility | no | stdlib only |
| Documentation / migration notes | no | no operator-visible procedure changes; receipt covers evidence |
| Geospatial / CRS / basin geometry | no | geometry pipeline untouched |
| Hydro-met time series / forcing windows | yes | the value being guarded is a forecast instant |
| SHUD numerical runtime | no | not reached |
| PostGIS / TimescaleDB domain behavior | no | no SQL text or bind change |
| Slurm production lifecycle | no | not reached |
| External hydro-met providers | no | not reached |
| Run manifest / QC provenance | no | not reached |
| Published NHMS artifacts / display identity | yes | `canonical_mvt_time` composes cache key + cache-row identity for all five layers |

## Decisions

### D1 — The helper raises a typed error, never a sentinel

`canonical_mvt_time` wraps both `.astimezone(UTC)` calls with
`except (OverflowError, ValueError) as exc: raise MvtTimeOutOfRangeError(...) from exc`, mirroring
the tuple `_require_seconds_precision_instant` already catches.

Rejected alternative: return the raw `text_value` (or `None`) as a sentinel. That would push a
non-canonical string into `cache_key` (`mvt.py::cache_key`) and into the cache-row identity
comparison (`mvt.py::_read_cache`, the `valid_time` clause), silently minting a cache
identity for an instant that has no canonical spelling — a worse failure than the 500 it replaces,
and invisible.

`ValueError` is the base class so any caller that already treats bad time text as a `ValueError`
keeps working; the subclass exists so the route layer can catch precisely.

### D2 — Route-boundary translation, not helper-level HTTP knowledge

`services/tiles/` must not import `apps.api.errors.ApiError` (layering). So the helper raises a
domain error and `apps/api/routes/hydro_display.py` owns the 422 translation. The translation point
is the extracted `_require_representable_instant(value, field_name)`, which is exactly the
`try/except → ApiError(422, VALIDATION_ERROR, "Tile time instants must be representable in UTC.")`
block that already exists inside `_require_seconds_precision_instant`.

`_require_seconds_precision_instant` keeps its sub-second check and then delegates the range check to
the extracted helper. This removes the sibling copy the issue explicitly warned about ("兄弟副本漂移"),
so the routes cannot drift on the error body.

**The refactor's blast radius is five public routes plus one discovery route, not three.**
`_require_seconds_precision_instant` is also imported by `apps/api/routes/precip.py` and called by
`precip_index` (`cycle`) and `precip_png` (`cycle` and `valid_time`), and it is reached from
`hydro_display.py::list_layer_valid_times` via `_validated_national_valid_time_selector`. Task 2.2 is
therefore a behavior-preserving extraction under those callers, and they carry regression rows
(Invariant Matrix) rather than being assumed unaffected.

### D3 — Legacy routes get the range guard only, never the sub-second guard

`hydro_mvt_tile` and `hydro_national_mvt_tile` today accept an in-range sub-second instant and
canonicalize it to `...:00.500000Z`. Reusing `_require_seconds_precision_instant` on them would turn
that into a 422 — a behavior break outside this issue's scope. They call
`_require_representable_instant` only.

The guard is placed immediately after `validate_xyz(z, x, y)`, which is before
`national_discharge_source_version(...)` on the national route and before `_require_display_ready`
on the single-run route. That satisfies acceptance criterion 3 (`sql=0`) on both routes.

### D4 — The original `valid_time` object stays the value passed downstream

`_require_representable_instant` returns a normalized UTC `datetime`, but the legacy routes discard
the return and keep passing the **original** `valid_time` into `_format_time`, the SQL binds, and
`_fetch_*_tile_bytes`. The diff is then purely additive on the legacy path, so the "zero shift for
in-range instants" claim is checkable by inspection: no existing expression changes. `_format_time`
canonicalizes to UTC anyway, and psycopg binds the same absolute instant regardless of tzinfo.

### D4b — `precip.py::_instant` is safe by call order, and that reasoning is load-bearing

`precip.py::_instant` normalizes with its own `.astimezone(UTC)` and then calls `canonical_mvt_time`.
That first line is itself unguarded, but every value reaching `_instant` on a request path has already
passed `_require_seconds_precision_instant` (`precip_index`, `precip_png`) or is derived from such a
value (`horizon_valid_times(cycle_instant)`, `PrecipWindowIncomplete.window_end`), so it is in-range by
construction. This change does not touch `precip.py`; the ordering invariant is what keeps it correct,
so it is recorded here and pinned by a regression row instead of being left implicit.

### D4c — `_require_seconds_precision_instant`'s return value is a contract, not an implementation detail

It returns `value.astimezone(UTC) if value.tzinfo is not None else value.replace(tzinfo=UTC)` — the
naive branch is load-bearing because the newly guarded legacy routes take a lax `valid_time: datetime`,
so collapsing the ternary to `value.astimezone(UTC)` would reinterpret naive instants in server-local
time. `precip.py::_require_whole_hour_instant` consumes that return
value and reads `.minute`/`.second`. `_RFC3339_INSTANT_RE` accepts half-hour offsets, so an extraction
that returned the caller's original object would let `2026-09-02T20:00:00+05:30` (14:30 UTC) pass the
whole-hour gate and be floored to hour 14 by `cycle_token` — the cache poisoning that gate exists to
prevent, and one no error-path test would catch. Task 3.11 pins it.

### D5 — Deliberate asymmetry: user instants 422, server-sourced values still 5xx

`_format_time` is also fed server-sourced values (`created_at`, `cycle_time`, `updated_at`, and cache
rows via `mvt.py::_read_cache`). Those do **not** get translated to 422: a DB row whose stored instant
cannot be normalized to UTC is genuinely bad server data, and answering 422 would blame the client
for a server anomaly. After this change such a value raises `MvtTimeOutOfRangeError` — a typed,
greppable 500 instead of a bare `OverflowError`. Acceptance criterion 1 is therefore satisfied as
"a bare `OverflowError` never escapes the helper", which is how the spec delta words it.

### D6 — Public-URL verification is deferred, with the reason recorded

Criterion 6's second half ("在 `test.nwm.ac.cn` 上实测越界 URL 返回 422") **cannot** be produced by this
PR. node-27's production checkout `/home/nwm/NWM` — the tree `nhms-display-api.service` serves — is
parked on `hotfix/node27-rollback-pre-2073` (`5a86841c`), 165+ commits behind master, and restoring it
is an ops decision already tracked by **#2145** (blocking migration) and **#2162** (blocked deployment).
This PR produces instead: (a) the pre-fix public baseline above (422/500/500, measured), and (b) an
isolated-worktree live receipt on node-27 at the PR head against the live RO role, which is the same
method PR #2164 used for #2030. The public-URL re-measure is routed to #2162's maintenance window.

## Invariant Matrix

```text
Governing invariant: a user-supplied tile instant NEVER yields 5xx — it is either canonicalized
byte-identically to today, or rejected 422 VALIDATION_ERROR before any SQL — and
`canonical_mvt_time` never lets a bare `OverflowError` escape at any call site.
Source-of-truth identity/contract: `services/tiles/mvt.py::canonical_mvt_time` is the single
canonical spelling of a tile instant; `cache_key` and `_read_cache` derive cache identity
from it.
Surfaces:
- Producers: services/tiles/mvt.py::canonical_mvt_time (both branches), ::_format_time (services side)
- Validators/preflight: apps/api/routes/hydro_display.py::_require_representable_instant (new),
  ::_require_seconds_precision_instant, ::_validated_national_valid_time_selector
- Storage/cache/query: mvt.py::cache_key, ::_read_cache (valid_time clause), ::_write_cache
  (`map.tile_cache` valid_time value), ::_ensure_tile_layer (`map.tile_layer` valid_time value)
- Public routes/entrypoints: hydro_display.py::hydro_mvt_tile, ::hydro_national_mvt_tile,
  ::hydro_national_source_cycle_mvt_tile, ::river_network_national_mvt_tile, ::river_network_mvt_tile,
  ::met_station_mvt_tile, ::list_layer_valid_times (`/api/v1/layers/{layer_id}/valid-times`, reaches the
  refactored validator through ::_validated_national_valid_time_selector);
  precip.py::precip_index and ::precip_png (both call the refactored
  ::_require_seconds_precision_instant), plus precip.py::_instant (a seventh `canonical_mvt_time`
  consumer, in-range by call order — design D4b)
- Frontend/downstream consumers: none — success-path bytes, ETag, headers and cache key are unchanged
  on every tile, precip and valid-times route, so no frontend or generated API type moves
- Failure paths/rollback/stale state: hydro_display.py::_require_hydro_mvt_source_identity 404 detail
  body (calls `_format_time` on the user instant); cache-row mismatch path in mvt.py::_read_cache
- Evidence/audit/readiness: hydro_display.py `_format_time` on server-sourced created_at / cycle_time /
  updated_at (D5: typed 500, not 422)
Regression rows:
- legacy hydro-national + `9999-12-31T23:59:59-08:00` -> 422 VALIDATION_ERROR, session.execute count 0
- legacy hydro/{run_id} + `0001-01-01T00:00:00+08:00` -> 422 VALIDATION_ERROR, session.execute count 0
- new {source}/{cycle} route + same instants -> 422 unchanged (already guarded, must not regress)
- any route + in-range `...T12:00:00Z` / `+00:00` / `.000Z` / `...T20:00:00+08:00` -> identical
  cache_key, identical SQL bind, identical bytes/ETag
- legacy routes + in-range sub-second `...T12:00:00.500Z` -> still accepted (NOT 422), canonicalizes to
  `...T12:00:00.500000Z` exactly as today
- legacy routes + NAIVE extreme `9999-12-31T23:59:59` / `0001-01-01T00:00:00` -> NOT 422, reaches SQL,
  identical to today (naive is read as UTC and cannot overflow)
- new route + in-range sub-second -> still 422 (unchanged)
- canonical_mvt_time string branch + `'9999-12-31T23:59:59-08:00'` -> MvtTimeOutOfRangeError, not OverflowError
- canonical_mvt_time + unparseable text (`'not-an-instant'`) -> returns the text unchanged, as today
- three unchanged sibling layers (river-network-national, river-network, met-stations) -> valid_time=None
  path untouched, tiles byte-identical
- `/api/v1/layers/{layer_id}/valid-times?source=&cycle=` + out-of-range `cycle` -> still 422 (unchanged);
  same route + in-range `cycle` -> identical `cycle_key` and identical response body
- precip.py::precip_index and ::precip_png + out-of-range `cycle`/`valid_time` -> still 422
  VALIDATION_ERROR with the same error body as before the extraction
- precip.py::precip_index and ::precip_png + in-range whole-hour instants -> response body, `cache_key`
  and rendered PNG bytes unchanged
```

## Boundary-surface checklist

- **Shared helper roots**: `canonical_mvt_time` (5 layers, cache key, cache-row comparison) — changed,
  additive failure branch only.
- **Public entrypoints**: 6 tile routes (2 changed additively, 4 proven untouched) + `list_layer_valid_times`
  + 2 precip routes reached through the refactored validator, all proven behavior-identical.
- **Read surfaces**: tile SQL binds and `_fetch_*_tile_bytes` — unchanged.
- **Write/delete/overwrite surfaces**: `tile_cache` writes — the written `valid_time` value is produced
  by the same unchanged expression; must be proven identical for in-range instants.
- **Producer/consumer evidence boundaries**: none (no manifest/QC artifact).
- **Stale-state/idempotency boundaries**: `_read_cache` — a cache row written before this change
  must still match after it for the same in-range request (no cache invalidation, no version bump).
- **Unchanged downstream consumers**: frontend MapLibre source keys and generated API types — unchanged
  because no OpenAPI response shape or success-path value moves; `precip.py` and the precip overlay
  frontend consumer are unchanged files whose behavior is nonetheless pinned by regression rows.
