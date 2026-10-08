## ADDED Requirements

### Requirement: Mobile form is defined by one viewport predicate shared by CSS and script

The frontend SHALL treat a viewport as **mobile form** when its width is below 768 CSS px or its height is below 500 CSS px, and as **desktop form** otherwise. Within mobile form, **short-landscape** SHALL mean a viewport whose height is below 500 CSS px in landscape orientation. The predicate SHALL be exposed once to styles (a `mobile` variant and a `mobile-landscape` variant) and once to script (a hook returning `mobile` and `landscape` booleans), both derived from the same media-query strings, and the hook SHALL update when the viewport crosses the predicate without a page reload. The application shell root SHALL expose the script-side result as `data-viewport-form` (`mobile` or `desktop`) and `data-viewport-short-landscape` (`true` or `false`), and SHALL expose the style-side result as the custom property `--nhms-viewport-form` (`mobile` or `desktop`) set through the `mobile` variant.

#### Scenario: Width boundary

- **WHEN** the viewport is 767×900
- **THEN** the shell root has `data-viewport-form="mobile"` and `data-viewport-short-landscape="false"`
- **AND** at 768×900 it has `data-viewport-form="desktop"`

#### Scenario: Height boundary

- **WHEN** the viewport is 900×499
- **THEN** the shell root has `data-viewport-form="mobile"` and `data-viewport-short-landscape="true"`
- **AND** at 900×500 it has `data-viewport-form="desktop"`

#### Scenario: Narrow and tall is not short-landscape

- **WHEN** the viewport is 390×664
- **THEN** the shell root has `data-viewport-form="mobile"` and `data-viewport-short-landscape="false"`

#### Scenario: Short but portrait is not short-landscape

- **WHEN** the viewport is 320×480
- **THEN** the shell root has `data-viewport-form="mobile"` and `data-viewport-short-landscape="false"`

#### Scenario: Narrow and short landscape

- **WHEN** the viewport is 600×400
- **THEN** the shell root has `data-viewport-form="mobile"` and `data-viewport-short-landscape="true"`

#### Scenario: Rotation updates the form without reload

- **WHEN** a page rendered at 390×664 is resized to 750×342
- **THEN** the shell root's `data-viewport-short-landscape` changes from `false` to `true` without a reload

#### Scenario: Style and script agree when only the height arm matches

- **WHEN** the viewport is 844×390
- **THEN** the shell root has `data-viewport-form="mobile"` and its computed `--nhms-viewport-form` is `mobile`

#### Scenario: Style and script agree in desktop form

- **WHEN** the viewport is 1280×900
- **THEN** the shell root has `data-viewport-form="desktop"` and its computed `--nhms-viewport-form` is `desktop`

### Requirement: The application shell is declared against the dynamic viewport and safe areas

The application shell root SHALL declare its height in dynamic viewport units and its width as the full available width, the document's viewport meta SHALL include `viewport-fit=cover`, and the shell root SHALL declare padding from the four `env(safe-area-inset-*)` values in both forms. No visible interactive control on `/` SHALL extend outside the viewport at any mobile-form viewport from 320 CSS px wide upward. Whether bottom controls clear a real browser toolbar and whether controls clear a real device notch are verified by the real-device checklist, not by emulation.

#### Scenario: Shell style facts

- **WHEN** `/` is loaded at 390×664
- **THEN** the shell root's height declaration resolves from `100dvh`, the viewport meta content includes `viewport-fit=cover`, and the shell root's padding declarations reference `env(safe-area-inset-top)`, `env(safe-area-inset-right)`, `env(safe-area-inset-bottom)` and `env(safe-area-inset-left)`

#### Scenario: Controls stay inside the viewport

- **WHEN** `/` is rendered at 320×568, 390×664, 750×342 or 844×390
- **THEN** every visible interactive control's bounding box lies inside the viewport

#### Scenario: Desktop geometry is not changed by the shell declarations

- **WHEN** `/` is rendered at 1280×900
- **THEN** the shell root's rendered width and height equal the viewport's and its computed padding is 0 on all four sides

### Requirement: The site header is compact in mobile form

In mobile form the site header SHALL be a single row 48 CSS px high showing the logo and the Chinese title on one line without wrapping, and SHALL NOT render the English subtitle. This applies on every route. In desktop form the header SHALL remain 84 CSS px high with the subtitle.

#### Scenario: Portrait phone header

