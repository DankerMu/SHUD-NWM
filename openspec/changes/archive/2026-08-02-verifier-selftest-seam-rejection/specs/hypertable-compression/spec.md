# Spec Delta: hypertable-compression (live-evidence seam visibility)

## ADDED Requirements

### Requirement: A bundle whose run plan carries a self-test seam MUST never verify as PASS

The live-evidence verifier SHALL reject, with its refusal error
naming the offending token, any bundle whose
`execution.run_plan.captures[*].argv` contains a token starting with
the `--self-test-` seam prefix — before any PASS verdict is
reachable — so that "this bundle is production forensics" is a
structural fact of the verifier rather than a convention. The
rejection covers every current and future `--self-test-*` flag by
prefix, and the producer's hidden-flag surface is pinned: every
suppressed capture-CLI flag must itself use the seam prefix.

#### Scenario: Docker-seam bundle is rejected

- **WHEN** a bundle's run-plan capture argv (and its equality-bound
  ledger event) carries `--self-test-docker-seam`
- **THEN** `verify_bundle` raises the verifier's refusal error with
  a message containing `--self-test-docker-seam`, and no PASS
  verdict is produced

#### Scenario: Free-bytes seam bundle is rejected

- **WHEN** a bundle's run-plan capture argv carries
  `--self-test-free-bytes` with an injected value
- **THEN** `verify_bundle` raises the refusal error naming
  `--self-test-free-bytes`, so a fabricated disk-headroom figure
  cannot satisfy the rollback-feasibility gate inside a PASS

#### Scenario: Future hidden flags cannot dodge the prefix

- **WHEN** a new suppressed flag is added to the capture CLI whose
  option string does not start with `--self-test-`
- **THEN** the structural registration test fails, forcing the flag
  onto the rejected prefix before it can become a new invisible seam

#### Scenario: Hermetic self-test coverage survives without a new seam

- **WHEN** the hermetic e2e exercises the real state machine with
  seam-carrying execution argv on CI
- **THEN** the bundle it verifies presents a seam-free production
  plan (seams live only on the execution side, ledger identities
  rewritten by the test's established production-identity pattern,
  with the executed argv asserted to have carried the seams), and
  the verifier gains no acceptance flag or bypass of its own
