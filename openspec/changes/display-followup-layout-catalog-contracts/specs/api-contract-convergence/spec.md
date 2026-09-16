## ADDED Requirements

### Requirement: Display lineage proposals are not delivered API promises
Current API documentation SHALL distinguish unimplemented display lineage proposals from registered, supported API endpoints. It SHALL NOT claim a delivered river-point/forcing-point/product lineage API or a display lineage UI after retirement of the basin detail lane. Existing model-asset provenance SHALL remain unaffected.

#### Scenario: Reader checks display lineage support
- **WHEN** a reader consults docs/spec/04_api_design.md section 9
- **THEN** the document explicitly states that all three proposed display lineage routes are unimplemented and the prior frontend call was removed
- **AND** no conflicting flat-object or nodes/edges response is presented as a current supported contract
