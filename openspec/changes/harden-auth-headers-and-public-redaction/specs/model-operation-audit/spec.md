## ADDED Requirements

### Requirement: Lifecycle and active-model responses SHALL NOT expose raw mesh properties

The system SHALL omit `mesh_properties_json` from lifecycle and active-model responses: `POST /api/v1/models/{model_id}/lifecycle` and `PUT /api/v1/models/{model_id}/active` `data.model` and `data.previous_model` SHALL NOT contain `mesh_properties_json`, for every outcome
(`already_current`, `blocked`, `allowed`, `refused`) and for the audit-persistence failure 503 body.
No raw `source_path`, `resolved_source_path`, `package_checksum`, `source_inventory_checksum`, or
`manifest_uri` value from mesh properties SHALL appear in the serialized response.
`preflight.lineage.mesh_properties` keeps its existing audit redaction.

#### Scenario: Allowed lifecycle operation response carries no raw mesh properties

- **WHEN** a lifecycle operation is allowed for a model whose mesh version properties carry source paths and checksums
- **THEN** `data.model` and `data.previous_model` have no `mesh_properties_json` key and the response JSON contains none of the raw values

#### Scenario: Audit persistence failure body carries no raw mesh properties

- **WHEN** the lifecycle audit record cannot be persisted and the route answers 503
- **THEN** `error.details` contains no `mesh_properties_json` key and none of the raw values
