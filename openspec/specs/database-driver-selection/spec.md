# database-driver-selection Specification

## Purpose
Every SQLAlchemy engine the project creates for PostgreSQL names its DBAPI explicitly (psycopg2), so library default-driver changes cannot silently switch drivers. SQLAlchemy stays unpinned below 2.1.

## Requirements

### Requirement: PostgreSQL engines name their DBAPI explicitly
Every SQLAlchemy engine the project creates for a PostgreSQL URL SHALL use the psycopg2 driver explicitly. The engine SHALL NOT depend on SQLAlchemy's default driver for a plain `postgresql://` URL. The driver is chosen by normalising the URL through one helper before the engine is created.

#### Scenario: Plain PostgreSQL URL
- **WHEN** an engine is created from `DATABASE_URL=postgresql://user:pw@host:5432/db`
- **THEN** the engine's dialect driver is `psycopg2`, on both SQLAlchemy 2.0 and 2.1

#### Scenario: Non-PostgreSQL, explicit-driver or `postgres://` URL
- **WHEN** the URL is `sqlite:///…`, already names a driver, or uses the unsupported `postgres://` scheme
- **THEN** the helper returns it unchanged

#### Scenario: A new engine factory bypasses the helper
- **WHEN** code under `apps/`, `packages/`, `services/`, `workers/` or `scripts/` calls `create_engine` without the helper
- **THEN** the driver guard test fails and names the call site

### Requirement: SQLAlchemy is not capped below 2.1
The project SHALL NOT cap SQLAlchemy below 2.1, and the locked version SHALL be on the 2.1 line.

#### Scenario: Fresh pip install
- **WHEN** CI installs the project with `pip install -e ".[dev]"`
- **THEN** it may resolve SQLAlchemy 2.1.x, and the database test lanes pass
