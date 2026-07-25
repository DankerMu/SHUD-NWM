# scheduler-registry-refresh — delta (persist-runner-cutover-gate-audit)

## ADDED Requirements

### Requirement: The runner refresh receipt SHALL persist the normalized cutover_gate audit block whenever the gate was evaluated

When a scheduler file-provider refresh run constructs a cutover-gate audit block (registry publish path), the persisted refresh receipt SHALL carry that block, normalized by the shared normalizer, as a top-level optional `cutover_gate` key — so that gated and bypassed runs are distinguishable from the on-disk runner artifact alone; runs that never evaluate the gate SHALL omit the key entirely (never persist a null placeholder), and the receipt JSON Schema SHALL admit exactly the three normalized fields (`mode`, `declaration_env`, `declaration_present`) and reject additional fields.

#### Scenario: Gated direct-grid refresh persists the audit block

- **WHEN** a direct-grid refresh publishes the registry with the cutover
  gate enforced
- **THEN** the persisted refresh receipt SHALL contain a top-level
  `cutover_gate` object equal to the normalizer's output for the runner's
  audit block, including the observed `declaration_present` boolean
  (both the declaration-present and declaration-absent runs are
  representable and distinguishable)

#### Scenario: Runs without gate evaluation omit the key

- **WHEN** a refresh run fails or completes without constructing a
  cutover-gate audit block
- **THEN** the persisted refresh receipt SHALL NOT contain a
  `cutover_gate` key

#### Scenario: Receipt schema is closed over the audit block

- **WHEN** a refresh receipt carrying a `cutover_gate` block with an
  extra fourth field is validated against the receipt schema
- **THEN** validation SHALL fail
