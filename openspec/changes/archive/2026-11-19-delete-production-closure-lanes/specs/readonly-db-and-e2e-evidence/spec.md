## REMOVED Requirements

### Requirement: Cross-plane pass gates

**Reason**: The two-node E2E evidence lane that computed this verdict (`two_node_e2e_evidence`, `scripts/validate_two_node_e2e_evidence.py`) is deleted (#3221).
**Migration**: None. The display read-only boundary stays specified by "Display readonly DB validation" (C2) and cross-plane identity remains item C3 of `docs/runbooks/node-27-bringup-checklist.md`, a live check with no validator behind it after this change.

### Requirement: Docker and E2E evidence location

**Reason**: Replaced by "Docker evidence location": the two-node E2E artifacts scenario is cut with the deleted evidence lane (#3221), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement; the remaining scenarios are unchanged.

## ADDED Requirements

### Requirement: Docker evidence location

All project-created Docker smoke and codeagent review artifacts SHALL be written under the repository or `/scratch/frd_muziyao`.

#### Scenario: Stage change review artifacts
- **WHEN** OpenSpec review or planning agents produce output
- **THEN** the output is saved under `artifacts/stage-change/m22-two-node-docker-readonly-display/` or `/scratch/frd_muziyao/...`
- **AND** no project-created temporary evidence is written to the system disk by default.

#### Scenario: Docker smoke artifacts
- **WHEN** Docker build, compose config, or container security smoke tests run
- **THEN** test logs and generated evidence are written under the repository `artifacts/` tree or `/scratch/frd_muziyao`
- **AND** commands document any unavoidable Docker daemon cache usage separately.

#### Scenario: Docker disk preflight
- **WHEN** Docker build or compose smoke is about to run
- **THEN** evidence records `docker version`, `docker compose version`, DockerRootDir from `docker info`, `docker system df`, and relevant `df -h`
- **AND** low available space marks Docker smoke as `BLOCKED` before large build steps run.
