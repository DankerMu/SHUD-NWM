## ADDED Requirements

### Requirement: Narrow station membership probes keep the full membership index

The membership EXISTS in the narrow forcing-store station leg of the QHH
latest-product read, and in the display-coverage refresh, SHALL run as a
correlated sub-plan, fenced by a trailing `OFFSET 0`, so that it is never pulled
up into a semi-join. As a result, the `interp_weight_qhh_latest_membership_idx`
probe SHALL use `model_id`, `station_id`, `variable` and `LOWER(source_id)` as
its index condition. The bound `%(variables)s::met.forcing_variable[]`, the
text comparison `iw.variable = fst.variable_e::text` and the legacy leg SHALL
stay as they were. The legs SHALL return the same rows as before.

#### Scenario: a station has several interpolation variables

- **WHEN** a station has `interp_weight` rows for four variables, and a fact row
  for one of them is probed
- **THEN** each membership probe locates at most one index row by variable, and
  the plan has no join filter comparing `variable_e::text` with `iw.variable`.

#### Scenario: the bound variable list contains a duplicate

- **WHEN** the same variable is bound twice
- **THEN** the leg returns each fact row once, as before.
