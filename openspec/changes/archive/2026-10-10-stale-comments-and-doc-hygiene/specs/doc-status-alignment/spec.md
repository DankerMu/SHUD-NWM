## ADDED Requirements

### Requirement: The legacy inventory SHALL name the CI jobs that currently cover the frontend

The governed legacy / dead-code inventory SHALL describe the frontend's CI coverage as it is: the build job and the separate sharded mocked end-to-end job SHALL both be named in the rows for the CI workflow and the frontend app.

#### Scenario: Inventory rows after the mocked lane was split out

- **WHEN** a reader looks up the CI workflow row or the frontend app row of the inventory
- **THEN** both rows name the `frontend-build` job and the `frontend-e2e` job
