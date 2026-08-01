# Spec Delta: two-node-docker-runtime

## MODIFIED Requirements

### Requirement: Docker-runtime self-tests MUST yield deterministic verdicts across platforms and MUST prove the probe-stage FAIL path

The docker-runtime self-tests in `tests/test_two_node_docker_runtime.py` and `tests/test_two_node_docker_source_trust.py` SHALL produce deterministic verdicts on macOS (ambient `TMPDIR=/var/folders/...`) and Linux (ambient `TMPDIR` unset). In `tests/test_two_node_docker_runtime.py`, every test that invokes `run_docker_smoke` — hermetic and host-contract alike — normalizes the process `TMPDIR` into the approved evidence root (`<repo_root>/artifacts/tmp`) so no verdict depends on the ambient environment, and platform-dependent path assertions compare canonicalized paths; in both files, node-22 host-contract tests carry an explicit skip guard instead of failing on hosts without the contract, and the scratch-layout smoke test (`test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`) SHALL clean its scratch evidence tree up on failure paths as well as success. Tests asserting probe-stage or blocked-stage outcomes SHALL prove the outcome came from the intended cause, so a `TMPDIR_OUTSIDE_APPROVED_ROOT` preflight block can never satisfy — or silently bypass — the assertion.

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
  reason naming the writable `/scratch/frd_muziyao` requirement, while
  running with their full assertion set where that root is writable

#### Scenario: Source-trust host-contract test is guarded, not red, on hosts without writable scratch

- **WHEN** `tests/test_two_node_docker_source_trust.py` runs on a host where
  `/scratch/frd_muziyao` is not writable (e.g. macOS with a read-only root
  filesystem)
- **THEN** `test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`
  SHALL skip with a reason naming the writable `/scratch/frd_muziyao`
  node-22 host contract, using the same guard condition and wording as the
  runtime-file guards, and the file's remaining tests SHALL pass
- **AND** on hosts where `/scratch/frd_muziyao` is writable (node-22, CI with
  the provisioned scratch root) the test SHALL run with its full body and
  assertions unchanged

#### Scenario: Scratch-layout smoke test verdict is TMPDIR-independent and cleans up on failure

- **WHEN** `test_docker_smoke_explicit_evidence_run_id_binds_scratch_layout_and_nested_preflight`
  runs on a host with writable `/scratch/frd_muziyao` and an ambient
  `TMPDIR` outside the approved roots (e.g. `TMPDIR=/tmp` in a Slurm
  allocation or exported shell)
- **THEN** the test SHALL normalize `TMPDIR` into
  `<repo_root>/artifacts/tmp` before invoking `run_docker_smoke`, the nested
  preflight SHALL report `status == "PASS"`, and the test SHALL pass —
  never reporting a misleading `BLOCKED != PASS` caused by ambient
  environment noise
- **AND** if any assertion in the test fails, the scratch evidence tree
  `/scratch/frd_muziyao/nwm-test/run-smoke-explicit/` SHALL still be removed
  (cleanup in `finally`), with the deletion scope never wider than that
  directory
