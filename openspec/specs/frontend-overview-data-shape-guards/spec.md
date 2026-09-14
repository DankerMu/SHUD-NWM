# frontend-overview-data-shape-guards Specification

## Purpose
TBD - created by archiving change add-overview-data-shape-guards. Update Purpose after archive.
## Requirements
### Requirement: Overview data chain rejects malformed payloads into an explicit data-anomaly state
The national overview data chain SHALL validate the minimum shape its consumers dereference for the layers, discharge cycles, per-cycle valid times, precipitation index, basins and runs responses, including element shape. A payload that fails validation MUST NOT be cached, MUST NOT throw during render, MUST NOT leave the map bootstrap loading indefinitely, and MUST degrade the affected scoped state to its existing unavailable/error form. The overview page MUST surface 「数据异常」 naming the affected data, distinct from the text shown for request failures: in the bootstrap or enrichment error text when those fail, otherwise in a dedicated notice that yields only to the notices that already precede it (station layer status, loading, empty/error). Payloads that satisfy the contract MUST produce the same state and rendering as before.

#### Scenario: Cycles with a null entry
- **WHEN** the discharge cycles response contains a `null` entry in `cycles`
- **THEN** the source's cycles state is the error state and the overview shows the 「数据异常」 notice naming 起报时次
- **AND** no region or route fallback is shown and the control bar and map remain rendered

#### Scenario: Cycles as a string array
- **WHEN** the discharge cycles response lists `cycles` as strings instead of objects
- **THEN** the source's cycles state is the error state and the overview shows the 「数据异常」 notice naming 起报时次

#### Scenario: Basins container malformed during bootstrap
- **WHEN** the basins response is not an array
- **THEN** the map bootstrap settles with a bootstrap error whose text contains 数据异常 instead of loading indefinitely

#### Scenario: Request failure keeps its existing text
- **WHEN** a request in the chain fails with an API or network error
- **THEN** the existing 暂不可用 text and states are shown and no 「数据异常」 notice appears

