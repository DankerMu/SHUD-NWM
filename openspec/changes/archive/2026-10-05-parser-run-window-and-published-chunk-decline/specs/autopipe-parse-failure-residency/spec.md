## ADDED Requirements

### Requirement: A published re-parse blocked by a compressed chunk is declined and visible

When the re-parse of a `published` run fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`, the autopipe SHALL record a `PUBLISHED_REPARSE_FAILED` decline whose detail starts with that code, SHALL leave the run `published`, and SHALL NOT fail the tick for that run again on the same evidence. The residency observer SHALL report the run. `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED` SHALL stay a retried failure. This is the one `OUTPUT_PARSE_`-prefixed code treated as terminal for a published run; the decline clears only by an operator re-run with `--force` or by deleting the decline row, because decompressing the chunk does not change the evidence the decline is keyed on.

#### Scenario: Compressed-chunk block on a published run

- **WHEN** a `published` run's re-parse fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED`
- **THEN** the run stays `published`, a `PUBLISHED_REPARSE_FAILED` decline whose detail starts with `OUTPUT_PARSE_COMPRESSED_CHUNK_BLOCKED: ` exists, and the next tick does not retry the same evidence or exit non-zero because of it

#### Scenario: Guard failure keeps retrying

- **WHEN** a `published` run's re-parse fails with `OUTPUT_PARSE_COMPRESSED_CHUNK_GUARD_FAILED`
- **THEN** no decline is written and the run is retried on the next tick
