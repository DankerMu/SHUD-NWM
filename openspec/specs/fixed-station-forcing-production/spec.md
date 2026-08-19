# fixed-station-forcing-production Specification

## Purpose
TBD - created by archiving change m23-qhh-22-production-automation. Update Purpose after archive.
## Requirements
### Requirement: Per-cycle forcing targets fixed stations
The forcing producer SHALL generate meteorological forcing for fresh cycles by mapping canonical grids to the fixed SHUD forcing stations seeded from the processed QHH package.

#### Scenario: Fixed stations selected
- **WHEN** forcing generation starts for a QHH model/cycle
- **THEN** it loads active `met.met_station` rows for the model's basin version with `station_role="forcing_grid"`
- **AND** it uses their SHUD forcing index and forcing filename metadata as the target station contract.

#### Scenario: No fixed stations blocks forcing
- **WHEN** no active forcing-grid stations exist for the QHH model/basin version
- **THEN** forcing generation fails with a missing-stations blocker
- **AND** no `met.forcing_version` is marked ready for that cycle.

### Requirement: Dynamic station timeseries are persisted
The system SHALL persist generated forcing values and provenance for each accepted model/source/cycle.

#### Scenario: Forcing version created
- **WHEN** station forcing generation completes for a canonical product
- **THEN** it writes one `met.forcing_version` linked to model, basin, source, cycle, canonical product, station count, variable set, time range, and quality metadata
- **AND** it writes `met.forcing_station_timeseries` rows for each generated station/variable/time value.

#### Scenario: Idempotent forcing generation
- **WHEN** forcing generation reruns for the same model/source/cycle/canonical identity
- **THEN** it reuses or replaces according to a deterministic idempotency policy
- **AND** it does not create duplicate ready forcing versions for the same candidate identity.

#### Scenario: Bad interpolation coverage blocks readiness
- **WHEN** canonical grids cannot cover a station or required variable/time range
- **THEN** forcing generation records the affected station/variable/time coverage gap
- **AND** downstream SHUD submission is blocked unless the policy explicitly permits reduced scope.

### Requirement: SHUD forcing package is produced

The system SHALL materialize SHUD-ready forcing files from persisted station forcing using the processed basin's file contract, publishing the main station-index member under the basin-neutral canonical identity.

#### Scenario: SHUD forcing files written

- **WHEN** forcing version is ready
- **THEN** the runtime package contains the canonical basin-neutral station-index member `shud/stations.tsd.forc` and per-station forcing CSV/text files expected by SHUD project mode
- **AND** file paths, checksums, station count, variable count, time range, and units are recorded in the runtime manifest.

#### Scenario: rSHUD contract honored without runtime dependency

- **WHEN** SHUD forcing files are created
- **THEN** their columns, units, station ordering, and filenames follow the existing rSHUD/AutoSHUD-informed processed basin contract
- **AND** the production cycle does not call rSHUD as the hydrologic runtime solver.

### Requirement: Producer output semantics are pinned to producer_version

The forcing producer SHALL pin its output-semantics surface — `OUTPUT_UNITS`, the
precipitation conversion branch (`mm/day` accepted as factor `1.0`, any other unit
rejected), and the `rn_shortwave_factor` default — to a deterministic regression
fingerprint bound to `producer_version`. Any change to those output semantics SHALL
flip the fingerprint and fail the guard test until the developer both bumps
`producer_version` and updates the pinned fingerprint in the same change.

#### Scenario: Changing output semantics forces a producer_version bump

- **WHEN** any of `OUTPUT_UNITS`, the precipitation conversion branch behavior, or the `rn_shortwave_factor` default is changed
- **THEN** the recomputed output-semantics fingerprint MUST differ from the pinned `EXPECTED_FINGERPRINT`
- **AND** the guard test MUST fail until both `producer_version` is bumped and `EXPECTED_FINGERPRINT` is updated.

#### Scenario: Unchanged semantics keep the gate green at the pinned version

- **WHEN** the producer output semantics are unchanged
- **THEN** the recomputed fingerprint MUST equal the pinned `EXPECTED_FINGERPRINT`
- **AND** `producer_version` MUST equal the pinned value (`m2.0`).

### Requirement: OUTPUT_UNITS and manifest-unit keysets stay in lockstep

The producer's `OUTPUT_UNITS` keyset SHALL equal the manifest's
`REQUIRED_FORCING_VARIABLES` keyset, and every required forcing variable SHALL map
to a non-empty manifest unit, so that adding an `OUTPUT_UNITS` key without wiring
its manifest unit is caught by a guard test.

#### Scenario: Keyset equality and non-empty manifest units

