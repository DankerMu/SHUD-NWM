## ADDED Requirements

### Requirement: Curve windows SHALL NOT offer a forecast-source switch

The river curve window SHALL show its forecast sources together in one chart and SHALL NOT render a control for switching between forecast sources; the station curve window SHALL NOT render a forecast-source switch either.

#### Scenario: River window has no source switch

- **WHEN** the river curve window is open and its issue times have been resolved
- **THEN** it contains an issue-time selector and no button for choosing GFS or IFS

#### Scenario: Station window has no source switch

- **WHEN** the station curve window is open and its issue times have been resolved
- **THEN** it contains an issue-time selector and no button for choosing GFS or IFS
