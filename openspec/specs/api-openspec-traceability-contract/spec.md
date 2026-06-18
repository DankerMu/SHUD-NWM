# api-openspec-traceability-contract Specification

## Purpose
TBD - created by archiving change m8-fourth-review-remediation. Update Purpose after archive.
## Requirements
### Requirement: OpenAPI success envelope does not conflict with endpoint data schemas

OpenAPI SHALL allow each endpoint to define the actual type of `data` without contradictory `allOf` constraints.

#### Scenario: Array data endpoints validate against OpenAPI

WHEN a success endpoint returns `data` as an array
THEN the OpenAPI schema for that response MUST validate the response
AND the shared success envelope MUST NOT require `data` to be an object.

#### Scenario: Object data endpoints remain documented

WHEN a success endpoint returns `data` as an object
THEN the endpoint-specific schema MUST document that object shape.

### Requirement: Forecast issue_time documents latest and datetime

The forecast-series API contract SHALL document all accepted `issue_time` values.

#### Scenario: latest issue time is documented

WHEN generated clients read the OpenAPI parameter for `issue_time`
THEN they MUST see that `latest` is an accepted default value
AND ISO 8601 datetime strings remain accepted.

#### Scenario: Generated frontend types stay fresh

WHEN OpenAPI changes the `issue_time` parameter or success envelope schema
THEN `apps/frontend/src/api/types.ts` MUST be regenerated
AND CI or tests MUST fail if generated types do not match the checked-in OpenAPI.

### Requirement: M4 OpenSpec strict validation passes

M4 IFS OpenSpec files SHALL use parseable requirement headings.

#### Scenario: M4 strict validation succeeds

WHEN `uv run openspec validate m4-ifs-multi-source --strict` is executed
THEN it MUST pass
AND all M4 specs under `openspec/changes/m4-ifs-multi-source/specs/` MUST parse at least one `### Requirement:` block with scenarios.

### Requirement: Delivery artifacts are tracked or explicitly excluded

Release evidence referenced by README, ROADMAP, or OpenSpec SHALL be traceable in version control or explicitly deferred.

#### Scenario: Referenced evidence is tracked

WHEN README or ROADMAP references OpenSpec tasks, docs, images, or README placeholders as evidence
THEN those files MUST be tracked in git or listed as intentionally excluded in the change notes
AND issue closure MUST include `git status --short --untracked-files=all` evidence.

### Requirement: GitHub issues trace this remediation

This remediation change SHALL be represented by one Epic and a bounded set of delivery-oriented child issues.

#### Scenario: Issue links recorded

WHEN issues are created
THEN `tasks.md` or a tracking section MUST record the Epic and child issue URLs
AND each child issue MUST link back to this OpenSpec change.

