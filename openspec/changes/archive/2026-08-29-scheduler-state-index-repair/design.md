## Context

The shared scheduler state index is one checksummed JSON payload. Validation checks the payload checksum before parsing `entries`, so an out-of-band entry edit makes every lookup fail with `state_snapshot_index_checksum_mismatch`. Production publication already owns canonical bytes, validation, provider locking, and CAS, but there is no supported repair mutation.

Issue #1482 concerns a different state surface. `repair_status` and `active_blocker` are annotations on projection copies, while `record_manual_repair` reads durable journal rows. Persisting those annotations or recomputing projection repair state on the write path would create a second repair authority. The current contract conservatively over-pins one attempt when the projection says repaired but neither durable marker evidence nor the single state-level winner names the target.

Fixture level: expanded. Repair intensity: high. Upstream suggested level: #1204 expanded (agree); #1482 absent (expanded required by persisted/shared retry-state scope even though the selected disposition is documentation-only). Minimal mergeable slice: #1204's dry-run-default, archive-first exact removal and checksum-only repair; #1482 is an independent terminal design disposition bundled by explicit user request.

## Goals / Non-Goals

**Goals:**

- Repair a checksum-invalid but otherwise structurally valid index without duplicating checksum or serialization logic.
- Remove exactly one entry selected by `state_id`, `run_id`, or the complete `(model_id, source_id, valid_time)` selector; zero or multiple matches refuse before mutation.
- Bind archive, mutation, CAS, read-back, and receipt evidence to each observed lane pre-image and classify partial/post-CAS uncertainty honestly.
- Preserve raw entry mappings and order except for the selected removal; checksum-only repair changes no entry bytes semantically.
- Keep the private/reference scratch index and shared/destination canonical index coherent for the repaired identity without treating either lane's full entry set as a replica of the other.
- Make #1482 option (c) explicit and terminal, acknowledging that the node-22 manual-retry execution issue #1186 remains open and can expose the conservative over-pin.

**Non-Goals:**

- Change the checksum algorithm, index schema version, fail-closed validation order, or object lifecycle.
- Edit arbitrary entry fields, remove more than one entry, repair malformed JSON/schema/entries, or validate historical object existence during repair.
- Change scheduler blocker evidence, retry decisions, projection winner semantics, or persist projection annotations.
- Change copyback replay behavior, journal rows, registry/readiness providers, Slurm scheduling, or node-27 services.
- Make the private/reference and shared/destination indexes byte-identical as whole payloads. They have different lifecycle/history sets; only the explicitly repaired logical identity is coordinated across them.

## Decisions

### D1: One public repair helper owns the topology-coordinated mutation

Add one narrow public helper in `packages/common/state_manager.py` that accepts the two production roots, operation, optional exact selector/lane, archive destination, and dry-run/enforce flag. It resolves both fixed index paths, performs bounded JSON/schema/entry validation, prepares each replacement through the same production builder, and owns all provider locking, pre-image binding, archive-before-write, CAS, and production read-back. The CLI owns only argument parsing, private receipt publication, exit codes, and bounded rendering; it does not assemble domain mutations or lock/CAS calls.

The CLI exposes the existing two-root production topology, not two arbitrary index files: `--reference-root` (private node-22 scratch lifecycle index, default `OBJECT_STORE_ROOT`) and `--destination-root` (shared canonical index, default `NHMS_OBJECT_STORE_COPYBACK_ROOT`), with both index paths fixed at `scheduler/state-index/index-last.json`. It reuses the copyback replay root-equality/overlap and filesystem-identity guards. The two indexes are not whole-file mirrors: the private lane can contain lifecycle history absent from shared, and shared can retain historical entries whose objects are archived. Therefore no repair copies one full payload over the other.

For `remove-entry`, preflight reads and validates both stable snapshots, resolves the same selector independently, and requires exactly one match in every lane where the logical identity is present. The default requires the target in both lanes; explicit `--allow-missing-reference` or `--allow-missing-destination` is permitted only for a recorded one-lane recovery after dry-run proves the absence. Enforce acquires the private/reference lock first and the shared/destination lock second — exactly the production copyback lock order, never path-sorted — then re-reads both snapshots/pre-images under lock, writes both archives, mutates the private/reference lane, and finally mutates the shared/destination lane. That order is deliberate: if the second write fails, shared still carries the entry while private no longer can reintroduce it through copyback; the state remains fail-safe and the receipt reports partial completion. Both replacement payloads use their own pre-image and preserve their own unrelated entries.

