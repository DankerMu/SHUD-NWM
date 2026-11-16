## Context

This is Change 4.5 of the direct-grid migration (docs/ForcingReplace §15), a **pilot pre-enablement** that closes three "ownerless" gaps between the mechanism changes (4/5/8) and the first single-source pilot (Phase 3 Pilot Build). It lands mechanism only and activates no production basin (§16).

Prior state that motivates each gap:

- **Mapping builder is library-only (Epic #909).** `workers/mapping_builder/` ships `integrity.py` (G0/G1), `algorithm.py` (G2 + ownership), `rewrite.py` (G4), `binding.py` (G5 + `ZPolicy`), and `evidence.py` (G0–G5 evidence). The archived `forcing-mapping-asset-build` `design.md:44-45` named a `builder.py` orchestrator and a `cli.py` entrypoint, and `proposal.md:28` named `cli.py` in the module list — but neither was ever tasked. There is no end-to-end command.
- **`z_policy` verdict never produced.** `binding.py`'s `ZPolicy` (`binding.py:955`), `ALLOWED_Z_POLICIES` (`binding.py:250` = `{canonical_orography, model_dem_at_cell_center, sentinel}`), and the required non-empty provenance checksum (`binding.py:984`) all assume an upstream verdict from the readiness change. Readiness (Epic #886) **intentionally descoped** the solver forcing-consumer audit (readiness proposal.md: *"Solver forcing-consumer auditing is intentionally out of scope"*), so no verdict exists, and `direct-grid-binding-artifact` spec §"Station coordinates and derived fields obey the tolerance rule" points at a deleted audit.
- **bbox guard unwired.** `canonical-source-grid-registry` Task 3.2 built `verify_download_bbox_matches_registry` (`packages/common/grid_registry_bbox_guard.py:136`) and pinned it to be *"importable and callable identically by the producer preflight … in follow-up changes"*, but wired no producer call site. Nothing enforces bbox↔registry consistency at production time.

Source of truth: docs/ForcingReplace §6 (mapping algorithm), §7 (binding, incl. §7.5 Z policy), §8.1 (forbidden runtime outputs), §5.1–§5.2 (registry / bbox pinning), §P0.3 (solver forcing-consumer audit — the descoped item this change narrowly revives). Oracle routing (CLAUDE.md): solver-source / production `.cfg` forensics on node-22; producer/bbox unit tests local + CI; no real DB.

## Goals / Non-Goals

**Goals:**

- Produce an authoritative, written `z_policy` verdict from a **narrow** solver audit (three questions only), committed to `evidence/`, and make it the single `z_policy` authority for the mapping builder.
- Fix the dangling `z_policy` spec reference in `direct-grid-binding-artifact` via a `## MODIFIED Requirements` delta (never editing `openspec/specs/` directly).
- Add `workers/mapping_builder/cli.py` that chains the existing G0–G5 library stages end to end, reads only object-store release-frozen packages, enforces §8.1 on the CLI path, and is deterministic.
- Wire `verify_download_bbox_matches_registry` into the direct-grid producer preflight as a fail-closed gate, and land the longitude-convention (0..360 vs -180..180) responsibility that registry Task 3.2 handed off.

**Non-Goals:**

- Do **not** re-audit the full solver. The audit answers only the three §7.5-gating questions (this is the narrow revival of the descoped readiness audit, not P0.3 in full).
- Do **not** modify any mapping-builder library stage, the contract parser (`workers/forcing_producer/direct_grid_contract.py`), or the runtime consumer.
- Do **not** rename the `ZPolicy.readiness_manifest_checksum` field (production-code rename is deferred — see "Naming debt").
- Do **not** register, activate, or migrate any basin; do not touch state (Change 5), routing (Change 4), display (Change 8), or scientific validation (Change 6).
- Do **not** change DB schema (migration head stays `000045`) or add real-DB tests.
- Do **not** add met-product orography to the registry (that unlocks `canonical_orography`; deferred).

## Decisions

### 1. `z_policy` verdict — narrow three-question audit (grill D10), verdict = `model_dem_at_cell_center`

The audit is deliberately the **narrow** revival of the readiness solver audit that Epic #886 descoped: it answers only the three questions §7.5/§P0.3 need to choose a `z_policy`, against pinned commit `3aec65755926c478e13ca7d4fea80715e4e90345`. Full findings with line-level citations are in `evidence/z-policy-solver-audit-verdict.md`; the load-bearing result:

- **(a) Read?** Yes — `SHUD/src/classes/TimeSeriesData.hpp:31` stores station `xyz[3]`; `ForcingProvider.hpp:33,63` exposes `z()` → `xyz[2]`.
- **(b) Used for?** Exactly one numerical consumer: temperature lapse correction at `SHUD/src/ModelData/MD_ET.cpp:32`, `t_temp = TemperatureOnElevation(t0, Ele.z_surf, xyz[2]) + gc.cTemp`, where `TemperatureOnElevation` (`SHUD/src/Equations/Equations.hpp:66-72`) returns `t + (Zt - Zi) * dTdZ` (`dTdZ = 0.0065 K/m`, `Macros.hpp:50`) unless either elevation equals `NA_VALUE` (`-9999`, `Macros.hpp:83`). Atmospheric **pressure** uses the *element* elevation (`Element.cpp:132 FixPressure = PressureElevation(z_surf)`), **not** the station `Z`.
- **(c) `.cfg` switch?** None — the lapse correction is unconditional in `tReadForcing`; the only disable path is data-level `Z == -9999`.

Per §7.5/§P0.3, `sentinel` is permitted *only* if station `Z` is proven unused. It is used, so an **explicit elevation source** is required. `canonical_orography` (met-product orography — the physically-correct `Zt`) is not available because the Change 2 registry stores no per-cell orography. `model_dem_at_cell_center` is an explicit source, preserves the baseline's lapse-downscaling behavior, and is derivable from assets the builder already reads (model DEM at cell center). **Verdict: `model_dem_at_cell_center`.** `canonical_orography` is recorded as the preferred upgrade once the registry carries orography.

The verdict evidence file is the authority. Resolution and sampling live in a NEW module `workers/mapping_builder/z_policy_verdict.py` (module home named so no existing library stage is modified): it pins the expected verdict value and the verdict file's SHA-256 as code constants — default resolution path is this change's `evidence/z-policy-solver-audit-verdict.md` and, post-archive, the same file under `openspec/changes/archive/<archive-date>-direct-grid-build-enablement/` (archiving relocates the whole change directory; the pinned checksum, not the path, anchors authority). Any path override is evidence-recorded and must still hash to the pin, else fail closed. The verified checksum passes through the existing `ZPolicy` provenance slot, and `per_cell_z` is derived for every used cell with the pinned sampler `nearest_mesh_node_elevation_v1`: transform the registered WGS84 cell center into the package CRS via the checksum-bound `gis/*.prj`, then take the `Elevation` of the nearest mesh node (planar distance in the package CRS, distance ties → smallest node `ID`). The rule is total over used cells — including centers outside the mesh hull, which nearest-cell ownership makes routine for boundary cells (guaranteed in small basins like the keliya fixture) — so out-of-hull never means a numeric default or a skip. A missing entry at binding time still fails closed (`ZPolicyCellMissingError`, never a numeric default).

### 2. Spec fix as a MODIFIED delta, not a direct edit

The dangling reference lives in `direct-grid-binding-artifact` §"Station coordinates and derived fields obey the tolerance rule" (scenario "x/y are recomputable and z follows the approved policy"). Per OpenSpec rules, the fix is a `## MODIFIED Requirements` block in `specs/direct-grid-binding-artifact/spec.md` carrying the **entire** requirement (all three scenarios) with only the `z` clause repointed. `openspec/specs/` is not touched; the delta applies at archive time. The two other scenarios (lon/lat cell-center equality; WGS84 basis) are reproduced verbatim so the MODIFIED block loses no detail.

### 3. `cli.py` orchestrates the existing stages; it does not re-implement them

The archived design named both `builder.py` (orchestrator) and `cli.py` (entrypoint); neither exists. Rather than introduce two files, `cli.py` is the single operator entrypoint that *chains* the already-shipped library stages in order — G0/G1 (`integrity`) → G2 + ownership (`algorithm`) → G4 rewrite (`rewrite`) → G5 binding (`binding`, consuming the §1 verdict) → evidence (`evidence`) — and writes the variant package only after all gates pass (fail-closed, no partial output). This matches the `workers/model_registry/` convention (focused modules + a thin `cli.py`) the archived design cited. No stage logic moves into the CLI.

### 4. Input authority is the object-store release-frozen package (enforced in the CLI)

The CLI's package-path resolver accepts only the release-frozen shape `<object-store-root>/models/basins_<basin>_shud/<release>/package/` and rejects any `Basins` dev-workspace path (node-27 `/home/ghdc/nwm/Basins/...`, node-22 `/volume/nwm/Basins/...`) unless an explicit `--allow-dev-workspace` flag is set (recorded in evidence with rationale) — the same discipline the archived `design.md` §Context established, now enforced at the entrypoint that operators actually invoke. The object-store root itself defaults to `/home/ghdc/nwm/object-store` and is changeable only via an explicit `--object-store-root` option whose non-default use is recorded in evidence (same discipline as `--allow-dev-workspace`); that override is the sanctioned test channel — local/CI runs stage the keliya fixture under a tmp root shaped `models/basins_keliya_shud/<release>/package/` rather than bypassing the resolver (the hardcoded `/home/ghdc/...` prefix does not exist on local/CI runners). A path that is neither object-store-shaped under the configured root nor a recognized `Basins` dev-workspace path fails closed.

### 5. §8.1 forbidden-runtime-output check is on the CLI path

The mapping stage must never emit cycle-dated `.tsd.forc`, per-station weather CSVs, or `met.*` rows (§8.1). The library already asserts this at the stage level; the CLI additionally asserts it over the final written variant tree so the guarantee holds for the actual operator invocation, not only in unit tests. Zero production writes: the CLI writes only into the new variant package tree and opens baseline files read-only.

### 6. Determinism

Same release-frozen package + same grid snapshot + same algorithm version ⇒ strictly byte-identical binding, `.sp.att`, evidence, and CLI-emitted manifest — raw-byte identity, no masking carve-out. The CLI adds no nondeterminism (no wall-clock in any emitted byte, stable ordering, no map/set iteration leakage); in particular it leaves the library's checksum-excluded `build_timestamp` evidence field (`EVIDENCE_CHECKSUM_EXCLUDED_FIELDS`, `workers/mapping_builder/evidence.py`) unset (`None`), so determinism holds at the raw-byte level rather than merely modulo excluded fields. Human-readable timing belongs in logs/stdout, never in emitted artifact bytes.

### 7. Producer bbox preflight — reuse the pinned guard, fail closed, own the longitude convention

The producer's direct-grid path calls `verify_download_bbox_matches_registry(registered_snapshot)` (default `env_reader=china_buffered_bbox_from_env`) at the top of the direct-grid branch of `workers/forcing_producer/producer.py::produce` — before the branch's first repository write (`ensure_direct_grid_met_stations` / `upsert_interp_weights` / any forcing-version write), i.e. before any direct-grid production side effect. On mismatch it raises `BboxMismatchError` and the production step aborts with no output (fail-closed). The guard is reused unchanged — no re-implementation — honoring the registry Task 3.2 pin that it be "callable identically by the producer preflight."

**Snapshot resolution + supersession (cross-change contract).** "The registered snapshot" is resolved from the verified contract identity — normalized `source_id`, `grid_id`, `grid_signature` — via the registry's supersession-aware current-version query (`find_snapshot_by_identity` semantics) plus a DB-only bbox/`superseded_at` read exposed through the producer's `ForcingRepository` protocol (new protocol method implemented in `workers/forcing_producer/store.py` and `file_store.py`; no `object_reader`, unlike the heavyweight `RegistryStore.load_snapshot`). No snapshot resolves → fail closed. `superseded_at` non-NULL → fail closed — this lands the producer-preflight half of `grid-drift-lifecycle` §"Consumers of a superseded snapshot fail closed" (the mapping-asset-build half already ships as `SupersededGridSnapshotError` in `workers/mapping_builder/algorithm.py`).

**Scope honesty: no downloads happen here.** `workers/forcing_producer/` performs no raw met downloads — those are issued upstream by `workers/data_adapters` (cycle-ingest, shared with legacy IDW basins, clipped from the same `NHMS_DOWNLOAD_BBOX_*` env). Download-time guarding is explicitly out of scope for this change (it would touch the legacy-shared path); the preflight's fail-closed guarantee therefore covers direct-grid forcing-production side effects, not upstream downloads. Preflight failures — `BboxMismatchError`, or a `ValueError` propagated from the env reader / the guard's finiteness gate — abort the run and are never swallowed.

**Longitude-convention responsibility landing (registry Task 3.2 hand-off).** `workers/data_adapters/region.py` fixes the canonical env convention as **-180..180** (`region.py:8-12,31-34`); GFS clips server-side in 0..360 (NOMADS) and IFS in -180..180 (cdo), both from the same `GeoBBox`. This change makes the direct-grid producer preflight the single site that asserts the deployment bbox is expressed in the registry's convention before it is compared to the snapshot, so a 0..360-vs-(-180..180) mix-up fails closed here rather than silently clipping a shifted region. (The signed-zero joint-normalization that registry Task 3.2 deferred remains a separate follow-up; this change does not change the guard's compare semantics.)

