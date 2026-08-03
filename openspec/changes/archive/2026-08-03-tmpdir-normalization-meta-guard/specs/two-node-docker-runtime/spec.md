# two-node-docker-runtime — delta for tmpdir-normalization-meta-guard (#1211)

## ADDED Requirements

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
