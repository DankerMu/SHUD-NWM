# Spec Delta

## Purpose
Critical modules carry a scoped AGENTS.md so agents read module boundaries and verification commands before editing.

## ADDED Requirements

### Requirement: Nine critical modules SHALL have a scoped AGENTS.md
`packages/common`, `services/precip`, `workers/model_registry`, `workers/data_adapters`, `workers/forcing_producer`, `workers/mapping_builder`, `workers/grid_registry`, `services/slurm_gateway` and `scripts` (the nine Context-🔴 modules of audit §1) SHALL each contain an `AGENTS.md` with the headings `Required Reading`, an ownership or boundary section, a fences or error-model section, and `Focused Verification`, mirroring the four existing scoped files. Every relative path the file references SHALL exist and every verification command SHALL be runnable from the repository root. The governance docs gate SHALL pass.

#### Scenario: References resolve
- **WHEN** a link scan (relative paths and backticked repo paths) runs over the nine files
- **THEN** zero dead references are reported

#### Scenario: Governance gate accepts
- **WHEN** `uv run pytest -q tests/test_entropy_audit_report_contract.py` runs
- **THEN** it passes
