# Tasks: harden-g14-write-probe-target

Fixture level: compact · Repair intensity: light · Issue #1087

Triage note: S+ single-concern hardening across three files (shared
contract module + two script call sites) + tests; fully locally
verifiable. Risk axes: (1) zero live behavior change — under route A
both generated default-target SQLs must be BYTE-EQUAL to the current
literals (supervisor :1177-1185 compact single-line, benchmark :54-69
multi-line); this is a convenience oracle for "nothing changed", NOT a
replay-identity constraint (verified: `_run_capture_argv` supervisor
:949-971 returns stdout only, activity capture is not in
`EXPECTED_CAPTURE_SEQUENCE` :110-118, benchmark `ACTIVITY_SQL` is
consumed only by `cursor.execute` at :305 and never hashed into a
receipt); (2) fail-closed only — a bad target must raise in
`validated_probe_target` before any SQL exists; (3) drift guards must
cover ALL copies: whitelist vs `HYPERTABLE_KEYS` in live_evidence:59
AND capture.py:55, and `RECOVERY_TARGET_SCHEMA`/`TABLE` vs the schema
JSON consts (:375-376); (4) G14 judgment semantics untouched (conflict
predicate, COALESCE fail-closed, CLIENT_BACKEND_TYPE intersection);
(5) ruff line-length is 120 (pyproject.toml:67) — the 125-char probe
line and frozen expected strings in tests need explicit folded
concatenation that still reproduces the exact bytes. Single review
round.

Must preserve:
- Generated supervisor `activity_sql` and benchmark `ACTIVITY_SQL`
  byte-identical to today's strings for the default target.
- Expected decompress argv content (supervisor :365-395) — same strings,
  now derived from the shared constants.
- `catalog_sql` (supervisor :1190-1203) and the
  `"hydro.river_timeseries"` catalog key at :1599 untouched.
- live_evidence.py, capture.py, and all `schemas/**` untouched.
- Existing test baselines green unmodified (no existing test pins the
  SQL literals — fakes match on the `pg_stat_activity` substring):
  supervisor 120, benchmark 20, live_evidence 270 (collect-only counts).

## Implementation tasks

- [x] 1. Add to `packages/common/node27_container_contract.py`:
  `RECOVERY_TARGET_SCHEMA`/`RECOVERY_TARGET_TABLE`/`RECOVERY_TARGET`,
  `SUPERVISED_HYPERTABLES`, and `validated_probe_target(target)`
  (whitelist membership + `^[a-z_][a-z0-9_]*\.[a-z_][a-z0-9_]*$`,
  `ValueError` on failure, returns the validated target string).
  Update the module docstring (it claims "three measured node-27 host
  contracts" — the new pinned-target block is a repo-side contract, not
  a measured host value; say so).
- [x] 2. Supervisor: extract the `activity_sql` literal into a builder
  (module-level function, `target` param defaulting to
  `RECOVERY_TARGET`) that calls `validated_probe_target` and
  interpolates into the existing compact format; derive the decompress
  expected argv schema/table from the shared constants; no other line
  changes.
- [x] 3. Benchmark: `ACTIVITY_SQL = _build_activity_sql(RECOVERY_TARGET)`
  with the builder validating + interpolating into the existing
  multi-line format; no other line changes.
- [x] 4. Tests: (a) byte-equality of BOTH generated SQLs vs the frozen
  pre-change strings for the default target; (b) switch-target case:
  builder(`met.forcing_station_timeseries`) probes that table and does
  not mention `hydro.river_timeseries`; (c) `ValueError` for
  `public.evil` (not whitelisted) and `hydro.river; DROP` (malformed);
  (d) drift guards: `SUPERVISED_HYPERTABLES == HYPERTABLE_KEYS` for
  BOTH live_evidence and capture modules (read-only imports, both
  already importable from tests), and `RECOVERY_TARGET_SCHEMA`/`TABLE`
  equal the `recovery_target` `hypertable_schema`/`hypertable_name`
  consts loaded from
  schemas/timeseries_compression_live_evidence.schema.json.
- [x] 5. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_supervisor.py
  tests/test_node27_timeseries_compression_benchmark.py
  tests/test_node27_timeseries_compression_live_evidence.py` → all green
  (120/20/270 baselines + new cases);
  `grep -rn "has_table_privilege(usename" scripts/` → 0 hits (currently
  2: benchmark:59, supervisor:1180 — this form catches both spacing
  variants; the target literal lives only in the contract constant);
  `uv run ruff check .`;
  `openspec validate harden-g14-write-probe-target --strict
  --no-interactive`.

## Required evidence

- Byte-equality test output; before/after output of
  `grep -rn "has_table_privilege(usename" scripts/`; pytest counts for
  the three suites; ruff output.

## Non-goals

- catalog_sql / :1599 catalog key consolidation; schema changes;
  live_evidence/capture edits; per-hypertable write_privileges map; G14
  predicate changes.
