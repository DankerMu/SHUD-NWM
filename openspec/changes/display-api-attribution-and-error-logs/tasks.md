## Risk Triage

```text
Issue type: bugfix (attribution regression) + guard hardening + observability
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium (display unit process; four connect seams behind six store/facade classes; error chokepoint)
Fixture level: expanded
Repair intensity: high
Upstream suggested level: absent (hand-written issues; expanded forced by shared-helper + security/redaction + production-config triggers)
Why:
- optional kwarg on the four packages/common connect seams and their store/facade owners (shared-helper trigger)
- logging of client-influenced `details` (secrets/redaction trigger)
- production log geometry + static guards that gate future changes
OpenSpec change: display-api-attribution-and-error-logs
Evidence floor:
- uv run ruff check .
- uv run pytest -q tests/test_node27_connection_attribution.py tests/test_node27_connection_attribution_delegated.py (baseline 91 collected) tests/test_api_errors_logging.py (new, #1704) + each touched store's suite
- openspec validate display-api-attribution-and-error-logs --strict --no-interactive
- node-27: pre-merge scratch-port uvicorn smoke (log line with request id); post-merge pg_stat_activity receipt after restart + traffic to every business router, and grep of a synthetic ApiError's X-Request-ID in /tmp/display-api.log
```

## Risk Packs

| Pack | 选择 | 理由 |
|---|---|---|
| Public API / CLI / script entry | selected | seven business routers + runtime + conditional slurm; error responses for every API client |
| Config / project setup | selected | logging handler install in `main.py`; unit log geometry relied upon |
| File IO / path safety / overwrite | not selected | no file writes beyond the existing stderr stream |
| Schema / columns / units / field names | not selected | no response shape or DB change |
| Auth / permissions / secrets | selected | `details` redaction before logging incl. key-level `rejected_value`; residual: non-path free-text values under other keys log verbatim (recorded); DSN credentials never logged; `nhms_display_ro` boundary untouched |
| Concurrency / shared state / ordering | selected | `@lru_cache` engine in `pipeline.py`; two uvicorn workers each importing `main.py` |
| Resource limits / large input / discovery | selected | log line size for large `details` (validation errors) bounded by an explicit 8 KiB render budget with a truncation marker (the redaction walk still runs over the full payload before the cut — measured ≈554 ms for a 20 000-item authorised validation body, recorded residual); `path=` bounded by the same 8 KiB budget + percent-encoded marker (line worst case ≈16.6 KB, measured 122 936 B before the bound from one 40 KiB `%FF` request); guard import-walk must terminate (visited set) |
| Legacy compatibility / examples | selected | stores' default `application_name=None` keeps every existing caller byte-compatible |
| Error handling / rollback / partial outputs | selected | logging failure must not alter the response |
| Release / packaging / dependency compatibility | not selected | no new dependency |
| Documentation / migration notes | selected | two runbooks carry acceptance items |
| 已发布 NHMS 制品 / display 身份 | selected | `hydro_display.py` digest basis and name must stay unchanged |
| 其余 domain packs | not selected | no geometry/forcing/SHUD/Slurm/provider/manifest/Timescale surface |

## Tasks

- [x] T1 (#1728) Optional `application_name` kwarg on every hop of the four seams in D1: `forecast_store._PsycopgTransaction` ← `PsycopgForecastStore` / `PsycopgStationLookup`; `model_registry._PsycopgTransaction` ← `PsycopgModelRegistryStore` (constructor + `from_env` + `_transaction()`); `PsycopgBestAvailableRepository` ← `BestAvailableManager.from_env`; `PsycopgStateSnapshotRepository` ← `StateManager.from_env`; forwarded as `fallback_application_name` at each connect site; unit tests per seam (kwarg present / absent / DSN override wins).
- [x] T2 (#1728) Route modules pass their `_APPLICATION_NAME`; `pipeline.py` engine `connect_args`; `hydro_display.py` untouched (assert byte-identical constant).
- [x] T3 (#1728) Unit-level guard rooted at `route_registry.py` (`_BUSINESS_ROUTERS` + runtime + conditional slurm), with the pinned `UNREACHABLE` rows (`grid_registry_store` at baseline; `met_store`, `chain_compat_runtime`, `chain_repository`, `tile_publisher/publisher`, `forcing_producer/store` after the round-1 ancestor-package fix — six rows plus the factory row, each with a file:line reason); three control experiments recorded in the PR body (bare connect in a router -> red; dropped forwarding -> red; removing the `met_store` row -> red).
- [x] T4 (#1726) Function-level delegated closure guard in `tests/test_node27_connection_attribution_delegated.py`; red-proof (second connect function in `display_watermark.py` + reachable call -> red naming the function; revert -> green, 91-test baseline intact); existing `Unclassified: [...]` path still red for a brand-new module — re-proven after the round-1 discovery change with a temporary connect-owning module imported from a registered component (output in the PR body).
- [x] T5 (#1704) New `tests/test_api_errors_logging.py`: `error_response()` logging through `_redact_error_details` (key-level `rejected_value` / `rejected_values`, list- and mapping-aware, then `redact_audit_payload`; rendered `details` truncated to an 8 KiB byte budget with a `…[truncated N bytes]` marker; `X-Request-ID` accepted only when it matches `[A-Za-z0-9._-]{1,64}`, else minted, at both the middleware and the pre-body path; `path=` percent-encoded so a path segment cannot plant fake tokens, control bytes or a NUL into the line); `caplog.text` tests asserting rendered text for both arms incl. `rejected_value: sk-live-…` -> `[redacted]` and `parse_reason` verbatim; unexpected `details` type does not raise; level mapping 5xx/4xx.
- [x] T6 (#1704) `main.py` idempotent stderr handler install on `apps.api` with propagation left on; tests: importing twice yields one handler; formatter includes timestamp and level; `models.py` logger records render once through it.
- [x] T7 Docs: `current-production-ops.md` name table (:3694) + cancel clauses (:3730, :3751); `object-store-forcing-series-read.md` post-hoc grep + redaction trade-off + slurm blind spot.
- [ ] T8 node-27 (queued session): pre-merge — from a detached worktree start uvicorn on a scratch port with `display.env` (no `--log-config`, same shape as the unit), issue one request that yields an `ApiError` (5xx arm → `ERROR`) and one that yields a `RequestValidationError` (4xx arm → `WARNING`), and paste both stderr lines showing the same `request_id` as the response `X-Request-ID` header; post-merge — restart `nhms-display-api.service`, touch every business router's GET face, `pg_stat_activity` shows zero empty `application_name` for `nhms_display_ro`, and one synthetic `ApiError(status_code=500, code='STATION_FORCING_FILE_MALFORMED', details={'station_id': …, 'expected_path': <abs path>, 'parse_reason': 'concurrent-replace: …'})` raised through `error_response()` on the running unit is found in `/tmp/display-api.log` by BOTH `grep -F <X-Request-ID>` and `grep 'concurrent-replace:'` hitting the same line, with `expected_path` rendered `[redacted]` (systemd `append:` hop verified; this carries #1704 AC5's grep/co-location assertions with a synthetic `ApiError` trigger — the concurrent-replace race itself is not reproduced, as the issue anticipates; recorded in the PR 偏离记录).

## Non-goals (explicit)

No new `packages/common` module; no change to the eight #1714 components; no slurm handler logging; no log rotation.
