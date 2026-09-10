## Context

`record_explicit_cycle_curve` captures the real `PsycopgForecastStore.forecast_series` call, selects its one primary forecast SQL statement, validates it, and returns SQL plus parameters to the #1895 G7 live adapter. The shipping statement now uses repeated named placeholders and a mapping, but `_RecordingCursor` converts that mapping to item tuples and the validator still counts positional `%s`. `execute_explain` then forces every parameter container to a tuple. This breaks the shared query/evidence boundary before GFS/IFS hot/cold sampling.

Fixture level: expanded. Repair intensity: high. Project profile: NHMS. The high escalation is required because a shared readiness helper controls production evidence acceptance and its digest is consumed across four lanes.

## Goals / Non-Goals

**Goals:**
- Preserve a defensive copy of the captured mapping or positional sequence through recorder output and psycopg execution.
- Require one primary explicit-cycle query, exact placeholder/container compatibility, and canonical run/model/timeseries-segment/cycle values. For named SQL, resolve each required predicate's referenced placeholder name and compare that key's value; key membership or `.values()` membership is forbidden as proof.
- Keep digest and frozen-lane behavior deterministic regardless of mapping insertion order.
- Expose one internal pure validation seam taking captured SQL, its native container, and expected identity so synthetic invalid captures are testable without altering the named-only shipping owner.
- Fail closed before live SQL when query branch, placeholder syntax, keys, values, or container shape drift.

**Non-Goals:**
- Do not rewrite `forecast_store` SQL, revive `selected_cycles`, change the API, receipt schema, lane selection, thresholds, timeout, readonly role, or rollout sequence.
- Do not claim local tests as node-27 live performance evidence.

## Decisions

1. **Preserve the native binding container.** Capture mappings as copied mappings and positional inputs as tuples; pass the same semantic container to psycopg. Rewriting named SQL into positional SQL was rejected because it duplicates placeholder parsing and no longer proves the shipping statement's real execution contract.
2. **Validate syntax and values together through a pure seam.** A single internal validator receives captured SQL, its mapping or sequence, and expected identity. Named SQL extracts every `%(name)s` reference, requires the mapping key set to equal the set of unique referenced names, and resolves the `h.cycle_time`, `h.run_id`, `h.model_id`, and SQL segment predicate by their referenced names to canonical values. Repeated pyformat references such as the two `%(issue_time)s` uses share one mapping key and are not a count mismatch. Positional SQL alone requires `query_text.count("%s") == len(sequence)` and checks the equality ordinal plus required bound values. Mixed, malformed, and unsupported placeholder styles are rejected. This seam, not a modified `forecast_store`, owns synthetic negative and positional tests.
3. **Canonicalize only for hashing.** Digest serialization sorts mapping keys recursively while preserving sequence order and normalized UTC datetimes. Runtime execution retains the native container; receipt fields remain unchanged.
4. **Keep stable failure boundaries.** Branch errors remain query-branch failures; placeholder/container mismatch remains binding-shape failure; missing run/model/segment values retain their identity-specific failures. No invalid capture reaches `EXPLAIN`.

## Risk Packs

Selected:
- Public API / CLI / script entry: the node-27 oracle CLI consumes the captured parameters.
- Schema / columns / units / field names: psycopg placeholder names and mapping keys are the binding schema.
- Resource limits / large input / discovery: the existing bounded four-lane sample and statement timeout must remain unchanged.
- Legacy compatibility / examples: retained positional captures and all existing receipt consumers must remain valid.
- Error handling / rollback / partial outputs: malformed capture must fail before SQL execution or PASS publication.
- Hydro-met time series / forcing windows: the issue-time equality fixes each seven-day product curve.
- PostGIS / TimescaleDB domain behavior: the same binding must execute against hot and compressed-cold lanes.
- Published NHMS artifacts / display identity: query digest and frozen lane identity feed accepted readiness evidence.

Not selected:
- Config / project setup: no setting or dependency changes.
- File IO / path safety / overwrite: publication helpers and paths are unchanged.
- Auth / permissions / secrets: readonly identity and DSN handling are unchanged and remain covered by existing tests.
- Concurrency / shared state / ordering: capture and probes are synchronous and introduce no shared state.
- Release / packaging / dependency compatibility: no package or dependency changes.
- Documentation / migration notes: the existing runbook remains authoritative; no operator migration is needed.
- Geospatial / CRS / basin geometry; SHUD numerical runtime; Slurm lifecycle; external providers; run manifest/QC provenance: no surface in these packs changes.

## Invariant Matrix

Governing invariant: the one shipping explicit-cycle SQL statement, its native parameter container, its canonical lane identity, its live execution, and its digest MUST describe the same run/model/segment/issue-time query.

- Source of truth: captured SQL predicate-to-placeholder references plus the referenced mapping key values or positional ordinals, bound against the canonical `record_explicit_cycle_curve` inputs.
- Producers: `_RecordingCursor.execute`, `_CaptureForecastStore`, `record_explicit_cycle_curve`; the shipping producer remains named-only.
- Validators/preflight: one pure captured-query validator plus primary-query selection in `node27_issue1895_query.py`; synthetic named/positional/invalid inputs target the pure seam directly.
- Storage/cache/query: `query_digest` and the returned in-memory query record; no DB or cache write.
- Public routes/entrypoints: `scripts/node27_issue1895_performance_oracle.py` full-probes mode.
- Frontend/downstream consumers: `bind_lane`, `freeze_lanes`, `make_sql_probe`, and performance receipt validators consume the unchanged digest/identity contract.
- Failure paths/rollback/stale state: invalid capture raises before `EXPLAIN`; live SQL errors remain typed failures and cannot publish PASS.
- Evidence/audit/readiness: four-lane performance/live CLI/publication tests and #1895 G7; no local result substitutes for live G7.
- Regression rows:
  - named shipping SQL + exact unique referenced-key set in arbitrary insertion order + predicate-referenced canonical values -> accepted, mapping reaches psycopg, stable digest; repeated `%(issue_time)s` uses share one key.
  - synthetic positional explicit-cycle SQL + matching placeholder count, equality ordinal, and sequence -> retained compatibility and stable digest through the pure validator seam.
  - synthetic selected-cycles/mixed or malformed style/missing or extra key/count mismatch/wrong cycle/run/model/segment reference or value/wrong container -> stable fail-closed error from the pure seam before live SQL.
  - unchanged lane freeze/publication consumer -> unchanged receipt shape and semantic digest comparison.

Boundary checklist:
- Shared helper roots: recorder, canonicalizer/digest, live `EXPLAIN` adapter.
- Public entrypoints: performance oracle CLI full-probes path.
- Producer/consumer evidence boundary: captured parameters -> SQL probe; digest -> frozen lane/receipt.
- Stale-state/idempotency: copied containers prevent caller mutation; repeated capture yields the same digest.
- Unchanged consumers: forecast API, publication files, readonly/session controls, and C1-C4 owners.

## Risks / Trade-offs

- [Placeholder parsing accepts a lookalike] -> constrain parsing to psycopg `pyformat`/`format` forms and test mixed, malformed, missing, extra, and wrong-equality cases.
- [Mapping mutation changes execution after digesting] -> copy at capture and avoid exposing mutable input aliases.
- [Compatibility change alters evidence semantics] -> keep receipt fields unchanged and assert old downstream validation/freeze tests.

## Migration Plan

Merge the independent #2227 repair before #2224 resumes its full targeted gate. No production action occurs in this PR. #1895 G7 later regenerates live evidence on the reviewed merged SHA; rollback is the code revert because there is no persisted migration.

## Open Questions

None.
