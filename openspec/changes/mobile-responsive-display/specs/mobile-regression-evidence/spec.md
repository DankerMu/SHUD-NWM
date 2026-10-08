## ADDED Requirements

### Requirement: The mocked-regression lane SHALL gate mobile viewports

The mocked-regression Playwright lane SHALL include three additional touch-enabled projects, all running Chromium: a portrait phone at 390×664, a short-landscape phone at 750×342, and a wide short-landscape phone at 844×390 that enters mobile form through the height condition only. The three projects SHALL run only specs whose file name marks them as mobile specs, and every mobile spec SHALL hold in all three projects. Existing specs SHALL keep running only in the existing desktop project, and the specs the lane excluded before this change SHALL stay excluded. Assertions for the tablet-portrait viewport, which is desktop form, SHALL live in desktop-project specs. A failing mobile assertion SHALL fail the lane; the lane's no-retry rule applies unchanged, and no additional browser SHALL be installed in CI.

#### Scenario: Mobile specs run in the three projects

- **WHEN** the mocked-regression lane runs
- **THEN** each mobile spec executes once per mobile project and the run log shows a non-zero passed count, not counting skipped tests, for each of the three projects

#### Scenario: Existing specs are not multiplied

- **WHEN** the mocked-regression lane runs
- **THEN** every pre-existing spec executes exactly once, in the desktop project

#### Scenario: Previously excluded specs stay excluded

- **WHEN** the mocked-regression lane runs
- **THEN** the preview-deeplink, live-display, live-c4-display and m15-visual-conformance specs are not executed by any project

#### Scenario: Lane configuration is not loosened

- **WHEN** the lane's configuration and the CI workflow are inspected after this change
- **THEN** test retries are still zero and the CI browser-install step installs the same single browser as before

#### Scenario: A mobile regression fails the lane

- **WHEN** a change makes the legend panel intersect the control bar at 390×664
- **THEN** the lane fails

### Requirement: The mocked lane SHALL be able to open both curve windows

The mocked-regression lane SHALL provide a shared fixture that renders a discharge river segment and a station on the map from mocked responses and opens the river window and the station window by a real pointer or touch activation at a located point. Locating SHALL go through test-gated read-only hooks — the existing river hook for a rendered river segment and the separate station hook defined in `frontend-river-click-live-evidence` for a rendered station — neither of which invokes any product callback. Without the test gate neither hook SHALL be exposed. The lane SHALL also provide a test-gated region crash switch: only when the gate is present, setting `window.__NHMS_E2E_CRASH_REGION__` to `map-controls`, `legend`, `control-bar` or `curve` SHALL make that region throw while rendering; without the gate the switch SHALL have no effect.

#### Scenario: River window opens in the desktop project

- **WHEN** the fixture locates a river segment and clicks it at 1280×900
- **THEN** the river window is visible with a loaded curve

#### Scenario: Station window opens in a mobile project

- **WHEN** the fixture locates a station and taps it at 390×664
- **THEN** the station window is visible with a loaded curve

#### Scenario: Hooks are absent without the gate

- **WHEN** `/` is loaded without the test gate
- **THEN** neither locating hook is exposed on the page

#### Scenario: Crash switch works only behind the gate

- **WHEN** `window.__NHMS_E2E_CRASH_REGION__` is `legend` and the gate is present
- **THEN** the legend region shows its error fallback
- **AND** with the same value and no gate, the legend region renders normally

### Requirement: Mobile assertions SHALL be geometry and style oracles

Mobile specs SHALL assert measured geometry, computed style and exposed state attributes — element bounding boxes inside the viewport or a container, pairwise non-intersection, minimum chart-area height, minimum touch-target size, minimum form font size, zoom-window attributes — and SHALL NOT depend on pixel-comparison screenshots. They SHALL NOT use `document.scrollWidth == innerWidth` as an overflow oracle, because the shell clips overflow.

#### Scenario: Baseline mobile invariants

- **WHEN** `/` is rendered in each mobile project
- **THEN** a spec asserts that every visible interactive control's bounding box lies inside the viewport

#### Scenario: No pixel or document-width oracles

- **WHEN** the mobile specs are inspected
- **THEN** none of them calls a screenshot-comparison assertion and none compares the document scroll width with the window width

### Requirement: Live mobile evidence SHALL be captured on node-27

The node-27 browser-evidence script SHALL accept a device preset that sets viewport, device scale factor, touch and user agent, SHALL evaluate its layout checks against the expectations of the preset's form rather than the desktop header and control-bar heights, and SHALL emit a screenshot and a geometry JSON for `/` in three states — default, river window open, station window open — for the portrait-phone and short-landscape presets against the live display entry. A receipt recording the command, the commit and the outputs SHALL be stored under the runbook receipts directory.

#### Scenario: Portrait preset produces evidence

- **WHEN** the script is run on node-27 with the portrait-phone preset against the live display entry
- **THEN** it writes three screenshots and a geometry JSON in which the river and station chart areas are each at least 160px high and the report passes

#### Scenario: Short-landscape preset produces evidence

- **WHEN** the script is run on node-27 with the short-landscape preset against the live display entry
- **THEN** it writes three screenshots and a geometry JSON in which the river and station chart areas are each at least 120px high and the report passes

#### Scenario: Receipt content

- **WHEN** the receipt is stored
- **THEN** it names the exact command lines, the deployed commit, and the paths of the screenshots and geometry JSON files

#### Scenario: Default invocation is unchanged

- **WHEN** the script is run without a device preset
- **THEN** its report has the same structure and the same desktop expectations as before this change

### Requirement: A real-device checklist SHALL close the epic

A runbook SHALL list the real-device checks that emulation cannot prove — bottom controls clear of the browser toolbar, controls clear of the device safe areas, no focus auto-zoom on selectors and inputs, pinch-zoom and drag on the map and in charts without gesture conflict, the selected feature staying visible beside an open sheet — together with every item under the change design's open questions, for at least one iOS Safari device and one Android Chrome device. The epic SHALL close only after the result of every check is recorded; a failed check SHALL be filed as a follow-up issue rather than waived.

#### Scenario: Checklist recorded

- **WHEN** the user completes the checklist on both device classes
- **THEN** the result of every check is recorded in the receipt, and each failed check references a follow-up issue
