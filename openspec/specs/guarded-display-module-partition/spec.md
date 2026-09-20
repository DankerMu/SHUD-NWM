# guarded-display-module-partition Specification

## Purpose
TBD - created by archiving change split-guarded-display-modules. Update Purpose after archive.
## Requirements
### Requirement: Display route facade keeps its patchable module surface

The system SHALL keep `apps.api.routes.hydro_display` behaving as the module that
owns the display route surface after implementation bodies move to owner modules:
every name that an existing test or script imports from it or patches on it SHALL
remain resolvable there, and a patch applied to that name SHALL still take effect
for the route that consumes it.

#### Scenario: Existing monkeypatch of a moved helper still takes effect

- **GIVEN** a test patches a private helper name on `apps.api.routes.hydro_display`
- **WHEN** that helper's implementation lives in an owner module and the route
  handler remains on the facade
- **THEN** the route handler resolves the patched name from the facade
- **AND** the test observes the injected fake rather than the real implementation

#### Scenario: Retargeted patch is proved to be live

- **GIVEN** a patch site had to be retargeted from the facade to an owner module
- **WHEN** the patch target is set back to the facade
- **THEN** the test fails, proving the retargeted patch is not inert

#### Scenario: Route and model identity is unchanged

- **WHEN** the split is applied
- **THEN** route paths, route handler function names, Pydantic model class names
  and the `hydro-display` router tag are byte identical to before the split

### Requirement: Runtime OpenAPI schema survives the patch-layer split

The system SHALL produce a byte-identical OpenAPI document after
`apps/api/openapi_patching.py` is partitioned, including patch application order
and finalization timing, and SHALL preserve function-object identity for the
patch entry points re-exported by `apps/api/main.py`.

#### Scenario: Static mirror still equals the runtime schema

- **WHEN** the runtime schema is generated after the split
- **THEN** it equals `openapi/nhms.v1.yaml` as a whole document
- **AND** `openapi/nhms.v1.yaml` is unchanged by this change

#### Scenario: Facade re-export preserves function identity

- **WHEN** `apps/api/main.py` re-exports a patch entry point
- **THEN** it is the same function object as the one the owner module defines
- **AND** no wrapper is interposed by the facade

### Requirement: Guard exemptions for hand-written display modules are retired

The system SHALL keep the six hand-written display modules under the
`.large-file-guard.json` line threshold without an exemption, and SHALL NOT
introduce an exemption for any module created by their partitioning.

#### Scenario: Exempt entries are gone and files are under threshold

- **WHEN** the change is applied
- **THEN** `.large-file-guard.json` contains none of the six paths
- **AND** every one of those files and every new partition is at or below the
  configured `maxLines`
- **AND** `apps/frontend/src/api/types.ts` remains exempt as a generated artifact

#### Scenario: A commit touching a partitioned file passes the guard unexempted

- **WHEN** a commit modifies one of the previously exempt files
- **THEN** the large-file guard permits it without consulting an exemption entry

### Requirement: Targeted CI selection covers every new partition

The system SHALL select every partition of a split test file and every owner
module of a split source file in the pull-request targeted-test lane, so that
partitioning does not reduce what the lane executes.

#### Scenario: Every collectible partition replaces the single target

- **GIVEN** a registry entry named a test file that is now partitioned
- **WHEN** the selector runs against a diff touching the guarded module
- **THEN** it selects every collectible partition of that file
- **AND** the lane executes assertions rather than degrading to collection-only

#### Scenario: New owner modules are inside the guarded closure

- **WHEN** a new display owner module is imported by non-gated modules
- **THEN** the guarded-module closure registry covers it
- **AND** the closure guard reports no gap

### Requirement: Migrated SQL and source-text assertions stay non-vacuous

The system SHALL keep every assertion that slices generated SQL by landmark, and
every assertion that reads a display module's source text, matching non-empty
content against the file that now holds that content.

#### Scenario: Landmark slice keeps its ordering preconditions

- **WHEN** a landmark-sliced SQL assertion moves to a test partition
- **THEN** its non-empty and slice-ordering preconditions move with it
- **AND** the assertion cannot pass by comparing empty strings

#### Scenario: Source-text assertion follows the moved code

- **GIVEN** an assertion reads a display module's file as text
- **WHEN** the asserted code has moved to another module
- **THEN** the assertion reads the module that now holds it
- **AND** the asserted substring is found in that file

### Requirement: Frontend overview contract and store modules keep their surface

The system SHALL preserve the import paths
`apps/frontend/src/lib/m11/overviewDataContracts.ts` and
`apps/frontend/src/stores/overviewData.ts` as the public surface after
partitioning, with unchanged export names and signatures.

#### Scenario: Importers and tests need no change

- **WHEN** both modules are partitioned behind barrels
- **THEN** every existing importer resolves the same exported names
- **AND** no test file under `apps/frontend/src` is modified
- **AND** typecheck, unit tests and production build pass

