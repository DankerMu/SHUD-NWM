## Context

The current single-map display already has separate state for a river forecast
panel and a station forcing panel, but the click handlers clear the other panel
when one opens. Both panels are centered absolute overlays with the same
`z-index` and no drag affordance, so opening both would overlap even if the
mutual clearing were removed.

Meteorological stations are still represented as `M11Layer = 'met-stations'`.
That makes station rendering mutually exclusive with the active hydrology layer
and causes `/meteorology` to encode `layer=met-stations`. The older GIS design
uses independent layer toggles and the customer-facing MVP description expects
one map where users can click both river segments and forcing stations.

The forecast cycle controls inside the dark glass popups are native `select`
elements. Their controls are styled dark, but the browser/OS renders native
option popups independently; on some devices the option surface becomes white
with low-contrast text.

## Goals / Non-Goals

**Goals:**

- Keep river q_down and station forcing comparison in one map workflow.
- Preserve shareable URL state while retiring `layer=met-stations` as the
  primary layer representation.
- Make station points/clusters visually and interactively sit above hydrology
  lines without making rivers impossible to click.
- Make curve windows movable and independently closable so users can compare a
  station forcing curve with a river flow curve.
- Make all issue-time selectors in M11 curve popups use a consistent dark
  popup surface across supported browsers.

**Non-Goals:**

- No backend API changes.
- No station-MVT endpoint implementation.
- No meteorological raster/grid layer restoration.
- No change to q_down forecast-series or station-series loading contracts.
- No new multi-station or multi-river comparison chart model.

## Decisions

### Use a controlled dark selector for issue-time menus

M11 popup issue-time controls should use the existing `components/ui/select.tsx`
Radix wrapper, or a thin M11-specific wrapper around it, instead of relying on
native `option` styling. The trigger and content must be styled for the dark
glass popup context, preserve accessible labels, preserve existing test ids
where practical, and support disabled retained-window options.

Alternative considered: add inline styles to native `<option>` elements. That is
smaller but remains dependent on browser/OS native menu behavior and is the
root of the current inconsistent white surface.

### Split station overlay state from hydrology layer state

`layer` should continue to represent the active hydrology product layer such as
`discharge`, `flood-return-period`, or `warning-level`. Station visibility should
be encoded as a separate boolean query state, serialized as `metStations=1`.

Legacy compatibility rules:

- `?layer=met-stations` is accepted as a stale alias and normalized to
  `layer=discharge&metStations=1`.
- `/meteorology` redirects to `/` with `metStations=1`; it does not create new
  `layer=met-stations` URLs.
- If a legacy URL also carries a valid hydrology layer, the valid hydrology
  layer remains active and station overlay is enabled.

Alternative considered: keep `met-stations` in `M11Layer` and introduce a
special case where that layer also draws discharge. That would preserve the
current enum but keeps the misleading mental model and makes timeline, legend,
and MVT source selection harder to reason about.

### Treat stations as an overlay in rendering and hit testing

When `metStations` is enabled and station features are available, the MapLibre
station source/layers render after hydrology layers. Station point and cluster
hit testing runs before river hit testing for overlapping pixels. Exposed river
line pixels still open the river forecast panel.

Station inventory loading uses the existing basin/model-scoped station API:
overview mode derives station requests from visible basin contexts, and
basin-detail mode uses the current basin context. Source/cycle identity remains
strict for the station-series curve request after a station is clicked; it is
not a station-inventory filter. Empty or truncated station inventory results
remain honest UI states.

### Use a shared draggable curve-window frame

River and station panels should share a draggable popup frame or hook. Dragging
starts from the panel header/handle only, not from the chart body, so chart
zoom/tooltip interactions remain intact. The frame clamps movement to the map
viewport, tracks focus so the active window rises above the other window, and
resets to a sensible default position when the selected feature identity changes.

Initial placement should avoid perfect overlap when both panels are open, for
example river left-of-center and station right-of-center on desktop, while still
falling back to centered/clamped placement on narrower viewports.

Alternative considered: keep one centered modal and require users to close it
before selecting another feature. That directly conflicts with the requested
same-screen comparison workflow.

## Risks / Trade-offs

- URL migration risk -> cover stale `layer=met-stations`, `/meteorology`, and
  new `metStations=1` parsing/serialization in unit and route tests.
- Hit-priority risk -> explicitly test station-over-river overlap and river
  clickability on exposed line pixels.
- Draggable layout risk -> clamp to the map viewport and test desktop plus
  narrow viewports so axes, close buttons, and charts remain usable.
- Selector regression risk -> keep keyboard-accessible labels and disabled
  option semantics while changing the implementation from native select to a
  controlled select.

## Migration Plan

1. Add the new query state and normalize old station-layer URLs.
2. Update controls and station loading to use the overlay boolean.
3. Update map render/hit ordering and panel coexistence.
4. Replace native issue-time selectors with the dark selector component.
5. Add/adjust unit, component, and mocked e2e coverage.
6. After merge/deploy, verify on node-27 display that `/`, `/meteorology`, river
   click, station click, and both-popups-open workflows still operate against
   live display data.

Rollback is local to frontend code: restoring the old query parser/control
behavior and disabling the station overlay toggle returns the map to the
previous exclusive layer model without backend migration.
