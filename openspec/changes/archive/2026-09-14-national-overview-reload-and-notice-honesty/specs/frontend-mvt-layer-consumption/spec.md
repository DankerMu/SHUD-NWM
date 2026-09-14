## ADDED Requirements

### Requirement: A cycle the source does not list is named, not reported as timeless
When the active national discharge pair is `(source, cycle)` with `cycle` absent from that source's arrived cycle list and the per-cycle valid-times list is empty, the discharge layer SHALL be disabled with a reason that names the source and the cycle. The reason MUST differ from `'Layer has no valid times.'`, from the fail-closed reason, and from the pending and error reasons for the active cycle's valid times.

#### Scenario: GFS-only cycle requested for IFS
- **WHEN** the URL is `?source=ifs&cycle=<C>` and the IFS cycle list has arrived, is non-empty and does not contain `<C>`
- **AND** valid times for `(ifs, <C>)` are an empty list
- **THEN** the discharge layer is unavailable and its `disabledReason` names `IFS` and `<C>`
- **AND** no national overlay is registered

#### Scenario: Symmetric and fabricated cycles
- **WHEN** the URL is `?source=gfs&cycle=<C>` with `<C>` listed only for IFS, or `?cycle=1999-01-01T00:00:00Z`
- **AND** valid times for the pair are empty
- **THEN** the same cycle-not-listed reason is used

#### Scenario: Listed cycle without coverage keeps the generic reason
- **WHEN** `<C>` is listed for the source but its valid-times list is empty
- **THEN** `disabledReason` stays `'Layer has no valid times.'`

#### Scenario: Unlisted cycle that still has valid times renders
- **WHEN** `<C>` is not listed for the source but its valid-times list is non-empty
- **THEN** the layer is available exactly as before this requirement

#### Scenario: Membership not yet known
- **WHEN** valid times for a non-default pair arrive empty before the source's cycle list has arrived
- **THEN** the layer shows the pending reason until the cycle list arrives and the reason is re-derived
