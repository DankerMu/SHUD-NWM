## ADDED Requirements

### Requirement: Curve windows render as sheets in mobile form

In mobile form the river q_down forecast window and the station forcing window SHALL render as a sheet attached to the map region instead of a floating draggable window. Outside short-landscape the sheet SHALL be attached to the bottom edge, span the full map width, and have a height equal to the smaller of 60% of the dynamic viewport height and the map region's height minus 8 CSS px. In short-landscape the sheet SHALL be attached to the right edge, span the full map height, and have a width equal to the smaller of half the viewport width and 28rem. The sheet's size SHALL NOT depend on its content state (loading, pending, empty, partial or loaded). The sheet SHALL NOT be draggable and SHALL NOT use a fixed aspect ratio. Its header (title and close control) SHALL stay visible while the sheet body scrolls. When the viewport returns to desktop form, an open window SHALL be shown as a draggable desktop window at the default position a newly opened window of that kind gets at that viewport.

#### Scenario: Portrait bottom sheet

- **WHEN** a river segment is tapped at 390×664
- **THEN** the river window's left, right and bottom edges coincide with the map region's and its height equals the smaller of 60% of the viewport height and the map region height minus 8px

#### Scenario: Short-landscape side sheet

- **WHEN** a station is tapped at 750×342
- **THEN** the station window's top, bottom and right edges coincide with the map region's and its width equals the smaller of half the viewport width and 28rem

#### Scenario: Side sheet through the height arm

- **WHEN** a river segment is tapped at 844×390
- **THEN** the river window is a right-side sheet

#### Scenario: Short portrait viewport uses the bottom sheet

- **WHEN** a river segment is tapped at 320×480
- **THEN** the river window is a bottom sheet

#### Scenario: Narrow short-landscape viewport uses the side sheet

- **WHEN** a river segment is tapped at 600×400
- **THEN** the river window is a right-side sheet

#### Scenario: Sheet size is the same while loading

- **WHEN** a river window is open at 390×664 and its forecast request has not resolved
- **THEN** the sheet has the same bounding box it has once the curves are loaded, and its title and close control are visible

#### Scenario: Sheet with an empty state

- **WHEN** a station window is open at 390×664 and its series fails identity validation
- **THEN** the unavailable-reason text lies inside the sheet and the sheet has its mobile bounding box

#### Scenario: Sheet is not draggable

- **WHEN** the user drags the sheet header by 100px at 390×664
- **THEN** the sheet's bounding box is unchanged

#### Scenario: Header stays visible while the body scrolls

- **WHEN** a station window is open at 750×342 and its body is scrolled to the end
- **THEN** the title and the close control are still inside the sheet's visible box

#### Scenario: Rotation re-anchors the sheet

- **WHEN** a river window is open at 390×664 and the viewport becomes 750×342
- **THEN** the same window is shown as a right-side sheet for the same river segment with the same selected issue time

#### Scenario: Leaving mobile form

- **WHEN** a river window is open at 390×664 and the viewport becomes 1280×900
- **THEN** the river window is a draggable desktop window whose bounding box equals that of a river window newly opened at 1280×900, and the control bar is visible

### Requirement: The curve chart keeps a minimum height inside a sheet

Inside a sheet the chart area SHALL take all height left after the header and selectors, and SHALL be at least 160 CSS px high outside short-landscape and at least 120 CSS px high in short-landscape. When the remaining height is smaller than that floor, the sheet body SHALL scroll vertically instead of shrinking the chart below the floor, and the chart area SHALL be scrollable fully into the sheet's visible box. The chart canvas SHALL match the chart area's size and SHALL be re-laid-out when the sheet's size changes.

#### Scenario: River chart height on a portrait phone

- **WHEN** a river window with loaded curves is open at 390×664
- **THEN** the river chart area is at least 160px high, lies inside the sheet, and contains a canvas of the same size

#### Scenario: Station chart height on a portrait phone

- **WHEN** a station window with loaded curves is open at 390×664
- **THEN** the station chart area is at least 160px high, lies inside the sheet, and contains a canvas of the same size

#### Scenario: River chart height in short-landscape

- **WHEN** a river window with loaded curves is open at 750×342
- **THEN** its chart area is at least 120px high

#### Scenario: Station chart reachable in short-landscape

- **WHEN** a station window with loaded curves is open at 750×342
- **THEN** its chart area is at least 120px high and, after scrolling the sheet body, the whole chart area lies inside the sheet's visible box

