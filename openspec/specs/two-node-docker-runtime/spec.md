# two-node-docker-runtime Specification

## Purpose
TBD - created by archiving change m22-two-node-docker-readonly-display. Update Purpose after archive.
## Requirements
### Requirement: One app image with role-specific startup

The Docker runtime SHALL build one default application image that can start role-specific commands through environment configuration.

#### Scenario: App image build
- **WHEN** `infra/docker/Dockerfile.app` is built
- **THEN** the image includes backend runtime dependencies and frontend static assets as required for MVP
- **AND** it does not install Slurm client or Munge by default.

#### Scenario: Role entrypoint
- **WHEN** the container starts with `NHMS_SERVICE_ROLE=display_readonly`
- **THEN** the entrypoint starts the display API/frontend service path
- **AND** it rejects compute-control-only commands or missing display requirements.

#### Scenario: Scheduler once command
- **WHEN** the compute compose runs a scheduler task
- **THEN** it uses an existing tested entrypoint such as `nhms-pipeline plan-production --plan`
- **AND** it does not reference a long-running scheduler loop unless that entrypoint exists and has tests.

### Requirement: Compute compose has write-capable production mounts

The compute compose file SHALL express the 22 node's compute-control capability without exposing it to 27.

#### Scenario: Compute compose mounts
- **WHEN** `infra/compose.compute.yml` is rendered or validated
- **THEN** compute services can mount Basins/model assets read-only, workspace read-write, and published artifact root read-write
- **AND** they use `NHMS_SERVICE_ROLE=compute_control`.

#### Scenario: Compute compose uses canonical publish-root names
- **WHEN** `infra/compose.compute.yml` is rendered or validated
- **THEN** host publish-root source configuration uses `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT` when it differs from the in-container root
- **AND** the container runtime target is `NHMS_PUBLISHED_ARTIFACT_ROOT`.

#### Scenario: Compute compose network exposure
- **WHEN** compute API or gateway ports are configured
- **THEN** they bind to localhost or an explicit internal control network by default
- **AND** the compose docs warn against exposing control endpoints publicly.

### Requirement: Role-specific env examples and Docker preflight

The Docker runtime SHALL provide role-specific env examples and preflight checks before large Docker work.

#### Scenario: Role-specific env examples
- **WHEN** `infra/env/compute.example`, `infra/env/display.example`, and shared env documentation are checked
- **THEN** they use canonical `NHMS_PUBLISHED_ARTIFACT_ROOT`, `NHMS_PUBLISHED_ARTIFACT_URI_PREFIX`, `NHMS_PUBLISHED_ARTIFACT_S3_BUCKET`, `NHMS_PUBLISHED_ARTIFACT_S3_PREFIX`, and optional `NHMS_PUBLISHED_ARTIFACT_HOST_ROOT`
- **AND** they document required and forbidden variables for compute and display roles.

#### Scenario: Docker disk preflight
- **WHEN** Docker preflight runs before build or smoke work
- **THEN** it records `docker version`, `docker compose version`, DockerRootDir, `docker system df`, `df -h`, `TMPDIR`, and the evidence root
- **AND** low space is reported as `BLOCKED` before build or smoke work continues.

#### Scenario: Dev compose is not production two-node compose
- **WHEN** production two-node static checks are run
- **THEN** `infra/docker-compose.dev.yml` is rejected as a production compute/display compose input
- **AND** the dev compose file remains available only for local development dependencies.

### Requirement: Display compose has no physical control capability

The display compose file SHALL encode 27 as a physically read-only display service.

