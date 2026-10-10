## MODIFIED Requirements

### Requirement: River and station curve windows can coexist and move

In desktop form (viewport at least 768 CSS px wide and at least 500 CSS px high)
M11 river q_down forecast windows and station forcing windows SHALL be
independent curve windows. Opening one type MUST NOT close the other type, and
each visible window SHALL be draggable by its header or drag handle so users can
compare river flow and forcing-station variables on the same map. In mobile form
the windows SHALL follow the `mobile-curve-sheet` capability instead: they are
not draggable and only one is open at a time.

#### Scenario: River then station leaves both windows open

- **WHEN** a user in desktop form opens a river q_down forecast window
- **AND** then clicks a meteorological station point
- **THEN** the station forcing window opens
- **AND** the river forecast window remains visible until the user closes it.

#### Scenario: Station then river leaves both windows open

- **WHEN** a user in desktop form opens a station forcing window
- **AND** then clicks a river segment that is not covered by a station symbol
- **THEN** the river q_down forecast window opens
- **AND** the station forcing window remains visible until the user closes it.

#### Scenario: Closing one window does not close the other

- **WHEN** both river and station curve windows are visible in desktop form
- **AND** the user activates the close control on one window
- **THEN** only that window closes
- **AND** the other window remains visible with its selected feature and chart
  state intact.

#### Scenario: Dragging a window repositions within the map viewport

- **WHEN** a user in desktop form drags a curve window by its header or drag handle
- **THEN** the window moves with the pointer
- **AND** the final position is clamped so the title, close control, and enough
  chart area remain reachable inside the map viewport.

#### Scenario: Dual windows initially avoid perfect overlap

- **WHEN** both river and station curve windows become visible on a desktop
  viewport
- **THEN** their default positions MUST avoid perfect overlap so both windows
  are discoverable
- **AND** on narrow desktop-form viewports the windows MUST fall back to clamped
  positions that keep headers and close controls reachable.

#### Scenario: Overlapping default positions keep the river window's close control clear

- **WHEN** both curve windows are opened at their default positions in desktop
  form with a map area at least 900px wide and the two default horizontal
  extents overlap
- **THEN** the default positions are vertically staggered, the river window
  above the station window, so that the river window's close control lies
  outside the station window's box and a hit test on it reaches the river
  window even when the station window was opened last

#### Scenario: Non-overlapping default positions share one top offset

- **WHEN** both curve windows are opened at their default positions in desktop
  form with a map area at least 900px wide and the two default horizontal
  extents do not overlap
- **THEN** both windows have the same default top offset and are anchored
  horizontally at 0.28 and 0.72 of the map width

#### Scenario: Active window rises above the other

- **WHEN** both curve windows are visible in desktop form
- **AND** the user focuses, clicks, or drags one window
- **THEN** that window MUST render above the other window
- **AND** the inactive window MUST remain visible and usable.

#### Scenario: Chart interactions do not start window drag

- **WHEN** a user interacts with the chart body, tooltip, data zoom, tabs, or
  issue-time selector inside a curve window
- **THEN** those interactions MUST keep their chart/control behavior
- **AND** they MUST NOT unintentionally start dragging the window.

#### Scenario: Mobile form does not apply dual-window rules

- **WHEN** the viewport is in mobile form
- **THEN** curve windows are sheets governed by `mobile-curve-sheet`, at most one
  is open, and none is draggable.
