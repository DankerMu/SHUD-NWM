## MODIFIED Requirements

### Requirement: UI tokens and component proportions follow the design spec

The system SHALL implement the documented visual tokens and component proportions or map them explicitly to existing project tokens.

#### Scenario: Layout tokens are applied
- **WHEN** overview or basin detail is rendered at supported desktop viewports
- **THEN** the top navigation (the `SiteHeader` brand header) MUST be 84px high
- **AND** side panels MUST use the documented 280px left and 320-360px right proportions where viewport size permits
- **AND** the bottom timeline MUST use the documented 64px height

#### Scenario: Component styling is consistent
- **WHEN** panels, cards, buttons, inputs, toggles, tags, tooltips, and popup cards render
- **THEN** they MUST follow the documented font sizes, 4px spacing scale, 4px/8px radii, shadows, status colors, and icon sizing
- **AND** any existing project token substitution MUST be documented in code or developer notes
