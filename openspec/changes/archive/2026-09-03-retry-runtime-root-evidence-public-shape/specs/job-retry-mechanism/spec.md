## ADDED Requirements

### Requirement: Manual-retry runtime-root evidence renders one public shape on both lanes

The `details.runtime_root_resolution` mapping carried by the retry route's `submission_failed` 503 SHALL have one public shape regardless of which retry lane produced it. Every scalar that is an absolute local path SHALL be rendered as `[local-path]`, every URI as `[uri]` or `[object-uri]`, and the response body SHALL contain no absolute local root text. Each `resolved.*` entry SHALL remain a mapping carrying `present`, `source` and the rendered `value` (plus `same_as_workspace` on `object_store_root` when both roots resolved); the public renderer SHALL recurse into mapping values under path-shaped keys instead of replacing them, while scalar values under those keys SHALL still render as `[local-path]`. The database lane SHALL apply that rendering when it reads the evidence for the response and SHALL leave the persisted event details unchanged; the file-journal lane SHALL apply it when it persists the evidence and SHALL return the persisted mapping unchanged. Secret redaction SHALL keep precedence over path rendering. Historical file-journal events that persisted a bare `[local-path]` string for a root entry SHALL be returned as recorded.

#### Scenario: Database lane 503 carries no absolute roots

- **WHEN** a database-lane manual retry resolves real `workspace_dir` and `object_store_root` values and the gateway submission raises
- **THEN** the 503 body contains neither root's text, `resolved.workspace_dir.value == "[local-path]"`, `resolved.object_store_root` is a mapping with `present`, `source`, `value == "[local-path]"` and `same_as_workspace`, and the persisted submission event still carries the real values

#### Scenario: Rejected URI candidates are placeholders on the wire

- **WHEN** a database-lane manual retry rejects a URL-valued candidate and the attempt ends in `submission_failed`
- **THEN** the 503 body's `rejected[].value` for that candidate is `[uri]` while the persisted event keeps the credential-stripped URL

#### Scenario: File lane keeps provenance for every resolved root

- **WHEN** a file-journal manual retry resolves `workspace_dir` and `object_store_root` and the submission is recorded
- **THEN** the persisted `runtime_root_resolution.resolved.object_store_root` is a mapping with `present is True`, a non-empty `source` and `value == "[local-path]"`, `resolved.workspace_dir` is unchanged in shape and value, and `runtime_root_contract.object_store_root` is still the scalar `[local-path]`

#### Scenario: Both lanes satisfy the same shape assertions

- **WHEN** the same shape assertions are applied to the database lane's and the file-journal lane's 503 `runtime_root_resolution`
- **THEN** both pass, and the file-journal response still equals its persisted event mapping

#### Scenario: Historical bare-string root entries pass through

- **WHEN** a file-journal submission event recorded before this change carries `resolved.object_store_root` as the bare string `[local-path]`
- **THEN** the route returns that entry as recorded and the 503 stays intact