For `recompute-checksum`, the CLI requires `--lane reference|destination`; it repairs only the checksum-invalid lane and validates the other lane read-only for the selected topology. This avoids gratuitously refreshing a healthy lane while ensuring the operator has not pointed at unrelated roots. Receipt evidence states the untouched lane and reason.

Alternative rejected: let the CLI import `_payload_checksum` and `_canonical_json_bytes`. That repeats the private-function incident rather than creating a supported mutation boundary and lets later checksum/publisher changes drift. Alternative rejected: copy canonical to scratch (or scratch to canonical); the two lanes intentionally carry different entry sets and object-lifecycle assumptions.

### D2: Checksum-invalid input gets a two-stage validator

The repair helper first checks every production structural rule with a temporary expected checksum computed by the production payload builder; object verification and freshness are disabled because historical objects may be archived and repair does not change their references. It separately records whether the original checksum matched. This admits only an intact schema/entry payload whose sole payload-level defect may be checksum mismatch; malformed JSON, unsupported schema, duplicate identity/state ID, unsafe URI, limits, or invalid fields refuse.

For `remove-entry`, the selected entry is removed from the original raw `entries` list so every surviving mapping and order stays unchanged. For `recompute-checksum`, the raw entries list is unchanged. The final payload is generated by the same canonical production path, which intentionally refreshes `generated_at` and checksum.

Alternative rejected: call the existing validator directly on the bad payload; checksum-first validation makes the supported checksum repair impossible. Alternative rejected: accept arbitrary JSON and merely hash it; that would bless semantic corruption.

### D3: Archive and receipt are different evidence boundaries

Enforce requires existing owner-private archive and receipt roots. Under both provider locks, each lane's exact pre-image bytes are written no-follow with mode `0600` to unique lane-labelled archive files before the first CAS; any archive write/read-back failure is a zero-index-write refusal. Archive evidence contains per-lane digest, byte count, original provider pre-image metadata, and path only in private receipt output.

The receipt is written after all reachable read-backs. A receipt failure after either CAS is commit-uncertain/incomplete, never a refusal. Exit codes are `0` success, `2` provably zero-index-write refusal, and `3` any partial, committed-incomplete, or commit-uncertain outcome. Typed provider phases at or after replace (`replace_uncertain`, `postcommit`, `release_uncertain`) take exit 3; only failures proven before the first lane CAS take exit 2. A reference-lane commit followed by destination-lane precommit failure is still exit 3 because the invocation already mutated one index. Unknown exceptions after mutation begins fail toward commit uncertainty.

Dry-run performs no archive, index write, receipt write, lock-directory creation, or evidence-directory creation. It emits a preview on stdout with operation, per-lane original checksum validity, per-lane selector outcome/identity (bounded), and before/after counts.

### D4: Selectors are exact logical identities and uniquely resolving per lane

`remove-entry` accepts exactly one selector family:

- `--state-id STATE_ID`
- `--run-id RUN_ID`
- all of `--model-id`, `--source-id`, and `--valid-time`

The complete tuple deliberately excludes optional cycle/lead fields because the issue's accepted selector is the three-field base key; therefore the helper must enforce uniqueness independently in reference and destination. If both lanes match, their full production identity key and `state_id` must agree; a selector that names different logical entries across lanes refuses. Zero or multiple matches are stable zero-index-write refusals unless one explicit missing-lane flag authorizes a proven absence. `recompute-checksum` accepts no entry selector and requires exactly one target lane.

### D5: #1482 chooses option (c), explicit permanent limitation

Do not copy projection logic into `record_manual_repair` and do not add `repair_status`, `active_blocker`, or a new repair marker to `_pipeline_job_row`. Durable journal facts and projection annotations remain separate authorities. The existing gate-contract keys stay readable for synthetic/legacy records, but the production writer does not claim to produce them.

Trade-off accepted: when a target is already annotated repaired only in the projection copy at marker-write time and neither `repaired_stage_evidence` nor `completed_stage_evidence` names it, row-present routing refuses while the later row-absent record path conservatively pins the marker attempt. This is an observable attempt-budget over-pin, not silent state mutation or duplicate execution. The limitation is terminal for #1482 and remains covered by the existing paired disclosure tests. #1186 remains the separate operator-entry/exposure issue; closed #1460/#1461 changed projection composition but did not make projection annotations durable.

Alternatives rejected: (a) recomputing projection annotations in the write path duplicates a complex state projection and can drift; (b) persisting annotations changes the closed pipeline-row schema, migration/immutability rules, and authority model to solve one conservative residue.

