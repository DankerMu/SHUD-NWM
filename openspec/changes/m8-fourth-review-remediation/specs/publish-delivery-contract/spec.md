## ADDED Requirements

### Requirement: Publish stage success requires delivery evidence

The publish stage SHALL be reported as successful only when it produces verifiable delivery artifacts.

#### Scenario: Publish implementation creates artifacts

WHEN the `publish` stage exits with success
THEN the system MUST have written the expected map/tile/delivery metadata in `map.tile_layer` or `map.tile_cache`, or the expected object-store artifacts under the documented tile key prefix
AND tests MUST assert those side effects, not only command existence.

#### Scenario: Publish is not implemented

WHEN tile publication is not implemented for the release
THEN `nhms-pipeline publish-tiles` MUST NOT exit as successful publication
AND the cycle MUST be marked `failed_publish` unless a forward migration adds and documents a legal skipped cycle state
AND monitoring MUST distinguish this from a published product.

#### Scenario: No-op publish cannot complete cycle

WHEN `publish-tiles` returns a skipped/no-op result
THEN the orchestrator MUST NOT mark the pipeline as fully complete due to that result alone.

### Requirement: Publish contract is documented

The selected publication behavior SHALL be reflected in docs, OpenAPI, and tests.

#### Scenario: Documentation names real delivery table and format

WHEN docs describe tile publication
THEN they MUST use the table and endpoint names implemented by migrations and code, including `map.tile_layer`, `map.tile_cache`, and `/api/v1/tiles/flood-return-period` where applicable
AND they MUST document whether the release publishes GeoJSON, MVT/PBF, or no publish artifact.
