## MODIFIED Requirements

### Requirement: The development role-override selector stays usable in mobile form

When the build enables the role override, in mobile form the role-override selector SHALL remain visible and operable while no curve sheet is open. On the map page (`/`) it SHALL NOT intersect the header, the launchers, the bottom control bar, or any floating notice or status overlay. On every other route it SHALL sit inside the site header, SHALL NOT intersect the page's scroll container, the header logo, the visible header title or the partner strip, and the header SHALL keep its mobile height. When the build does not enable the role override, the selector SHALL be absent in both forms and the header's markup SHALL be the same as without the selector.

#### Scenario: Selector clear of the mobile chrome

- **WHEN** `/` is rendered at 390×664 or 750×342 with the role override enabled
- **THEN** the selector's bounding box lies inside the viewport and does not intersect the header, any launcher or the control bar, and choosing a role through it changes the active role

#### Scenario: Selector clear of the notice band

- **WHEN** a floating notice and the map-source-error status overlay are shown at 390×664 with the role override enabled
- **THEN** the selector's bounding box intersects neither of them

#### Scenario: Selector in the header on scrolling pages

- **WHEN** `/ops`, `/monitoring` or `/system/model-assets` is rendered at 390×664, 750×342, 844×390 or 320×568 with the role override enabled
- **THEN** the selector's bounding box lies inside the 48px header and does not intersect the page's scroll container, the logo or the visible title

#### Scenario: Roles can be chosen from the header

- **WHEN** `/ops` is rendered at 390×664, 750×342 or 844×390 with the role override enabled
- **THEN** each of the five roles can be chosen through the selector

#### Scenario: Selector beside the partner strip

- **WHEN** `/ops` is rendered at 1280×400 with the role override enabled, where the partner strip is visible in mobile form
- **THEN** the selector lies inside the header to the right of the partner strip and intersects neither the strip nor the title

#### Scenario: Selector reachable on the access-denied page

- **WHEN** a role without access opens `/ops` in mobile form with the role override enabled
- **THEN** the selector is inside the header and choosing a role with access through it shows the page

#### Scenario: Selector absent without the override

- **WHEN** the shell is rendered with the role override disabled
- **THEN** no role-override selector is rendered and the header's markup is unchanged
