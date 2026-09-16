# Spec Delta: readonly-db-and-e2e-evidence

## ADDED Requirements

### Requirement: The readonly validation DSN MUST carry its libpq options in a form libpq accepts

The readonly-DB validation lane SHALL encode the bounded connection options it writes into the display
connection URL so that a libpq client recovers the intended option string. Encoding conventions that only a
form decoder understands MUST NOT be used for this value.

The lane's regression suite SHALL prove this by opening a real connection with the emitted URL through the
libpq-direct client, not only through a driver that applies form decoding of its own.

#### Scenario: A bounded connection URL is handed to a libpq client

- **WHEN** the lane rebuilds the display connection URL with its bounded connection options and a client
  passes that URL directly to libpq
- **THEN** percent-decoding the emitted options value alone SHALL yield the lane's intended option string
  verbatim
- **AND** the connection SHALL open, reporting the lane's configured statement, lock and
  idle-in-transaction timeouts
- **AND** an automated test SHALL execute that connection against a real PostgreSQL, so reintroducing a
  form-encoded options value fails the suite instead of surfacing only as a connection `FATAL` on production
