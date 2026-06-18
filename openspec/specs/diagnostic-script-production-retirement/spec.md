# diagnostic-script-production-retirement Specification

## Purpose
TBD - created by archiving change m24-multibasin-continuous-daemon-live. Update Purpose after archive.
## Requirements
### Requirement: An enforceable guardrail proves production excludes QHH scripts
A guardrail artifact (test) SHALL prove that the production scheduler/chain path does not invoke QHH
diagnostic scripts. (m20 already states production must not depend on QHH scripts; m24 adds the
enforceable check, not a restated contract.)

#### Scenario: Guardrail asserts no QHH script invocation
- **WHEN** the guardrail test scans/executes the production cohort path
- **THEN** it asserts no call to `scripts/run_qhh_cycle.sh`, `scripts/run_qhh_continuous.py`, or
  `scripts/create_qhh_shud_manifest.py`
- **AND** the test fails if any production code path would invoke them.

#### Scenario: Retained diagnostic scripts have a defined smoke condition
- **WHEN** the diagnostic scripts are kept for manual debugging
- **THEN** their headers and docs/runbook mark them diagnostic-only and state a concrete smoke
  command with a minimal pass condition
- **AND** docs name the generic daemon as the supported production path.

### Requirement: Production manifest builder is the chain, not the QHH script
The production runtime manifest SHALL be produced by the chain's runtime-manifest assembly, not by
`scripts/create_qhh_shud_manifest.py` (which hardcodes the packaged calibrated state).

#### Scenario: Chain builds the warm-start-capable manifest
- **WHEN** a production forecast stage prepares its runtime manifest
- **THEN** the chain assembles it with the selected `initial_state.ic_file_uri` and `init_mode`
- **AND** the hardcoded packaged-state manifest from `create_qhh_shud_manifest.py` is not used in
  the production path.