## Risk Packs Considered

- Public API / CLI / script entry: selected - new operator CLI and stable exit contract.
- Config / project setup: selected - object-store/index/archive/receipt roots are environment or CLI inputs.
- File IO / path safety / overwrite: selected - shared index overwrite plus archive/receipt writes require no-follow containment, owner/private roots, atomic writes, and bounded bytes.
- Schema / columns / units / field names: selected - state-index checksum/schema and selectors; #1482 explicitly preserves the durable row schema.
- Auth / permissions / secrets: selected - provider ownership and private evidence directories; public errors must not expose unrelated content or credentials.
- Concurrency / shared state / ordering: selected - two-lane reference-then-destination lock ordering, archive-before-first-CAS, per-lane expected pre-images, partial-commit classification, and read-back ordering are core.
- Resource limits / large input / discovery: selected - reuse state-index byte/entry/JSON complexity limits; no directory sweep.
- Legacy compatibility / examples: selected - raw surviving entries, copyback replay, normal publish defaults, and legacy retry marker behavior stay unchanged.
- Error handling / rollback / partial outputs: selected - refusal versus commit-uncertain classification and archive/receipt boundaries.
- Release / packaging / dependency compatibility: not selected - no dependency, package, or distribution change.
- Documentation / migration notes: selected - current node-22 recovery procedure and #1482 terminal disposition.
- Geospatial / CRS / basin geometry: not selected - no geometry.
- Hydro-met time series / forcing windows: not selected - timestamps identify entries but forcing semantics do not change.
- SHUD numerical runtime / conservation / NaN: not selected - state object content and solver behavior are untouched.
- PostGIS / TimescaleDB domain behavior: not selected - DB-free file state only.
- Slurm production lifecycle / mock-vs-real parity: selected at scheduler-control boundary - corrupt index blocks all candidates; no sbatch/Slurm behavior changes or live job required.
- External hydro-met providers / snapshot reproducibility: not selected - no provider acquisition.
- Run manifest / QC provenance: selected only as preserved entry lineage - repair must not rewrite surviving entry mappings.
- Published NHMS artifacts / display identity: not selected - no display artifact.

## Boundary-Surface Checklist

- Shared helper roots: `packages/common/state_manager.py`, `packages/common/provider_atomic.py`, safe filesystem primitives.
- Public entrypoints: `scripts.scheduler_state_index_repair`; normal state-index publisher remains unchanged.
- Read surfaces: fixed private/reference and shared/destination state-index files plus per-lane post-write read-back only.
- Write/delete/overwrite surfaces: required per-lane pre-image archives, reference then destination index CAS, receipt files; no state object deletion and no whole-index cross-lane copy.
- Staging/publish/rollback surfaces: both archives before first publish; source-first partial completion; each provider's existing verified restore/uncertainty behavior.
- Producer/consumer evidence boundaries: both raw index payloads, canonical publisher, production validator, per-lane CLI receipt, scheduler reader, and copyback source/destination merge.
- Stale-state/idempotency boundaries: shared reference-then-destination lock order, exact per-lane pre-image CAS, repeated explicit-lane checksum repair, and per-lane selector uniqueness/identity agreement.
- Unchanged downstream consumers: strict warm-start lookup, copyback replay, provider refresh, retry projection and legacy markers.

## Invariant Matrix: State-Index Repair

