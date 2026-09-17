## ADDED Requirements

### Requirement: Strict warm-start manifest upgrade preserves pre-forecast resumes

Strict warm-start manifest reconciliation SHALL preserve an existing ordinary retry whose canonical restart stage is recognized before forecast, including convert and forcing. It SHALL retain the decision's original evidence and SHALL NOT manufacture a forecast retry merely because no forecast manifest exists yet. Stage normalization and restart_stage/restart_from_stage precedence SHALL follow existing canonical contracts.

#### Scenario: Convert-only candidate still builds forcing

- **WHEN** real candidate state decision returns resume_after_completed_stage at forcing from a succeeded convert cohort and strict warm-start evidence is ready but no forcing or forecast manifest exists
- **THEN** manifest reconciliation retains the forcing retry and the witness guard does not misclassify it as a forecast retry missing forcing
- **AND** the normal chain can submit its required forcing stage without fabricated witness or operator repair marker

#### Scenario: Aliases and precedence preserve recognized pre-forecast work

- **WHEN** a retry resolves to convert or forcing through a canonical alias or restart_from_stage fallback
- **THEN** its stage, decision and evidence remain unchanged through strict reconciliation
- **AND** a conflicting nonempty restart_stage follows existing precedence rather than the fallback

#### Scenario: Unknown stage gains no bypass

- **WHEN** restart stage is missing or not a recognized canonical pre-forecast stage
- **THEN** the new phase boundary grants no pre-forecast exemption and existing strict behavior remains intact

### Requirement: Forecast and later strict protections survive the phase boundary

Retries at forecast and later SHALL retain existing strict manifest reconciliation and per-model forcing witness enforcement. Terminal, quarantine, successor-state and explicitly authorized missing-forcing repair semantics SHALL remain unchanged.

#### Scenario: Late-stage mismatch still requires a protected forecast rerun

- **WHEN** a forecast, parse, state_save_qc, publish or copyback retry lacks a matching strict warm-start run manifest
- **THEN** existing strict forecast rerun behavior remains
- **AND** missing model-specific forcing still returns the stable FORCING_VERSION_ROW_ABSENT or missing-forcing blocker

#### Scenario: Matching manifest and specialized retry lanes retain behavior

- **WHEN** a late-stage manifest matches strict lineage, or an existing terminal/quarantine/successor/authorized-repair exception applies
- **THEN** existing action, reason, restart stage, retry-budget and force-resubmit semantics remain intact
