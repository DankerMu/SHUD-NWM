## ADDED Requirements

### Requirement: A manual-retry marker SHALL restart a candidate from its failed stage when the upstream output is provably its own

When a manual-retry marker is the decision for a candidate, the scheduler SHALL resolve the failed stage with the same scope-blind failed-stage axis the restart router uses (so a failure recorded on a model-less cohort row counts). When that stage is `parse`, `state_save_qc` or `publish` (a stage after forecast), the manual-retry decision SHALL carry it as `restart_stage`/`restart_from_stage` exactly when the candidate's own forecast output is durable — its own hydro run is in a durable success status, or a native-SHUD terminal-success row attributable to the candidate (naming its model or run, or a recorded-membership `member` row) exists — the failure is not a forced native-SHUD rerun, and it is not a cold-start quarantined failure. The failure's permanence does not matter: the marker is the operator's authority to retry. When the failed stage is `forecast`, the decision SHALL carry `restart_stage=forecast` only when the per-model forcing witness for the candidate is found. The emitted restart stage SHALL pass the existing restart-stage upstream-artifact guards; when any guard does not pass — including the strict warm-start lane's post-decision manifest upgrade and forcing-witness consultation — the manual-retry decision SHALL drop the restart stage this requirement added and the candidate SHALL rerun the full chain; a manual retry is never turned into a blocker by a restart stage this requirement added. In every other case — failed stage `convert` or `forcing`, an unknown stage, an unprovable or `incomplete`-membership output — the decision SHALL carry no restart stage and the candidate SHALL rerun the full chain as before. A cold-start quarantined failure SHALL keep its forced `forecast` restart unchanged.

#### Scenario: A state_save_qc-only failure reruns only state_save_qc

- **WHEN** an operator marks a cohort candidate whose forecast succeeded (durable own output present) and whose `state_save_qc` failed
- **THEN** the next pass restarts that candidate at `state_save_qc`, no convert, forcing or forecast job is submitted for it, and the manual-retry attempt accounting is unchanged

#### Scenario: A multi-member cohort restarts once at the failed stage

- **WHEN** the operator marks the model-less cohort master of a multi-member cohort whose `state_save_qc` failed after its forecast succeeded
- **THEN** every member's basin manifest carries `restart_stage=state_save_qc`, the members form one restart cohort, and the submitted stages are `state_save_qc` (and later stages) only

#### Scenario: Unprovable upstream output reruns the full chain

- **WHEN** the marked candidate's failed stage is after forecast but its own durable SHUD output cannot be proven
- **THEN** the decision carries no restart stage and the candidate reruns from convert, identical to the pre-change behavior

#### Scenario: Convert or forcing failures are unchanged

- **WHEN** the marked candidate failed at `convert` or `forcing`
- **THEN** the decision is identical to the pre-change behavior

#### Scenario: A forecast failure without a forcing witness reruns the full chain

- **WHEN** the marked candidate failed at `forecast` and no forcing witness is found for its own model
- **THEN** the decision carries no restart stage, the candidate reruns the full chain, and it is not blocked

#### Scenario: The strict warm-start lane does not block a manual restart

- **WHEN** forecast warm start is required (strict lane), a manual-retry decision carries a restart stage added by this requirement, and the post-decision forcing-witness consultation finds no witness
- **THEN** the decision is the manual retry without a restart stage (full chain), not a blocker; a cold-start quarantined manual retry keeps its existing behavior
