# cleanup-delivery Specification

## Purpose
TBD - created by archiving change m35-frontend-modernization. Update Purpose after archive.
## Requirements
### Requirement: Legacy file removal
The system SHALL delete the legacy standalone `apps/frontend/monitoring.html` after migration. The legacy `apps/frontend/index.html` SHALL be replaced by the Vite SPA entry `apps/frontend/index.html` (a minimal HTML file that loads the React app bundle). The Vite entry file MUST NOT be deleted.

#### Scenario: No legacy standalone HTML files in production
- **WHEN** the migration is complete
- **THEN** `apps/frontend/monitoring.html` MUST NOT exist, and `apps/frontend/index.html` MUST be the Vite SPA entry (containing a `<script type="module" src="/src/main.tsx">` tag, not legacy inline JS)

### Requirement: FastAPI static mount update
The system SHALL update `apps/api/main.py` to mount `apps/frontend/dist/` instead of `apps/frontend/`, with a SPA fallback catch-all route.

#### Scenario: SPA fallback serves index.html
- **WHEN** a browser requests `/monitoring` directly
- **THEN** FastAPI MUST serve `apps/frontend/dist/index.html` so React Router can handle the route

### Requirement: CI frontend build step
The system SHALL add a CI step that runs `pnpm install --frozen-lockfile && pnpm build && pnpm test` in `apps/frontend/`.

#### Scenario: CI catches build failure
- **WHEN** a TypeScript compilation error is introduced
- **THEN** the CI frontend build step MUST fail and block the PR

### Requirement: Build artifact size budget
The system SHALL ensure the Vite build output is under 500KB gzip (excluding MapLibre GL JS which is loaded separately).

#### Scenario: Bundle size check
- **WHEN** `pnpm build` completes
- **THEN** the total gzipped size of JS + CSS output files (excluding MapLibre GL) MUST be less than 500KB

### Requirement: Environment configuration
The system SHALL provide `apps/frontend/.env.example` documenting `VITE_API_BASE_URL` and any other required environment variables.

#### Scenario: Default configuration works
- **WHEN** developer copies `.env.example` to `.env` without modification
- **THEN** `pnpm dev` MUST start successfully with the default API base URL `/api/v1`

