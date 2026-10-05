## ADDED Requirements

### Requirement: Handoff apply does not rewrite an existing compatible station row

The forcing-domain handoff apply SHALL leave an existing `met.met_station` row physically unwritten when that
row satisfies the station identity predicate for the incoming station. It SHALL insert a station that does not
exist with `active_flag = false`, and it SHALL fail the whole apply with `HANDOFF_APPLY_STATION_CONFLICT`,
writing nothing, when an existing row does not satisfy the predicate.

#### Scenario: Re-applying the same handoff creates no new station tuple version

- **GIVEN** a handoff whose stations already exist in `met.met_station` with matching identity
- **WHEN** the handoff is applied again
- **THEN** each of those rows keeps its `ctid` and `xmin`
- **AND** the apply reports `applied`

#### Scenario: An incompatible existing station still fails closed

- **GIVEN** an existing `met.met_station` row whose basin version, coordinates, elevation, or name and role
  differ from the incoming station outside the direct-grid cache branch
- **WHEN** the handoff is applied
- **THEN** the apply raises `HANDOFF_APPLY_STATION_CONFLICT`
- **AND** the existing row and every other table are unchanged

#### Scenario: A missing station is inserted inactive and an active one stays active

- **GIVEN** a handoff with one station absent from `met.met_station` and one present with `active_flag = true`
- **WHEN** the handoff is applied
- **THEN** the absent station is inserted with `active_flag = false`
- **AND** the present station still has `active_flag = true`

#### Scenario: A predicate that cannot be decided is a conflict

- **GIVEN** an existing `direct_grid_cache` row whose `properties_json` has no `direct_grid` key, and an
  incoming `forcing_grid` station with the same id and a different name
- **WHEN** the station writer processes it without the pre-check having rejected it
- **THEN** it raises `HANDOFF_APPLY_STATION_CONFLICT`
