## ADDED Requirements

### Requirement: Active document entropy budget SHALL be explicitly burned down

Active document entropy budget findings SHALL be reduced or explicitly
reclassified through current-authority wording and machine-readable markers.

#### Scenario: active OpenSpec mentions a legacy display route alias

- **WHEN** an active OpenSpec spec mentions a legacy display route alias token
  from the current route-authority matrix
- **THEN** the text SHALL either use `/` as the current display entrypoint or
  mark the legacy route as redirect alias, compatibility context, or historical
  evidence in a way the entropy audit can classify.

#### Scenario: current doc or source comment mentions a legacy display route alias

- **WHEN** a current doc or source-code comment mentions a legacy display route
  alias token from the current route-authority matrix
- **THEN** the text SHALL either use `/` as the current display entrypoint or
  mark the legacy route as redirect alias, compatibility context, or historical
  evidence in a way the entropy audit can classify.

#### Scenario: active document mentions retired active-tree path

- **WHEN** an active spec, current doc, or source-code comment mentions
  a retired active-tree path token from the audit retired-prefix registry
- **THEN** the text SHALL either use the current canonical path or mark the
  retired path as retired/historical/compatibility context in a way the entropy
  audit can classify.

#### Scenario: budget is remeasured

- **WHEN** the report-only entropy audit is rerun after active document cleanup
- **THEN** non-archive budget-counted route/path findings SHALL decrease from
  the pre-change count of 36 or every remaining active finding SHALL map to an
  explicit follow-up issue with a documented owner and reason.

### Requirement: Cleanup SHALL preserve audit history

Document entropy cleanup SHALL preserve useful historical evidence instead of
deleting it merely to improve finding counts.

#### Scenario: historical evidence remains relevant

- **WHEN** a document or archived OpenSpec artifact records why a route/path
  existed or was retired
- **THEN** cleanup SHALL preserve the evidence with status/current-authority
  metadata instead of deleting it solely to reduce total findings.