### 8. Naming debt (explicit, deferred)

`ZPolicy.readiness_manifest_checksum` (`binding.py:984`) is now a misnomer — the provenance it binds is this change's verdict evidence file, not a readiness manifest. Renaming a shipped production field is out of scope (would touch Epic #909 code and its tests). The field is reused as the provenance slot; the rename is recorded here as known debt for a future dedicated change.

## Risks / Trade-offs

- **Risk: `sentinel` looks "safe" because of the `-9999` branch, tempting a wrong verdict.** → The audit shows the branch merely *disables* a correction the solver otherwise applies; §7.5 forbids `sentinel` when `Z` is used. The verdict evidence file records the rejection with citations so the reasoning is auditable, not asserted.
- **Risk: `model_dem_at_cell_center` is an approximation of the true met orography.** → Accepted as the available explicit source; it preserves baseline lapse behavior. `canonical_orography` is documented as the preferred upgrade, gated on a registry orography column (out of scope here).
- **Risk: `cli.py` silently diverges from the library gates (drift returns).** → The CLI only *chains* stages and re-asserts §8.1 over the written tree; the keliya end-to-end test exercises G0→G5 through the CLI, so a stage bypass fails the test.
- **Risk: CLI reads a dev-workspace package and ships a non-frozen asset.** → Package-path resolver rejects non-object-store paths unless `--allow-dev-workspace` is set and recorded in evidence.
- **Risk: producer preflight added to the wrong call site (after a side effect).** → The gate must precede the direct-grid branch's first repository write in `producer.py::produce`; the preflight test asserts zero production side effects (no repository/store writes, no forcing output) when the bbox mismatches, when no registered snapshot resolves, or when the snapshot is superseded. (Downloads are not a producer side effect — they live upstream in `workers/data_adapters`; see Decision 7.)
- **Risk: MODIFIED delta drops detail at archive time.** → The full requirement block (all three scenarios) is reproduced; only the `z` clause changes.
- **Deviation carried into artifacts:** the task brief said "readiness #895"; the readiness change is **Epic #886** (per `docs/stage-pipeline-log.jsonl` and `binding.py` docstrings). Artifacts use #886.
