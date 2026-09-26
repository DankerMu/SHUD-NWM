## ADDED Requirements

### Requirement: The db-free forecast runtime manifest SHALL conform to run_manifest.schema.json

Every forecast runtime manifest the db-free chain writes to `runs/<run_id>/input/manifest.json` SHALL validate against `schemas/run_manifest.schema.json` with zero violations. The schema SHALL declare every top-level key the builder writes, with root `additionalProperties: false` retained; the builder SHALL write the top-level `schema_version`; `runtime.executable` SHALL be optional because the SHUD runtime resolves the executable from its own configuration, not from the manifest. Fields the builder writes as null on a cold start or a packaged initial condition SHALL be declared nullable. The runtime manifest validator SHALL require only schema-declared fields and SHALL NOT reject any manifest it accepted before; conformance to the schema is enforced by regression tests on real builder output (warm start, cold start, packaged initial condition). No key already written SHALL be removed.

#### Scenario: A real builder manifest validates against the schema

- **WHEN** a db-free forecast cycle writes its runtime manifest for a warm-start, a cold-start, or a packaged-initial-condition basin
- **THEN** validating that `input/manifest.json` against `run_manifest.schema.json` yields no violations

#### Scenario: An undeclared key fails the contract

- **WHEN** a key not declared by the schema is added to a built runtime manifest
- **THEN** the schema validation fails
