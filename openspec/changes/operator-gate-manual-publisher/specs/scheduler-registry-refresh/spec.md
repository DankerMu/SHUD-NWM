# scheduler-registry-refresh — delta (operator-gate-manual-publisher)

## ADDED Requirements

### Requirement: Manual publisher concurrency SHALL be operator-gated and the CLI SHALL warn about the refresh-timer prohibition on startup

The manual publisher CLI's concurrency with the provider-refresh timer is governed by an explicit operator prohibition (runbook), not by an `expected_preimage` CAS — the CLI SHALL print a startup WARNING line to stderr, unconditional for every run that reaches argument-validated startup (argparse usage errors, exit 2, are out of scope), naming the refresh timer unit and directing the operator to confirm the timer AND its oneshot service are not active; the warning SHALL NOT alter exit codes or corrupt the machine-readable stderr JSON payload (which remains parseable from the final stderr line), and the capability's governing documents — the `scheduler-registry-refresh` design/spec/tasks text and `docs/runbooks/current-production-ops.md` (§3.1.2 plus the manual-publisher entry) — SHALL NOT claim CAS protection for the manual-publisher path while `main()` does not populate `expected_preimage`.

#### Scenario: Startup warning is present on success and failure runs

- **WHEN** the manual publisher CLI runs to a successful publish, or exits
  non-zero on a publish/discovery/provider error
- **THEN** stderr SHALL contain the startup WARNING line naming
  `nhms-scheduler-file-provider-refresh.timer`
- **AND** on the failure run the existing JSON error payload SHALL still
  parse from the final stderr line with unchanged fields

#### Scenario: Design and runbook state the factual gating boundary

- **WHEN** an operator reads the D7#7 concurrency invariant or the
  runbook's manual-publisher section
- **THEN** both SHALL state that manual-publisher concurrency is
  operator-gated (explicit timer prohibition with a status-check command)
  and that the CAS parameter is exercised only by the internal refresh
  runner
