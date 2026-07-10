## ADDED Requirements

### Requirement: Rehearsal preconditions are provisioned under the evidence-only identity

Because the readiness-archived synthetic registration (`openspec/changes/archive/2026-07-10-cmfd-direct-grid-platform-readiness/evidence/register-synth-p02.sql`) does not satisfy the real cutover path's preconditions — its `model_package_uri` is a GitHub tree URL with no stored package evidence (Change 4 preflight blocks it), its mirror rows are `station_role='forcing_proxy'` / `synth-station-001..003` with `grid_snapshot_id IS NULL` (not the Change 4 M1 mirror shape, so a correctly implemented flip selects zero of them as the target set), and no synthetic cycle/product exists to browse — the rehearsal SHALL first provision, entirely under evidence-only identifiers on the synthetic `basin_version` (`basin__evidence_cmfd_p02_synth__v1`) and recorded step-by-step in the receipt with matching cleanup/restore:

- (a) a synthetic previous-active baseline: a legacy-shaped evidence `core.model_instance` made `active` on the synthetic `basin_version` by recorded SQL — this baseline provisioning is a recorded bypass, because the rehearsed real-path lifecycle op is the M1 cutover activation, not the M0 baseline — with the readiness `synth-station-001..003` rows (legacy-shaped: `forcing_proxy`, `grid_snapshot_id IS NULL`) provisioned `active_flag=true` as its display set for the rehearsal window;
- (b) a Change-4-shaped synthetic M1 target: a direct-grid variant registered through the Change 4 registration surface, producing conforming mirror rows (`station_id = "<mapping_asset_identity>::cell:<grid_cell_id>"`, `station_role='direct_grid_cache'`, derived-cache `properties_json`, `grid_snapshot_id` FK to a registered synthetic canonical grid snapshot, `active_flag=false`) and carrying an object-store-hosted synthetic package URI plus a checksum rereadable from stored package evidence, so the Change 4 activation preflight passes;
- (c) a Change 5 explicit cold-start approval covering the synthetic variant's `applicable_source_ids`, so the engaged clone hook traverses its real, sanctioned approved-skip path (writing no clone row and recording the approval audit); the positive fingerprint-clone body is exercised by Change 5's own verification and is NOT re-proven by this rehearsal — the receipt records this scoping;
- (d) one seeded pre-cutover synthetic cycle: a ready `hydro.hydro_run` (`run_type='forecast'`, `cycle_time` earlier than the rehearsal cutover) plus its latest-product row bound to the synthetic baseline model, so the synthetic basin becomes discoverable in the `/` display's basin selector (`GET /api/v1/basins` `has_display_product` discovery resolves basins from ready forecast runs) and the popup path is reachable; the seeded run/product rows are deleted in the recorded cleanup.

#### Scenario: provisioning is recorded and evidence-only

- **WHEN** the rehearsal preconditions are provisioned
- **THEN** every provisioning step (baseline model + station flags, Change-4-shaped M1 registration with object-store-hosted package, cold-start approval, seeded run/product) is recorded in the committed receipt together with its cleanup/restore step
- **AND** all provisioned rows carry evidence-only identifiers under the synthetic `basin_version`; no production row is written.

#### Scenario: exercised-versus-bypassed steps are enumerated in the receipt

- **WHEN** the receipt is assembled
- **THEN** it enumerates the real-path steps exercised (Change 4 activation preflight, the engaged Change 5 clone hook via its sanctioned approved-skip path, the engaged station-flag flip hook, the supersede+activate swap, same-transaction audit, the post-commit scheduler registry manifest re-publish) versus the recorded bypasses (baseline provisioned by SQL instead of a lifecycle op; clone body skipped under the cold-start approval)
- **AND** the receipt's claims match what actually ran — nothing bypassed is claimed as rehearsed.

### Requirement: Rehearsal runs a real cutover transaction on a synthetic identity and restores it to non-active with zero production impact

The change SHALL, on node-27, run ONE real cutover transaction — the Change 4 `activate` lifecycle operation with mounted hooks — against the provisioned synthetic identity, capture the flip receipts while the flip is committed, and then RESTORE the synthetic identity: deactivate the synthetic model via the Change 4 `deactivate` lifecycle operation committed with the sys_admin missing-active override (returning the synthetic `core.model_instance` to a non-active `lifecycle_state`), and restore every synthetic `met.met_station` row (legacy-shaped and mirror) to `active_flag=false` by recorded SQL — the flip hook is pre-activation-only, so the deactivate does not un-flip station flags by itself. Zero production impact SHALL be proven by SQL assertions scoped to production rows, evaluated both during the committed rehearsal window and after the restore. The receipt SHALL be produced on the live host, not asserted by local ruff.

