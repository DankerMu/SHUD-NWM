## ADDED Requirements

### Requirement: Living documents cite code by symbol and pin historical measurements to a SHA

A living document that describes current code SHALL cite that code by a symbol name or a greppable literal. A line number MAY follow the symbol as a helper, but SHALL NOT be the only anchor. A historical measurement SHALL cite its coordinates with the commit SHA at which they were measured, and SHALL NOT be moved to current line numbers. Every rewritten citation SHALL be re-derived from the symbol and read back. It SHALL NOT be computed by adding a line offset.

#### Scenario: A current-truth anchor survives unrelated edits

- **WHEN** code above the cited symbol gains or loses lines
- **THEN** the citation still identifies the symbol by name

#### Scenario: A historical measurement keeps its original coordinates

- **WHEN** a document records a measurement taken at an earlier commit
- **THEN** its coordinates carry that commit's SHA, and resolving them at that SHA shows the measured code