#### Scenario: Chart follows a size change

- **WHEN** a river window with loaded curves is open and the viewport changes from 390×664 to 750×342
- **THEN** the chart canvas's size equals the new chart area's size

### Requirement: Mobile form shows one curve window at a time

In mobile form opening a river window SHALL close an open station window and opening a station window SHALL close an open river window. When the viewport changes from desktop form to mobile form while both windows are open, the active window SHALL stay open and the other SHALL close.

#### Scenario: Station replaces river

- **WHEN** a river window is open in mobile form and a station is selected
- **THEN** the station window is open and the river window is closed

#### Scenario: River replaces station

- **WHEN** a station window is open in mobile form and a river segment is selected
- **THEN** the river window is open and the station window is closed

#### Scenario: Entering mobile form with two windows

- **WHEN** both windows are open in desktop form with the station window active and the viewport becomes 390×664
- **THEN** only the station window remains open

### Requirement: An open sheet yields the map chrome

In mobile form, in both orientations, while a curve sheet is open the bottom control bar and the launcher column SHALL be hidden and inoperable but SHALL keep their state, and any expanded overlay panel SHALL collapse. Opening a sheet while the timeline is playing SHALL pause playback. Closing the sheet SHALL make the control bar and the launchers visible again with the same source, issue cycle and valid time as before the sheet opened, with playback stopped and no panel expanded.

#### Scenario: Opening a sheet hides the control bar and the launchers

- **WHEN** a river window opens at 390×664 or 750×342
- **THEN** the control bar and the launchers are not visible and receive no pointer input

#### Scenario: Opening a sheet collapses a panel

- **WHEN** the legend panel is expanded at 390×664 and a river segment is tapped
- **THEN** the river window is open and none of the three panels is visible

#### Scenario: Opening a sheet pauses playback

- **WHEN** the timeline is playing at 390×664 and a river window opens
- **THEN** playback stops and the valid time no longer advances while the sheet is open

#### Scenario: Closing a sheet restores the chrome

- **WHEN** the user closes the sheet
- **THEN** the control bar and the launchers are visible, the source, issue cycle and valid time equal their values when the sheet opened, playback is stopped, and none of the three panels is visible

### Requirement: Opening a sheet pans the selected feature into view

In mobile form, when a curve sheet opens the map SHALL ease, without changing zoom, so that the selected feature's anchor point lies inside the part of the map region that the sheet does not cover: the bottom side is treated as covered outside short-landscape and the right side in short-landscape. The same SHALL happen once when one kind of window replaces the other and once when the viewport switches between the bottom-sheet and side-sheet layouts while a sheet is open. The map SHALL be moved only at those three moments: after the user moves the map by hand while the sheet is open it SHALL NOT be moved back, although a later replacement or layout switch pans again. Closing the sheet SHALL NOT move the map, and leaving mobile form with a sheet open SHALL NOT move the map. In desktop form opening a curve window SHALL NOT move the map. The map container SHALL expose the selected anchor's current viewport position — the active window's anchor when two windows are open in desktop form — as `data-selected-anchor-x` and `data-selected-anchor-y` (CSS px) in both forms, and SHALL carry neither attribute when no feature is selected.

#### Scenario: River anchor visible above a bottom sheet

- **WHEN** a river window is opened at 390×664 and the camera has settled
- **THEN** the point (`data-selected-anchor-x`, `data-selected-anchor-y`) lies inside the map region and above the sheet's top edge

#### Scenario: Station anchor visible beside a side sheet

- **WHEN** a station window is opened at 750×342 and the camera has settled
- **THEN** the point (`data-selected-anchor-x`, `data-selected-anchor-y`) lies inside the map region and left of the sheet's left edge

#### Scenario: Rotation re-pans

- **WHEN** a river window is open at 390×664 and the viewport becomes 750×342
- **THEN** after the camera settles the anchor point lies inside the map region and left of the side sheet

#### Scenario: Replacement pans to the new feature

- **WHEN** a river window is open in mobile form and a station is selected
- **THEN** the map is moved once more so that the station's anchor lies inside the uncovered part of the map region

#### Scenario: Zoom is unchanged

- **WHEN** a river window is opened in mobile form
- **THEN** the map's zoom level after the pan equals its zoom level before the sheet opened

#### Scenario: Manual movement is respected

