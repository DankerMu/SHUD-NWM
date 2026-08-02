# Spec Delta: hypertable-compression (capture tool-path values)

## ADDED Requirements

### Requirement: Run-plan capture argv tool-path values MUST match the committed production tooling

The live-evidence verifier SHALL reject any bundle whose run-plan
capture argv binds `--psql`, `--systemctl`, `--docker`,
`--journalctl`, `--git`, `--repo` or `--container` to anything other
than the committed production value (the `/usr/bin/*` host tools,
the production repo path, the production container name), or binds
`--database` to a value other than the run plan's validated
database — each binding present exactly once — and SHALL reject any
token that is an argparse-acceptable proper prefix of a pinned
option, so that an identity-anchored argv cannot point the committed
producer at substitute tooling. `--evidence-dir` and the
`--schema-dump-*` options stay deliberately unpinned (run-scoped
and legitimately parameterized data paths); the supervisor stays
value-unpinned on all tool options (the executor legitimately runs
hermetic plans with stub tools; the forensic claim is
verifier-owned).

#### Scenario: Stub tooling cannot verify

- **WHEN** a bundle's run-plan capture argv binds any pinned tool
  option to a non-production value (e.g. `--psql /tmp/stub-psql`),
  omits a pinned option, or binds it more than once (in either
  `--flag value` or `--flag=value` spelling)
- **THEN** `verify_bundle` refuses with an error naming the option,
  the observed bindings and the expected value, and no PASS verdict
  is produced

#### Scenario: A pinned option cannot be rebound by abbreviation

- **WHEN** a capture argv carries a token whose base is a proper
  prefix of any pinned option (e.g. `--ps`, `--do`, `--rep=/x`)
- **THEN** the verifier rejects the argv even though the full-name
  exactly-once binding looks clean, closing the last-wins rebinding
  class for the tool options the same way it is closed for the
  identity anchor

#### Scenario: The hermetic e2e still executes stub tools and verifies PASS

- **WHEN** the end-to-end test runs the real state machine with
  stub tools under a test checkout
- **THEN** the recorded plan carries production tool values while
  only the executed plan variant carries the stub paths, the ledger
  maps executed argv back to the recorded argv, and the bundle
  still verifies PASS — hermetic execution needs no gate weakening
