## ADDED Requirements

### Requirement: Responsive screenshot evidence
Visual conformance SHALL produce screenshot evidence for named routes and viewport matrix.

#### Scenario: Evidence capture
WHEN screenshots are captured
THEN artifacts include route, viewport, fixture mode, SHA, and expected state label

#### Scenario: Required route matrix
WHEN visual conformance evidence is generated
THEN loaded-state screenshots exist for overview, basin detail, flood alerts, and monitoring at 1920x1080, 1440x900, and 1280x900

#### Scenario: Extended route matrix
WHEN deterministic fixtures exist for completed M12-M14 surfaces
THEN `/segments/seg-009?source=gfs&cycle=2026-05-18T00:00:00Z&validTime=2026-05-18T06:00:00Z&basinVersionId=bv-001&riverNetworkVersionId=rn-v1`, `/meteorology?tab=grid&source=GFS&variable=PRCP&validTime=2026-05-18T06:00:00.000Z&gridQueryLon=114.35&gridQueryLat=30.62`, `/meteorology?tab=stations&basin=yangtze&stationId=HMT-Y2-0237`, and `/system/model-assets` are included in evidence or documented as remaining surfaces with a concrete reason

#### Scenario: Overlap check
WHEN screenshot route renders dynamic content
THEN test asserts key panels/buttons/text do not overlap incoherently

#### Scenario: Bounded artifacts
WHEN screenshot capture writes local evidence
THEN it writes only under the documented issue evidence directory and records a manifest entry for each artifact
