# model-operation-audit Specification

## Purpose
TBD - created by archiving change m18-model-asset-operations. Update Purpose after archive.
## Requirements
### Requirement: Model Operation Audit
Every model lifecycle operation SHALL produce redacted, queryable audit evidence.

#### Scenario: Successful operation
WHEN activation, deactivation, version switch, or rollback succeeds
THEN audit records actor, `roles[]`, action id, model_id, basin_version_id, previous/new active state, reason, request id, and lineage checksums.

#### Scenario: Blocked operation
WHEN preflight or RBAC blocks an operation
THEN evidence records the blocker reason without mutating model state.

#### Scenario: Sensitive lineage
WHEN model lineage contains local paths or sensitive URI components
THEN audit output redacts them using public-safe projection.

### Requirement: Lifecycle and active-model responses SHALL NOT expose raw mesh properties

The system SHALL omit `mesh_properties_json` from lifecycle and active-model responses: `POST /api/v1/models/{model_id}/lifecycle` and `PUT /api/v1/models/{model_id}/active` `data.model` and `data.previous_model` SHALL NOT contain `mesh_properties_json`, for every outcome
(`already_current`, `blocked`, `allowed`, `refused`) and for the audit-persistence failure 503 body.
No raw `source_path`, `resolved_source_path`, `package_checksum`, or `source_inventory_checksum` value
from mesh properties SHALL appear in the serialized response. Other `manifest_uri` fields on the model
projection (for example `resource_profile.manifest_uri`) keep the existing public URI sanitizer policy
(object-store URIs kept, local paths nulled), identical to `GET /api/v1/models/{model_id}`.
`preflight.lineage.mesh_properties` keeps its existing audit redaction.

#### Scenario: Allowed lifecycle operation response carries no raw mesh properties

- **WHEN** a lifecycle operation is allowed for a model whose mesh version properties carry source paths and checksums
- **THEN** `data.model` and `data.previous_model` have no `mesh_properties_json` key and the response JSON contains none of the raw values

#### Scenario: Audit persistence failure body carries no raw mesh properties

- **WHEN** the lifecycle audit record cannot be persisted and the route answers 503
- **THEN** `error.details` contains no `mesh_properties_json` key and none of the raw values

