# Tasks: bind-recovery-target-six-field-contract

Fixture level: compact · Repair intensity: light · Issue #1242

Triage note: S single-concern follow-up of #1087 (same failure class,
strictly narrower); fully locally verifiable, zero runtime behavior
change. Risk axes: (1) supervisor expected-argv CONTENT must be
string-identical after constant substitution (the argv is an equality
oracle for the armed replay lane); (2) capture preflight SQL must be
BYTE-EQUAL for the pinned values (it executes on node-27 during live
capture; byte-freeze is the zero-change oracle, same pattern as #1241)
— the frozen string INCLUDES the `/* capture:recovery_preflight */`
prefix as first token (the capture test psql stub dispatches on it,
tests/test_node27_timeseries_compression_capture.py:171, and the
matcher is prefix-sensitive per :150-151); (3) the widened drift guard
must be a real binding — the existing guard already `read_text()`s the
real schema JSON from the repo path (:2822-2824), so single-side
mutation fails; (4) capture's `RECOVERY_TARGET` must keep the exact
six-key SET (schema `additionalProperties: false` + verifier
`_require_exact_keys` live_evidence.py:1838; key ORDER is irrelevant —
emission uses `json.dumps(..., sort_keys=True)` capture.py:208);
(5) naming trap: `contract.RECOVERY_TARGET` is the STRING
`"hydro.river_timeseries"` while capture/evidence `RECOVERY_TARGET`
are six-field dicts — capture must import only
`RECOVERY_TARGET_FIELDS` (or the module as a namespace), never rebind
its dict name to the contract string. Single review round.

Must preserve:
- All six recovery-target values verbatim (hydro / river_timeseries /
  _timescaledb_internal / _hyper_3_7_chunk / 2026-05-28T00:00:00Z /
  2026-06-04T00:00:00Z).
- Supervisor `target_args` list content (:376-390) string-identical.
- Capture preflight SQL byte-identical
  (scripts/node27_timeseries_compression_capture.py:418-425) and the
  evidence `"target"` payload identical (:430).
- `schemas/**`, live_evidence.py, plan_author.py untouched (the latter
  two are closed by guards, not edits — see proposal).
- #1241's `validated_probe_target`/G14 code untouched.
- Existing baselines green: supervisor 127, capture 8, benchmark 24,
  live_evidence 270 (collect-only counts at master c91b4133).

## Implementation tasks

- [x] 1. `packages/common/node27_container_contract.py`: add the four
  chunk/range constants + derived `RECOVERY_TARGET_FIELDS` mapping
  (six fields, assembled from the individual constants; document it as
  the single source the supervisor argv, capture evidence, verifier
  oracle, and schema consts are guarded against).
- [x] 2. Supervisor: replace the four bare literals in `target_args`
  (:384/:386/:388/:390) with the shared constants; no other changes.
- [x] 3. Capture: import the contract module as a namespace (or
  `RECOVERY_TARGET_FIELDS` only — see naming trap above);
  `RECOVERY_TARGET = dict(RECOVERY_TARGET_FIELDS)`; build the recovery
  preflight SQL (:418-425) by interpolating the same constants
  (extract to a module-level constant or small builder so a test can
  freeze it, keeping the dispatch comment prefix first); no other
  changes.
- [x] 4. Tests: (a) widen
  `test_recovery_target_constants_match_the_live_evidence_schema_consts`
  to all six consts + `decompress_return_relation` ==
  `chunk_schema + "." + chunk_name`, AND bind the verifier oracle in
  the same guard: `evidence.RECOVERY_TARGET ==
  dict(contract.RECOVERY_TARGET_FIELDS)` and
  `evidence.RECOVERY_RETURN_RELATION` == the same synthetic relation
  (the guard file already imports `evidence` at :2817); (b) capture
  shape case: `capture.RECOVERY_TARGET` key SET equals the schema
  `recovery_target` required set (value equality vs contract is
  definitional once derived — assert it once for completeness but the
  binding content is the key set + the schema↔contract equality from
  (a)); (c) byte-equality freeze of the capture preflight SQL against
  the current literal (comment prefix included); (d) rebuild the
  supervisor decompress fixture argv (tests/...supervisor.py:143-163)
  chunk/range values from the shared constants — deliberately
  chunk/range only; the `hydro`/`river_timeseries` literals at
  :153/:155 stay as-is (issue scope is the four fields);
  (e) red proof (scratch mutations, restored, outputs recorded): flip
  `contract.RECOVERY_TARGET_CHUNK_NAME` → the widened schema drift
  guard goes red AND
  `test_plan_author_emits_a_plan_the_real_supervisor_gate_accepts`
  (tests/test_node27_timeseries_compression_capture.py:282) goes red
  via plan_author's `_RECOVERY_TARGET_ARGS` flowing through
  `_assert_exact_argv`; note: the rebuilt supervisor decompress
  fixture stays GREEN on a contract flip (it derives from the same
  source — its oracle role transfers to the schema guard; this is
  expected, do not fake a red there).
- [x] 5. Oracle: `uv run pytest -q
  tests/test_node27_timeseries_compression_supervisor.py
  tests/test_node27_timeseries_compression_capture.py
  tests/test_node27_timeseries_compression_live_evidence.py
  tests/test_node27_timeseries_compression_benchmark.py` → green
  (127/8/24/270 + new); `uv run ruff check .`;
  `git diff --stat` → contract + supervisor + capture + 2 test files
  (+ fixture tasks.md); `schemas/`, live_evidence.py, plan_author.py
  untouched; `openspec validate bind-recovery-target-six-field-contract
  --strict --no-interactive`.

## Required evidence

- Byte-equality outputs (preflight SQL incl. prefix, supervisor argv
  content); red-proof outputs per 4(e) (schema guard + plan_author
  gate); pytest counts; ruff.

## Non-goals

- Value changes; schema edits; live_evidence/plan_author edits; runtime
  target selection; #1240/#1090 surfaces; G14/probe code.
