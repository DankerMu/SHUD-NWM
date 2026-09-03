## ADDED Requirements

### Requirement: Header brand identity for V2.0
The site header SHALL display the title `全国水文模拟系统（V2.0）` (full-width parentheses) in bold at a larger size than the V1 header, and SHALL display the sponsor logo strip at a larger size; the header height MAY increase to accommodate both.

#### Scenario: Title text and weight
- **WHEN** the application shell renders
- **THEN** the header title text equals `全国水文模拟系统（V2.0）` exactly (full-width `（` and `）`)
- **AND** the title uses a bold weight and a font size of at least 28px

#### Scenario: Sponsor strip enlarged
- **WHEN** the header renders on a `lg` viewport
- **THEN** the sponsor image renders at a height of at least 56px with `object-contain`
- **AND** the header height is at least 84px so the title and sponsor strip do not clip
