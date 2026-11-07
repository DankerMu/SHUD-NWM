## ADDED Requirements

### Requirement: Gateway-bound task payloads SHALL NOT carry candidate state evidence

The orchestrator SHALL remove the `state_evidence` key from every manifest entry it sends to the Slurm gateway and from every manifest index file it writes. This covers the array `tasks`, the stage manifest's `manifest_index`, and the publish stage's `basins`. The orchestrator's in-memory basins SHALL keep `state_evidence` unchanged.

#### Scenario: An array submission carries no state evidence
- **WHEN** the orchestrator submits an array stage for basins whose candidates carry `state_evidence`
- **THEN** no `tasks[*]` entry and no `manifest.manifest_index[*]` entry sent to the gateway contains `state_evidence`
- **AND** the written `<stage>_manifest_index.json` contains no `state_evidence`
- **AND** the orchestrator's active basins still carry their `state_evidence`

#### Scenario: The wire payload does not grow with recorded events
- **WHEN** two otherwise identical candidates carry 1 and 500 `state_evidence.pipeline_events` entries
- **THEN** their serialized gateway task entries are byte-identical