- **WHEN** the guard test compares `set(OUTPUT_UNITS)` with `set(REQUIRED_FORCING_VARIABLES)`
- **THEN** the two keysets MUST be equal
- **AND** `package_manifest_unit(v)` MUST return a non-empty string for every required forcing variable.

### Requirement: Direct-grid mirror maintenance preserves the registration-owned active_flag

The runtime producer's direct-grid `met.met_station` mirror maintenance SHALL NOT set `active_flag=true` on either plane: the DB-plane upsert (`workers/forcing_producer/store.py:ensure_direct_grid_met_stations`) and the DB-free file plane's station-inventory handoff (`workers/forcing_producer/file_store.py:_handoff_station_rows` → `station_inventory.json` → the `met.met_station` ingest) SHALL preserve an existing row's current `active_flag` on conflict-update — never escalating `false`→`true` — and SHALL insert fresh mirror rows with `active_flag=false`. Mirror activation belongs exclusively to the cutover station-flag flip (Change 8); the writers' fail-closed derived-cache collision predicate is retained unchanged.

#### Scenario: A pre-cutover production run leaves the mirror inactive

- **WHEN** a direct-grid forcing production run executes against a registered-but-inactive variant whose registration wrote the mirror rows with `active_flag=false`
- **THEN** after the run every one of the variant's mirror rows still has `active_flag=false`
- **THEN** the shadow-window station-MVT query (`active_flag=true`) still returns only the legacy station track, so pre-cutover production cannot create a mixed display.

#### Scenario: The producer upsert never escalates active_flag

- **WHEN** the producer's mirror upsert hits an existing `met.met_station` row for the same derived-cache binding
- **THEN** the update preserves the row's current `active_flag` value (a `false` row stays `false`; a row flipped `true` by the Change 8 cutover stays `true`)
- **THEN** no code path in the producer writes the literal `active_flag=true` for the mirror, on insert or update.

#### Scenario: The file-plane handoff carries the same ownership rule

- **WHEN** the DB-free file plane emits `station_inventory.json` for the `met.met_station` handoff
- **THEN** the emitted station rows do not force `active_flag: true`, and the ingest applies the same preserve-on-update / insert-inactive rule as the DB-plane upsert
- **THEN** both planes leave mirror-activation ownership with the registration step (`active_flag=false`) and the Change 8 flip (`true`).

#### Scenario: The fail-closed collision predicate is unchanged

- **WHEN** the producer's mirror upsert targets an existing `station_id` that is not the same derived direct-grid cache binding (the conditional-update identity predicate over `station_role='direct_grid_cache'` and the `properties_json` identity fields fails)
- **THEN** the write still fails closed with the existing collision error and mutates no row
- **THEN** relaxing the flag ownership does not relax the identity collision policy (docs §7.4).

### Requirement: Forcing package station-index identity is basin-neutral and fails closed

The SHUD forcing package main station-index member SHALL carry the fixed basin-neutral canonical identity `shud/stations.tsd.forc`, with the legacy identity `shud/qhh.tsd.forc` accepted read-only for historical packages. Producers SHALL emit only the canonical member. Direct-grid consumers SHALL resolve the station index by requiring exactly one member from the {canonical, legacy} set in both the package manifest and the staged filesystem, failing closed on ambiguity or absence and on a manifest-declared member that is absent from the object tree. Before writing any member, direct-grid staging SHALL delete from the staged tree every accepted station-index member that this attempt's checksum-verified package manifest does not declare — prior-attempt workspace residue self-heals instead of poisoning the run — deleting nothing outside the accepted-member set; a residue deletion that fails SHALL terminate the attempt loudly with the dedicated typed error `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` instead of continuing to stage. Ambiguity that survives this hygiene — a manifest declaring more than one accepted member, or a staged filesystem still carrying more than one accepted member after hygiene — remains fail-closed. Non-direct-grid staging, which copies the whole package prefix and can therefore legitimately hold a residual second member from an in-place re-produce, SHALL resolve a multi-member filesystem by the declared member from the package manifest, or the run manifest's diagnostic file list when the package manifest publishes none, with canonical-first fallback instead of failing, and SHALL NOT delete residual members. No consumer SHALL treat the member filename as evidence of the forcing data's basin identity. The declaration-source matching SHALL accept the same entry shapes the direct-grid consumer accepts for the identical manifest: a `./`-prefixed `relative_path` normalizes before the accepted-member intersection, and an entry that omits `relative_path` resolves through its `uri` relative to the forcing package root; an entry that is invalid or underivable under these rules is skipped, because the anchor is a best-effort resolver and SHALL NOT introduce a new fail-closed surface on the non-direct-grid lane.

#### Scenario: canonical member published for every basin

