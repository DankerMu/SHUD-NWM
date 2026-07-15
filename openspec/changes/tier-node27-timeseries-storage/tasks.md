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

- [x] 2.1 Build the inventory audit
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
  satisfy start <= end. Forcing/run `cycle_time` must resolve to an exact UTC
  hour (`minute == second == microsecond == 0`); a non-hour metadata row is a
  blocker and must never be truncated into another cycle's archive identity.

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
  artifact. Legacy clone origin comparison canonicalizes both `NULL` and the
  existing empty-string representation to the same `legacy-unqualified`
  source while keeping every provider distinct. Any malformed URI,
  containment escape, symlink, permission or
  I/O error is an audit blocker, not ordinary absence.

  Forcing archive basin identity comes from authoritative
  `core.model_instance.basin_version_id`, not an arbitrary detail row; the
  detail LATERAL remains a boolean presence probe. Clone provenance is bound
  in the same repeatable-read snapshot by self-joining
  `cloned_from_state_id`: the origin must exist and its model/source/
  `valid_time`/`state_uri`/checksum must match the clone's declared shared
  artifact, while `cloned_from_model_id` names that origin model and the
  fingerprint is canonical 64-character lowercase hex.

  Manifest JSON reads are capped at 16 MiB. A product archive verifies only
  when regular non-symlink manifest/tarball siblings are contained under the
  archive root, the bounded manifest is
  schema-valid and passes shared semantic identity/path binding, and actual
  tarball size + streaming sha256 match the manifest. A missing archive root
  or missing canonical siblings means no archive coverage; unreadable or
  malformed existing evidence yields a `blocked` terminal receipt. Verified salvage
  coverage is discovered by a bounded, symlink-safe scan of
  `<archive-root>/db-export/**/manifest.json`; every manifest and referenced
  object must pass the pinned schema, containment, size and sha256 checks,
  and duplicate/conflicting exact selectors yield `blocked`. Discovery is
  capped at 10,000 manifests, 100,000 total namespace entries and eight
  directory levels beneath `db-export/`; exceeding any bound is a blocker.
  The salvage walk holds one descriptor-bound `db-export` tree for directory
  enumeration, entry stat, child open, manifest bounded read and referenced
  object streaming hash. It never stores a manifest `Path` and reopens it
  after traversal, so a real-directory rename/swap cannot mix namespaces or
  bypass the global entry cap.
  Inventory is capped at 100,000 subjects, and exceeding the cap yields a
  `blocked` terminal receipt.
  Run-output discovery is likewise capped at 10,000 entries and eight
  directory levels per run; it still inspects every bounded sibling so a
  valid file cannot hide a later unsafe entry. Enumeration, entry stat and
  child-directory opens for the complete traversal stay bound to the same
  held directory-FD tree; a pathname directory replacement cannot substitute
  different siblings between list and stat.

  All evidence access and output publication is descriptor-bound: walk from
  a trusted root directory FD with `openat`-style `dir_fd` calls and
  `O_NOFOLLOW`, distinguish `ENOENT` on an already verified directory chain
  from symlink/non-directory/permission/I/O blockers, and perform fstat/read/
  parse/size/sha256 on the same opened file descriptor. Missing leaves behind
  an existing intermediate symlink are blockers. JSON parsing and checksum
  verification MUST consume the same bytes/inode, and output temp creation +
  atomic replace MUST stay anchored to the pinned receipt-parent FD.
  For a state subject (including clones), product-archive coverage additionally
  requires exactly one manifest `files` entry whose path is the physical
  `state_uri` relative to its strict provider/legacy state root and whose
  sha256 equals the DB/origin state checksum. Missing, duplicate, wrong-path
  or wrong-checksum state members yield `blocked`; tarball identity alone is
  never state preservation proof.

  Classification precedence is fixed: verified product archive;
  verified exact `db-export` selector (forcing/runs only); hot object-store
  coverage before the minimum-age cutoff; aged hot-only pending archive;
  otherwise gap. Every salvageable forcing/run gap has exactly one selector
  identical in identity/window to its subject, while state gaps have none.
  Every terminal receipt pins `schema_version=1.1` and validates against
  exactly one branch. Success receipts are deterministically ordered, contain
  every inventoried subject exactly once, contain no duplicate subject, and
  prove both the complete/incomplete aggregate and forcing/run gap-selector
  bijection. Empty inventory is `blocked/EMPTY_INVENTORY`; any config/evidence
  blocker reached before publication starts publishes a current `blocked`
  receipt instead of leaving a previous success in place. The stable output
  interface is required absolute
  `--receipt-path` or `NODE27_STORAGE_INVENTORY_RECEIPT_PATH`; its existing
  parent and every parent component must resolve to a non-symlink directory.
  Publication writes a mode-0600 same-directory exclusive temporary file,
  flushes + fsyncs it, uses atomic `os.replace`, fsyncs the directory, and
  re-verifies after replacement that the pinned parent FD still names the
  configured parent path. Once the single publication attempt begins, any
  write/replace/fsync/parent-identity error is stderr-only, MUST NOT trigger a
  second publish, and MUST NOT report `published`; temporary residue is removed
  on pre-replace failure. The receipt parent is
  an operator-controlled, non-rotating namespace during publication because
  no pathname protocol can linearize against a privileged rename after its
  final identity check. Publication errors before replace preserve the old
  receipt byte-for-byte; errors discovered after replace leave target content
  unknown and are reported as indeterminate publication, never as successful
  publication or preservation. Because
  replace may already have made a fully validated, file-fsynced payload
  visible, #855 independently evaluates the currently configured receipt by
  its own no-follow/schema/freshness/coverage rules; neither producer exit
  status nor a sidecar/systemd marker becomes a third gate. These strict post-replace checks are an explicit receipt-only
  mode of the shared atomic-write helper; its default behavior remains
  compatible for existing non-receipt callers that have not adopted the
  indeterminate/possibly-committed error model. Publication-failure diagnostics
  are emitted as JSON to stderr, never as a second replacement receipt. Runtime schema
  validation uses `jsonschema` as a direct production dependency, not a dev
  transitive dependency.
  Archive minimum age is parsed without truthiness fallback and validated
  against the shared 30-day DB retention invariant; explicit or environment
  values below 30 (including zero) fail before DB/filesystem audit work.
  Every readable size/checksum mismatch discovered for a subject is appended
  to its evidence before coverage precedence is selected, so a valid fallback
  copy never erases evidence of a corrupt sibling. This includes readable hot
  forcing manifest/member and state-file checksum mismatches: they are absent
  coverage with retained evidence, while malformed identity, unsafe type,
  permission and I/O failures remain blockers.
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
  - Input: a forcing version whose detail rows contain a wrong/multiple basin
    while its model has one authoritative basin.
    Expected: inventory identity uses `core.model_instance.basin_version_id`;
    the presence probe never projects an arbitrary detail basin and remains
    index-only on node-27.
  - Input: forcing package URI whose source/cycle/model/basin disagrees with
    its DB row, whose manifest range does not contain the DB subject window,
    or a run root without an exactly row-bound input manifest / regular output.
    Expected: audit fails closed and publishes a stable `blocked` receipt.
  - Input: provider-qualified and legacy state URIs, plus a clone state whose
    URI aliases its provenance-declared source model.
    Expected: normal rows bind row and URI identity; the clone keeps its own
    `state_id` subject but shares the physical artifact coverage. An
    undeclared model alias or source/time drift yields `blocked`.
  - Input: clone provenance naming a missing origin state, invalid fingerprint,
    or origin model/source/time/URI/checksum drift.
    Expected: the repeatable-read self-join yields `blocked`; a valid clone
    retains its own `state_id` subject while sharing only the exact origin
    artifact coverage.
  - Input: provider, legacy, or clone state archive with a valid tarball but
    no unique manifest member matching the physical state URI and DB checksum.
    Expected: invalid evidence yields `blocked`; a unique correct member yields verified
    `complete/product-archive` for that stable state subject.
  - Input: missing archive root or absent canonical archive siblings.
    Expected: archive coverage is absent and classification continues via
    salvage/hot/gap; no configuration crash.
  - Input: archive/salvage evidence with permission error, symlink,
    containment escape, malformed/oversized manifest, size mismatch,
    checksum mismatch, or duplicate exact selector.
    Expected: permission/I/O/unsafe/malformed/oversized/conflicting evidence
    publishes `blocked` before the publication phase starts. A fully readable
    product archive or salvage object whose declared size/checksum mismatches
    is known-invalid coverage: record the mismatch in subject evidence,
    treat that copy as absent, and safely continue to another coverage source
    or `pending-archive`/`gap`.
  - Input: an intermediate evidence component swapped to a symlink before
    open, a missing leaf behind a pre-existing symlink, or a regular file
    replaced between path validation and read/hash.
    Expected: root-anchored no-follow FD access blocks every escape/race;
    parse, size and checksum come from one inode, and a current `blocked`
    receipt replaces stale success when the destination is safe.
  - Input: verified salvage object whose selector exactly matches a
    forcing/run subject, and a near-match with different identity/window.
    Expected: exact match is `complete/db-export`; near-match is ignored for
    coverage and cannot satisfy or steal another subject.
  - Input: equal-window distinct subjects, duplicate/omitted subject,
    selector without a matching gap, gap without its selector, or empty
    inventory.
    Expected: distinct stable subjects publish independently; duplicate/
    omitted/bijection-invalid sets publish `blocked`, and empty inventory
    publishes `blocked/EMPTY_INVENTORY`.
  - Input: one repeatable-read snapshot with inverted metadata window, wrong
    computed coverage bounds, or audit time changed between subjects.
    Expected: inversion/bounds mismatch publishes `blocked`; every selector
    uses the metadata window and every age decision uses the one captured
    audit time without a full detail rescan.
  - Input: manifest over 16 MiB, more than 10,000 salvage manifests or
    100,000 total salvage namespace entries, scan depth over eight, more than
    100,000 subjects, or a run output tree over 10,000 entries/eight levels.
    Expected: bounded audit publishes `blocked` and exits non-zero when the
    bootstrapped destination is safe.
  - Input: missing-value/duplicate/ambiguous/missing/relative receipt path or
    symlinked parent/target. Expected: unwriteable-exception JSON stderr and no
    publication claim. Input: output schema/semantic validation failure before
    publication with a safe destination. Expected: `blocked` or
    `indeterminate` according to error class. Input: atomic publication failure
    after the one attempt begins. Expected: stderr-only, no second publish, old
    bytes preserved pre-replace, and no readable temporary residue. A valid
    receipt is mode 0600 and atomically replaces the prior file.
  - Input: the receipt parent is renamed/replaced between the last pre-replace
    identity check and `os.replace`, or directory fsync returns `EIO`.
    Expected: post-replace parent verification/fsync blocks `published` and
    reports indeterminate publication; no write follows the replacement
    parent pathname. The configured target may already contain the new
    file-fsynced receipt, and #855 later accepts or rejects only the currently
    readable two receipt contents, not this producer's exit status.
  - Input: forcing/run metadata has a non-zero UTC minute, second or
    microsecond in `cycle_time`.
    Expected: inventory blocks before archive lookup instead of truncating to
    a neighboring canonical hourly identity.
  - Input: provider, legacy and clone state archive fixtures whose manifest
    member paths are independent literal expectations, exercised through
    `run_audit` with a real object-store prefix.
    Expected: the production prefix wiring yields product-archive coverage;
    a wrong prefix or wrong literal member fails closed.
  - Input: run-output directory is atomically replaced after enumeration but
    before a child stat, with an unsafe sibling in the originally opened tree.
    Expected: the held directory FD still observes and rejects the original
    unsafe sibling; the replacement tree cannot manufacture hot coverage.
  - Input: readable forcing/state hot checksum mismatch together with valid
    product-archive or salvage fallback, and the same mismatch without a
    fallback.
    Expected: fallback precedence continues safely and every receipt verdict
    retains the mismatch evidence; no fallback yields gap rather than an
    audit blocker.
  - Input: a legacy clone/origin pair stores equivalent source-less identity
    as `NULL` versus empty string in either direction.
    Expected: provenance validation canonicalizes both to
    `legacy-unqualified`; provider-versus-legacy remains a drift blocker.
  - Input: `db-export` is replaced after directory enumeration but before
    child stat, manifest read or referenced-object hash; the replacement has
    extra entries beyond the configured global cap.
    Expected: one held FD tree supplies every operation, so evidence cannot
    mix namespaces and replacement entries cannot bypass cap accounting.
  - Input: directory fsync fails after replace for the inventory receipt and
    for an existing non-receipt atomic-write caller.
    Expected: the receipt explicitly opts into and reports indeterminate;
    the legacy caller retains the shared helper's prior default error model.
  - Input: pinned completeness example plus a mixed forcing-gap/state-gap
    receipt.
    Expected: examples pass both JSON Schema and runtime set invariants; the
    forcing gap selector identity/window exactly equals its subject, while
    the state gap has no selector.
  - Input: production dependency install without dev extras.
    Expected: importing the audit module and runtime `jsonschema` validation
    succeeds.
  - Input: archive minimum age 20 or explicit CLI zero, with/without an env
    fallback; and legal CLI 30 overriding an invalid env value.
    Expected: 20/zero always fail without fallback, while explicit legal CLI
    wins and shares the foundation retention-age invariant.
  - Input: corrupt product archive with valid exact salvage, and corrupt
    exact salvage with valid product archive.
    Expected: verdict remains complete through the valid copy, while evidence
    retains the corrupt sibling's size/checksum mismatch in both directions.
  Implementation evidence (#847): local focused audit suite 92 passed and
  the broader audit/storage/schema/object-store/state/journal regression set
  627 passed; ruff, strict OpenSpec validation, lock check and diff check
  passed. On node-27 at exact candidate `d3e74f5a`, the read-only forcing and
  run plans completed in 36.364 ms and 26.274 ms with identity-leading
  Timescale chunk `Index Only Scan` nodes and no detail full scan/hash
  aggregate. The isolated non-production audit emitted a schema/semantic
  valid mode-0600 receipt for 1,585 subjects (733 forcing, 852 runs): 1,357
  complete hot-object-store, 228 gaps, and exactly 228 salvage selectors.
  Node-27 oracle for #847 is limited to the read-only transaction/query plan,
  real forcing/run URI shapes, and a non-publishing temporary audit run.
  Current `state_snapshot` inventory is empty, so provider/legacy/clone state
  coverage is unit-test evidence only and MUST NOT be claimed as live proof.
- [x] 2.2 Build the archive mover (`scripts/node27_product_archive.py` +
  `_once.sh`).
  Evidence floor: per-cycle `tar.zst` + `manifest.json` with sha256 (no row
  counts), same-volume staging + atomic rename only after re-read checksum
  verification, verify-before-delete, quarantine of unverified final-path
  residue, candidate eligibility = cycle age older than
  `NHMS_ARCHIVE_MIN_AGE_DAYS` (default 45), source lanes `forcing/`, `runs/`,
  and `states/`, flock, per-tick cycle bound, dry-run default, JSON receipts.
  This issue also pins `schemas/product_archive_receipt.schema.json` plus
  positive/negative examples because no #846 schema covers mover operations.

  The filesystem-only discovery contract is lane-specific and does not infer
  identity from convenient names alone:
  - forcing candidates are exact leaf packages
    `forcing/<source>/<cycle>/<basin_version_id>/<model_id>/`; the bounded
    `forcing_package.json` must bind that source/cycle/basin/model identity.
    Sibling basin/model leaves in the same source cycle are independent
    archive/delete units.
  - run candidates are whole `runs/<run_id>/` trees. Source/cycle/model/basin
    identity comes from bounded `input/manifest.json`, must bind the directory
    `run_id`, and is never parsed solely from the historical run-id spelling.
    Top-level run/source/cycle/window plus `model.model_id`,
    `model.basin_version_id` and `outputs.{run_manifest_uri,output_uri}` are
    authoritative as in #847; any duplicated `identity.*` run/source/cycle/
    model/basin/window value must agree rather than silently override them.
    Output URIs bind the configured canonical `OBJECT_STORE_PREFIX`, including
    scheme, bucket and optional key prefix; wrong authority/prefix,
    query/fragment, encoded traversal, backslash or non-S3 scheme fails closed.
    The run output directory must exist and the bounded no-follow snapshot must
    contain at least one regular product.
  - forcing producer completeness requires safe `forcing_version_id`, a
    non-empty unique `files` list, canonical member URIs bound inside the exact
    leaf, and valid sha256 values matching the same pinned snapshot. The leaf
    is either legacy (manifest + declared products) or additionally contains
    the complete fixed five-file domain-handoff/version bundle; partial bundle,
    unknown extra, identity/contract/version/URI/checksum/package-digest or
    lineage drift fails closed. The outer archive manifest retains the stable
    producer subject, producer-manifest digest/path, authoritative window and
    model/basin identity; #847 archive coverage binds those values and the
    archived producer-manifest member digest to its DB subject and verifies the
    actual decompressed member bijection before declaring coverage.
  - state candidates are whole physical valid-time trees. Provider-qualified
    layout is `states/<source>/<physical_model>/<valid_time>/...`; legacy
    layout is `states/<physical_model>/<valid_time>/...` and maps only to
    `legacy-unqualified`. Provider recognition uses the canonical source
    allowlist and ambiguous/unknown layouts fail closed. Clone target identity
    is never synthesized: DB clone subjects share the one physical origin
    archive, and this mover performs no DB access.

  One UTC `now` is captured. Eligibility is strict
  `eligibility_end < now - minimum_age`; equality is not eligible. Forcing
  and runs use their authoritative non-inverted manifest `end_time`, matching
  #847 receipt/DB/display hot-window age; states use point `valid_time`.
  Canonical archive identity/order remains cycle-time based, while candidate
  and receipt also bind eligibility end. Explicit CLI
  zero/invalid age never truthiness-falls back. Candidate order is stable
  `(cycle_time,lane,canonical identity)`; at most the configured positive
  bound is selected and every remaining eligible candidate is recorded as
  deferred. Discovery is capped at 100,000 candidates; each candidate tree is
  capped at 100,000 entries, depth 16, a 16 MiB source manifest, 256 GiB per
  file and 1 TiB total source payload bytes. Staged compressed tar size is
  capped at 1 TiB and streamed uncompressed tar bytes (payload plus headers/
  padding) at 2 TiB. The compressor/decompressor timeout
  is 3,600 seconds and captured stderr is capped at 64 KiB. Overflow,
  malformed/unreadable/permission evidence, symlink/hardlink/device/FIFO,
  duplicate/path-traversing member or identity drift is a deterministic
  `discovery_failure` keyed by lane hint + safe root-relative locator, never a
  fabricated canonical identity. Discovery failures count toward the global
  discovery cap and overall non-zero outcome but not the valid eligible
  selected/deferred partition or per-tick processing bound.
  Hot forcing/run leaves perform only bounded manifest identity/window and
  declaration/URI-shape validation, then skip before any full tree hash,
  bundle completeness or run-output scan. Identity, inverted-window and wrong
  configured-prefix evidence still fail even while hot.

  Source traversal is descriptor-bound and no-follow. Every opened descendant
  must remain on the pinned source-root device **and Linux mount ID**, rejecting
  cross-device and same-filesystem bind mounts. Mount ID is read from the
  opened FD (for example `/proc/self/fdinfo/<fd>` `mnt_id`/`statx`), never from
  a pathname; inability to prove it in production is a fail-closed blocker.
  The manifest file list
  is deterministic, root-relative and contains exactly every regular source
  file with size + sha256. Traversal holds only the bounded directory-FD stack
  plus one regular-file FD at a time: each file is opened no-follow relative
  to the pinned tree, fstat-bound before/after, and the same byte stream feeds
  tar + sha256; a complete second tree scan detects later drift.
  the staged `tar.zst` is then decompressed and re-read to prove the exact
  regular-member set, paths, sizes and sha256 values match the manifest, in
  addition to re-reading tarball size + sha256. Production compression uses a
  configured absolute regular non-symlink executable (node-27 default
  `/usr/bin/zstd`) invoked with fixed argv, `shell=False`, bounded timeout and
  stderr; bare/relative paths or absence are preflight blockers. Tests may
  inject a protocol-compatible executable.

  Staging is a unique directory below the archive root on the same pinned
  device and mount ID; every opened archive descendant must match both.
  Its verified `archive.tar.zst` + `manifest.json` pair is published as one
  leaf-directory dirfd-bound no-replace atomic rename and re-verified at
  the final path before source retirement. A final leaf counts as existing
  only if it contains exactly the expected pair and passes schema, shared
  semantic path/identity binding, internal-member verification and tarball
  verification. A corrupt/partial/unexpected final leaf is atomically moved
  whole into a same-device quarantine namespace before fresh staging; dry-run
  records `would-quarantine` and mutates neither namespace. A verified final
  with source content identical to its manifest is idempotent and may retire
  that source in enforce mode; a verified final whose still-present source
  differs is a conflict that preserves both and exits non-zero. Publication
  fsyncs both staged files and staging directory before rename, then every
  created ancestor and affected parent. Quarantine/tombstone cross-directory
  renames fsync both parents; recursive tombstone removal fsyncs its parent.
  A raced destination is never overwritten; if no native no-replace rename is
  available the mutation fails closed.
  Existing-final verification is typed: only deterministic schema,
  identity/path, member-set, size or checksum invalidity is `corrupt` and may
  quarantine. Timeout, tool spawn/read/I/O or mount-proof failure is
  operational/indeterminate, preserves canonical final + source and never
  triggers quarantine. Conflict is a separate typed outcome, not string
  matching. A verified manifest compares source members by unique
  path -> (size, sha256), never by array input order.
  A corrupt final remains pinned through quarantine: the rename must target the
  exact guarded inode that failed verification, and a path replacement fails
  closed without moving or labelling the replacement. Decompressor non-zero,
  timeout, spawn, stream or stderr failure is operational even when tar parsing
  also fails. Bounded local PAX metadata may support deterministic long paths
  and large-file size fields, but extension-header size is checked before body
  streaming; global/Solaris PAX, GNU longname/longlink and unexpected PAX keys
  are rejected.
  Raw headers, local-PAX count, consecutive local-PAX structure and cumulative
  PAX bytes have explicit expected-member-derived limits.
  Both sidecar files remain namespace-bound to the exact descriptors used for
  final reads; pre-retirement guards recheck the exact tar+manifest pair. The
  producer block is semantically self-bound to lane/identity/window/model/
  basin and its unique producer-manifest member digest, not merely schema-valid.
  A same-mount mover-owned retirement guard durably references the exact
  verified tar+manifest inodes across every destructive source step. Canonical
  pair drift preserves that guard as truthful residue and is indeterminate.
  Each destructive canonical-pair check re-proves the pinned archive root,
  leaf and child device + Linux mount ID in addition to inode signatures.
  The same bounded tar pass parses the embedded producer manifest and binds its
  identity/window/model/basin/subject/object URIs/checksums to the outer
  identity and configured object-store prefix.

  Immediately before retirement the still-pinned source root and complete
  tree must equal the archived preimage (inode/type/path/size/mtime and
  sha256); observed late writes, new/deleted entries or root swaps block
  deletion. Aged manifest-complete product trees are an immutable-producer
  precondition: no filesystem protocol can prevent a producer from writing
  through an already-open FD after rename. The mover therefore revalidates
  the tombstone once more before unlink; detected drift preserves it and
  reports a producer-contract violation rather than claiming universal
  protection from post-check open-FD writes. The
  source leaf is first atomically renamed, via held parent FDs on the same
  object-store device, into a unique delete tombstone. Only that verified
  inode tree is recursively unlinked with no-follow descriptor operations;
  a replacement created at the original path is never followed or deleted.
  Any stage/final/quarantine/source rename `EXDEV`, fsync or observed namespace
  identity failure is non-zero/indeterminate and never reports archived.
  Failures before tombstone rename preserve the original source path; after
  rename/unlink begins, failure may leave a complete or partial tombstone and
  no original pathname, which must be reported precisely rather than falsely
  claiming `source untouched` or automatic rollback.
  Before any tombstone child unlink, the still-pinned final archive pair is
  completely re-verified (manifest + tar exact members/checksums), and the
  tombstone is compared to the archived preimage. Recursive removal is driven
  by the exact expected path/inode/signature allowlist, not by deleting every
  newly enumerated name; an extra/missing/drifted file or directory preserves
  residue and fails non-zero.
  Before unlink or directory removal, every child is no-replace renamed into a
  same-mount mover-exclusive claim namespace and its claimed inode/signature is
  compared to the allowlist. A same-name replacement before claim is preserved
  as residue; directories recursively apply the same rule. Post-rename fsync
  uncertainty reports the real destination residue and removes stale staging
  locators from the receipt.

  The Python entrypoint itself owns a non-blocking flock before discovery or
  mutation, so direct invocation cannot bypass single-instance behavior; the
  wrapper validates its mode-0600 env file/absolute paths/tool availability
  then invokes that entrypoint. The lock file is safe coordination metadata
  at an absolute path: every parent is opened from a trusted dirfd with
  no-follow, an existing target is opened without truncation and fstat-bound
  as mode-0600 regular, and first creation uses exclusive no-follow open plus
  parent fsync. Only the lock holder may publish the shared stable receipt. A
  contender emits one structured JSON skip diagnostic to stderr and does not
  touch that receipt. Dry-run is the default and
  `--enforce` is the sole mutation opt-in. In dry-run the lock metadata and
  configured atomic mode-0600 receipt are the only permitted writes. Receipt
  parents/target/temp use the #847 dirfd/no-follow contract: absolute trusted
  parent, exclusive temp, file fsync, atomic replace, mandatory directory
  fsync + post-replace parent identity check, pre-replace failure preserves
  the old receipt and post-replace uncertainty is indeterminate/non-zero. The
  receipt validates against `product_archive_receipt.schema.json` and records
  one captured now/cutoff, mode/bound,
  deterministic candidates/selected/deferred, one terminal outcome per
  selected identity, ordered side events, disjoint discovery failures, byte
  totals and stable identities/paths/reasons. Legacy skipped/quarantined
  action arrays are not alternate terminal representations.
  Runtime set invariants distinguish validated work from the lightweight queue:
  candidates = selected(validated) ∪ deferred(`pending-validation`) with no
  duplicates/omissions. Selected entries have exact non-negative source bytes;
  deferred entries do not claim source bytes or manifest completeness. Full
  validation attempts (successes plus validation failures) never exceed the
  tick bound. Every selected identity has exactly one terminal outcome (`planned`, `archived`,
  `retired-from-existing`, `failed`, or `indeterminate`) plus zero or more
  ordered side events such as `quarantined`; discovery failures remain a
  disjoint locator-keyed collection. Bytes are non-negative, ordering is
  deterministic and overall outcome matches all terminal/discovery failures.
  Enforce may continue across bounded independent failures but exits non-zero
  when any candidate failed; temporary/tombstone residue is reported.
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
    Expected: source-driven discovery sees an identical present source as
    terminal `planned` + would-retire detail in dry-run or
    `retired-from-existing` in enforce; no duplicate object. Archive-only
    identities with no source are outside mover discovery and produce no
    candidate/action (the inventory audit verifies them).
  - Input: corrupt final-path object left by an interrupted run.
    Expected: quarantined and re-archived via fresh staging; quarantine in
    the receipt; source untouched until the replacement verifies.
  - Input: cycle younger than the minimum age.
    Expected: not selected as a candidate; remains in the hot object-store.
  - Input: forcing/run cycle is old but authoritative end_time is equal to or
    newer than cutoff, then end_time becomes older.
    Expected: first remains hot; only second is eligible. Missing/inverted
    windows fail discovery; state eligibility remains valid-time point.
  - Input: more candidates than the per-tick bound.
    Expected: lightweight eligible order is stable; no more than the bound are
    fully scanned/hashed. Successful validations are selected, validation
    failures consume attempts and remain locator failures, and the untouched
    remainder is `pending-validation` deferred without fabricated source bytes.
  - Input: one forcing source cycle with two basin/model leaves, one valid and
    one malformed/unreadable.
    Expected: only the verified leaf can publish/retire; the shared cycle root
    and failing sibling remain; the malformed leaf is a locator-keyed
    discovery failure and does not consume the valid processing bound.
  - Input: a flat run directory whose name resembles one source/cycle but
    whose `input/manifest.json` declares another identity.
    Expected: identity drift blocks; run-id spelling is not authoritative;
    conflicting duplicated `identity.*` fields also block.
  - Input: run output URI has wrong bucket/prefix, query/fragment, encoded
    traversal, backslash or unsupported scheme.
    Expected: strict configured object-store authority binding blocks before
    candidate selection or source mutation.
  - Input: forcing manifest has missing/empty/duplicate `files`, missing or
    unsafe stable subject, escaped URI, missing/checksum-different member or an
    unknown extra product; forcing finalized sidecars are legacy-absent,
    complete-valid, partial, or binding/checksum-drifted; or run output is
    missing/empty/non-regular-only.
    Expected: discovery fails before selection in dry-run and enforce; source
    remains and no canonical archive is created for invalid shapes; legacy and
    complete-valid forcing shapes archive, including canonical uppercase IFS
    and a domain end earlier than the forcing eligibility end. A valid archive
    carries producer provenance which #847 binds to the exact DB subject before
    declaring product-archive coverage complete, after decompressing and
    verifying its actual exact members rather than trusting the sidecar alone.
  - Input: provider state, source-less legacy state and a clone target model
    that references the provider physical artifact.
    Expected: provider/legacy paths are collision-disjoint; only the physical
    origin tree is archived and no clone-target archive is fabricated.
  - Input: staged tar whose tarball sha matches its manifest but whose internal
    member is missing, duplicated, unsafe, non-regular or checksum-different.
    Expected: final publication and source deletion are blocked.
  - Input: source root/file swap or late create/write/delete at scan-to-tar,
    tar-to-final-verify, final-verify-to-tombstone or tombstone-recheck
    boundaries.
    Expected: observed drift preserves the changed/replacement source or
    tombstone; the aged producer-immutability precondition is explicit.
  - Input: opened archive tar or manifest directory entry is replaced after its
    final byte read; or a shape-valid producer block drifts from identity,
    window/model/basin or producer-manifest member digest.
    Expected: exact child-pair/producer semantic binding fails before source
    retirement; source and current archive evidence are preserved. If a
    replacement occurs after the durable retirement guard is installed, the
    exact valid guarded pair remains as reported residue and the terminal is
    indeterminate rather than falsely archived.
  - Input: the tar and sidecar are internally checksum-consistent but the
    embedded forcing/run producer manifest drifts in subject/source/cycle/
    window/model/basin or configured-prefix URI identity.
    Expected: mover and inventory member verification reject the archive;
    outer producer claims cannot manufacture completeness.
  - Input: final tar/manifest is replaced after final verify but before
    tombstone rename, or an extra child appears after tombstone recheck but
    before recursive removal.
    Expected: full final-pair and expected-allowlist validation blocks child
    unlink, preserves source/tombstone residue and returns non-zero.
  - Input: a tombstone file or directory is replaced after allowlist stat but
    before its removal.
    Expected: atomic child claim observes the replacement inode, preserves it
    as residue and never removes data outside the allowlist.
  - Input: source has been renamed to a tombstone, then canonical-pair or second
    allowlist validation fails before the first child claim.
    Expected: terminal residue contains every actual surviving tombstone,
    claim and durable-guard path; no obsolete source locator is substituted.
  - Input: verified final pair plus identical source, and verified final pair
    plus drifted source.
    Expected: identical is recorded idempotent without duplicate and may
    retire in enforce; drift is a conflict preserving both.
  - Input: corrupt/partial final leaf, unexpected sibling, cross-device
    or same-filesystem bind-mounted staging/source (different mount ID), raced
    rename destination, unavailable mount-ID proof, or any staged
    file/directory/ancestor/quarantine/tombstone rename/fsync failure.
    Expected: whole-leaf quarantine/fresh publish only when safe; otherwise
    non-zero/indeterminate; before tombstone rename source is untouched, while
    later failures truthfully record complete/partial tombstone residue.
    Dry-run only records plans.
  - Input: a corrupt canonical final is namespace-swapped for a different valid
    leaf immediately before quarantine.
    Expected: exact guarded-inode binding detects the swap; the replacement is
    not moved or called corrupt and the candidate fails non-zero.
  - Input: quarantine succeeds then fresh archive succeeds/fails, and receipt
    temp/replace/fsync/parent-swap faults or unsafe lock/receipt parents.
    Expected: one terminal outcome plus ordered quarantine event(s); strict
    receipt publication preserves old content pre-replace and reports
    indeterminate post-replace; unsafe coordination paths block.
  - Input: publish or quarantine rename succeeds but its following parent fsync
    fails.
    Expected: terminal is indeterminate and residue names the real destination
    only; no stale staging locator or falsely durable quarantine event.
  - Input: existing-final verify reports deterministic corruption versus
    timeout/tool/read/mount operational error; and a valid manifest reverses
    its `files` array.
    Expected: only deterministic corruption quarantines; operational failure
    preserves final+source; reversed valid order remains idempotent.
  - Input: a real decompressor emits no tar or a valid tar and then exits
    non-zero.
    Expected: both are operational/indeterminate; canonical final and source
    remain and no quarantine event is emitted.
  - Input: Linux mount-ID evidence is missing or malformed.
    Expected: operational/indeterminate; canonical final and source remain and
    no quarantine event is emitted.
  - Input: tar begins with an unexpected member, declared-size mismatch or
    more members than manifest/tree cap; or with oversized/global PAX, GNU
    longname/longlink, GNU sparse or any non-POSIX-regular representation.
    Expected: reject at the offending header before streaming its body;
    member-count, size, extension metadata, cumulative payload and depth caps
    are fail-fast while bounded writer-generated local PAX still round-trips.
  - Input: the decompressor keeps writing after the first header/PAX rejection.
    Expected: parser failure immediately terminates and reaps the tool, restores
    the archive FD offset and preserves deterministic failure classification
    rather than holding the global lock until the full tool timeout.
  - Input: many small or consecutive local-PAX headers precede one member.
    Expected: raw/PAX count, structure or cumulative-byte limit rejects before
    recursion/global tar limits; failure remains typed deterministic corruption.
  - Input: hot forcing payload changes/checksum is incomplete, or hot run output
    is not yet present, while manifest identity/window/URI shape remains valid.
    Expected: leaf is skipped without full scan or discovery failure; the same
    invalidity on a cold leaf fails, and hot identity/window/prefix drift still
    fails during the lightweight manifest gate.
  - Input: cutoff equality, CLI age zero/20, candidate/tree/depth/manifest/
    file/source/tar/uncompressed/timeout/stderr cap overflow, unreadable state
    directory, relative/bare zstd path, and lock contention.
    Expected: equality remains hot; invalid age/caps/unreadable fail closed;
    lock contention does not overwrite the holder receipt and emits only its
    structured skip diagnostic.

  Implementation evidence (#848, candidate `36531b0960bb1810e7225ff2fc1353af4bfcdbd9`):
  - local macOS: the mover/storage/schema/inventory/object-store/state/journal
    target set passed `799` tests; `uv run ruff check .`, strict OpenSpec,
    `uv lock --check`, wrapper shell syntax and `git diff --check` passed.
  - node-27 isolated worktree at the exact candidate passed the same `799`
    tests plus targeted ruff. A read-only copy of a real production forcing
    leaf was enforced only inside the isolated oracle directory with real
    `/usr/bin/zstd`: `41` exact members archived, source copy retired, durable
    guard/claim residue cleaned, and the DB-subject-equivalent inventory check
    returned member-verified `product-archive` coverage.
  - a production-shape run manifest with trailing-slash `output_uri` passed the
    mode-0600 wrapper dry-run and emitted the two-phase receipt with one
    validated attempt. Production `/home/ghdc/nwm` ACL/archive-root provisioning
    and first real-lane enforce remain explicitly owned by tasks 2.3/2.5; no
    live state candidate was claimed because current state inventory is empty.
- [x] 2.3 Add systemd units + env + governance registration for the mover
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
- [x] 2.4 Extend `scripts/node27_resource_governance.py` capacity
  visibility and the mover's free-space refusal.
  Evidence floor: governance receipt reports archive root size and
  shared-volume free space; mover refuses enforce below the configured
  free-space threshold.
  Test rows:
  - Input: free space below the refuse threshold, enforce requested.
    Expected: mover refuses, sources untouched, receipt warning emitted.
- [x] 2.5 node-27 live: first audit receipt + first enforce archive run.
  Evidence floor: committed schema-valid archive-completeness receipt whose
  salvage selector list covers the known pre-2026-06-16 forcing gap; first
  enforce archive receipt covering aged `forcing/` + `runs/` + `states/`
  cycles with ≥1 verified object per source lane present in rotation scope,
  0 checksum failures, and source removal only for verified objects; both
  receipts committed under runbook receipts.
  Reopened #849 closure note (2026-07-15): the #1065 controlled receipt is
  `runs/`-only and is not the qualifying receipt. The qualifying bounded
  enforce receipt must itself cover every aged lane in its complete discovery,
  have `outcome=success` and no discovery failures, and commit candidate lane
  counts plus cutoff/age/bound and receipt hash. A fresh dry-run must derive the
  minimum prefix bound that spans all nonzero candidate lanes; increasing the
  deployed bound above `8` requires a human-go naming the exact count and
  selected-byte ceiling, after which only one same-age/bound enforce within that
  ceiling is authorized. Multiple receipts cannot be combined. Accept the
  immutable 228-selector baseline receipt
  `completeness-incomplete-live-20260713T155314Z.json` at SHA-256
  `e2d4f08150943f09af87d3e53e79cff26728fb438aabb545dabff07842497d04`
  (normalized selector-set SHA-256
  `ad5da1c51e1e90ec7bf2912d204186d21879be4e69536cc24a469520a486d0c6`);
  any replacement selector set must be its superset. The receipt may remain
  `incomplete`; #1070 owns salvage and the follow-up `complete` audit. Only the
  archive tick's existing verify-before-retire mutation is in scope here; no DB
  mutation, salvage, compression, drill, retention, or manual deletion.

## 3. One-time DB-export salvage (`db-export-salvage`)

- [x] 3.1 Build the salvage exporter
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
- [x] 3.2 Document the manual `COPY FROM` restore procedure for `db-export`
  objects.
  Evidence floor: archive runbook section documents the checksum pre-check +
  manual `COPY FROM` sequence as the **only** restore path for salvage
  objects, states that no automated restore lane exists (ADR 0002 decision
  3), and is cross-linked from the retention runbook section (6.2).
- [x] 3.3 node-27 live: execute salvage for the audit-derived DB-only
  windows.
  Evidence floor: committed salvage receipt covering every audit-emitted
  salvage selector / salvageable forcing or river `gap` from the live
  completeness receipt (expected: forcing before 2026-06-16);
  per-selector manifest row count equals the DB row count at export time; a
  follow-up audit run marks those salvageable subjects `complete` via
  verified salvage objects and emits an empty salvage list. Any
  non-salvageable state gap remains `gap` and keeps retention fail-closed
  until product coverage is restored.
  Issue #1070 live closure note (2026-07-15): consume only the immutable
  228-selector audit baseline at SHA-256
  `e2d4f08150943f09af87d3e53e79cff26728fb438aabb545dabff07842497d04`
  (normalized selector-set SHA-256
  `ad5da1c51e1e90ec7bf2912d204186d21879be4e69536cc24a469520a486d0c6`).
  Hard-coded fallback scope is forbidden. Use an explicitly read-only role,
  prove SELECT plus write refusal, and use `per_tick_bound=228` so dry-run and
  enforce each enumerate the entire input list; default 32 is insufficient.
  Install the accepted receipt at a frozen mode-0600 non-timer path and bind
  full/ordered-selector hashes before and after. A streaming read-only COPY
  preflight must prove every selector has `row_count > 0`, exact CSV bytes below
  the tightened 512 MiB cap, `4 * max_bytes` below available/cgroup memory,
  aggregate uncompressed bytes preserve 300 GiB free-space headroom, and a
  doubled observed duration plus fixed overhead fits a four-hour external
  timeout. Otherwise create/fix a streaming-export blocker before live enforce.
  Require clean/no-error export or verified skips for all selectors; treat the
  object/manifest pair as non-transactional and independently verify both. A
  zero-row manifest must not provide audit coverage. Commit the fresh follow-up
  audit with an empty salvage list. Secrets, DB mutation, object deletion,
  compression, drill, retention, timers, and node-22 remain out of scope.
  Closure evidence (2026-07-15): node-27 head
  `bcd66a9f68b406463a0579aae6b6e0f57b9d0778` exported and independently
  verified all 228 selectors (75,922,800 rows; 1,065,525,623 compressed
  bytes). The fresh inventory receipt at SHA-256
  `2277c617900a62f5eca1253ff967650da6790b5952bd4658c10eb1a6d281bb54`
  reports 1,585/1,585 windows `complete`, reconciles all 228 baseline
  selectors through verified `db-export` coverage, and emits zero salvage
  selectors. The terminal envelope SHA-256 is
  `01f6256203530704840ce528d03fe2ef1c4939b05e50e1788dfefc98dc24e767`
  with verdict `PASS_TASK_3_3`.

## 4. Hypertable compression (`hypertable-compression`)

- [ ] 4.1 Migration `000047`: compression settings for both hypertables. (Issue #851 body still cites `000043` from the earlier planning window; slot 000043–000046 are now occupied, so this task uses the next free slot 000047. #851 fixture in `design.md` records the deviation.)
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
- [x] 4.3 Add the fail-closed compressed-chunk write guard to all three
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
- [x] 4.4 Add compression systemd units + env + governance registration.
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
  Issue #1069 closure note: the mandatory fixture in `design.md` additionally
  requires exact D3 catalog proof, a schema forensic snapshot, successful-first
  idempotency evidence, quiesced ingest, and one dry-run-bound exact selected
  chunk (`bound=1`, at most 8 GiB, 300 GiB free-space headroom, 900-second
  external timeout). Independent schema/semantic/sha256 validation and the
  actual production curve/MVT SQL must pass the pinned warm-cache thresholds.
  The compression timer is installed and enabled but remains inactive. This
  issue also closes the discovered task-4.2 lock-receipt gap and #853 wiring
  gap: contention publishes `refused_lock`, while the committed timer service
  invokes the wrapper with literal `--enforce`. Tasks 4.1, 4.2, and 4.5 remain
  open until their respective code/live evidence passes.

## 5. Archive rebuild drill (`archive-rebuild-drill`)

- [x] 5.1 Build the drill script
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

- [x] 6.1 Build the retention runner
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
- [x] 6.2 Add retention systemd units + env + governance registration.
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

- [x] 7.1 Cross-link ADR 0002, the new runbook sections (archive operation
  and rollback, decompress procedure, manual salvage restore), and
  `docs/governance/DOC_STATUS.md`.
  Evidence floor: `openspec validate tier-node27-timeseries-storage --strict
  --no-interactive`, `uv run ruff check .`, and targeted pytest for the new
  scripts pass as the change-level verification floor; runbook cross-links
  resolve.
  Delivery: ADR 0002 gained an "Implementation" section that cross-links
  the tier-node27-timeseries-storage runbook (§2 archive operation and
  rollback, §3.2 manual salvage restore, §4.3 decompress procedure, §7
  archive rebuild drill, §8 gated retention) plus the 7 sub-issue scripts
  and 4 committed receipt schemas.
  `docs/governance/DOC_STATUS.md` `Current Notes` records the runbook
  as the current-authority operator entrypoint.
  §6.3 note: first live retention receipt on node-27 is committed at
  `docs/runbooks/receipts/tier-node27-timeseries-storage/timeseries-retention/refusal-completeness-missing-20260713T030936Z.json`
  (mode=enforce, outcome=refused,
  refusal_reason=COMPLETENESS_RECEIPT_MISSING, schema-valid, exit 1),
  satisfying §6.3 test row 1 (missing/stale completeness receipt);
  test rows 2-4 (dry-run + enforce + metadata invariant) remain
  pending upstream #849/#851/#853/#854 §5.2 live receipts and
  systemd unit installation on node-27, tracked separately by #856.

## 8. Live-cascade defect closure

- [x] 8.1 Fix issue #1067's node-27 wrapper import contract.
  Evidence floor: the exact seven issue-named `scripts/node27_*_once.sh`
  wrappers (including the newly required archive-rebuild-drill wrapper)
  prepend their parameterized repository root to `PYTHONPATH` before
  `exec`, preserve an existing non-empty `PYTHONPATH`, and retain their
  existing Python entrypoint behavior; focused regression tests cover the
  all seven wrappers' default/empty root, absolute root override,
  relative/delimiter-root refusal, pre-source inherited-path preservation,
  import-origin binding, checkout identity, and sibling hygiene.
  Test rows:
  - Input: unset or empty audit repository-root override. Expected:
    `/home/nwm/NWM` is the first `PYTHONPATH` entry.
  - Input: absolute custom audit repository root. Expected: it becomes the
    first entry; a relative or colon-bearing override is refused before
    Python launch.
  - Input: empty inherited `PYTHONPATH` and a test repository root.
    Expected: `from scripts import node27_product_archive` succeeds through
    the wrapper launch contract.
  - Input: existing two-entry caller `PYTHONPATH` plus env-file empty or
    non-empty `PYTHONPATH`. Expected: both caller entries are preserved
    byte-for-byte and in order after the resolved root.
  - Input: inherited path containing a later regular `scripts` package.
    Expected: governed module origin wins or wrapper refuses before the
    audit entrypoint.
  - Input: `PYTHONSAFEPATH=1` and an otherwise safe governed checkout.
    Expected: all seven wrappers reach the intended entrypoint; preflight
    does not discard the root entry.
  - Input: a regular `scripts` package in the actual entrypoint directory,
    including an explicit script override outside the root. Expected:
    wrapper refuses before entrypoint side effects; audit never loads a
    shadow `node27_product_archive.py`.
  - Input: retention/raw caller `PYTHONPATH` with an empty segment.
    Expected: preflight and the post-`cd` file launch resolve the same
    effective search path.
  - Input: custom root with no interpreter/script overrides. Expected:
    default interpreter and entrypoint derive from that same checkout.
  - Input: the exact seven wrappers across unset/empty/absolute/relative/
    delimiter roots and empty/non-empty inherited paths. Expected: every
    wrapper enforces the same root contract before Python launch; original
    arguments, entrypoint semantics, and downstream exit code remain
    unchanged.
  - Input: node-27 `nhms-node27-storage-inventory-audit.service` after
    deployment. Expected: journal contains no `No module named 'scripts'`;
    any later #1066/#1065 blocker remains separately attributable.
  Verification: `uv run pytest -q tests/<wrapper-contract-test>.py`;
  `uv run ruff check .`; `openspec validate
  tier-node27-timeseries-storage --strict --no-interactive`; committed
  node-27 journal evidence under the tier runbook receipts tree. A complete
  archive-completeness receipt is explicitly deferred until #1066/#1065
  merge and does not block this issue's PR.

- [x] 8.2 Fix issue #1066's audit prefix and terminal receipt contract.
  Evidence floor: align the audit and product-archive node-27 examples to the
  producer/DB canonical `s3://nhms` prefix while documenting why the separate
  compute/display consumer/config identities retain `s3://nhms-prod` without
  asserting physical-store topology; migrate the archive completeness schema
  to `schema_version=1.1` and exact terminal `oneOf` branches `complete` /
  `incomplete` / `blocked` / `indeterminate`; publish a schema-valid receipt for
  every pre-publication audit-controlled terminal path whenever the receipt
  destination itself is safe and writable; keep publication-attempt failures
  stderr-only and preserve explicit uncertainty instead of fabricating success.
  Test/evidence rows:
  - Input: the node-27 producer chain (`node27-download.example`,
    `node27-ingest.example`, `scripts/node27_ingest_run.py`) plus a real
    DB-shaped `s3://nhms/forcing/...` URI. Expected: audit/product examples use
    `s3://nhms`, `_object_key` binds the URI to its expected hot key, and
    compute/display `s3://nhms-prod` remain documented consumer/config values;
    no physical-store separation is asserted.
  - Input: a real DB-shaped `s3://nhms/...` URI with an intentionally
    mismatched configured bucket, executed through `main()` against a fake
    object-store/DB boundary rather than the audit's own prefix normalizer.
    Expected: non-zero exit; configured receipt exists on disk with
    `outcome=blocked`, a stable sanitized refusal reason, and exactly one schema
    branch validates.
  - Input: coverage where every subject is complete, then the same inventory
    with one `pending-archive` or `gap`. Expected: `outcome=complete`, then
    `outcome=incomplete`; subject windows, evidence, and salvage-selector
    bijection retain their existing meaning; runtime aggregate validation
    rejects a contradictory outcome; `complete` has an empty selector array.
    Empty inventory yields `blocked/EMPTY_INVENTORY`, never empty success.
  - Input: missing DB URL, bad archive age, unknown option, argparse type error,
    or other config error after a valid absolute receipt path has been
    bootstrapped independently of later parsing. Expected: a schema-valid
    on-disk `blocked` receipt. Input: receipt option with missing value,
    duplicate/ambiguous receipt options, missing CLI+env destination, or an
    unsafe destination. Expected: sanitized structured stderr, non-zero exit,
    and no false claim that a terminal receipt was published.
  - Input: unexpected audit exception with a valid receipt destination.
    Expected: schema-valid on-disk `indeterminate` receipt, non-zero exit, and
    no DB URL/credential in receipt or stderr.
  - Input: injected first-publication failure before replace, then after
    replace. Expected: both are stderr-only and trigger no second publish;
    pre-replace keeps prior bytes exactly, while post-replace reports
    indeterminate publication with target content unknown; neither reports
    `published` or writes an on-disk replacement error receipt.
  - Input: all four terminal examples plus the migrated legacy successful
    example. Expected: every receipt pins `schema_version=1.1` and validates
    against exactly one top-level `oneOf` branch with date-time format checking.
    Success branches require coverage fields and forbid reasons/detail;
    `blocked` requires `refusal_reason`, optional sanitized `detail`, and no
    coverage/`error_reason`; `indeterminate` requires `error_reason`, optional
    sanitized `detail`, and no coverage/`refusal_reason`. Schema-invalid and
    aggregate-inconsistent shapes fail before publication. Stable reason-code
    tests cover all fixture-pinned blocked codes, `UNEXPECTED_AUDIT_ERROR`, and
    the two stderr-only publication codes; raw exceptions appear only in
    sanitized `detail`.
  - Input: `blocked` and `indeterminate` receipts passed to the DB-export
    salvage input loader. Expected: stable refusal before any DB read/export or
    archive write; only `complete`/`incomplete` coverage branches can supply
    `salvage_selectors`.
  - Input: unchanged `scripts/node27_timeseries_retention.py` read path, one
    blocked receipt file, and one absent path. Expected: static/schema evidence
    proves the terminal outcome is distinguishable from “audit never ran”;
    no downstream behavior change or live retention execution occurs here.
  - Input: node-27 systemd audit with canonical env and live read-only DB.
    Expected: committed schema-valid terminal receipt under
    `docs/runbooks/receipts/tier-node27-timeseries-storage/`, no prefix-mismatch
    blocker, and journal/receipt SHA evidence tied to the deployed commit.
    Before #1065 closes, accepted live outcomes are: `complete` or `incomplete`
    with `windows` containing at least one inventoried subject; or `blocked`
    with a non-empty stable reason attributable to #1065 (for example evidence
    access/discovery blocked) and optional sanitized detail. `indeterminate`
    never substitutes for this live oracle.
  Verification: `uv run pytest -q
  tests/test_node27_storage_inventory_audit.py
  tests/test_node27_db_export_salvage.py
  tests/test_node27_timeseries_retention.py
  tests/test_timeseries_storage_schemas.py`; schema example validation loop;
  `uv run ruff check .`; `openspec validate
  tier-node27-timeseries-storage --strict --no-interactive`; node-27 live
  receipt produced through the installed systemd unit. #1065 mover discovery
  repair and every #856 dry-run/enforce live-cascade action remain explicit
  non-goals.

- [x] 8.3 Fix issue #1065's product-archive live-shape and states-access
  diagnostics.
  Evidence floor: retain strict forcing exact-leaf and run identity/output URI
  validators; add a disk-backed live-shape fixture covering canonical and
  historical-prefix GFS/IFS forcing/runs for qhh/heihe plus inaccessible state
  leaves; aggregate every states discovery/full-validation EACCES reached before
  candidate processing into one sanitized `STATES_ACCESS_DENIED` diagnostic
  with a distinct non-zero exit reason; document the complete NFS
  group/mode-or-ACL operator repair without executing it in this PR; commit a
  post-repair non-failed mover receipt while preserving the first-live failure
  receipt. Before process-stage mutation, preflight the complete selected
  batch's effective source-retirement capability; one failed source-parent or
  tree-directory check aborts all selected work before archive publication.
  Process-stage permission changes after that gate retain the existing
  independent-candidate terminal model and are not converted into a
  transactional batch rollback by this issue. Task 3.3 salvage and its
  follow-up complete audit are routed to open issue #1070, not this row.
  Test/evidence rows:
  - Input: disk-backed GFS and IFS forcing packages for qhh and heihe whose
    manifests use canonical `s3://nhms/<exact-package-leaf>/...`. Expected:
    discovery accepts them. Input: the identical packages with configured
    prefix `s3://nhms-object-store`, or a file URI moved outside its exact leaf.
    Expected: deterministic `forcing manifest file URI escapes its exact
    package leaf` failures; no validator weakening.
  - Input: disk-backed GFS and IFS qhh/heihe runs with directory-bound `run_id`,
    exact run-manifest URI, and output URI with or without one directory trailing
    slash. Expected: discovery accepts them. Input: the historical mismatched
    configured prefix or drifted run/output identity. Expected: deterministic
    `run manifest identity/outputs do not bind run directory` failure.
  - Input: one, then two or more inaccessible state leaves spanning GFS and
    IFS. Expected: the receipt has exactly one item with `lane_hint=states`,
    `locator=states`, and reason
    `STATES_ACCESS_DENIED count=<N> euid=<uid> egid=<gid>`; after durable receipt
    publication stderr has exactly one compact JSON line with
    `status=failed`, `exit_reason=STATES_ACCESS_DENIED`, the same count/euid/
    egid, and process exit code `2`. Raw absolute paths/exception text are
    absent and no source/archive mutation occurs. A non-access discovery
    failure retains ordinary per-locator diagnostics and exit code `1`.
  - Input: runbook permission repair. Expected: it states that supplementary
    group membership alone does not fix mode-0700 leaves; documents group
    directory `rwx` plus file read and future-writer inheritance, or an
    equivalent named-user/default ACL with the file-write inheritance tradeoff;
    explains that `rx` is insufficient for enforce; requires a new
    login/user-manager only for group membership changes; and verifies with
    `id`, `namei`, `getfacl`, directory `test -x`/`test -w`, file `test -r`, and
    complete logged `find` as `nwm`.
  - Input: a selected forcing/run/state source whose parent lacks effective
    write/search, whose root/internal directory lacks effective
    read/write/search, or whose file cannot satisfy existing read/identity
    validation. Expected: dry-run reports one sanitized non-zero selected-batch
    preflight failure instead of `planned`, performs no probe, and writes only
    lock/receipt metadata. Enforce checks the entire selected batch first, then
    performs one randomized hidden mkdir/fsync/rmdir/fsync probe per unique
    descriptor-bound source parent before candidate one; any failure produces
    zero archive publication and zero source mutation. A cleaned probe reports
    no residue; cleanup uncertainty is indeterminate with explicit safe
    root-relative probe residue. Mixed-batch tests prove one inaccessible
    candidate prevents every candidate from publishing, and receipt reasons
    contain no raw exception or absolute path. Parent deduplication and probe
    use the same held fd and reject a post-probe namespace rebound. Sticky-bit
    source parents/roots/internal directories require an ownership proof even
    when `os.access` and the probe would succeed. The blocker is bound by its
    selected identity; its reason contains only a closed check token, batch
    abort reasons are constant, legal space-bearing forcing/run/state locators
    remain valid, and semantic validation rejects unknown tokens plus dry-run
    probe-only tokens.
  - Input: a verified existing archive and one or more prior
    `.archive-guards/*` residues from failed source retirement. Expected: before
    new source mutation, bounded held-fd reconciliation removes only an exact
    two-member guard whose tar/manifest are the same inode/signature pair as
    the canonical verified pair. Successful retry retires source and leaves no
    matching hard-link guard while reporting empty terminal residue. Foreign,
    extra-entry, copied-but-not-hard-linked, or ambiguous guards are preserved;
    matching cleanup failure preserves source, exits non-zero/indeterminate,
    reports safe guard-relative residue, and leaks no raw exception/path.
  - Input: node-27 direct mover before permission repair. Expected: committed
    schema-valid access-failure evidence with the exact receipt/stderr/exit-2
    contract. Input: default-env direct dry-run after operator repair at the
    frozen implementation SHA. Expected: `outcome != failed` and neither pinned
    forcing/run reason appears; an empty queue is valid at the current 45-day
    cutoff. Input: the explicitly authorized controlled run with
    `--minimum-age-days 30 --enforce`. Expected: candidates non-empty,
    `bytes.source > 0`, `bytes.archived > 0`, selected terminals succeed, and
    each retired source is preceded by staged archive re-read/checksum
    verification; production env remains 45.
  - Input: the first authorized 30-day enforce attempt on the implementation
    without selected-batch retirement preflight. Expected: failed evidence
    records 320 candidates, eight selected, eight verified archive
    publications followed by eight source-parent tombstone-rename permission
    failures, all eight sources still present, and explicit archive/guard
    residue. This is not a passing receipt. The repaired implementation MUST
    reproduce the same permission shape as a prepublish batch failure with
    zero new archive publication and zero source mutation.
  - Input: runbook selected-source closure. Expected: executable NUL-safe (or
    equivalent encoding-safe) extraction from the receipt covers every
    selected forcing/runs/states source, verifies source parent `wx`, all tree
    directories `rwx`, files readable, effective ACL masks and sticky
    ownership, repeats against real writer-created future leaves with recorded
    writer groups/umask, and uses the mover's controlled parent probe rather
    than a shell approximation. It forbids manual durable-guard cleanup and
    explains exact automatic reconciliation. Failed live evidence remains
    identified as awaiting repaired rerun; no passing receipt is fabricated.
  - Input: cascade boundary after the passing mover receipt. Expected: the 228
    audit selectors remain untouched and are explicitly handed to open issue
    #1070; no task 3.3 salvage, compression, drill, retention dry-run, or
    retention enforce command runs. The original
    `first-live-run-20260713T043808Z.json` remains unchanged.
  Verification: `uv run pytest -q tests/test_node27_product_archive.py
  tests/test_node27_storage_inventory_audit.py`; product/archive receipt schema
  example validation; `uv run ruff check .`; `openspec validate
  tier-node27-timeseries-storage --strict --no-interactive`; node-27 direct
  mover receipt. Task 3.3 salvage, follow-up complete audit, retention, drill,
  compression, source deletion outside the authorized product-archive enforce
  run, and every #856 live-cascade command are explicit non-goals.
