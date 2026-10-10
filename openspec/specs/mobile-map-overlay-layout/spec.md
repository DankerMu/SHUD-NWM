# mobile-map-overlay-layout Specification

## Purpose
定义移动形态下主图上各浮层的布局：图层 / 底图 / 图例三个启动器及其面板、底部控制条与时间滑块、状态提示之间互不遮挡，并满足触控目标与字号下限。

## Requirements

### Requirement: Map overlay panels collapse to launchers in mobile form

In mobile form the layer switcher, the basemap switcher and the legend on `/` SHALL each render as a launcher button, in a column at the top-right of the map region, and SHALL be collapsed by default so that the map is not covered by any panel on load. Activating a launcher SHALL expand its panel; activating it again SHALL collapse it. At most one of the three panels SHALL be expanded at any time. An expanded panel SHALL collapse when the user taps the map outside the panel and the launchers, when the user presses Escape, and when the viewport changes form. Expanding or collapsing SHALL NOT change the URL query and SHALL NOT issue any data request. The launcher is the mobile presentation of its panel: the existing requirements that the legend shows discharge units and bins and the precipitation classes are met in mobile form by the content of the expanded legend panel.

#### Scenario: Collapsed by default

- **WHEN** `/` is rendered at 390×664, 750×342 or 844×390
- **THEN** the layer, basemap and legend launchers are visible and none of the three panels is visible

#### Scenario: Expanding one collapses the others

- **WHEN** the legend panel is expanded and the user activates the layer launcher
- **THEN** the layer panel is visible and the legend panel is not

#### Scenario: Toggle collapses

- **WHEN** the layer panel is expanded and the user activates the layer launcher again
- **THEN** none of the three panels is visible

#### Scenario: Dismiss by tapping the map

- **WHEN** a panel is expanded and the user taps a point of the map outside the panel and the launchers
- **THEN** none of the three panels is visible

#### Scenario: Dismiss by Escape

- **WHEN** a panel is expanded and the user presses Escape
- **THEN** none of the three panels is visible

#### Scenario: Expanding does not touch the URL or the network

- **WHEN** the user expands and then collapses each of the three panels
- **THEN** the URL is unchanged and no API request was issued by those actions

#### Scenario: Panel content matches desktop

- **WHEN** the layer panel is expanded in mobile form and the user toggles the station overlay
- **THEN** the URL query changes exactly as the same toggle changes it in desktop form

#### Scenario: Expanded legend carries the legend content

- **WHEN** the legend panel is expanded at 390×664 with the discharge layer active and the precipitation overlay on
- **THEN** the panel lists the discharge bins with their unit and the six precipitation classes

#### Scenario: Rotation keeps the expanded panel

- **WHEN** the legend panel is expanded at 390×664 and the viewport becomes 750×342
- **THEN** the legend panel is still visible and lies inside the map region

#### Scenario: Leaving and re-entering mobile form

- **WHEN** a panel is expanded at 390×664, the viewport becomes 1280×900 and then 390×664 again
- **THEN** at 1280×900 the three desktop panels are visible at their desktop offsets and no launcher is present
- **AND** back at 390×664 none of the three panels is visible

### Requirement: Expanded mobile panels stay inside the map region

An expanded mobile overlay panel SHALL lie entirely inside the map region, SHALL NOT intersect the bottom control bar or the launcher column, and SHALL scroll its own content vertically when the content is taller than the available height.

#### Scenario: Legend fits a short-landscape viewport

- **WHEN** the legend is expanded at 750×342 with both the discharge and the precipitation legends present
- **THEN** the panel's bounding box lies inside the map region and above the control bar, and after scrolling the panel to its end the last legend entry's bounding box lies inside the panel's visible box

#### Scenario: Legend fits a portrait viewport

- **WHEN** the legend is expanded at 390×664
- **THEN** the panel's bounding box does not intersect the control bar or the launcher column, and after scrolling the panel to its end the last legend entry's bounding box lies inside the panel's visible box

#### Scenario: Layer panel fits a short-landscape viewport

- **WHEN** the layer panel is expanded at 750×342
- **THEN** the panel's bounding box lies inside the map region and above the control bar

### Requirement: The bottom control bar is fully operable in mobile form

In mobile form the bottom control bar SHALL keep the source switch, the issue-cycle selector, the step and play controls, the playback-speed selector and the timeline slider all visible inside the viewport, SHALL keep its bottom edge 40 CSS px above the map region's bottom edge, and SHALL NOT intersect the map attribution. Outside short-landscape it SHALL use two rows: the source switch, the issue-cycle selector and the playback-speed selector on the first, the step and play controls and the timeline slider on the second. In short-landscape it SHALL use a single row. The timeline slider SHALL never be narrower than 120 CSS px. The issue-cycle selector and the playback-speed selector SHALL each be at least 44 CSS px high with a font size of at least 16 CSS px. The 64px control-bar height token is a desktop-form fact and SHALL NOT constrain the mobile-form bar's height.

#### Scenario: Portrait phone uses two rows

