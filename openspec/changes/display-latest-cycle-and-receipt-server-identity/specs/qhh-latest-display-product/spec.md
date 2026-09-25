## ADDED Requirements

### Requirement: Narrow station membership probes keep the full membership index

The narrow forcing-store station leg of the QHH latest-product read and of the
display-coverage refresh SHALL compare the station-membership variable as text
on both sides of `met.interp_weight.variable`. The bound variables SHALL be
provided as a de-duplicated text relation. The fact side SHALL be matched with
the cast applied to that relation's value, never to an indexed column. As a
result, the `interp_weight_qhh_latest_membership_idx` probe SHALL use all of
`(model_id, station_id, variable, LOWER(source_id))` as its index condition.
The legs SHALL return the same rows as before.

#### Scenario: a station has several interpolation variables

- **WHEN** a station has `interp_weight` rows for four variables and the request
  binds one of them
- **THEN** each membership probe locates one index row by variable, and the plan
  has no join filter comparing `variable_e::text` with `iw.variable`.

#### Scenario: the bound variable list contains a duplicate

- **WHEN** the same variable is bound twice
- **THEN** the leg returns each fact row once, as before.
