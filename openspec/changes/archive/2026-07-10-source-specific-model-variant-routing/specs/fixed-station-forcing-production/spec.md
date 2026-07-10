## ADDED Requirements

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
