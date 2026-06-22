## ADDED Requirements

### Requirement: Display compose port env key is canonical

The display compose configuration SHALL use `NHMS_DISPLAY_API_PORT` as the only
display API host-port interpolation key, and the display env template SHALL
define the same key with the production-compatible default `8080`.

#### Scenario: compose and display env share the same port key

- **WHEN** the two-node docker runtime static checker validates the display
  compose file and display env template
- **THEN** it SHALL find `NHMS_DISPLAY_API_PORT` in both artifacts
- **AND** it SHALL reject alternate display port interpolation aliases that are
  absent from the display env template
