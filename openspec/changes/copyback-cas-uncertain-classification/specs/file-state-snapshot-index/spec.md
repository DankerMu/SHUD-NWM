# Delta: file-state-snapshot-index (copyback-cas-uncertain-classification)

## ADDED Requirements

### Requirement: Destination-CAS uncertain and postcommit merge failures SHALL be classified commit-uncertain on the natural copyback path

The natural orchestration copyback path SHALL classify a state-index merge failure by the error's self-described phase, regardless of carrier: a bare classified provider error carrying a phase, or a state-manager error wrapping one with the phase preserved in its evidence. A failure whose phase places it at or past the destination compare-and-swap — release-uncertain, replace-uncertain, or postcommit — SHALL surface as the distinct commit-uncertain copyback error code, never as the fail-closed merge-failure code; only a failure with no self-described phase or with a pre-commit phase keeps the fail-closed code. This mirrors the operator replay tool's refusal contract (only audited pre-commit raise points prove the destination index unchanged), so the two operator surfaces give the same verdict for the same failure and future uncertain phases fail safe as uncertain. A postcommit restored-previous failure — where the provider verifiably rolled the destination back to its prior bytes — SHALL still classify as commit-uncertain, matching the replay tool's exclusion of it from the pre-commit allowlist: the merged bytes were transiently visible and "nothing happened" is not provable to the caller. Both the commit-uncertain and the fail-closed copyback errors SHALL carry the underlying failure reason in their details alongside the existing error text, so runbook triage can key on the reason under either code. The #1193 release-uncertain classification requirement is unchanged; this requirement widens the same distinct code to the remaining post-CAS family.

#### Scenario: rewrapped replace-uncertain failure surfaces as commit-uncertain with the committed fact assertable

- **WHEN** the destination compare-and-swap's atomic replace has executed but its durability or identity confirmation fails, and the provider error is rewrapped by the state manager with reason `provider_replace_uncertain` and phase `replace_uncertain` in its evidence
- **THEN** the natural copyback path raises the commit-uncertain copyback code (not the fail-closed code) with `error_reason` `provider_replace_uncertain` in details, and the destination index bytes hold the merged entries, so a caller or test can assert the commit as a fact

#### Scenario: post-CAS read-back failure surfaces as commit-uncertain

- **WHEN** the post-CAS read-back verification fails and no verified rollback succeeds, rewrapped with reason `provider_postread_failed`
- **THEN** the natural copyback path raises the commit-uncertain copyback code with that reason in details

#### Scenario: verified rollback still classifies commit-uncertain

- **WHEN** the post-CAS read-back fails but the provider restores the previous destination bytes and verifies the restoration (reason `provider_restored_previous`, phase postcommit)
- **THEN** the natural copyback path raises the commit-uncertain copyback code with that reason in details, the destination index bytes are the previous content, and the classification matches the replay tool's non-refusal verdict for the same reason

#### Scenario: pre-commit failures keep the fail-closed code

- **WHEN** the merge fails with a pre-commit phase (for example a preimage change) or with a state-manager index-validation reason that carries no phase
- **THEN** the copyback raises the fail-closed merge-failure code exactly as before, with the underlying reason now present in details

#### Scenario: runbook triage keys on one coherent verdict table

- **WHEN** an operator triages a copyback failure event per the production runbook
- **THEN** the fail-closed code means provably pre-commit (no unresolved uncertain family rides under it), and the commit-uncertain code enumerates the release-uncertain, replace-uncertain, and postcommit reasons with the entry-count check as the common next step
