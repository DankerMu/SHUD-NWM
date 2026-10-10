## ADDED Requirements

### Requirement: Curve windows SHALL resolve their issue-time options without a shared popup product hook

The river forecast window and the station forcing window SHALL each obtain the latest-product identity and its available issue times directly from the latest-product fetch, and the frontend SHALL NOT keep a separate popup product hook that requests the same endpoint without a caller.

#### Scenario: A curve window opens

- **WHEN** a river forecast window or a station forcing window opens for a source
- **THEN** the window itself requests the latest-product identity for that source and builds its issue-time selector from the returned available issue times
