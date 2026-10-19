## ADDED Requirements

### Requirement: The working-set CLI test SHALL not depend on the host's `/home` layout
`tests/test_node27_working_set.py` SHALL fake filesystem calls so that a configured path and its resolved form reach the same fake, evaluated at call time. The test SHALL then pass on a host where `/home` is a symlink and on one where it is not, without a platform skip. The production path resolution in `packages/common/node27_resource_governance_collection.py` SHALL stay unchanged.

#### Scenario: macOS host with a symlinked `/home`
- **WHEN** `test_cli_configured_paths_and_duplicate_device_labels` runs on a host where `/home` resolves to `/System/Volumes/Data/home`
- **THEN** all three parameters pass and the home filesystem still shares the working-set device identity