#### Scenario: rehearsal flips the synthetic set and captures a before/after MVT diff

- **WHEN** the rehearsal cutover transaction commits on the synthetic identity
- **THEN** the receipt records the station-MVT source identity for the synthetic `basin_version` before the flip as a source-version string over the provisioned synthetic legacy active set (a defined observation, not `MVT_SOURCE_IDENTITY_NOT_FOUND`) and after the flip as a different source-version string over exactly the conforming M1 mirror rows
- **AND** the before/after source-identity / feature-set diff is committed as part of the receipt.

#### Scenario: production basins are provably untouched during and after the rehearsal

- **WHEN** the rehearsal window is open (the synthetic model committed active) and again after the restore completes
- **THEN** a SQL assertion scoped to production rows proves, at BOTH observation points, that the 13 production basins' `met.met_station.active_flag` state is unchanged and the production-scoped active `core.model_instance` count (excluding `model_id LIKE 'model__evidence%'`, matching the archived `register-synth-p02.sql` post-check pattern) equals 13
- **AND** the receipt explicitly records the transient GLOBAL active count of 14 during the committed window as the expected rehearsal state — not a violation — and the global count returning to 13 after the restore.

#### Scenario: restore returns the synthetic identity to non-active and final assertions run after it

- **WHEN** the rehearsal ends
- **THEN** the synthetic model is deactivated via the Change 4 `deactivate` lifecycle operation committed with the sys_admin missing-active override, and every synthetic `met.met_station` row is restored to `active_flag=false`
- **AND** the final zero-impact SQL assertions run AFTER this restore; no evidence-only row remains display-active or lifecycle-active (the append-only lifecycle audit rows the rehearsal wrote remain, by design, and are recorded in the receipt).

#### Scenario: scheduler plane is clean after the rehearsal

- **WHEN** the rehearsal `activate` and the restore `deactivate` each commit
- **THEN** each re-publishes the scheduler registry manifest per the Change 4 dispatch-set-changing rule, and the receipt asserts the post-restore published manifest contains no `model__evidence%` model, so node-22's dispatch candidate set carries no synthetic model after the rehearsal
- **AND** a post-restore SQL assertion proves no `hydro.hydro_run` row was created for any synthetic model during the rehearsal window (the window is timed between scheduler cycle boundaries so the transient manifest exposure dispatches nothing).

#### Scenario: popup retention empty state is captured on the live host

- **WHEN**, after the flip and before the restore, the rehearsal browses the seeded pre-cutover synthetic cycle on `https://test.nwm.ac.cn` — the synthetic basin is selectable via the seeded ready run/product, and the popup resolves the seeded product's baseline `model_id` for that cycle — and opens a new M1 cell-station pin whose station-series disk file does not exist for that cycle
- **THEN** the `STATION_FORCING_FILE_NOT_FOUND` retention empty state is screenshot-captured on node-27 (`https://test.nwm.ac.cn` + `docker exec nhms-db psql`)
- **AND** it is not substituted by a local ruff pass, per the CLAUDE.md live-receipt red line.

### Requirement: Flow-curve cross-cutover continuity receipt is explicitly deferred, not silently skipped

The change SHALL explicitly record that the flow-curve cross-cutover continuity receipt is DEFERRED to the pilot's first real cutover, so the absence is a recorded deferral bound to backfill and is never misread as a passed verification.

#### Scenario: deferral is written into the receipt

- **WHEN** the rehearsal receipt is assembled
- **THEN** it records the flow-curve cross-cutover continuity receipt as DEFERRED-to-pilot
- **AND** it records the rationale that no production basin is activated here, so no real cross-cutover flow curve exists to sample.

#### Scenario: deferral is a recorded absence, not a certification gap

- **WHEN** the change is certified
- **THEN** the deferred flow-curve receipt is declared a recorded absence of evidence, not a certification gap
- **AND** it is bound to be backfilled at the pilot's first real cutover, so the deferral cannot be misread as a passed verification.
