## ADDED Requirements

### Requirement: The forcing producer, the output parser and their CLIs MUST report guard-internal failures with codes distinct from compressed-chunk-blocked

`workers/forcing_producer/producer.py`, `workers/forcing_producer/cli.py`, `workers/output_parser/parser.py` and `workers/output_parser/cli.py` SHALL catch `CompressedChunkWriteError` before `CompressedChunkGuardError`, keep the existing `FORCING_COMPRESSED_CHUNK_BLOCKED` / `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` codes and `FORCING_PRODUCE_COMPRESSED_CHUNK_BLOCKED:` / `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED:` prefixes for the subclass only, and report `FORCING_COMPRESSED_CHUNK_GUARD_FAILED`, `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED`, or the CLI prefixes `FORCING_PRODUCE_COMPRESSED_CHUNK_GUARD_FAILED:` / `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED:` for the base class; a generic exception arm SHALL remain after both. The apply layer's `HANDOFF_APPLY_*` codes (already split) and the identity backfill's neutral tuple catches are outside this requirement and unchanged.

#### Scenario: Real compressed-chunk hit keeps its code

- **WHEN** the guard raises `CompressedChunkWriteError` inside the forcing producer or the output parser
- **THEN** the recorded `error_code` is `FORCING_COMPRESSED_CHUNK_BLOCKED` / `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED` and the CLIs print the existing `_BLOCKED:` prefix with exit 1

#### Scenario: Guard failure is reported as such

- **WHEN** the guard raises the base `CompressedChunkGuardError` (catalog timeout, partial window, unregistered hypertable)
- **THEN** the recorded `error_code` is `FORCING_COMPRESSED_CHUNK_GUARD_FAILED` / `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED` and the CLIs print the `_GUARD_FAILED:` prefix with exit 1

#### Scenario: Runbook routes only blocked codes to decompress

- **WHEN** an operator reads runbook §4.3.1 for a `_GUARD_FAILED` code
- **THEN** the procedure directs DB-health / caller-bug triage, not the manual decompress procedure
