## ADDED Requirements

### Requirement: Best-available selections preserve model and basin lineage

Best-available selection records SHALL NOT overwrite unrelated model, basin, source, or forcing-version products that share the same `valid_time` and `variable`.

#### Scenario: Different models at same time are isolated

WHEN two model instances write best-available selections for the same `valid_time` and `variable`
THEN both selections MUST remain queryable
AND each selection MUST retain enough lineage to identify its model and forcing source
AND the unique key MUST include `forcing_version_id` or the domain dimensions needed to reproduce lineage, such as `model_id`, `basin_version_id`, and `source_id`.

#### Scenario: Different basins at same time are isolated

WHEN two basin versions write best-available selections for the same `valid_time` and `variable`
THEN one basin's selection MUST NOT overwrite the other
AND API or repository lookup MUST require or derive the intended basin/model dimension.

#### Scenario: Existing best-available rows are migrated safely

WHEN the best-available selection key is expanded
THEN the migration MUST define a deterministic backfill, rebuild, or cleanup strategy for existing derived rows
AND tests MUST document that strategy.

#### Scenario: Global product is explicitly documented

WHEN the system intentionally writes a global best-available product
THEN the schema and API MUST document the aggregation rule
AND the selected source lineage MUST remain reproducible.

### Requirement: Forecast-hour inputs are source validated

Data-source adapters SHALL validate caller-provided forecast hours before generating raw paths or valid times.

#### Scenario: GFS forecast hours obey configured range

WHEN GFS `build_manifest()` receives forecast hours
THEN negative hours, non-integer hours, duplicate hours, non-step-aligned hours, and hours beyond configured max lead MUST be rejected before manifest persistence.

#### Scenario: IFS forecast hours obey cycle-specific horizon

WHEN IFS `build_manifest()` receives forecast hours
THEN 06Z and 18Z cycles MUST reject hours beyond 144
AND 00Z and 12Z cycles MUST reject hours beyond 168
AND all hours MUST be non-negative, integer, unique, and step-aligned
AND unsupported IFS cycle hours MUST be rejected.

#### Scenario: ERA5 analysis hours are hourly within a day

WHEN ERA5 `build_manifest()` receives forecast hours
THEN only unique integer hours from 0 through 23 MUST be accepted.

### Requirement: Object store prefix isolates S3 URIs

The local object-store adapter SHALL reject S3-style URIs outside the configured object store prefix.

#### Scenario: Matching S3 prefix is accepted

WHEN `OBJECT_STORE_PREFIX` is configured and an object URI starts with that exact prefix
THEN the URI MAY be normalized to a repository object key and validated against storage layout rules.

#### Scenario: Prefix boundary is segment-aware

WHEN `OBJECT_STORE_PREFIX` is `s3://bucket/prefix`
THEN `s3://bucket/prefix/key` MAY match
AND `s3://bucket/prefix-other/key` MUST NOT match
AND URL encoded path traversal or `..` segments MUST be rejected.

#### Scenario: Mismatched S3 prefix is rejected

WHEN `OBJECT_STORE_PREFIX` is configured and an `s3://` URI uses a different bucket or prefix
THEN object-store normalization MUST reject it
AND MUST NOT silently strip the bucket and write under the local object root.

#### Scenario: Bare keys remain supported

WHEN a worker passes a bare object key such as `raw/gfs/...`
THEN the key MUST remain valid if it satisfies storage layout validation.
