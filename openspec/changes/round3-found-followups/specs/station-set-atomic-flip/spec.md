## REMOVED Requirements

### Requirement: Cutover atomically flips the station set within the activation transaction

**Reason**: The station-flag flip hook is retired (#3164, owner decision 2026-11-16). No production code registers it at the `station_flag_flip` mount point, so it has never run in production. The only remaining production reader of `met.met_station.active_flag` is `workers/forcing_producer/store.py::load_met_stations`, and its `active_flag = true` legacy-set selection excludes the mirrors that registration writes inactive, so deleting the hook changes no behaviour. The station list stopped reading the flag in #2699, and #2773 retired the station MVT route.

**Migration**: None. `packages/common/station_set_flip.py` and its suites are deleted. The shadow-period guarantee stays owned by `direct-grid-variant-registration` ("Registration writes the met.met_station cell-station mirror with active_flag false") and by the `fixed-station-forcing-production` producer guard ("Direct-grid mirror maintenance preserves the registration-owned active_flag"). The historical receipts stay under `openspec/changes/archive/2026-11-15-direct-grid-display-cutover/`.

### Requirement: The flip hook engages only for direct-grid cutover activations

**Reason**: The station-flag flip hook is retired (#3164, owner decision 2026-11-16). No production code registers it at the `station_flag_flip` mount point, so it has never run in production. The only remaining production reader of `met.met_station.active_flag` is `workers/forcing_producer/store.py::load_met_stations`, and its `active_flag = true` legacy-set selection excludes the mirrors that registration writes inactive, so deleting the hook changes no behaviour. The station list stopped reading the flag in #2699, and #2773 retired the station MVT route.

**Migration**: None. `packages/common/station_set_flip.py` and its suites are deleted. The shadow-period guarantee stays owned by `direct-grid-variant-registration` ("Registration writes the met.met_station cell-station mirror with active_flag false") and by the `fixed-station-forcing-production` producer guard ("Direct-grid mirror maintenance preserves the registration-owned active_flag"). The historical receipts stay under `openspec/changes/archive/2026-11-15-direct-grid-display-cutover/`.

### Requirement: Shadow-period mirror stays inactive so no mixed display occurs

**Reason**: The station-flag flip hook is retired (#3164, owner decision 2026-11-16). No production code registers it at the `station_flag_flip` mount point, so it has never run in production. The only remaining production reader of `met.met_station.active_flag` is `workers/forcing_producer/store.py::load_met_stations`, and its `active_flag = true` legacy-set selection excludes the mirrors that registration writes inactive, so deleting the hook changes no behaviour. The station list stopped reading the flag in #2699, and #2773 retired the station MVT route.

**Migration**: None. `packages/common/station_set_flip.py` and its suites are deleted. The shadow-period guarantee stays owned by `direct-grid-variant-registration` ("Registration writes the met.met_station cell-station mirror with active_flag false") and by the `fixed-station-forcing-production` producer guard ("Direct-grid mirror maintenance preserves the registration-owned active_flag"). The historical receipts stay under `openspec/changes/archive/2026-11-15-direct-grid-display-cutover/`.
