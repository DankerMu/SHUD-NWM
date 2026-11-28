## MODIFIED Requirements

### Requirement: Parsed q_down publication
The system SHALL publish node-27-readable q_down display manifests and logs after successful parse.

#### Scenario: Parsed display artifacts written
- **WHEN** parse and publish complete
- **THEN** q_down display products, run manifest, station/river metadata, and bounded logs are written under the configured published artifact root
- **AND** DB records reference supported `published://`, publish-root `file://`, or allowlisted object-store URIs.

#### Scenario: Retired supplemental products are not required
- **WHEN** parsed SHUD discharge exists
- **THEN** the run may publish q_down display artifacts without any supplemental retired product
- **AND** it does not fabricate retired supplemental products or block q_down publication on their absence.

#### Scenario: Private workspace paths rejected
- **WHEN** a publish candidate references workspace-only, scratch-only, or non-allowlisted local paths
- **THEN** publication fails with a display-boundary blocker
- **AND** node 27 is not required to mount private compute workspace paths.

#### Scenario: Publish identity is strict
- **WHEN** publication writes latest-product or display manifest state
- **THEN** product identity includes `run_id`, `source`, `cycle_time`, `model_id`, basin version, river network version, forcing version, station count, and segment count
- **AND** downstream 27 evidence can verify it consumed the same run produced by the compute role.
