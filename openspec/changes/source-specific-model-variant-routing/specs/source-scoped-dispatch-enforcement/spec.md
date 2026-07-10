## ADDED Requirements

### Requirement: Dispatch fails closed when the requested source is outside applicable_source_ids

The dispatch/staging variant-selection boundary SHALL fail closed when the requested `source_id` is not a member of the selected variant's `resource_profile.direct_grid_forcing.applicable_source_ids`, reusing the contract parser's normalized membership check, and SHALL NOT fall back to legacy IDW or any other source (INV-4 / INV-5 extension).

#### Scenario: A source in scope dispatches normally

- **WHEN** dispatch selects a direct-grid variant for a `(basin, source, cycle)` and the normalized requested `source_id` is a member of the variant's `applicable_source_ids`
- **THEN** the run proceeds for that source
- **THEN** the selected variant's contract resolves through the parser without a source-scope error.

#### Scenario: A source out of scope refuses to run with no fallback

- **WHEN** dispatch selects a variant whose `applicable_source_ids` does not contain the normalized requested `source_id`
- **THEN** dispatch fails closed before forcing production, surfacing the parser's `DirectGridContractError("Direct-grid contract does not apply to the current source.")`
- **THEN** no legacy IDW path is used, no other source is substituted, and no forcing is produced for that run.

#### Scenario: Source-scope is enforced at the dispatch boundary, not only in the producer

- **WHEN** the production route pairs an active model with a requested source and cycle — the candidate-assembly seam `services/orchestrator/scheduler_candidates.py:build_candidates`, upstream of `services/orchestrator/scheduler_execution.py:produce_forcing_for_candidates` invoking `forcing_producer.produce`
- **THEN** the source-scope membership check runs at that boundary, before forcing production (INV-5): an out-of-scope `(model, source)` pairing becomes a blocked candidate with recorded evidence and never reaches the producer
- **THEN** the check is not deferred to the producer alone; the producer's own parser check remains as inner defense in depth.

### Requirement: No cross-source substitution within a single run

The compute layer SHALL keep a single run single-source end to end, forbidding mid-run splicing of another source's grid cells and forbidding any legacy-package compatibility layer used to substitute sources (§2.3).

#### Scenario: A run stays single-source end to end

- **WHEN** a direct-grid run executes for a `(basin, source, cycle)`
- **THEN** every required variable for the run comes from the one requested source's canonical products
- **THEN** the `.sp.att` and forcing package for the run belong to the one selected variant.

#### Scenario: Mid-run splicing of another source is forbidden

- **WHEN** the requested source is missing a required variable or cell mid-run
- **THEN** the run does not splice in another source's cell values
- **THEN** the run fails closed rather than producing a mixed-grid result.

#### Scenario: No legacy-package compatibility layer substitutes sources

- **WHEN** a direct-grid variant is selected for a run
- **THEN** no legacy CMFD package or IDW compatibility layer is consulted to fill a source gap
- **THEN** substitution through a legacy fallback is not available at the compute layer.

### Requirement: A source missing data for a cycle yields no run for that source and cycle

The system SHALL treat a source that is missing data for a cycle as "no run for that `(source, cycle)`" with the reason recorded on the scheduler's existing blocked-candidate evidence surface (`services/orchestrator/scheduler_execution.py` `context.blocked_candidate(candidate, reason, state_evidence=…)`, the surface already used for `forcing_production_blocked`) under a named reason code that distinguishes missing source data, and SHALL leave cross-source availability to display best-available selection rather than compute substitution (§2.3 / §10). No new database table or ops row type is introduced for the record.

#### Scenario: Missing source data produces a recorded no-run, not a substitution

- **WHEN** the requested source has no usable data for a cycle
- **THEN** no run is produced for that `(source, cycle)`, and the scheduler pass evidence records a blocked-candidate entry whose named reason code identifies missing source data for the cycle
- **THEN** the compute layer does not substitute another source to fill the gap.

#### Scenario: Cross-source availability is a display concern

- **WHEN** one source is unavailable for a cycle while another source has produced output
- **THEN** cross-source availability is resolved by display best-available selection over already-produced per-source products
- **THEN** the compute layer does not merge sources to present continuous availability.
