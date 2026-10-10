## MODIFIED Requirements

### Requirement: Responsive Layout

The monitoring page SHALL adapt to different screen sizes. The width bands below apply in desktop form (viewport at least 768 CSS px wide and at least 500 CSS px high); in mobile form the page SHALL follow the single-column fallback of the `mobile-ops-fallback` capability instead, with no accordion, tab or drawer required.

#### Scenario: Wide screen (>=1440px)

- **WHEN** the viewport is in desktop form and its width is 1440px or greater
- **THEN** the page SHALL display the three-column layout: left pipeline view, center job table, right trends panel

#### Scenario: Medium screen (1024px-1439px)

- **WHEN** the viewport is in desktop form and its width is between 1024px and 1439px
- **THEN** the trends panel SHALL collapse into a toggleable drawer or accordion below the job table
- **THEN** the pipeline view and job table SHALL share the available width

#### Scenario: Narrow screen (<1024px)

- **WHEN** the viewport is in desktop form and its width is between 768px and 1023px
- **THEN** the stage cards SHALL collapse into a compact horizontal scrollable strip or accordion
- **THEN** the job table SHALL take full width with horizontal scroll for overflow columns
- **THEN** the trends panel SHALL be accessible via a dedicated tab or expandable section

#### Scenario: Mobile form uses the single-column fallback

- **WHEN** the viewport is in mobile form
- **THEN** the page SHALL show its sections in one column inside its page scroll container, and SHALL NOT be required to collapse stage cards or trends into an accordion, tab or drawer
