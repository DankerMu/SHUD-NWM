## ADDED Requirements

### Requirement: The frontend CI filter entries SHALL be pinned by a meta-test

The `frontend` paths-filter block of the CI workflow SHALL contain the entries for the workflow file itself, the frontend app tree and the OpenAPI tree, and a selector meta-test SHALL fail when any of them is removed from that block or moved to another filter block.

#### Scenario: An entry leaves the frontend block

- **WHEN** the workflow-file entry is deleted from the `frontend` filter block or moved under another filter block
- **THEN** the meta-test for the frontend filter fails