- **WHEN** `/` is rendered at 390×664 or 320×568
- **THEN** every control-bar control's bounding box lies inside the viewport, the timeline slider is at least 120px wide, the slider's top edge is at or below the source switch's bottom edge, and the bar does not intersect the map attribution

#### Scenario: Short-landscape phone uses one row

- **WHEN** `/` is rendered at 750×342 or 844×390
- **THEN** every control-bar control's bounding box lies inside the viewport, the timeline slider is at least 120px wide, the slider and the source switch overlap vertically, and the bar does not intersect the map attribution

#### Scenario: Bar keeps the attribution band

- **WHEN** `/` is rendered at 390×664 or 750×342
- **THEN** the bar's bottom edge is 40px above the map region's bottom edge

#### Scenario: Selector floors

- **WHEN** `/` is rendered at 390×664
- **THEN** the issue-cycle selector and the playback-speed selector are each at least 44px high with a computed font size of at least 16px

#### Scenario: Timeline behaviour is unchanged

- **WHEN** the user steps the timeline forward once in mobile form
- **THEN** the valid-time query changes exactly as one forward step changes it in desktop form

#### Scenario: Desktop bar unchanged

- **WHEN** `/` is rendered at 1280×900
- **THEN** the bar is 64px high on a single row

### Requirement: Map zoom controls are hidden in mobile form

In mobile form the map SHALL NOT render the zoom and compass control. In desktop form the control SHALL remain rendered. The map's gesture configuration is not changed by this capability; pinch-zoom and drag-pan behaviour on real devices is verified by the real-device checklist.

#### Scenario: Mobile hides the control

- **WHEN** `/` is rendered at 390×664 or 844×390
- **THEN** no zoom-in, zoom-out or reset-bearing button is present

#### Scenario: Desktop keeps the control

- **WHEN** `/` is rendered at 1280×900 or 768×1024
- **THEN** the zoom-in, zoom-out and reset-bearing buttons are present

### Requirement: Notices and status overlays do not collide with mobile chrome

In mobile form every floating map notice (station status, overview loading, empty data, data anomaly, precipitation) and every map status overlay (basin layer unavailable, map unavailable, selected segment unavailable, map source error) SHALL be placed inside the map region where it does not intersect the launcher column, the bottom control bar or another notice or status overlay, and its text SHALL occupy at most two lines, truncating beyond that. A notice or status overlay is not required to avoid an expanded overlay panel or an open curve sheet, either of which may cover it.

#### Scenario: Station notice

- **WHEN** the station overlay is on and its status notice is shown at 390×664
- **THEN** the notice's bounding box lies inside the map region, does not intersect any launcher or the control bar, and is at most two text lines high

#### Scenario: Loading notice

- **WHEN** the overview-loading notice is shown at 390×664 or 750×342
- **THEN** the notice's bounding box lies inside the map region and does not intersect any launcher or the control bar

#### Scenario: Status overlay alone

- **WHEN** the map-source-error status overlay is shown at 390×664 or 750×342
- **THEN** its bounding box lies inside the map region, does not intersect any launcher or the control bar, and is at most two text lines high

#### Scenario: Notice with a status overlay

- **WHEN** a floating notice and the map-source-error status overlay are shown together at 390×664
- **THEN** their bounding boxes do not intersect each other

### Requirement: The ops entry joins the launcher column

In mobile form the ops entry SHALL be the last item of the launcher column, at least 44×44 CSS px, for roles that can see it, and SHALL be absent for roles that cannot.

#### Scenario: Viewer has no ops entry

- **WHEN** `/` is rendered at 390×664 for a viewer role
- **THEN** no ops entry is present

#### Scenario: Operator has an ops entry

- **WHEN** `/` is rendered at 390×664 for an operator role
- **THEN** an ops entry at least 44×44 is present below the legend launcher and navigates to `/ops`

### Requirement: Region error fallbacks fit the mobile layout

In mobile form the error fallback of the map-controls region (which holds the layer switcher, the basemap switcher and the ops entry), the legend region and the control-bar region SHALL render inside the map region without intersecting the bottom control bar or the launchers that are still rendered. A failed curve region SHALL render its fallback inside the map region and SHALL NOT leave the bottom control bar or the launchers hidden.

#### Scenario: Legend region fallback

- **WHEN** the legend region fails to render at 390×664
- **THEN** its fallback's bounding box lies inside the map region and does not intersect the control bar

#### Scenario: Map-controls region fallback

- **WHEN** the map-controls region fails to render at 390×664
- **THEN** its fallback's bounding box lies inside the map region and does not intersect the control bar or the legend launcher

#### Scenario: Control-bar region fallback

- **WHEN** the control-bar region fails to render at 390×664
- **THEN** its fallback's bounding box lies inside the map region and does not intersect any launcher or the map attribution

#### Scenario: Curve region fallback does not strand the chrome

- **WHEN** a curve panel throws while rendering at 390×664
- **THEN** the curve region's fallback's bounding box lies inside the map region and does not intersect the control bar or any launcher, and the control bar and the launchers are visible and operable