- **WHEN** a direct-grid forcing package is produced for any basin, QHH included
- **THEN** the main station-index member is `shud/stations.tsd.forc` with manifest role `shud_forcing`
- **AND** the station rows and per-station CSVs derive from that basin's own inputs, not from the member filename.

#### Scenario: legacy package remains consumable

- **WHEN** a historical package whose only station-index member is `shud/qhh.tsd.forc` is staged
- **THEN** the runtime resolves, checksums, and stages it exactly as before the migration
- **AND** existing object-store packages are not rewritten.

#### Scenario: prior-attempt workspace residue self-heals at direct-grid staging

- **WHEN** a direct-grid run's reused input workspace carries a staged station-index member from a prior attempt that this attempt's checksum-verified package manifest does not declare (for example a pre-migration `shud/qhh.tsd.forc` under a manifest that declares only `shud/stations.tsd.forc`)
- **THEN** direct-grid staging deletes the undeclared accepted member before writing any member, the staging completes without `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS`, and the staged tree holds exactly the declared station-index member
- **AND** the deletion is confined to the accepted station-index member set: prior-attempt station CSVs and files outside that set are not deleted.

#### Scenario: residue deletion failure fails loud with a dedicated typed code

- **WHEN** the pre-staging deletion of an undeclared accepted station-index member fails (for example the member path is a directory, or the unlink raises an I/O error)
- **THEN** the attempt terminates with the typed error `DIRECT_GRID_FORCING_RESIDUE_CLEANUP_FAILED` naming the member, staging does not continue past the failure
- **AND** the orchestrator does not auto-retry it: the code is not in `TRANSIENT_ERROR_CODES`.

#### Scenario: ambiguous index membership fails closed

- **WHEN** a direct-grid package manifest lists more than one station-index member from the {canonical, legacy} set, or a direct-grid staged filesystem still contains both files after the pre-staging hygiene
- **THEN** the runtime raises the fail-closed error `DIRECT_GRID_FORCING_INDEX_AMBIGUOUS` naming the conflicting members
- **AND** no fallback member selection is attempted
- **AND** the filesystem-level message attributes the surviving ambiguity to writes outside the manifest-allowlisted staging (out-of-band writes), not to prior-attempt residue, which the hygiene has already removed.

#### Scenario: non-direct-grid staging resolves a residual second member by manifest

- **WHEN** a non-direct-grid package's staged filesystem contains both station-index files, as a full-prefix copy can after a pre-migration prefix is re-produced in place
- **THEN** the runtime resolves the member to stage from the first available declaration source — the checksum-verified package manifest's `files` list when it publishes a non-empty one, otherwise the run manifest's diagnostic `forcing.files` entries — staging the member that source names when it names exactly one accepted member, and the canonical member otherwise
- **AND** the run does not fail on the residual member
- **AND** the residual member is not deleted: the pre-staging hygiene applies only to the direct-grid lane.

#### Scenario: missing index membership fails closed for direct-grid

- **WHEN** a direct-grid package contains no station-index member from the {canonical, legacy} set
- **THEN** the existing missing-member errors are raised and their messages name both accepted identities.

#### Scenario: manifest-declared member absent from the object tree fails closed with an accurate code

- **WHEN** a direct-grid package manifest declares one accepted station-index identity but the object tree carries only the other
- **THEN** the runtime fails closed with a checksum-read error naming the declared member, not a size-limit error.

#### Scenario: real QHH model asset keeps its identity

- **WHEN** the real QHH model asset `data/Basins/qhh/input/qhh/qhh.tsd.forc` or its bootstrap tooling is exercised
- **THEN** its QHH-specific naming and semantics are unchanged by this contract.

#### Scenario: dot-prefixed manifest declaration still anchors the legacy member

- **WHEN** a non-direct-grid staged filesystem carries both station-index members and the declaration source names the legacy member with a `./`-prefixed `relative_path` (a shape the direct-grid checksum lane already accepts)
- **THEN** the anchor resolves the legacy member and the run does not silently fall back to the stale canonical member

#### Scenario: uri-only manifest declaration still anchors the legacy member

- **WHEN** the declaration source's entry for the legacy member omits `relative_path` and carries only a `uri` located under the forcing package root
- **THEN** the anchor derives the member from the `uri` and resolves the legacy member

#### Scenario: an underivable entry is skipped without failing the lane

- **WHEN** a declaration-source entry is invalid or its `uri` is not under the forcing package root
- **THEN** that entry is skipped, the anchor falls back to canonical-first when no other entry names exactly one accepted member, and no error is raised on the non-direct-grid lane

#### Scenario: a declaration naming both accepted members still falls back

- **WHEN** the declaration source names both accepted station-index members, in plain, dot-prefixed, or uri-only shapes
- **THEN** the anchor returns no member and staging falls back to canonical-first, preserving the pre-existing ambiguity semantics