#### Scenario: Display compose forbidden mounts
- **WHEN** `infra/compose.display.yml` is rendered or validated
- **THEN** the display service does not mount `/etc/slurm`, `/run/munge`, `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, `/var/run/docker.sock`, `.nhms-runs`, or 22 private `/scratch`
- **AND** the published artifact mount is read-only.

#### Scenario: Display compose forbidden env
- **WHEN** display env examples are checked
- **THEN** they do not configure `SLURM_GATEWAY_URL`, `SLURM_GATEWAY_BACKEND=slurm`, `WORKSPACE_ROOT`, `NHMS_BASINS_ROOT`, or `SHUD_EXECUTABLE`
- **AND** they set `NHMS_SERVICE_ROLE=display_readonly`, `NHMS_DISPLAY_DISABLE_CONTROL_MUTATIONS=true`, and `NHMS_DISPLAY_ALLOW_LOCAL_FILE_LOGS=false`.

#### Scenario: Display container security probe
- **WHEN** the display container is started in a Docker smoke test
- **THEN** checks show no `sbatch` or `scancel` executable, no `/etc/slurm/slurm.conf`, no Munge socket, and no Docker socket
- **AND** `/api/v1/slurm/*` is unavailable.

#### Scenario: Display HostConfig isolation
- **WHEN** `infra/compose.display.yml` is rendered and the display container is inspected
- **THEN** display services are not privileged
- **AND** they do not use host PID, host IPC, host network, broad host-root bind mounts, Docker socket mounts, or `cap_add`
- **AND** the display API uses a readonly root filesystem where feasible.

#### Scenario: Display published root readonly
- **WHEN** the display container is inspected
- **THEN** the published artifact mount is readonly
- **AND** its in-container target matches `NHMS_PUBLISHED_ARTIFACT_ROOT`.

### Requirement: Systemd and deployment docs

The Docker runtime SHALL include operator-facing systemd units and two-node Docker documentation.

#### Scenario: systemd units
- **WHEN** systemd unit examples are added
- **THEN** they start compute and display compose files from the repository `infra` directory
- **AND** the Slurm Gateway host-service unit is documented as the MVP-recommended first phase if independent gateway containerization is not yet proven.

#### Scenario: Two-node Docker README
- **WHEN** `infra/README.two-node-docker.md` is added
- **THEN** it documents 22/27 responsibilities, environment files, compose commands, scratch/evidence directories, security checks, and rollback
- **AND** it states that the dev compose file is not a production two-node deployment.

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

### Requirement: The run_docker_smoke TMPDIR-normalization invariant is machine-enforced in-file

A static in-file meta-guard SHALL enforce the file-wide invariant
that every `run_docker_smoke`-invoking test in
`tests/test_two_node_docker_runtime.py` normalizes the process
`TMPDIR` in its own body: the guard parses the file's own source,
collects every function (sync or async) that calls
`run_docker_smoke`, and fails — naming each offending function —
when any collected function lacks an in-body
`monkeypatch.setenv("TMPDIR", ...)` call whose target expression
carries the approved-root shape (contains both `artifacts` and
`tmp`), so the in-file counter-idiom `setenv("TMPDIR", "/tmp")`
cannot green the guard. The guard SHALL
assert its collected call-site set is non-empty so a broken
collector reds instead of greening, and SHALL carry no skip guard or
host dependency, so a missed normalization reds identically on
macOS, CI, and node-22 — instead of the historical failure shape
(CI green via the unset-`TMPDIR` fallback, macOS skipped, node-22
red with a misleading `BLOCKED != PASS`). Tests that deliberately
unset `TMPDIR` to verify the production fallback contract remain
outside the invariant: the guard keys strictly on `run_docker_smoke`
call sites.

#### Scenario: Missing normalization reds naming the function

- **WHEN** a test invoking `run_docker_smoke` is added or edited
  without an in-body `monkeypatch.setenv("TMPDIR", ...)` call, or
  with one whose target lacks the approved-root shape (e.g. the
  `"/tmp"` counter-idiom)
- **THEN** the meta-guard fails on any host and its failure message
  contains that test function's name

#### Scenario: Host-independent verdict

- **WHEN** the meta-guard runs on a host without a writable
  `/scratch/frd_muziyao` (e.g. a macOS dev machine — CI provisions
  that path and runs the Class C tests), where the Class C smoke
  tests themselves are skipped
- **THEN** the guard still executes and judges every call site,
  including the Class C ones, with the same verdict as on node-22

#### Scenario: Broken collector reds instead of greening

- **WHEN** the guard's AST matching collects zero
  `run_docker_smoke` call sites (matching bug or mass rename)
- **THEN** the guard fails its non-empty self-check rather than
  passing vacuously

### Requirement: Scratch-writing host-contract tests clean up on failure paths without masking the failure

Two named host-contract tests SHALL clean up the real `/scratch`
evidence they create on failure paths as well as success:
`test_source_trust_single_role_report_is_role_scoped_and_explicit_run_bound`
(`tests/test_two_node_docker_source_trust.py`) and
`test_static_report_explicit_evidence_run_id_overrides_scratch_path_inference`
(`tests/test_two_node_docker_runtime.py`) run their action and
assertions inside `try:` with cleanup in `finally:`, so an assertion
failure (contract regression or host drift) cannot strand debris on
the shared node that is indistinguishable from legitimate preflight
output. Their cleanup SHALL be non-raising best-effort
(`shutil.rmtree(..., ignore_errors=True)`; a suppressed
`unlink(missing_ok=True)`) so the `finally` can never replace the
original failure signal with a cleanup exception, and green-path
cleanup SHALL remain at least as complete as before the wrap. The
third family member — the Class C scratch-layout smoke test — keeps
its own #1128-delivered cleanup contract (see the existing
requirement covering it in this spec); this requirement neither
widens to future tests nor re-judges that one.

#### Scenario: Assertion failure still cleans the scratch tree

- **WHEN** a scratch-writing host-contract test fails one of its
  assertions after the evidence was written
- **THEN** the `finally` cleanup still removes the evidence
  artifacts (for the source-trust explicit-run test: both report
  files and both directory levels under
  `source-trust-explicit/`), leaving no debris on the shared node

#### Scenario: Cleanup never masks the failure signal

- **WHEN** the test's action fails before creating the evidence
  tree, the cleanup target is already absent, or the fixed path is
  occupied by an unexpected entry (e.g. a leftover directory where
  a file is expected)
- **THEN** the `finally` raises nothing — `rmtree` carries
  `ignore_errors=True` and the `unlink(missing_ok=True)` is wrapped
  in `contextlib.suppress(OSError)` (`missing_ok` alone does not
  cover `IsADirectoryError`/`PermissionError`) — and the pytest
  failure output shows the original error, never a cleanup
  `OSError`/`FileNotFoundError`

#### Scenario: Green path stays complete

- **WHEN** the test passes
- **THEN** the cleanup removes everything the pre-wrap manual
  sequence removed (the wrapped forms strictly cover the old
  unlink/rmdir set), and the suite's pass/skip distribution is
  unchanged on hosts where the test is skipped

