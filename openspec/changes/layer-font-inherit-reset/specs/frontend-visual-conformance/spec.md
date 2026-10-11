## ADDED Requirements

### Requirement: Font utilities SHALL take effect on form controls

The frontend's global stylesheet SHALL NOT declare a font reset for `button`, `input`, `select` or `textarea` outside a cascade layer. Font-size, font-weight, font-family and line-height utilities written on those elements SHALL determine their computed style without an `!important` modifier; the reset that makes form controls inherit by default is the one Tailwind's preflight provides inside `@layer base`.

#### Scenario: A font-size utility on a button wins over the ancestor's size
- **WHEN** a `button` carries a non-important font-size utility and its ancestor has a different font size
- **THEN** the button's computed font size equals the utility's value

#### Scenario: The built stylesheet has no unlayered font reset for form controls
- **WHEN** the production build's CSS is inspected
- **THEN** no rule outside an `@layer` block sets `font: inherit` on `button`, `input`, `select` or `textarea`

#### Scenario: The mobile 16px floor holds without important modifiers
- **WHEN** the display entry is rendered in mobile form
- **THEN** every visible `select`, select trigger and `input` covered by the mobile 16px floor has a computed font size of at least 16px
- **AND** the shipped base components reach it without an `!important` font-size utility
