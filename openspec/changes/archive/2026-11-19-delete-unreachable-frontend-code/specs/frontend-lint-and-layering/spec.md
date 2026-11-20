## MODIFIED Requirements

### Requirement: `pages/` SHALL be a leaf layer
The hydro-met bootstrap SHALL live at `src/stores/hydroMetBootstrap.ts`; no module SHALL exist at `src/pages/hydroMet/bootstrap.ts`. No non-test file outside `src/pages` and `src/App.tsx` SHALL import from `src/pages`. `import-x/no-cycle` SHALL report zero cycles over `src/`.

#### Scenario: No directory cycle remains
- **WHEN** `pnpm lint` runs on `src/`
- **THEN** `import-x/no-cycle` reports nothing

#### Scenario: The bootstrap is not reintroduced under pages
- **WHEN** the source tree is listed
- **THEN** `src/stores/hydroMetBootstrap.ts` exists and `src/pages/hydroMet/bootstrap.ts` does not
