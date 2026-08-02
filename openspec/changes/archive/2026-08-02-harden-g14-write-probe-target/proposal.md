# Harden the G14 write-privilege probe against target drift (#1087)

## Why

The G14 checkpoint judges a session as a conflicting writer via
`COALESCE(has_table_privilege(usename,'hydro.river_timeseries',
'INSERT,UPDATE,DELETE'),false) AS has_write_privilege_on_target`
(scripts/node27_timeseries_compression_supervisor.py:1180-1181), and
scripts/node27_timeseries_compression_benchmark.py:58-62 carries a
sibling copy in `ACTIVITY_SQL` (different formatting, same target
literal). The column is named "on_target", but the target is a bare
literal with no tie to the actual recovery target — which in this fully
static architecture is pinned by the expected decompress argv
(supervisor `target_args` :372-387, schema at :376, table at :378).
Today the literals agree, so the probe is correct. The day the recovery
target is extended or switched to `met.forcing_station_timeseries`
(already in `HYPERTABLE_KEYS`), a concurrent writer on the new target is
silently judged `has_write_privilege_on_target=false` and the checkpoint
passes — the exact G6..G14 false-oracle class. Nothing forces the
literals to move together.

## What Changes

Single-source the target with a fail-closed validator; each script keeps
its own SQL formatting and stays byte-identical for the default target
(fixture-review route A — the issue kills target-literal drift, not
formatting duplication; there is no runtime `recovery_target` payload to
read, so the source of truth is a shared constant):

1. `packages/common/node27_container_contract.py` (already imported by
   both scripts) gains: `RECOVERY_TARGET_SCHEMA = "hydro"`,
   `RECOVERY_TARGET_TABLE = "river_timeseries"`, `RECOVERY_TARGET`
   (derived `schema.table`), `SUPERVISED_HYPERTABLES =
   ("hydro.river_timeseries", "met.forcing_station_timeseries")`, and
   `validated_probe_target(target)` — returns `target` unchanged iff it
   is in `SUPERVISED_HYPERTABLES` AND matches
   `^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$`, else raises `ValueError`
   before any SQL can be built. Module docstring updated (it currently
   claims to cover only measured host contracts).
2. Supervisor: `activity_sql` construction becomes a small builder
   taking `target` (default `RECOVERY_TARGET`), validating via
   `validated_probe_target` and interpolating into the EXISTING compact
   single-line format; the expected decompress argv derives
   `--hypertable-schema`/`--hypertable-name` from
   `RECOVERY_TARGET_SCHEMA`/`RECOVERY_TARGET_TABLE` — probe and target
   pin share one source.
3. Benchmark: `ACTIVITY_SQL = _build_activity_sql(RECOVERY_TARGET)`
   with the builder interpolating into the EXISTING multi-line format.
4. Tests (contract + supervisor + benchmark): byte-equality of both
   generated SQLs against the current literals for the default target
   (zero-behavior-change oracle, free on both sides under route A);
   switch-target case (`met.forcing_station_timeseries` probed, default
   absent); fail-closed cases (non-whitelisted `public.evil`, malformed
   `hydro.river; DROP` → `ValueError`); drift guards:
   `SUPERVISED_HYPERTABLES` equals `HYPERTABLE_KEYS` in BOTH
   scripts/node27_timeseries_compression_live_evidence.py:59 and
   scripts/node27_timeseries_compression_capture.py:55 (today those two
   copies have no guard at all), and `RECOVERY_TARGET_SCHEMA`/`TABLE`
   equal the schema consts at
   schemas/timeseries_compression_live_evidence.schema.json
   `recovery_target.hypertable_schema`/`hypertable_name` (:375-376,
   read-only JSON load — closes the new constant/schema drift pair
   without touching the schema).

No schema change: no runtime target selection is introduced, so
`recovery_target` consts (incl. `chunk_name` `_hyper_3_7_chunk` :378)
stay — they remain honest and are the second barrier.

## Non-goals

- Weakening the write-privilege check or altering G14's three-part
  judgment (external client backend AND writer AND COALESCE fail-closed).
- Touching `catalog_sql` (supervisor :1190-1203, literals at
  :1194/:1198/:1202) or the `"hydro.river_timeseries"` key in
  `validate_current_d3`'s expected catalog (:1599) — catalog scope is a
  different oracle (issue Out-of-scope); do NOT "fix" them in passing.
- Editing scripts/node27_timeseries_compression_live_evidence.py,
  scripts/node27_timeseries_compression_capture.py, or any schema file.
- Per-hypertable `write_privileges` forensic map (issue marks it
  conditional; full-fidelity capture already retains every session's
  backend_type/usename unchanged).