### Requirement: Station handoff provenance records the actually-resolved forcing-index member

The DB-free file plane's station-inventory handoff (`_handoff_station_rows`) SHALL derive each station row's `properties_json.source` default from the package manifest's `files` list — selecting entries whose role is the SHUD forcing-index role AND whose `relative_path` is an accepted station-index member, preferring the canonical member when multiple match — and SHALL write that member's basename. When no such member resolves, the handoff SHALL omit the `source` key rather than fabricate a value. A station property that already carries `source` SHALL be preserved unchanged.

#### Scenario: Canonical package labels stations with the canonical basename

- **WHEN** the handoff assembles station rows for a package whose manifest lists the canonical station-index member (`shud/stations.tsd.forc`)
- **THEN** station rows without a pre-existing `source` property carry `"source": "stations.tsd.forc"`, for every basin identically (no QHH-specific literal)

#### Scenario: Legacy replay package labels stations with the legacy basename truthfully

- **WHEN** the handoff runs against a legacy package whose manifest lists only `shud/qhh.tsd.forc` as the station-index member
- **THEN** station rows carry `"source": "qhh.tsd.forc"` — now naming a member that actually exists in that package

#### Scenario: Unresolvable member yields absent provenance, not a fabricated label

- **WHEN** the package manifest lacks a resolvable station-index member (missing `files`, wrong shape, or no role/membership match)
- **THEN** emitted station rows contain no `source` key in `properties_json`, and the handoff otherwise proceeds unchanged

#### Scenario: Pre-existing provenance is never overwritten

- **WHEN** a station's properties already include a `source` value (e.g. QHH bootstrap real-asset stations)
- **THEN** the handoff preserves that value verbatim

### Requirement: Bootstrap seed station provenance follows the parameterized project identity

The model-registry bootstrap's station seeding (`_seed_station_rows`) SHALL derive the persisted station provenance labels `properties_json.source` and `properties_json.elevation_metadata.source` from the invocation's `project_name` (as `<project_name>.tsd.forc`), consistent with the row's `project_name` and `forcing_source_identity` fields and with the sibling river-segment lane's parameterized provenance — never from a hardcoded basin literal. Under the default QHH invocation the persisted values SHALL remain byte-identical to `"qhh.tsd.forc"`.

#### Scenario: Non-default project bootstrap writes self-consistent provenance

- **WHEN** the bootstrap seeds station rows with a non-default
  `project_name` (for example `heihe`)
- **THEN** each persisted row's `source` and
  `elevation_metadata.source` equal `heihe.tsd.forc`, its
  `forcing_source_identity` begins with `heihe.tsd.forc:`, its
  `project_name` is `heihe`, and none of these fields carries the
  `qhh` literal

#### Scenario: Default QHH bootstrap output is unchanged

- **WHEN** the bootstrap seeds station rows with the default
  `project_name` `qhh`
- **THEN** the persisted `source` and `elevation_metadata.source`
  values are byte-identical to `"qhh.tsd.forc"`

### Requirement: Direct-grid station CSV staging SHALL treat a prior attempt's own residue as replaceable while still refusing anything this attempt staged

Staging SHALL remove only station CSV residue that predates this attempt's
staging, and SHALL keep failing closed when a file this same attempt just
staged — a model package member, a forcing package member, or an initial
state — already occupies a declared station CSV target path. A retried SHUD
attempt reuses the same deterministic run workspace, so its own previous
output is not a collision; a file the current attempt produced is. Removal is
no-follow and contained within the model input directory, and a removal
failure SHALL abort the attempt with the existing residue-cleanup error code
rather than continuing to stage. Two rows declaring the same filename in one
row set remain a refusal, not a last-write-wins overwrite.

#### Scenario: a second attempt on the same run workspace stages successfully

WHEN the same manifest is staged twice into the same run workspace, so the
station CSV targets from the first attempt are still present
THEN the second staging succeeds and every staged station CSV holds the
content produced by the current staging pass

#### Scenario: a file staged by this same attempt still fails closed

WHEN the model package staged earlier in this same attempt carries a member
whose name equals a declared station CSV target
THEN staging fails with the direct-grid station filename collision error and
the already-staged copy of that member inside the model input directory is
left byte-for-byte unchanged

#### Scenario: duplicate declarations in one row set are refused

WHEN the forcing station row set declares the same filename twice
THEN staging fails with the direct-grid station filename collision error
rather than silently letting the second copy overwrite the first

#### Scenario: residue deletion failure aborts loudly

WHEN removing a prior attempt's station CSV fails
THEN the attempt terminates with the existing direct-grid residue cleanup
error code, staging does not continue, and no partially staged station CSV
set is left behind

