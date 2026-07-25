# scheduler-registry-refresh — delta (persist-runner-cutover-gate-audit)

## ADDED Requirements

### Requirement: The runner refresh receipt SHALL persist the normalized cutover_gate audit block whenever the runner constructs the audit block

When a scheduler file-provider refresh run constructs a cutover-gate audit block (registry publish path), the persisted refresh receipt SHALL carry that block, normalized by the shared normalizer, as a top-level optional `cutover_gate` key — so that gated and bypassed runs are distinguishable from the on-disk runner artifact alone; runs that fail before the block is constructed SHALL omit the key entirely (never persist a null placeholder), and the receipt JSON Schema and the runtime receipt validator SHALL both admit exactly the three normalized fields (`mode`, `declaration_env`, `declaration_present`) and reject additional or malformed fields over the same corpus.

#### Scenario: Registry-publish refresh persists the audit block

- **WHEN** a refresh run publishes the registry with the cutover gate
  enforced
- **THEN** the persisted refresh receipt SHALL contain a top-level
  `cutover_gate` object equal to the shared normalizer's output for the
  runner's audit block, including the observed `declaration_present`
  boolean (both the declaration-present and declaration-absent runs are
  representable and distinguishable)

#### Scenario: Runs failing before block construction omit the key

- **WHEN** a refresh run fails before the audit block is constructed
  (for example lock contention or a provider-preimage mismatch)
- **THEN** the persisted refresh receipt SHALL NOT contain a
  `cutover_gate` key

#### Scenario: Schema and runtime validator reject the same malformed blocks

- **WHEN** a refresh receipt carrying a `cutover_gate` block with an
  extra fourth field or a mode outside the audited mode set is validated
  against the receipt JSON Schema, or read back from disk through the
  runtime receipt validator
- **THEN** both validations SHALL fail

### Requirement: CLI registry-publish failure diagnostics SHALL carry a normalizer-produced cutover_gate block

When the registry-publish CLI exits non-zero on a publish, discovery, or provider error, the JSON payload written to stderr SHALL embed a `cutover_gate` block produced by the shared normalizer rather than an inline literal, so a failed run leaves the same audited three-field fact a successful summary would.

#### Scenario: Failure payload routes through the shared normalizer

- **WHEN** the CLI's publish call raises and the stderr error payload is
  emitted
- **THEN** the payload's `cutover_gate` SHALL be the shared normalizer's
  output for the CLI-constructed audit block
