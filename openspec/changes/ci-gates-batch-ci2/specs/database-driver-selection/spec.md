## MODIFIED Requirements

### Requirement: SQLAlchemy is not capped below 2.1
The project SHALL NOT cap SQLAlchemy below 2.1, and the locked version SHALL be on the 2.1 line.

#### Scenario: Fresh pip install
- **WHEN** CI installs the project with `uv sync --locked --all-extras --dev`
- **THEN** it installs the locked SQLAlchemy 2.1.x, and the database test lanes pass
