## ADDED Requirements

### Requirement: Published OpenAPI MUST be valid for its declared dialect

The runtime `/openapi.json` document and `openapi/nhms.v1.yaml` MUST declare the same OpenAPI 3.1 version, MUST be semantically equal, and MUST contain no OpenAPI-3.0-only `nullable` keyword. Every value accepted as null before this repair MUST remain nullable through JSON Schema 3.1 union semantics, and unchanged business schemas MUST generate byte-identical frontend TypeScript.

#### Scenario: Ordinary nullable schema preserves its contract

- **WHEN** a hand-patched schema with an ordinary scalar, array, or object `type` is nullable
- **THEN** the published schema contains a 3.1 type union with `null`, preserves format/description/items/bounds/additional-properties siblings, and generates the same TypeScript value-or-null type

#### Scenario: Composed nullable schema preserves its contract

- **WHEN** the nullable `Layer.metadata` schema combines a type with `allOf`
- **THEN** the published schema expresses the complete composition or null without producing an extra intersection, and generated TypeScript remains `LayerMetadata | null`

#### Scenario: Runtime and static contract cannot diverge

- **WHEN** either the patch owner or static YAML changes without the other
- **THEN** the exact runtime/static drift test fails and the artifact cannot be accepted

### Requirement: OpenAPI server and security metadata MUST describe the enforced boundary

The published OpenAPI contract MUST declare the same-origin server and explicit anonymous-by-default root security. Every operation protected by the current auth/RBAC middleware or dependency MUST override that root with the conditional credential alternatives actually accepted by the server; public operations MUST NOT inherit a false global authentication requirement. Security descriptions MUST state their environment conditions and MUST NOT contain credential values.

#### Scenario: Public operation remains public by default

- **WHEN** a caller or generator inspects one of the 43 currently unprotected operations
- **THEN** it inherits root `security: []` and no global bearer requirement is asserted

#### Scenario: Protected operation is never documented as anonymous

- **WHEN** a caller or generator inspects any of the 11 protected mutation/retry/cancel operations
- **THEN** that operation overrides root security with alternatives for the enabled non-production role header, configured non-production bearer token, or the complete internal live-proof header set

#### Scenario: Runtime authorization behavior is unchanged

- **WHEN** an anonymous, unauthorized, release-blocked, or authorized request reaches a protected operation
- **THEN** the existing 401/403/503/allow decision and no-mutation guarantees remain unchanged; this schema repair adds no auth bypass or policy decision

#### Scenario: Contract contains no secret

- **WHEN** security schemes and operation requirements are serialized
- **THEN** only scheme/header names and conditions appear, and no configured token, actor, credential, or signed value is embedded
