## ADDED Requirements

### Requirement: One discharge catalog identity per overview load
Within one `loadOverview` request generation whose map bootstrap succeeded with a `discharge` catalog entry, every resolution of the national discharge `(source, cycle)` pair SHALL read the `discharge` catalog entry of the bootstrap (runless) snapshot, so the keys written by layer-time enrichment and the keys read when deriving layer states and the precipitation overlay are the same. A run-scoped catalog fetched later in the same generation MUST NOT replace that entry.

#### Scenario: Default cycle flips between the two catalog fetches (no URL cycle)
- **WHEN** the runless catalog advertises discharge `default_cycle` C1 and the run-scoped catalog fetched in the same generation advertises C2 ≠ C1
- **AND** the URL carries no `cycle`
- **THEN** the discharge layer's `activeNationalCycle` is C1 and its valid times are the runless catalog's list
- **AND** the precipitation index is requested for C1 and the overlay resolves against C1 instead of staying hidden as pending

#### Scenario: Deep link to the runless default cycle
- **WHEN** the same flip happens and the URL carries `cycle=C1`
- **THEN** the discharge layer is available with the runless catalog's list and never shows the pending reason while no valid-times request is in flight

#### Scenario: Runless catalog has no discharge entry
- **WHEN** the runless catalog succeeds without a `discharge` entry and the run-scoped catalog carries one
- **THEN** the run-scoped discharge entry is used, as before this requirement

#### Scenario: Bootstrap failed
- **WHEN** the map bootstrap fails (for example the basin list rejects) and the run-scoped catalog succeeds
- **THEN** the run-scoped discharge entry is used, as before this requirement
