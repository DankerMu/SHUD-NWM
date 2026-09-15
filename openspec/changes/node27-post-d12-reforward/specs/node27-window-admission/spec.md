## ADDED Requirements

### Requirement: Post-D12 re-admission binds a fresh state to retained provenance

After a D12 recovery that retained the narrow table, the executor SHALL admit a new re-forward only through a fresh
`reprepare` state. Admission SHALL repeat every runtime admission of initial preparation and SHALL verify, read-only,
hash-pinned prior D12 provenance against the live catalog, ledger, route column, narrow table shape and every retained
run before the state reaches `REPREPARED`. It MUST NOT resume, overwrite or write the prior state.

#### Scenario: Retained-D12 state with unchanged provenance

- **WHEN** the live catalog is exactly canonical = prior OLD OID and `river_timeseries_narrow_rollback` = prior narrow
  OID, the ledger equals prior history plus `000059` with no pending file, and every retained `run_key` matches the
  prior snapshot's `run_id` and `parsed_at` with status `parsed` or `published`
- **THEN** a fresh state records both OIDs, the ledger, the retained runs, copies of the pinned provenance and the
  route inventory, and ends in phase `REPREPARED` with mode `reforward`

#### Scenario: Catalog, ledger, ownership or provenance diverges

- **WHEN** a table is extra, missing or foreign; an owner changed; the ledger differs or a migration is pending; a
  provenance file hash differs; a retained run changed since D12; or the rollback table holds a `run_key` absent from
  provenance
- **THEN** admission refuses with a typed check and changes no catalog, ledger, route, service or prior state

### Requirement: Re-forward reattaches the retained narrow table under a verified fence

`reforward` SHALL accept only a `REPREPARED` reforward state with the signed GO and unchanged config, driver, boot,
source and ledger. It SHALL repeat re-admission before T0 and inside the drained fence, then in one transaction verify
both OIDs, rename OLD to `river_timeseries_legacy` and the retained table to `river_timeseries`, and derive routes:
retained runs `narrow`, other parsed/published runs `legacy`, unparsed runs `narrow`. It MUST NOT delete ledger rows,
drop the rollback table or execute `000059`. Validation SHALL require the admitted OIDs and derived routes, and proofs
SHALL include a real NEW parse, a narrow read, a retained-run read and the unchanged legacy read before startup.
The initial `window` path SHALL accept only `PREPARED` initial states and remain unchanged.

#### Scenario: Re-forward after OLD-window writes succeeds

- **WHEN** runs were created with the default route and parsed by OLD after D12, and re-forward is executed
- **THEN** OLD-window runs route `legacy` and stay readable, retained runs route `narrow` and read their original facts,
  a new unparsed run parses into the reattached table, both OIDs and the ledger are unchanged, and the window reaches
  its validated milestone

#### Scenario: Phase or mode is not the expected transition

- **WHEN** `reforward` is given an initial `PREPARED` state or `window` is given a `REPREPARED` state
- **THEN** it refuses before any service, catalog or ledger action

#### Scenario: Live state drifts after re-admission

- **WHEN** retained provenance or source identity changes between `reprepare` and `reforward`
- **THEN** re-forward refuses before T0 without stopping services

### Requirement: Re-forward failure recovers OLD with all data retained

Any failure after re-forward admission SHALL run the existing D12 recovery. When the reattached catalog is present,
recovery SHALL additionally require the canonical OID to equal the admitted narrow OID. Recovery SHALL preserve both
OIDs, all facts in both tables, the ledger, and write a fresh route snapshot usable as provenance for a later attempt.

#### Scenario: Interruption after reattach commits or during readiness

- **WHEN** the executor is killed after the reattach transaction commits, killed at display start, or readiness fails
- **THEN** recovery (repeated twice) restores OLD canonical and the retained narrow table with unchanged OIDs, retained
  and newly parsed narrow facts, OLD facts and ledger, restores only authorized timers, and a later fresh re-admission
  from the new state succeeds read-only
