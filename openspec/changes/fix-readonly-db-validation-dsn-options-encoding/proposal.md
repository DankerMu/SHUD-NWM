# Stop form-encoding the readonly validation DSN's libpq options

## Why

`_bounded_database_url` (`services/production_closure/readonly_db_route_smoke.py:320-348`) rebuilds the
display `DATABASE_URL` with a bounded `options` value, and builds the query string with
`urlencode(query_items)` (`:345`). `urlencode` defaults to `quote_via=quote_plus`, the
`application/x-www-form-urlencoded` convention, so the spaces in `_validation_pgoptions()` (`:421-426`,
`-c statement_timeout=… -c lock_timeout=… -c idle_in_transaction_session_timeout=…`) become `+`.

libpq only percent-decodes; it does not treat `+` as a space. The server therefore receives
`-c+statement_timeout=10000+-c+…`, reads the GUC name as the literal `+statement_timeout`, and refuses the
connection outright:

```text
FATAL:  unrecognized configuration parameter "+statement_timeout"
```

Measured on node-27 at 2026-09-16T01:51Z with the canonical entrypoint
`scripts/validate_readonly_db_boundary.py` as `nhms_display_ro`: `stations` and `latest_product` FAIL with
that exact text, `models` returns HTTP 500 `MODEL_REGISTRY_ERROR` / `OperationalError` from the same cause,
and the lane's overall status is FAIL.

Two things hid this for two release cycles:

1. `_display_validation_env()` (`:361-365`) also exports a correctly-spaced `PGOPTIONS`, but a connection-string
   parameter outranks the environment variable in libpq, so the broken URL masked its own safety net.
2. SQLAlchemy 2.0.49's `make_url` decodes query values with `unquote_plus`, turning `+` back into a space, so
   every `create_engine(DATABASE_URL)` route (`apps/api/routes/pipeline.py:129`,
   `apps/api/routes/hydro_display.py:210`) returns 200. Only the stores that hand the URL straight to libpq
   break — `packages/common/met_store.py:328,357` (stations), `packages/common/model_registry.py:4025`
   (models), `packages/common/forecast_store.py:2646` (latest-product) — which is exactly the split the live
   run shows.

Pre-existing since 32f2e2905 (#234), relocated by a0b6ccf9d (#1895); PR #2411's first real live run exposed it.

This blocks bring-up checklist C2's readonly deny-write receipt, which is required evidence for #1987 task 5.2,
which gates #1988.

```text
Issue type: bugfix
Fixture level: compact
Upstream suggested level: compact (one encoding call, no new surface, no schema, no operator procedure change)
Blast radius: the readonly boundary lane cannot connect at all on the psycopg2-direct routes, so C2 can never
  reach PASS and the entire narrow-store rollout evidence chain stalls
Selected risk packs: Auth / permissions / secrets; Error handling / rollback / partial outputs; Legacy
  compatibility / examples
Evidence floor: the rebuilt URL's `options` value percent-decodes (without `+`-to-space) to
  `_validation_pgoptions()` verbatim; a real PostgreSQL connection opened with that URL succeeds and reports
  the three timeouts as configured; the node-27 lane no longer reports `unrecognized configuration parameter`
```

## What Changes

- `_bounded_database_url` encodes the query string with `quote_via=quote`, so a space becomes `%20` rather
  than `+`. `quote` is already imported and used in the same module (`:309 _url_value`), so no new dependency.
- A unit test that can actually go red: decode the emitted `options` value with percent-decoding only and
  compare it verbatim to `_validation_pgoptions()`.
- A real-PostgreSQL integration test that opens a connection with the rebuilt URL and reads back
  `statement_timeout`, `lock_timeout` and `idle_in_transaction_session_timeout`.

## Out of Scope

- The `jobs` / `pipeline_status` / `pipeline_stages` identity findings and the `job_logs` BLOCKED entry from
  the same live run. Those are a data condition — `ops.pipeline_job` has no producer in the deployed topology
  — tracked in #2420, and unrelated to encoding.
- The sibling `urlencode` calls at `readonly_db_route_smoke.py:162` (`_query_path`) and
  `packages/common/node27_pgdata_workload_query.py:167` (`forecast_series_query`). Both build HTTP query
  strings, where `+` is a valid space encoding that Starlette/httpx decode correctly. Not the same defect;
  do not touch them.
- The asymmetry that `parse_qsl` at `:324` decodes an incoming `+` as a space while libpq would keep it
  literal. It is real, and this change does shift it: a retained incoming query value containing a literal `+`
  used to be decoded to a space and re-encoded to `+` (two errors cancelling, so libpq still read a literal
  `+`), and will now be re-emitted as `%20`, which libpq reads as a space. The shape is unreachable for the
  DSNs this lane actually consumes — `infra/env/display.env` keeps the password in the netloc and the
  remaining parameters are `sslmode`/`application_name`-class values with no literal `+` — so this change
  does not address it.
- Swapping the URL for a `key=value` libpq DSN: the value is consumed both by SQLAlchemy `create_engine` and
  by `psycopg2.connect(url)`, so it must stay a URL.
- The permission-probe matrix (`readonly_db_probe_adapter.py:761-764`), which passes the same
  `_validation_pgoptions()` through psycopg2's `options` connect keyword and never touches a URL query. It
  already reports 23/23 denials PASS.
