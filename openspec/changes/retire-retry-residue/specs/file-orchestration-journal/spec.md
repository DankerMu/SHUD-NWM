## ADDED Requirements

### Requirement: Pipeline event writes SHALL NOT keep a write-only private runtime-root recovery lane

A pipeline-event write SHALL NOT write a private `runtime-root-recovery/` record under the journal root. The only reader of
those records fed the runtime-root resolution of the manual-retry submit lanes, which #3157 retired, so the records were
write-only. Runtime-root and DB-free runtime fields in event details SHALL still be redacted from every public surface
exactly as before. Records already on disk under `private/runtime-root-recovery/` are left in place: no reader, writer or
migration touches them.

#### Scenario: An event carrying runtime-root fields writes no private recovery record

- **WHEN** a `pipeline_event` whose details carry runtime-root or DB-free runtime fields is appended to the journal
- **THEN** no file is created under `<journal root>/private/runtime-root-recovery/`
- **AND** the public event rows still carry the runtime-root values only in their redacted form

#### Scenario: Pre-existing private recovery records are inert

- **WHEN** a journal root already holds files under `private/runtime-root-recovery/` from an earlier release
- **THEN** no journal code path parses, rewrites or deletes them; a whole-tree byte or inventory scan may still count them
