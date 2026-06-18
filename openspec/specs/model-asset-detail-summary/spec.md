# model-asset-detail-summary Specification

## Purpose
TBD - created by archiving change m14-model-asset-management-ui. Update Purpose after archive.
## Requirements
### Requirement: Model asset detail summary
The selected model detail SHALL show six KPI cards and metadata fields from the public model response.

#### Scenario: Detail loaded
WHEN model detail is available
THEN summary shows exactly six KPI cards in order: `流域版本`, `河网版本`, `网格版本`, `率定版本`, `SHUD / 模型`, `河段 / 面积`
AND maps them from basin version, river network version, mesh version/checksum, calibration version, SHUD code/model id, and segment count/area fields

#### Scenario: Redaction
WHEN detail contains local source paths or sensitive URI components
THEN UI displays redacted/public-safe values only

#### Scenario: Nested redaction
WHEN source/package lineage is nested inside resource_profile, graph nodes, product assets, tooltips, or screenshot-visible text
THEN local absolute paths and URI userinfo/query/fragment are redacted before rendering

#### Scenario: Missing detail values
WHEN KPI source fields or checksums are missing or null
THEN the affected KPI or metadata field displays `暂不可用`
AND does not invent placeholder identifiers, checksums, relationships, or areas

