## ADDED Requirements

### Requirement: Frontend test files SHALL stay under the large-file limit without exemptions
A frontend test file SHALL be split by topic instead of being exempted when it reaches the 1000-line limit. Shared fixtures go into a non-collected helper module. Each split file declares its own `vi.mock` calls. The set of collected test titles and every assertion SHALL stay unchanged across the split.

#### Scenario: Popup test split
- **WHEN** `apps/frontend/src/components/map/__tests__/M11StationForcingPopup.test.tsx` is split
- **THEN** every resulting file is under 1000 lines, `.large-file-guard.json` no longer exempts it, and the collected `it` titles equal the 24 before the split
