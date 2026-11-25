## ADDED Requirements

### Requirement: Integration runs on the display node SHALL use a throwaway PostgreSQL instance and never the production postmaster

Integration tests create and drop one database per test on the server they
are pointed at. On the display node they SHALL be pointed at a throwaway
PostgreSQL instance started by `scripts/node27_integration_pg.py`, never at
the production postmaster. The tool SHALL start that instance from the image
id of the production container, on a loopback port other than the production
port, with its data under a directory it marks as its own, and SHALL NOT
stop, remove or reconfigure any container or directory other than its own.
The instance password SHALL NOT appear in an argument list or in the tool's
output. A pytest session that has opted in to integration and whose
integration URL names the production port SHALL end with a usage error
before any test runs; this is a tripwire for the documented production
endpoint, not containment.

#### Scenario: an opted-in session aimed at the production port is refused

- **WHEN** the session would run integration tests and its integration URL
  (dedicated, or the generic one under its compatibility flag) names port
  55432, or names no port while `PGPORT` is 55432
- **THEN** pytest ends with a usage error that names the tool, and no test
  runs

#### Scenario: other sessions are unaffected

- **WHEN** the session would skip integration (no opt-in, or no integration
  URL), or its integration URL names another port, or names no port while
  `PGPORT` is not 55432
- **THEN** the tripwire does nothing

#### Scenario: the instance is started from the production image on its own port

- **WHEN** `start` runs on a host where the production container exists
- **THEN** one container named `nhms-it-db` is created from that container's
  image id, published on `127.0.0.1` at a port that is not 55432, with
  memory, CPU and log limits, and the password reaches it through an
  environment file readable only by the caller

#### Scenario: the production port is refused as the instance port

- **WHEN** `start` is asked for port 55432
- **THEN** it exits non-zero and creates nothing

#### Scenario: a directory that is not the tool's own is never removed

- **WHEN** `start` or `stop` meets a non-empty directory without the tool's
  sentinel
- **THEN** the directory is left untouched and the command says so

#### Scenario: only the tool's own container is removed

- **WHEN** `stop` runs
- **THEN** the only container it removes is the `nhms-it-db` that carries
  the tool's label for that instance directory, and it refuses while a
  `run` of that instance is alive unless forced

#### Scenario: run hands the URL to the command only

- **WHEN** `run -- <command>` executes with a running instance
- **THEN** the command's environment carries the integration opt-in and the
  instance's URL, and the tool prints neither the URL nor the password

#### Scenario: an instance that differs from production where tests would notice is not handed out

- **WHEN** the started cluster's collation or encoding differs from the
  expected pair, or it does not become ready in time
- **THEN** `start` removes what it created and exits non-zero