- **WHEN** `/` is rendered at 390×664
- **THEN** the header is 48px high, the title's rendered height is one line, the English subtitle is absent, and the map region's top edge is at y=48

#### Scenario: Short-landscape header through the height arm

- **WHEN** `/` is rendered at 844×390
- **THEN** the header is 48px high

#### Scenario: Header on an operational route

- **WHEN** an authorized user opens `/ops` at 390×664
- **THEN** the header is 48px high

#### Scenario: Desktop header unchanged

- **WHEN** `/` is rendered at 768×1024 or 1280×900
- **THEN** the header is 84px high and the English subtitle is present

### Requirement: Mobile form enforces touch-target and form-font floors

In mobile form every visible interactive control on `/` (buttons, links, select triggers, toggles, chips) SHALL have a hit area of at least 44×44 CSS px, in every UI state: default, each overlay panel expanded, and a curve sheet open. In mobile form every visible form control that takes focus for text or option entry on `/`, `/ops` and `/monitoring` SHALL have a computed font size of at least 16 CSS px. The MapLibre attribution control and the development-only role-override selector are exempt from both floors.

#### Scenario: Touch-target audit in the default state

- **WHEN** `/` is rendered at 390×664 or 750×342 with no panel expanded and no sheet open
- **THEN** every visible interactive control other than the exempt ones measures at least 44×44

#### Scenario: Touch-target audit with a panel expanded

- **WHEN** the layer panel, the basemap panel or the legend panel is expanded at 390×664 or 750×342
- **THEN** every visible interactive control inside the expanded panel measures at least 44px high and at least 44px wide

#### Scenario: Touch-target audit with a sheet open

- **WHEN** a river window or a station window is open at 390×664 or 750×342
- **THEN** every visible interactive control inside the sheet measures at least 44×44

#### Scenario: Form-font audit on the map page

- **WHEN** `/` is rendered at 390×664 or 750×342
- **THEN** every visible `select` and select trigger other than the exempt ones has a computed font size of at least 16px

#### Scenario: Form-font audit on operational pages

- **WHEN** an authorized user opens `/ops` or `/monitoring` at 390×664
- **THEN** every visible `select`, select trigger and `input` other than the exempt ones has a computed font size of at least 16px

### Requirement: The development role-override selector stays usable in mobile form

When the build enables the role override, in mobile form the role-override selector SHALL remain visible and operable while no curve sheet is open, and SHALL NOT intersect the header, the launchers, the bottom control bar, or any floating notice or status overlay. When the build does not enable the role override, the selector SHALL be absent in both forms.

#### Scenario: Selector clear of the mobile chrome

- **WHEN** `/` is rendered at 390×664 or 750×342 with the role override enabled
- **THEN** the selector's bounding box lies inside the viewport and does not intersect the header, any launcher or the control bar, and choosing a role through it changes the active role

#### Scenario: Selector clear of the notice band

- **WHEN** a floating notice and the map-source-error status overlay are shown at 390×664 with the role override enabled
- **THEN** the selector's bounding box intersects neither of them

#### Scenario: Selector absent without the override

- **WHEN** the shell is rendered with the role override disabled
- **THEN** no role-override selector is rendered

### Requirement: Desktop form geometry is unchanged

At viewports in desktop form the layout of `/`, `/ops`, `/monitoring` and `/system/model-assets` SHALL be identical to the layout before this change — the header is 84px high, the layer switcher, basemap switcher and legend are always-expanded panels at their existing offsets, curve windows are draggable, and map zoom controls are rendered — with one exception: the default size of a curve window at viewport widths from 768 to 1142 CSS px, which follows the `map-feature-popups` minimum-size requirement.

#### Scenario: Existing desktop collision oracle still passes

- **WHEN** the existing overlay-collision assertions run at 1920, 1440, 1280 and 800px widths
- **THEN** they pass without modification to their expected geometry

#### Scenario: Tablet portrait stays in desktop form

- **WHEN** `/` is rendered at 768×1024
- **THEN** the header is 84px high, the layer switcher, basemap switcher and legend are expanded panels, no launcher is present, and the map zoom controls are visible

#### Scenario: Operational pages in desktop form

- **WHEN** an authorized user opens `/ops`, `/monitoring` or `/system/model-assets` at 1280×900
- **THEN** the header is 84px high
- **AND** the existing monitoring regression assertions pass without modification