- **WHEN** a sheet is open and the user drags the map so that the anchor is under the sheet
- **THEN** the map is not moved back

#### Scenario: A layout switch after manual movement pans again

- **WHEN** a sheet is open at 390×664, the user has dragged the map, and the viewport becomes 750×342
- **THEN** the map is moved once so that the anchor lies inside the map region and left of the side sheet

#### Scenario: Closing does not move the map

- **WHEN** the user closes the sheet
- **THEN** the map's centre and zoom are the same as immediately before closing, and the map container carries neither anchor attribute

#### Scenario: Leaving mobile form does not move the map

- **WHEN** a sheet is open at 390×664 and the viewport becomes 1280×900
- **THEN** the map's centre and zoom are the same as immediately before the change

#### Scenario: Two desktop windows expose the active anchor

- **WHEN** a river window and a station window are both open at 1280×900 with the station window active
- **THEN** the anchor attributes give the station's anchor position

#### Scenario: Desktop form does not pan

- **WHEN** a river window is opened at 1280×900
- **THEN** the point (`data-selected-anchor-x`, `data-selected-anchor-y`) is within 1 CSS px of the point that was clicked

### Requirement: Curve time axes are zoomable by touch

In mobile form the river curve and the station curve SHALL zoom their time axis by two-finger pinch and pan it by one-finger drag inside the chart area, and SHALL show the data tooltip for a touched time. Each chart container SHALL expose its current zoom window as `data-zoom-start` and `data-zoom-end` (percent of the full range, 0–100) in both forms. The river window's on-chart hint SHALL describe the two-finger gesture in mobile form and SHALL remain the existing wheel hint in desktop form. In desktop form the charts' zoom and pan configuration SHALL be unchanged.

#### Scenario: Pinch zooms the river time axis

- **WHEN** a two-finger pinch-out is performed inside the river chart in mobile form
- **THEN** `data-zoom-end` minus `data-zoom-start` becomes less than 100

#### Scenario: Pinch zooms the station time axis

- **WHEN** a two-finger pinch-out is performed inside the station chart in mobile form
- **THEN** `data-zoom-end` minus `data-zoom-start` becomes less than 100

#### Scenario: One-finger drag pans a zoomed river chart

- **WHEN** the river chart is zoomed in and a one-finger horizontal drag is performed inside it in mobile form
- **THEN** `data-zoom-start` changes while the zoom span stays the same

#### Scenario: One-finger drag pans a zoomed station chart

- **WHEN** the station chart is zoomed in and a one-finger horizontal drag is performed inside it in mobile form
- **THEN** `data-zoom-start` changes while the zoom span stays the same

#### Scenario: Touch shows the tooltip

- **WHEN** the user taps inside the river chart's plot area or the station chart's plot area in mobile form
- **THEN** that chart's tooltip becomes visible

#### Scenario: Hint matches the form

- **WHEN** a river window is open in mobile form
- **THEN** the hint text names the two-finger gesture and does not contain the wheel wording
- **AND** in desktop form the hint text is the existing wheel hint

#### Scenario: Zoom window is exposed in desktop form

- **WHEN** a river window or a station window with loaded curves is open at 1280×900
- **THEN** its chart container has `data-zoom-start="0"` and `data-zoom-end="100"`

#### Scenario: Desktop chart configuration unchanged

- **WHEN** a river window or a station window is open in desktop form
- **THEN** the chart's zoom and pan options equal the options it had before this change

### Requirement: Sheet controls meet the mobile floors

In mobile form the close control of the river window and of the station window SHALL each be at least 44×44 CSS px; the issue-time selector trigger of each window SHALL be at least 44 CSS px high with a font size of at least 16 CSS px, and each of its options SHALL be at least 44 CSS px high; the station variable chips SHALL each be at least 44 CSS px high and SHALL wrap onto further rows rather than extend outside the sheet.

#### Scenario: Close control size

- **WHEN** a river window or a station window is open at 390×664
- **THEN** its close control measures at least 44×44

#### Scenario: Issue-time selector size

- **WHEN** a river window or a station window is open at 390×664
- **THEN** its issue-time selector trigger is at least 44px high with a computed font size of at least 16px
- **AND** when the selector is opened each option is at least 44px high

#### Scenario: Station variable chips fit

- **WHEN** a station window is open at 390×664
- **THEN** every variable chip's bounding box lies inside the sheet's horizontal bounds and is at least 44px high