- Governing invariant: a repair either leaves the shared index byte-identical, or archives the exact observed pre-image and atomically publishes one production-valid canonical payload whose only semantic entry change is the requested unique removal.
- Source-of-truth identity/contract: provider pre-image metadata/digest; `nhms.scheduler.file_state_snapshot_index.v1`; raw entry mappings; production checksum and canonical publisher.
- Producers: normal state-index publishers and the new repair helper.
- Validators/preflight: bounded provider snapshot, production structural validator with rebuilt checksum, selector uniqueness, archive read-back, CAS, production post-write validator.
- Storage/cache/query: private/reference scratch index + lock and shared/destination canonical index + lock; each keeps its own unrelated entry set; no state object or journal mutation.
- Public routes/entrypoints: two-root repair CLI dry-run/enforce and its stable per-lane exit/status contract.
- Frontend/downstream consumers: scheduler strict warm-start reader, state-save writer, and source-to-destination copyback/refresh tools remain unchanged.
- Failure paths/rollback/stale state: malformed/unsafe/ambiguous/cross-lane identity input refuses; archive failure refuses before either CAS; any post-first-CAS failure reports partial/commit-uncertain; no claim of automatic cross-lane rollback beyond provider semantics.
- Evidence/audit/readiness: two private archives plus schema-versioned per-lane receipt bound to pre/post digests, operation, lock/write order, and untouched-lane rationale.
- Regression rows:
  - checksum-mismatched, otherwise valid destination payload + `recompute-checksum --lane destination` dry-run -> per-lane preview, zero filesystem mutation, healthy reference validated but untouched.
  - same input + enforce with valid private roots -> destination pre-image archived, destination checksum/read-back valid, destination entries semantically identical, reference bytes unchanged.
  - both valid payloads + unique selector matching the same logical identity -> both pre-images archived; identity removed from reference then destination; each lane's unrelated entries retain order and mapping equality.
  - target missing in one lane without its explicit allow flag, selector resolves differently/multiply, or either payload is malformed/unsafe -> stable refusal before either index CAS.
  - reference removal commits and destination CAS/read-back fails -> exit 3 partial/uncertain receipt; reference can no longer reintroduce the target, destination remains conservative until repaired.
  - archive/first-CAS/read-back/receipt injected failures -> correct zero-write refusal or partial/commit-uncertain classification without a false zero-mutation claim.
  - concurrent production copyback + repair -> shared reference-then-destination lock order avoids deadlock and CAS prevents stale publication.
  - existing normal publish/copyback callers -> unchanged defaults and behavior.

## Invariant Matrix: Retry Projection Authority (#1482)

- Governing invariant: durable manual-repair markers record durable row facts only; projection-only repaired annotations never become durable truth implicitly.
- Source-of-truth identity/contract: append-only journal row and marker details versus ephemeral projection annotations and single-winner state mappings.
- Producers: `record_manual_repair` remains unchanged; projection annotators remain unchanged.
- Validators/preflight: existing marker sanitizer and row-present/row-absent routing remain unchanged.
- Storage/cache/query: `_pipeline_job_row` closed schema remains unchanged.
- Public routes/entrypoints: node-22 manual retry remains tracked by #1186; this change adds no retry entrypoint.
- Frontend/downstream consumers: scheduler attempt derivation keeps the disclosed conservative over-pin.
- Failure paths/rollback/stale state: projection-only repaired-at-write target not named by state mapping may over-pin one attempt; no under-pin or new mutation path is introduced.
- Evidence/audit/readiness: existing paired disclosure matrix plus spec/source wording consistency.
- Regression rows:
  - synthetic record carrying repaired gate keys -> existing row-absent refusal remains supported.
  - production durable row lacking projection keys -> existing marker bytes and decisions remain unchanged.
  - projection-only repaired target not state-mapping winner -> disclosed row-present refuse / row-absent pin remains the terminal accepted limitation.

## Risks / Trade-offs

- [Repairing a checksum can legitimize a structurally valid but operator-wrong edit] → checksum-only mode is explicit, dry-run-default, archive-first, and cannot repair schema/identity/URI defects; the runbook requires pre-image review.
- [Archive succeeds but CAS does not] → report refusal and retain the archive as evidence; no index mutation claim.
- [CAS may commit but later evidence fails] → exit 3 and report commit uncertainty; never claim refusal.
- [Repair races normal publication or copyback] → acquire reference then destination locks in the production copyback order, bind exact per-lane pre-images under lock, and permit no lock-external mutation window.
- [Reference removal commits but destination repair fails] → exit 3 with per-lane evidence; source-first ordering prevents copyback resurrection while shared remains conservatively unchanged until the operator completes the destination repair.
- [Accepting #1482 can consume an attempt budget] → limitation is explicit, conservative, covered by paired tests, and avoids a second state authority; #1186 remains separately tracked.

## Migration Plan

1. Deploy code and run local/node-27 test oracle; no data migration.
2. On node-22, stop/freeze the scheduler, state-save jobs, copyback/replay, recalibration, and provider-refresh writers before a real repair per runbook, then run the two-root dry-run and inspect both lane previews.
3. Run enforce as the provider owner with private archive/receipt roots; retain per-lane receipt, assert the repaired identity is absent from both lanes (or the explicit missing-lane disposition is recorded), and validate the next bounded scheduler pass.
4. If a source-first partial completion occurs, complete the destination repair from a fresh dry-run before re-enabling writers; do not repopulate source from destination.
5. Rollback, if needed, is an explicit operator decision using each lane's own archived bytes and a fresh pre-image check; the CLI does not silently overwrite a later valid publication or copy one lane wholesale over the other.

## Open Questions

None.
