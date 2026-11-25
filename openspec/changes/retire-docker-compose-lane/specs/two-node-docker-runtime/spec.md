## REMOVED Requirements

### Requirement: One app image with role-specific startup

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: None. The requirement describes files and tests that are deleted with the lane.

### Requirement: Compute compose has write-capable production mounts

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: None. The requirement describes files and tests that are deleted with the lane.

### Requirement: Role-specific env examples and Docker preflight

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: `infra/env/display.example` stays as the template of the live `display.env`; what it must hold is in
`runtime-service-role-boundary` ("The display env template is a configuration the runtime guard accepts").
`infra/env/compute.example` is deleted; the compute units' templates are listed in `infra/env/README.md`.

### Requirement: Display compose has no physical control capability

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: What node-27 can still be shown not to have is read from the running unit: `runtime-service-role-boundary`, "The display deployment receipt observes the running unit". The template's role keys are in "The display env template is a configuration the runtime guard accepts".

### Requirement: Systemd and deployment docs

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: None. The requirement describes files and tests that are deleted with the lane.

### Requirement: Docker-runtime self-tests MUST yield deterministic verdicts across platforms and MUST prove the probe-stage FAIL path

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: None. The requirement describes files and tests that are deleted with the lane.

### Requirement: The run_docker_smoke TMPDIR-normalization invariant is machine-enforced in-file

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: None. The requirement describes files and tests that are deleted with the lane.

### Requirement: Scratch-writing host-contract tests clean up on failure paths without masking the failure

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: None. The requirement describes files and tests that are deleted with the lane.

### Requirement: Display compose port env key is canonical

**Reason**: The docker/compose deployment lane is retired (#3231): production runs systemd user units on both nodes and nothing builds or starts the image.

**Migration**: The port key of the live path is owned by `production-ops-readiness`, "Display API restart wrapper uses documented port env"; the template's value by `runtime-service-role-boundary`, "The display env template is a configuration the runtime guard accepts".
