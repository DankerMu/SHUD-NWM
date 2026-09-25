## ADDED Requirements

### Requirement: Latest-cycle discovery for forecast-series is driven from run metadata

When a forecast-series request resolves `issue_time=latest`, the store SHALL
find each requested scenario's latest cycle from `hydro.hydro_run`.
Candidate forecast runs of the requested basin that satisfy the request's
scenario and identity filters SHALL be taken in `cycle_time` descending order.
The scenario's cycle SHALL be that of the first candidate with at least one
`q_down` fact row for the requested basin, river network and segment. The store
SHALL NOT scan the segment's fact rows to compute the maximum. The per-scenario
result SHALL equal the maximum `cycle_time` over forecast runs that have such
rows. The discovery SHALL NOT add a run `status` predicate: a run with fact rows
is eligible whatever its status.

#### Scenario: the newest run of the basin has no rows for the segment

- **WHEN** the newest forecast run of the requested basin and scenario has no
  fact rows for the requested segment, and an older run of that basin and
  scenario does
- **THEN** that scenario resolves to the older run's `cycle_time`, never to the
  newest global or basin cycle.

#### Scenario: a superseded run is the latest with rows

- **WHEN** the only runs with fact rows for the segment at the latest cycle have
  status `superseded` or `failed`
- **THEN** that cycle is still returned, as before this change.

#### Scenario: several scenarios are requested

- **WHEN** a request names several scenarios
- **THEN** each scenario is resolved independently, and the result maps each
  scenario that has rows to its own latest cycle.

#### Scenario: the default request stays inside the buffer gate

- **WHEN** the production default request (`issue_time=latest`, a single segment,
  one or two sources) runs warm on node-27 for a segment that has retained fact
  rows
- **THEN** the discovery statement touches at most 5000 shared buffers.

#### Scenario: a segment has no retained fact rows

- **WHEN** no candidate run of the basin has retained fact rows for the segment
- **THEN** every candidate is probed once and the result is empty. The cost is
  bounded by the number of candidates times the number of chunks, and it is not
  covered by the 5000-buffer gate.
