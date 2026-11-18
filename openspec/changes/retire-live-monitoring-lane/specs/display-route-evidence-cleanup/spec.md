## MODIFIED Requirements

### Requirement: Mocked e2e evidence is separate from live proof

Display evidence docs SHALL distinguish deterministic mocked regression specs from node-27 live display proof.

#### Scenario: mocked Playwright spec uses API route mocks
- **WHEN** a Playwright spec uses broad API route mocks
- **THEN** docs and config classify it as mocked regression evidence rather than live node-27 proof

#### Scenario: live display spec is executed
- **WHEN** a live display evidence lane (`test:e2e:live-c4-display` or `test:e2e:live-river-click`) runs against node-27
- **THEN** it does not rely on broad API mocks and uses explicit live base URL configuration
