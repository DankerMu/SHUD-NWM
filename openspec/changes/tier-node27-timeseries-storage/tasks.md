# Tasks: Tier Node-27 Timeseries Storage

Order is load-bearing:

- Foundation (1) lands first — every later script consumes its env/helper,
  overlap validation, and pinned schemas.
- Archive + audit lane (2) and salvage (3) follow; compression (4) may
  proceed in parallel once 1 lands.
- The drill (5) is gated on archive/salvage live receipts (2.5, 3.3). It
  writes only its isolated staging schema — never production hypertables —
  so it has **no ordering constraint against compression (4)**: production
  compression state can neither block nor be touched by the drill.
- Retention enforce (6.3) is hard-gated on exactly two receipts: the drill
  PASS (5.2) and a fresh archive-completeness receipt from the recurring
  inventory audit (2.1/2.3, which folds in salvage coverage from 3).
  Compression (4.x) is **not** a retention gate.

## 1. Storage config foundation (`runtime-storage-source-canonicalization`)

- [x] 1.1 Canonicalize `NHMS_ARCHIVE_ROOT` and extend the shared
  storage-path helper used by the new scripts.
  Evidence floor: the helper resolves the archive root from
  `NHMS_ARCHIVE_ROOT`, with per-script `NODE27_<SCRIPT>_ARCHIVE_ROOT`
  overrides taking precedence (same aliasing convention as
  `NODE27_RAW_RETENTION_OBJECT_STORE_ROOT` /
  `NODE27_GOVERNANCE_OBJECT_STORE_ROOT`); it exposes an archive-provenance
  lookup (cycle identity → archive object + manifest path) consumed only by
  the non-display tooling (inventory audit, rebuild drill, salvage);
  configuration validation rejects (a) any overlap between the archive root
  and any retention/cleanup target roots and (b)
  `NHMS_ARCHIVE_MIN_AGE_DAYS` below the DB retention window (30 days);
  display API code paths do not import the archive resolver (ADR 0001
  carve-out).
  Test rows:
  - Input: `NHMS_ARCHIVE_ROOT` set, no per-script override.
    Expected: helper resolves the shared root; provenance lookup returns the
    manifest path for a fixture cycle.
  - Input: a per-script `NODE27_*_ARCHIVE_ROOT` override set alongside
    `NHMS_ARCHIVE_ROOT`.
    Expected: the per-script override wins.
  - Input: archive root nested under (or containing) a raw-retention or
    cleanup target root.
    Expected: validation error naming both roots; no tool can run enforce.
  - Input: archive and cleanup roots that are equal, contain `..` or `~`
    aliases resolving to overlap, or reach the same/ancestor directory via
    an existing symlink.
    Expected: compare `expanduser()` + resolved filesystem identities,
    reject equality or ancestry in either direction, and name the normalized
    archive and cleanup roots. The helper accepts the complete cleanup-root
    set explicitly so every later mutation-capable caller must supply all of
    its retention/cleanup targets rather than relying on a hidden partial
    env list.
  - Input: `NHMS_ARCHIVE_MIN_AGE_DAYS=20` with the 30-day retention window.
    Expected: validation error before any mutation.
  - Input: canonical archive identity `(lane=forcing|runs|states, source,
    cycle_identity, cycle_time, lane-specific fields)`, where ISO-8601 UTC
    `cycle_time` must correspond to the compact path `cycle_identity`, with forcing requiring
    `basin_version_id + model_id`, runs requiring `run_id`, and states
    requiring `model_id`; every component is a non-empty safe path segment.
    Expected: deterministic paths under
    `<archive-root>/<lane>/<source-segment>/<cycle-identity>/<lane-scope...>/archive.tar.zst`
    and the same directory's `manifest.json`; repeated lookup is identical,
    while different sources with the same cycle/scope resolve distinctly.
    Manifest `source` uses the shared canonical storage IDs (`gfs`, `ERA5`,
    `IFS`); the filesystem `source-segment` is the corresponding lowercase
    object-store segment. Case-insensitive aliases normalize to the same
    identity/path, while an unknown source fails closed. The states lane also
    has one exact reserved canonical source, `legacy-unqualified`, for valid
    source-less `state_snapshot` rows/object paths (`source_id` NULL or the
    existing equivalent empty-string representation); it is forbidden for
    forcing/runs and never inferred as a real provider. Its cycle identity is
    derived from the row's required `valid_time`, giving a deterministic,
    collision-disjoint `states/legacy-unqualified/...` archive path.
  - Input: identity with an unknown lane, empty/dot/dot-dot component, path
    separator, absolute component, missing lane-required field, or field from
    the wrong lane.
    Expected: stable validation error before any filesystem access.
  - Input: product manifest whose identity or declared archive path differs
    from the canonical identity-derived path.
    Expected: shared manifest-binding preflight rejects it before any
    idempotency skip, completeness verdict, rebuild selection, or deletion.
  - Input: product manifest using a known but non-canonical source alias
    (`GFS`, `era5`, or `ifs`) even when its path uses the lowercase storage
    segment.
    Expected: schema and semantic manifest-binding preflight both reject it;
    direct operator/lookup identities still normalize aliases before a
    canonical manifest is produced.
  - Input: a valid source-less legacy state reference
    `states/<model>/<valid-time>/...` with `source_id = NULL` or `""`.
    Expected: it maps explicitly to the states-only
    same `legacy-unqualified` identity using `valid_time` for canonical cycle
    identity/time; manifest/path binding round-trips deterministically and
    cannot collide with provider-qualified states. Forcing/runs reject the
    sentinel and no provider is synthesized.
  - Input: existing `validate_object_path` callers and the established
    `NODE27_RAW_RETENTION_OBJECT_STORE_ROOT` /
    `NODE27_GOVERNANCE_OBJECT_STORE_ROOT` precedence behavior.
    Expected: unchanged results and override behavior; archive helpers add no
    display import/call dependency.
  Implementation evidence (#846): focused storage, raw-retention,
  resource-governance, display-boundary, and schema contract tests pass;
  unsafe identities fail before root resolution and normalized overlap / age
  checks fail closed.
- [x] 1.2 Pin the manifest/receipt JSON Schemas under `schemas/`.
  Evidence floor: JSON Schemas + `schemas/examples/` documents exist for the
  archive manifest, archive-completeness receipt, salvage manifest, drill
  receipt, and retention receipt; they pass the json-schema-validate CI gate
  and are the single format source for all five scripts. The product-archive
  manifest schema has **no row-count field** (product parity in the drill is
  file-derived); the salvage manifest schema **requires** per-selector
  exported row counts; the drill receipt schema requires declared
  (source, window) coverage tuples; the completeness receipt schema requires
  per-inventoried-subject verdicts, the salvage selector list, coverage
  bounds, and `generated_at`. Every verdict is bound to exactly one
  lane-discriminated stable subject (`forcing_version_id`, `run_id`, or
  `state_id`) even when multiple subjects share one time window; the
  coverage mechanism is represented separately from the subject lane.
  Test rows:
  - Input: each schema's example document.
    Expected: validates in the json-schema-validate CI gate.
  - Input: a completeness receipt missing per-window verdicts, or a salvage
    manifest missing row counts.
    Expected: schema validation fails.
  - Input: a product-archive manifest carrying any row-count field.
    Expected: schema validation fails; product parity remains file-derived.
  - Input: drill PASS without compared cycles/selectors/counts, staging
    schema/database identity, or declared `(source, window)` coverage; drill
    FAIL without a per-item diff.
    Expected: schema validation fails for each missing verdict-specific
    requirement.
  - Input: retention refusal without a refusal reason, or successful enforce
    without per-dropped-chunk name/freed bytes, deferred remainder, and the
    salvage-backed windows field (which may be an empty list).
    Expected: schema validation fails for each missing outcome-specific
    requirement.
  - Input: completeness/salvage selector with a typo, unknown identity key,
    or forcing/river table-key mismatch.
    Expected: both schemas reject it; forcing requires exactly
    `forcing_version_id`, river requires exactly `run_id`.
  - Input: product-only drill PASS with forcing/runs coverage and an empty
    required `comparisons.selectors` array.
    Expected: schema validation passes; a non-empty selector becomes a
    runtime semantic requirement only when `db-export` coverage is present.
  - Input: product archive/file or salvage object path that is absolute,
    contains an empty/dot/dot-dot segment, backslash, or control character.
    Expected: schema validation fails; ordinary nested root-relative paths
    with the correct archive lane / `db-export` prefix pass.
  - Input: salvage object path under `db-export/` whose filename does not end
    in `.csv.zst`.
    Expected: schema validation fails; an ordinary nested
    `db-export/.../data.csv.zst` path passes.
  - Input: two forcing versions sharing the same time window, one complete
    and one gap; or a verdict with a missing/cross-lane subject identity.
    Expected: the receipt represents the two subjects distinctly and rejects
    the missing/cross-lane identity. Runtime inventory coverage (task 2.1)
    must later prove exactly one verdict per inventoried subject and exact
    `gap` to salvage-selector correspondence.
  - Input: a state subject declaring `coverage: db-export` and
    `verdict: complete`.
    Expected: schema validation fails because DB-export salvage covers only
    forcing/river timeseries; a state gap remains fail-closed and cannot be
    converted into a salvage selector.
  - Input: clean default dev/test environment after dependency sync.
    Expected: every schema positive/negative pytest executes with zero skip;
    missing `check-jsonschema` is a test failure, not a skipped contract gate.
  Implementation evidence (#846): all five examples and schemas pass the CI
  `check-jsonschema` example + metaschema loops; focused negative-schema tests
  reject every missing or forbidden contract field above. Invariant closure
  adds source-qualified lane identities, manifest/path binding, exact typed
  selectors, safe relative paths, product-only drill PASS, and a default
  dependency-backed zero-skip negative-contract gate.

## 2. Inventory audit and product archive lane (`timeseries-product-archive`)

- [ ] 2.1 Build the inventory audit
  (`scripts/node27_storage_inventory_audit.py`) emitting the
  archive-completeness receipt.
  Evidence floor: compares DB coverage (`hydro_run` cycles,
  `forcing_version` windows, `state_snapshot.state_uri` references) against
  checksum-verified archive objects and hot object-store presence; emits the
  archive-completeness receipt (schema from 1.2) with per-window verdict
  (`complete` / `pending-archive` / `gap`), the salvage selector list,
  coverage bounds, and `generated_at`; an archive object counts as present
  only when checksum-verified; unit tests for the classification logic.
  The DB transaction is `REPEATABLE READ READ ONLY`, captures one audit time,
  and applies a 20-second statement timeout. Forcing/run subjects are
  included only when the corresponding
  detail hypertable contains at least one row; the non-decorrelated
  correlated `LIMIT 1` probes must retain an identity-leading
  index-only node-27 query plan (no detail-hypertable full scan/hash
  aggregate). Their selector windows use the authoritative metadata
  `[start_time, end_time]` bounds (inclusive); the audit does not rescan all
  detail rows to recompute bounds. Age is evaluated at `window.end`; state
  subjects use the point window
  `[valid_time, valid_time]`. Receipt `coverage_bounds` equals the exact
  min(start)/max(end) across the captured subject set, and every window must
  satisfy start <= end.

  Product references are strict, root-contained object-store URIs. Forcing
  hot coverage requires `forcing_package.json` as a bounded regular file,
  its sha256 equal to `met.forcing_version.checksum`, its source/cycle/model/
  basin identity equal to the row/URI, its manifest time range contain the DB
  subject `[start_time,end_time]` (the DB range remains the receipt/selector
  authority), and every manifest-listed file be regular, contained and
  checksum-valid. Run hot coverage requires the
  row's bounded input manifest as a regular file with run/source/cycle/model/
  basin identity bound to the row plus at least one contained regular file
  below the row's output directory. State hot coverage requires the
  referenced regular file and sha256 equality with `state_snapshot.checksum`.
  A clone state may alias its authoritative source artifact only when
  clone provenance is present and the physical model segment matches
  `cloned_from_model_id`; its stable receipt subject remains the clone
  `state_id`, while archive/hot coverage follows the shared physical
  artifact. Any malformed URI, containment escape, symlink, permission or
  I/O error is an audit blocker, not ordinary absence.

  Manifest JSON reads are capped at 16 MiB. A product archive verifies only
  when regular non-symlink manifest/tarball siblings are contained under the
  archive root, the bounded manifest is
  schema-valid and passes shared semantic identity/path binding, and actual
  tarball size + streaming sha256 match the manifest. A missing archive root
  or missing canonical siblings means no archive coverage; unreadable or
  malformed existing evidence blocks receipt publication. Verified salvage
  coverage is discovered by a bounded, symlink-safe scan of
  `<archive-root>/db-export/**/manifest.json`; every manifest and referenced
  object must pass the pinned schema, containment, size and sha256 checks,
  and duplicate/conflicting exact selectors block publication. Discovery is
  capped at 10,000 manifests and eight directory levels beneath
  `db-export/`; exceeding either bound is a blocker. Inventory is capped at
  100,000 subjects, and exceeding the cap blocks publication.

  Classification precedence is fixed: verified product archive;
  verified exact `db-export` selector (forcing/runs only); hot object-store
  coverage before the minimum-age cutoff; aged hot-only pending archive;
  otherwise gap. Every salvageable forcing/run gap has exactly one selector
  identical in identity/window to its subject, while state gaps have none.
  Before atomic replace, the receipt must be schema-valid, deterministically
  ordered, contain every inventoried subject exactly once, contain no
  duplicate subject, and prove the forcing/run gap-selector bijection. Empty
  inventory or any blocker exits non-zero and never overwrites the previous
  valid gate receipt. The stable output interface is required absolute
  `--receipt-path` or `NODE27_STORAGE_INVENTORY_RECEIPT_PATH`; its existing
  parent and every parent component must resolve to a non-symlink directory.
  Publication writes a mode-0600 same-directory exclusive temporary file,
  flushes + fsyncs it, uses atomic `os.replace`, fsyncs the directory, and
  removes temporary residue on failure. Failure diagnostics are emitted as
  JSON to stderr, never as a replacement gate receipt. Runtime schema
  validation uses `jsonschema` as a direct production dependency, not a dev
  transitive dependency.
  Test rows:
  - Input: window with a checksum-verified archive object.
    Expected: verdict `complete`; not in the salvage list.
  - Input: window older than `NHMS_ARCHIVE_MIN_AGE_DAYS` whose products
    exist only in the hot object-store.
    Expected: verdict `pending-archive`.
  - Input: DB rows whose products exist in neither object-store nor archive.
    Expected: verdict `gap`; exact selectors appear in the salvage list.
  - Input: a `state_snapshot` reference whose state artifact exists in
    neither object-store nor archive.
    Expected: verdict `gap`; no DB-export selector is fabricated, and the
    receipt cannot satisfy retention until product coverage is restored.
  - Input: final-path archive object whose tarball sha256 mismatches its
    manifest.
    Expected: treated as absent (`pending-archive`/`gap`); mismatch reported
    in the receipt.
  - Input: a source-less legacy `state_snapshot` row and its existing
    `states/<model>/<valid-time>/...` hot object.
    Expected: inventory uses the explicit `legacy-unqualified` archive
    identity; a verified legacy archive can yield `complete`, while a
    missing legacy object remains a non-salvageable `gap`.
  - Input: forcing/run metadata with no corresponding detail-hypertable row.
    Expected: it is excluded from inventory and cannot produce a zero-row
    salvage selector; node-27 `EXPLAIN` proves the correlated `LIMIT 1`
    presence probes use identity-leading indexes within the statement timeout
    and do not decorrelate into detail full scans/hash aggregates.
  - Input: forcing package URI whose source/cycle/model/basin disagrees with
    its DB row, whose manifest range does not contain the DB subject window,
    or a run root without an exactly row-bound input manifest / regular output.
    Expected: audit fails closed and does not publish a gate receipt.
  - Input: provider-qualified and legacy state URIs, plus a clone state whose
    URI aliases its provenance-declared source model.
    Expected: normal rows bind row and URI identity; the clone keeps its own
    `state_id` subject but shares the physical artifact coverage. An
    undeclared model alias or source/time drift blocks publication.
  - Input: missing archive root or absent canonical archive siblings.
    Expected: archive coverage is absent and classification continues via
    salvage/hot/gap; no configuration crash.
  - Input: archive/salvage evidence with permission error, symlink,
    containment escape, malformed/oversized manifest, size mismatch,
    checksum mismatch, or duplicate exact selector.
    Expected: permission/I/O/unsafe/malformed/oversized/conflicting evidence
    is a blocker and no previous receipt is replaced. A fully readable
    product archive or salvage object whose declared size/checksum mismatches
    is known-invalid coverage: record the mismatch in subject evidence,
    treat that copy as absent, and safely continue to another coverage source
    or `pending-archive`/`gap`.
  - Input: verified salvage object whose selector exactly matches a
    forcing/run subject, and a near-match with different identity/window.
    Expected: exact match is `complete/db-export`; near-match is ignored for
    coverage and cannot satisfy or steal another subject.
  - Input: equal-window distinct subjects, duplicate/omitted subject,
    selector without a matching gap, gap without its selector, or empty
    inventory.
    Expected: distinct stable subjects publish independently; all invalid
    set shapes fail before schema-validated atomic replace.
  - Input: one repeatable-read snapshot with inverted metadata window, wrong
    computed coverage bounds, or audit time changed between subjects.
    Expected: inversion/bounds mismatch blocks publication; every selector
    uses the metadata window and every age decision uses the one captured
    audit time without a full detail rescan.
  - Input: manifest over 16 MiB, more than 10,000 salvage manifests, scan
    depth over eight, or more than 100,000 subjects.
    Expected: bounded audit fails non-zero without replacing the prior gate
    receipt.
  - Input: missing/relative receipt path, symlinked parent/target, atomic
    replace failure, or output schema/semantic validation failure.
    Expected: non-zero JSON diagnostic on stderr, old receipt byte-identical,
    and no readable temporary residue. A valid receipt is mode 0600 and
    atomically replaces the prior file.
  - Input: pinned completeness example plus a mixed forcing-gap/state-gap
    receipt.
    Expected: examples pass both JSON Schema and runtime set invariants; the
    forcing gap selector identity/window exactly equals its subject, while
    the state gap has no selector.
  - Input: production dependency install without dev extras.
    Expected: importing the audit module and runtime `jsonschema` validation
    succeeds.
  Node-27 oracle for #847 is limited to the read-only transaction/query plan,
  real forcing/run URI shapes, and a non-publishing temporary audit run.
  Current `state_snapshot` inventory is empty, so provider/legacy/clone state
  coverage is unit-test evidence only and MUST NOT be claimed as live proof.
- [ ] 2.2 Build the archive mover (`scripts/node27_product_archive.py` +
  `_once.sh`).
  Evidence floor: per-cycle `tar.zst` + `manifest.json` with sha256 (no row
  counts), same-volume staging + atomic rename only after re-read checksum
  verification, verify-before-delete, quarantine of unverified final-path
  residue, candidate eligibility = cycle age older than
  `NHMS_ARCHIVE_MIN_AGE_DAYS` (default 45), source lanes `forcing/`, `runs/`,
  and `states/`, flock, per-tick cycle bound, dry-run default, JSON receipts.
  Test rows:
  - Input: aged fixture cycle, enforce mode.
    Expected: verified tarball + manifest at the final path; source removed
    only after verification passes.
  - Input: an aged source-less `states/<model>/<valid-time>/...` fixture.
    Expected: archived under the collision-disjoint
    `states/legacy-unqualified/...` path with no provider inference and the
    same verify-before-delete guarantees.
  - Input: tarball sha256 mismatch during verification.
    Expected: source untouched; non-zero exit; failure recorded in receipt.
  - Input: re-run over a cycle with a verified existing object.
    Expected: skip recorded; no duplicate object.
  - Input: corrupt final-path object left by an interrupted run.
    Expected: quarantined and re-archived via fresh staging; quarantine in
    the receipt; source untouched until the replacement verifies.
  - Input: cycle younger than the minimum age.
    Expected: not selected as a candidate; remains in the hot object-store.
  - Input: more candidates than the per-tick bound.
    Expected: bound respected; deferred remainder listed in the receipt.
- [ ] 2.3 Add systemd units + env + governance registration for the mover
  and the recurring audit.
  Evidence floor: `infra/systemd/nhms-node27-product-archive.{service,timer}`
  and `nhms-node27-storage-inventory-audit.{service,timer}`;
  `infra/env/node27-product-archive.example` (incl. `NHMS_ARCHIVE_ROOT`,
  `NHMS_ARCHIVE_MIN_AGE_DAYS`, per-tick bound, free-space watermarks) and
  `infra/env/node27-storage-inventory-audit.example`; all four units
  registered in the `scripts/node27_resource_governance.py` audited unit
  list; runbook section for operation and rollback; documented audit timer
  cadence shorter than the retention gate's receipt validity window so a
  fresh completeness receipt exists at every retention tick.
  Test rows:
  - Input: resource-governance audit run (systemctl mocked).
    Expected: receipt includes archive and inventory-audit service/timer
    states.
- [ ] 2.4 Extend `scripts/node27_resource_governance.py` capacity
  visibility and the mover's free-space refusal.
  Evidence floor: governance receipt reports archive root size and
  shared-volume free space; mover refuses enforce below the configured
  free-space threshold.
  Test rows:
  - Input: free space below the refuse threshold, enforce requested.
    Expected: mover refuses, sources untouched, receipt warning emitted.
- [ ] 2.5 node-27 live: first audit receipt + first enforce archive run.
  Evidence floor: committed schema-valid archive-completeness receipt whose
  salvage selector list covers the known pre-2026-06-16 forcing gap; first
  enforce archive receipt covering aged `forcing/` + `runs/` + `states/`
  cycles with ≥1 verified object per source lane present in rotation scope,
  0 checksum failures, and source removal only for verified objects; both
  receipts committed under runbook receipts.

## 3. One-time DB-export salvage (`db-export-salvage`)

- [ ] 3.1 Build the salvage exporter
  (`scripts/node27_db_export_salvage.py`).
  Evidence floor: consumes the archive-completeness receipt's salvage
  selector list verbatim (hardcoded date lists refused); `COPY` per selector
  to `csv.zst` + manifest (`provenance: db-export`, exact selector, exported
  row count, column list, per-object sha256, source database identity) under
  `NHMS_ARCHIVE_ROOT`; dry-run default; idempotent re-runs skip verified
  existing objects; never deletes DB rows or products; unit tests.
  Test rows:
  - Input: receipt with two selectors, one already exported and verified.
    Expected: only the missing selector is exported.
  - Input: completed export for a selector.
    Expected: manifest row count equals the DB row count for that selector
    at export time.
  - Input: invocation with a hardcoded selector list and no receipt.
    Expected: refused; the receipt is the only scope source.
- [ ] 3.2 Document the manual `COPY FROM` restore procedure for `db-export`
  objects.
  Evidence floor: archive runbook section documents the checksum pre-check +
  manual `COPY FROM` sequence as the **only** restore path for salvage
  objects, states that no automated restore lane exists (ADR 0002 decision
  3), and is cross-linked from the retention runbook section (6.2).
- [ ] 3.3 node-27 live: execute salvage for the audit-derived DB-only
  windows.
  Evidence floor: committed salvage receipt covering every audit-emitted
  salvage selector / salvageable forcing or river `gap` from the live
  completeness receipt (expected: forcing before 2026-06-16);
  per-selector manifest row count equals the DB row count at export time; a
  follow-up audit run marks those salvageable subjects `complete` via
  verified salvage objects and emits an empty salvage list. Any
  non-salvageable state gap remains `gap` and keeps retention fail-closed
  until product coverage is restored.

## 4. Hypertable compression (`hypertable-compression`)

- [ ] 4.1 Migration `000043`: compression settings for both hypertables.
  Evidence floor: `ALTER TABLE ... SET (timescaledb.compress,
  compress_segmentby, compress_orderby)` per design D3; no policy job.
  Verification on the real-DB oracle:
  `timescaledb_information.hypertables.compression_enabled = true` for both
  tables, and `timescaledb_information.compression_settings` rows match the
  configured segmentby (`segmentby_column_index` set) and orderby
  (`orderby_column_index` set) columns — on TimescaleDB 2.10 the
  `hypertables` view does not expose segmentby/orderby.
  Test rows:
  - Input: migration applied on the node-27 real-DB oracle.
    Expected: both catalog assertions above pass for both hypertables.
- [ ] 4.2 Build the compression runner
  (`scripts/node27_timeseries_compression.py` + `_once.sh`).
  Evidence floor: compresses only chunks whose `range_end` is older than the
  configurable lag (default 7d), never the active chunk; dry-run default +
  explicit enforce flag; flock; per-tick chunk bound; receipts with
  per-chunk and per-table before/after bytes; unit tests.
  Test rows:
  - Input: chunk with `range_end` inside the lag window.
    Expected: skipped.
  - Input: more eligible chunks than the per-tick bound.
    Expected: bound respected; deferred remainder listed in the receipt.
  - Input: run without the enforce flag, or with the flock already held.
    Expected: nothing compressed; dry-run candidate list or lock-skip
    receipt emitted.
- [ ] 4.3 Add the fail-closed compressed-chunk write guard to all three
  hypertable write paths.
  Evidence floor: one shared pre-write helper detects compressed-chunk
  targets and aborts before any row mutation with an error naming the chunk
  and referencing the decompress runbook section (silent skips/partial
  writes forbidden); wired into all three upsert sites —
  `workers/output_parser/parser.py` (`hydro.river_timeseries`),
  `workers/forcing_producer/store.py` and
  `packages/common/forcing_domain_handoff_apply.py`
  (`met.forcing_station_timeseries`); decompress procedure runbook section
  written; one guard test per write path.
  Test rows:
  - Input: reingest targeting a compressed chunk through each of the three
    write paths.
    Expected: abort before any row mutation; error names the chunk and the
    runbook procedure.
  - Input: write targeting only uncompressed chunks.
    Expected: behavior unchanged.
- [ ] 4.4 Add compression systemd units + env + governance registration.
  Evidence floor:
  `infra/systemd/nhms-node27-timeseries-compression.{service,timer}` +
  `infra/env/node27-timeseries-compression.example` (lag, per-tick bound,
  enforce flag); units registered in the resource-governance audit unit
  list.
  Test rows:
  - Input: resource-governance audit run (systemctl mocked).
    Expected: receipt includes compression service/timer states.
- [ ] 4.5 node-27 live: apply the migration and run the initial
  terminal-chunk compression.
  Evidence floor: committed receipt with per-table before/after totals
  (acceptance: combined on-disk size of the two hypertables strictly
  reduced; compressed-chunk count > 0) and representative curve/MVT query
  timings before/after (acceptance: no representative query regresses past
  the threshold documented in the receipt).

## 5. Archive rebuild drill (`archive-rebuild-drill`)

- [ ] 5.1 Build the drill script
  (`scripts/node27_archive_rebuild_drill.py`).
  Evidence floor: restores sample archived cycles and reingests them via the
  existing ingest code path configured to write an **isolated staging
  schema** (same DDL, no compression; production hypertables never written;
  staging reset per run and its identity recorded in the receipt); product
  parity compares per-(run, variable) staging counts against expected counts
  parsed from the restored files (archive manifests carry no row counts);
  `db-export` objects are verified by sha256 + decompressed per-selector row
  count against the salvage manifest (no reingest); the receipt declares the
  validated (source, window) tuples and PASS/FAIL per spec; unit tests with
  fixture archives and manifests.
  Test rows:
  - Input: fixture archive cycle with known file contents.
    Expected: PASS receipt naming cycles/selectors/counts and the staging
    schema identity.
  - Input: truncated tarball or mutilated restored file.
    Expected: FAIL with per-item diff; non-zero exit.
  - Input: fixture `db-export` object whose manifest says N rows but whose
    file holds N-1.
    Expected: FAIL.
  - Input: production tables pre-seeded with rows for the drilled window.
    Expected: parity judged only on staging counts (pre-existing production
    rows cannot produce a vacuous PASS); production row counts unchanged.
  - Input: production chunks for the drilled window compressed.
    Expected: drill completes without decompressing or writing any
    production chunk.
- [ ] 5.2 node-27 live: execute the drill.
  Evidence floor: committed PASS receipt covering at least one `forcing/`
  cycle, one `runs/` cycle, and one `db-export` salvage object, with
  declared (source, window) tuples satisfying the coverage rule for the
  planned 30-day drop window; zero count mismatches; production hypertable
  row counts unchanged by the drill. This unlocks 6.3.

## 6. Gated DB retention (`timeseries-db-retention`)

- [ ] 6.1 Build the retention runner
  (`scripts/node27_timeseries_retention.py` + `_once.sh`).
  Evidence floor: `drop_chunks` older than 30d targeting exactly the two
  detail hypertables; hard gate consumes exactly two receipts — a fresh
  archive-completeness receipt with every window in the drop window
  `complete`, and a drill PASS receipt whose declared coverage includes the
  drop window (compression state is never consulted); dry-run default;
  flock; per-tick chunk bound; statement timeout; refusal receipts with
  reasons; unit tests for gate refusal and bound deferral.
  Test rows:
  - Input: missing or stale completeness receipt, or one carrying
    `pending-archive`/`gap` inside the drop window.
    Expected: refusal, non-zero exit, reason in the receipt.
  - Input: drill receipt FAIL, stale, or with coverage tuples not including
    the drop window.
    Expected: refusal with the coverage shortfall recorded.
  - Input: both gate receipts fresh and covering the drop window.
    Expected: eligible chunks dropped up to the per-tick bound; deferred
    remainder and salvage-backed windows recorded in the receipt.
  - Input: metadata/coverage table row counts before vs after enforce.
    Expected: unchanged.
- [ ] 6.2 Add retention systemd units + env + governance registration.
  Evidence floor:
  `infra/systemd/nhms-node27-timeseries-retention.{service,timer}` +
  `infra/env/node27-timeseries-retention.example` (window, bounds, gate
  receipt validity windows); registered in the governance audit unit list;
  runbook section covering metadata-table exemptions and linking the manual
  salvage restore procedure (3.2).
  Test rows:
  - Input: resource-governance audit run (systemctl mocked).
    Expected: receipt includes retention service/timer states.
- [ ] 6.3 node-27 live: dry-run receipt review, then first enforce run.
  Evidence floor: committed dry-run receipt reviewed first; first enforce
  gated on 5.2's drill PASS plus a fresh archive-completeness receipt from
  the recurring audit (2.3) — compression (4.5) is not a gate; committed
  enforce receipt records dropped chunks and freed bytes; metadata/coverage
  tables unchanged (row-count check embedded in the receipt); DB size delta
  reported. Steady state: timer-driven enforce keeps passing gates via
  recurring audit receipts; a drill re-run is required whenever the drill
  receipt exceeds its validity window or archive tooling/format changes.

## 7. Docs and verification floor

- [ ] 7.1 Cross-link ADR 0002, the new runbook sections (archive operation
  and rollback, decompress procedure, manual salvage restore), and
  `docs/governance/DOC_STATUS.md`.
  Evidence floor: `openspec validate tier-node27-timeseries-storage --strict
  --no-interactive`, `uv run ruff check .`, and targeted pytest for the new
  scripts pass as the change-level verification floor; runbook cross-links
  resolve.
