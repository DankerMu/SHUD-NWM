# Bind the recovery-target six-field contract to one source (#1242)

## Why

The recovery target is a six-field contract (hypertable pair + chunk
pair + time-window pair). PR #1241 (#1087) single-sourced only the
hypertable pair; the chunk/range four fields still live in mutually
independent copies: the supervisor expected decompress argv
(scripts/node27_timeseries_compression_supervisor.py:376-390, bare
literals at :384/:386/:388/:390), capture's `RECOVERY_TARGET` dict
(scripts/node27_timeseries_compression_capture.py:63-70, all six
inlined), capture's recovery-preflight SQL (:418-425), the schema
`$defs.recovery_target` consts
(schemas/timeseries_compression_live_evidence.schema.json:374-379), the
synthetic `decompress_return_relation` const (:103), the supervisor
test fixture (tests/test_node27_timeseries_compression_supervisor.py
:143-163), **the verifier's own acceptance oracle**
(scripts/node27_timeseries_compression_live_evidence.py:201-209
`RECOVERY_TARGET` + `RECOVERY_RETURN_RELATION`, enforced at :1840 and
:1871 — fixture review found this copy; the issue undercounted), and
the plan author's `_RECOVERY_TARGET_ARGS`
(scripts/node27_timeseries_compression_plan_author.py:69-76). The
#1241 drift guard (:2821-2827) asserts only 2 of 6 fields. Nothing
cross-checks that the chunk belongs to the hypertable the guarded pair
names, so the next retarget moves half the contract and strands the
chunk/range half — worst case the capture plane emits the new target
while the verifier still judges with the old dict, dying late with
`EvidenceError` on the armed node-27 replay lane.

## What Changes

Issue's recommended route — complete the single source and bind the
targeted copy set to it, each copy either derived from the source or
asserted equal to it (values unchanged everywhere; the two copies that
remain outside the bound set are enumerated below and tracked):

1. `packages/common/node27_container_contract.py`: alongside the
   existing `RECOVERY_TARGET_SCHEMA`/`RECOVERY_TARGET_TABLE`, add
   `RECOVERY_TARGET_CHUNK_SCHEMA = "_timescaledb_internal"`,
   `RECOVERY_TARGET_CHUNK_NAME = "_hyper_3_7_chunk"`,
   `RECOVERY_TARGET_RANGE_START = "2026-05-28T00:00:00Z"`,
   `RECOVERY_TARGET_RANGE_END = "2026-06-04T00:00:00Z"`, and a derived
   `RECOVERY_TARGET_FIELDS` mapping (the six-field dict, single
   assembly point).
2. Supervisor `target_args`: the four bare literals become the shared
   constants (argv content string-identical).
3. Capture: import the contract module (scripts→packages direction,
   same as its existing `packages.common.evidence_io` import);
   `RECOVERY_TARGET = dict(RECOVERY_TARGET_FIELDS)`; the recovery
   preflight SQL interpolates the same constants, generated string
   byte-identical for the pinned values (frozen-string test, including
   the `/* capture:recovery_preflight */` prefix the test psql stub
   dispatches on).
4. Tests: widen the #1241 drift guard to all six schema consts +
   `decompress_return_relation` == `chunk_schema + "." + chunk_name`,
   AND bind the verifier's oracle copy in the same guard
   (`evidence.RECOVERY_TARGET == dict(contract.RECOVERY_TARGET_FIELDS)`,
   `evidence.RECOVERY_RETURN_RELATION` == the same synthetic relation —
   live_evidence.py itself stays untouched; the guard closes it);
   capture key-set/shape case; preflight-SQL byte freeze; rebuild the
   supervisor decompress fixture's chunk/range values from the shared
   constants.

Known remaining copies, deliberately outside this diff:
plan_author's `_RECOVERY_TARGET_ARGS` is transitively guarded by
`test_plan_author_emits_a_plan_the_real_supervisor_gate_accepts`
(tests/test_node27_timeseries_compression_capture.py:282), whose argv
flows through the supervisor's `_assert_exact_argv` — a contract-side
flip turns that test red. Two copies remain outside the contract-bound
set (cross-review finding, claims narrowed accordingly, tracked in
follow-up #1244), with different exposure — final review measured both
by isolated single-side flips: capture's `_capture_catalog_post` SQL
literals (scripts/node27_timeseries_compression_capture.py:449-452)
are the truly silent copy — a one-sided mutation ships green through
every suite and only dies on the armed replay lane; the verifier's
inline expected decompress argv
(scripts/node27_timeseries_compression_live_evidence.py:666-677) is
not contract-derived and not in the drift guard, but a one-sided
mutation IS caught (125 failures) by the live_evidence bundle
fixtures — gate-test coverage of the kind the spec delta permits,
just not single-sourced. Binding them needs the byte-freeze pattern
extended to catalog_post and a decision on deriving the verifier argv
tail.

The schema JSON is data and stays untouched — the guard binds Python
constants to it, so mutating either side alone fails the guard (the
issue's negative-verification acceptance).

## Non-goals

- Changing any recovery-target value; the six values are frozen as-is.
- Touching `validated_probe_target`, the G14 probe, or anything #1241
  delivered.
- Editing scripts/node27_timeseries_compression_live_evidence.py or
  scripts/node27_timeseries_compression_plan_author.py (both closed by
  guards instead).
- Relaxing schema consts to enums or introducing runtime target
  selection (future retarget decision).
- #1240 (INVOCATION_ARGV island) and #1090 (RECORD/EXEC docker argv
  split) — different surfaces.
- `packages/` importing `scripts/` (direction stays scripts→packages).
