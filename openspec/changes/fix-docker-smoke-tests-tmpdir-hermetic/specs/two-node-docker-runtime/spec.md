# Spec Delta: two-node-docker-runtime

## ADDED Requirements

### Requirement: Docker-runtime self-tests MUST yield deterministic verdicts across platforms and MUST prove the probe-stage FAIL path

The docker-runtime self-tests in `tests/test_two_node_docker_runtime.py` SHALL produce deterministic verdicts on macOS (ambient `TMPDIR=/var/folders/...`) and Linux (ambient `TMPDIR` unset): hermetic tests normalize the process `TMPDIR` into the approved evidence root (`<repo_root>/artifacts/tmp`), platform-dependent path assertions compare canonicalized paths, and node-22 host-contract tests carry an explicit skip guard instead of failing on hosts without the contract. Tests asserting probe-stage or blocked-stage outcomes SHALL prove the outcome came from the intended cause, so a `TMPDIR_OUTSIDE_APPROVED_ROOT` preflight block can never satisfy — or silently bypass — the assertion.

#### Scenario: Required-probe failure yields FAIL through a passing preflight

- **WHEN** a `run_docker_smoke` self-test stubs a required probe
  (`image_absence_probe`, `compute_scheduler_command`,
  `display_startup_start`, or `display_startup_probe`) to return non-zero
  with `TMPDIR` normalized under `<repo_root>/artifacts/tmp`
- **THEN** the nested preflight evidence
  (`<evidence_root>/preflight/docker-preflight.json`) SHALL report
  `status == "PASS"`
- **AND** the smoke result SHALL be `status == "FAIL"` with the probe's
  expected blocker code present — proving the FAIL contract executed rather
  than being masked by `DOCKER_PREFLIGHT_BLOCKED`

#### Scenario: Blocked-outcome tests prove the intended blocker, not a TMPDIR side effect

- **WHEN** a self-test asserts a BLOCKED outcome caused by docker
  unavailability (the `..._when_preflight_blocks` test) with `TMPDIR`
  normalized under `<repo_root>/artifacts/tmp`
- **THEN** the nested preflight blockers SHALL contain the docker-unavailable
  blocker family and SHALL NOT rely on `TMPDIR_OUTSIDE_APPROVED_ROOT` to
  produce the BLOCKED status

#### Scenario: Environment-dependent tests are guarded, not silently red or vacuously green

- **WHEN** the full `tests/test_two_node_docker_runtime.py` suite runs on a
  macOS host with ambient `TMPDIR` outside approved roots, or on a Linux host
  with `TMPDIR` unset and no writable `/scratch/frd_muziyao`
- **THEN** every hermetic self-test SHALL produce the same verdict on both
  platforms, with explicit-TMPDIR assertions comparing canonicalized paths
  (`/tmp` vs `/private/tmp`)
- **AND** the two node-22 scratch-layout host-contract tests SHALL skip with a
  reason naming the writable `/scratch/frd_muziyao` requirement, while running
  unmodified where that root is writable
