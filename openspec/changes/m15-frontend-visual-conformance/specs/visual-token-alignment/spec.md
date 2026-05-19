## ADDED Requirements

### Requirement: Visual token alignment
The frontend SHALL align shared tokens for nav, panels, cards, controls, typography, colors, shadows, timeline, warnings, and chart defaults with 06B.

#### Scenario: Token audit
WHEN visual conformance begins
THEN the implementation documents current token mappings and updates shared CSS/components rather than one-off page styles

#### Scenario: Warning colors
WHEN warning/return-period levels render
THEN colors match the shared warning palette consistently across overview, basin, and flood pages

#### Scenario: Shared component precedence
WHEN a route-specific visual issue is fixed
THEN the fix uses shared tokens or shared component styles first unless the route has a documented exception

#### Scenario: Focus and control tokens
WHEN buttons, icon controls, inputs, tabs, and toggles receive keyboard focus
THEN focus rings and active states use shared tokens and remain visible on supported backgrounds

#### Scenario: Shared control roots
WHEN select, tabs, dialog, toast, button, card, badge, app shell, filters, modals, or toast controls render
THEN radius, shadow, spacing, control height, focus ring, and overlay z-index use the shared M15 token baseline or a documented compatible override

#### Scenario: Map-first overlay stacking
WHEN shared select/dropdown popovers, dialogs/overlays, and toast layers render on map-first routes
THEN their computed z-index values exceed page navigation, map controls, map route popups, and timeline chrome in the order popover < overlay/modal < toast

### Requirement: Deterministic visual evidence
M15 screenshot evidence SHALL be bounded, repeatable, and independent of live external map tile/style/font hosts.

#### Scenario: External network guard
WHEN M15 visual evidence runs
THEN API calls use deterministic fixtures, known external map tile/style/font requests are fulfilled with deterministic neutral stubs, and unexpected non-local requests fail the evidence run

#### Scenario: Frozen head metadata
WHEN M15 visual evidence runs in CI for a PR
THEN the manifest SHA is a real commit SHA equal to `GITHUB_SHA`, and PR evidence is regenerated after the final commit/frozen head before closure
