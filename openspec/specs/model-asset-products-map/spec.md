# model-asset-products-map Specification

## Purpose
TBD - created by archiving change m14-model-asset-management-ui. Update Purpose after archive.
## Requirements
### Requirement: Model asset products and mini map
The page SHALL show associated product assets and a small basin/river map for the selected model.

#### Scenario: Assets available
WHEN product asset metadata and geometry are available
THEN asset list and mini map render with stable IDs and checksums

#### Scenario: Geometry unavailable
WHEN boundary/river geometry is unavailable or over budget
THEN mini map shows `暂无空间预览` for missing geometry or `空间几何超出预览预算` for geometry above 50 features or 2,000 coordinate vertices
AND does not render unsafe large geometry

#### Scenario: Product list over budget
WHEN product asset metadata exceeds the page budget
THEN the product list renders at most 12 displayed assets with stable IDs and checksums
AND shows `仅显示前 12 个资产`

