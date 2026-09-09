## ADDED Requirements

### Requirement: A user-supplied tile instant never produces a 5xx

`services/tiles/mvt.py::canonical_mvt_time` SHALL NOT let a bare `OverflowError` escape from either
of its UTC-normalization branches (the `datetime` branch and the parsed-string branch): a value whose
normalization to UTC leaves `datetime`'s representable range SHALL raise the typed
`MvtTimeOutOfRangeError` (a `ValueError` subclass) instead.

Every MVT tile route that accepts a path-parameter instant — `hydro_mvt_tile`,
`hydro_national_mvt_tile`, and `hydro_national_source_cycle_mvt_tile` — SHALL reject such an instant
with HTTP **422** and code `VALIDATION_ERROR` **before issuing any SQL statement**, using one shared
validator so the routes cannot drift on the error body. That shared validator is also reached by
`/api/v1/layers/{layer_id}/valid-times` and by the two precip overlay routes, whose status, code,
message and details for every already-rejected instant SHALL be unchanged. The rejection is a range
check only:
the legacy routes SHALL keep accepting an in-range sub-second instant exactly as before, and the
`{source}/{cycle}` route SHALL keep rejecting sub-second instants with its existing 422.

Canonicalization of in-range instants SHALL be unchanged: the same canonical string, the same
`cache_key`, the same `_read_cache` identity comparison, the same SQL binds, the same tile bytes,
ETag and cache headers, and no `*_QUERY_VERSION` bump — for all five tile layers.

Server-sourced values reaching `canonical_mvt_time` (`created_at`, `cycle_time`, `updated_at`, cached
`valid_time` rows) are deliberately NOT translated to 422: an unnormalizable stored instant is a
server-data defect and SHALL surface as the typed error, not as a client-fault status.

#### Scenario: Out-of-range instant on the legacy national tile route
WHEN `GET /api/v1/tiles/hydro-national/q_down/9999-12-31T23:59:59-08:00/6/50/25.pbf` is requested
THEN the response is HTTP 422 with code `VALIDATION_ERROR` AND zero SQL statements were executed on
the request session

#### Scenario: Out-of-range instant on the legacy single-run tile route
WHEN `GET /api/v1/tiles/hydro/{run_id}/q_down/0001-01-01T00:00:00+08:00/6/50/25.pbf` is requested
THEN the response is HTTP 422 with code `VALIDATION_ERROR` AND zero SQL statements were executed on
the request session — in particular `_require_display_ready` and `_require_hydro_mvt_source_identity`
are never reached

#### Scenario: In-range instants are unshifted
WHEN the same instant is requested as `...T12:00:00Z`, `...T12:00:00+00:00`, `...T12:00:00.000Z`, or
`...T20:00:00+08:00`
THEN every spelling yields the identical canonical `valid_time`, the identical `cache_key`, the
identical SQL bind, and identical tile bytes and ETag

#### Scenario: Legacy sub-second acceptance is preserved
WHEN a legacy tile route is requested with an in-range sub-second instant such as `...T12:00:00.500Z`
THEN the route does NOT return 422 and canonicalizes the instant to `...T12:00:00.500000Z`, exactly as
before this change

#### Scenario: Helper raises a typed error rather than OverflowError
WHEN `canonical_mvt_time` is called with `datetime.fromisoformat("9999-12-31T23:59:59-08:00")` or with
the equivalent string `"9999-12-31T23:59:59-08:00"`
THEN it raises `MvtTimeOutOfRangeError` and not a bare `OverflowError`

#### Scenario: Routes sharing the validator are behavior-identical
WHEN `/api/v1/layers/{layer_id}/valid-times`, `/api/v1/precip/{source}/{cycle}/index`, or the precip PNG
route is requested with an out-of-range instant
THEN each still returns its existing 422 `VALIDATION_ERROR` with an unchanged body, and each in-range
request returns an unchanged response body, cache key and rendered bytes

#### Scenario: Unparseable text is still passed through
WHEN `canonical_mvt_time` is called with text that is not an ISO instant at all
THEN it returns that text unchanged, as before this change
