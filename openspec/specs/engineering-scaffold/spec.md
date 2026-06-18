# engineering-scaffold Specification

## Purpose
TBD - created by archiving change m35-frontend-modernization. Update Purpose after archive.
## Requirements
### Requirement: Vite project initialization
The system SHALL initialize a Vite 6 + React 18 + TypeScript project under `apps/frontend/` with `pnpm` as the package manager.

#### Scenario: Fresh project setup
- **WHEN** developer runs `pnpm create vite` in `apps/frontend/`
- **THEN** a working Vite project is created with React + TypeScript template, `pnpm dev` starts the dev server on port 5173

### Requirement: Tailwind CSS theme tokens
The system SHALL configure Tailwind CSS v4 with theme tokens mapped from the existing CSS variables (background, panel, foreground, muted, border, accent, danger, river, river-strong).

#### Scenario: Design token consistency
- **WHEN** a component uses `className="bg-background text-foreground"`
- **THEN** the rendered colors MUST match the existing `--bg` and `--text` CSS variable values

### Requirement: shadcn/ui component library
The system SHALL install shadcn/ui and generate base components: Button, Card, Table, Dialog, Badge, Select, Toast, Tabs, DropdownMenu.

#### Scenario: Component availability
- **WHEN** a page imports `<Button>` from `@/components/ui/button`
- **THEN** the component renders with Tailwind styling consistent with the project theme

### Requirement: API type generation from OpenAPI
The system SHALL use `openapi-typescript` to generate TypeScript types from `openapi/nhms.v1.yaml` and use `openapi-fetch` for type-safe API calls.

#### Scenario: Type-safe API call
- **WHEN** a component calls `client.GET('/api/v1/pipeline/stages', { params: { query: { source: 'GFS', cycle_time: '...' } } })`
- **THEN** the response type MUST be automatically inferred as the PipelineStagesResponse schema

#### Scenario: Type regeneration on spec change
- **WHEN** developer modifies `openapi/nhms.v1.yaml` and runs `pnpm generate:api`
- **THEN** `src/api/types.ts` is updated and any type mismatches cause TypeScript compilation errors

### Requirement: Zustand state management
The system SHALL use Zustand stores for auth (role state), monitoring (stages/jobs/metrics), and forecast (selected segment/data).

#### Scenario: Store reactivity
- **WHEN** the monitoring store's `stages` array is updated via `fetchAll()`
- **THEN** all components subscribed to `useMonitoringStore(s => s.stages)` MUST re-render with the new data

### Requirement: SPA routing with RBAC
The system SHALL use react-router-dom v7 with routes `/` (forecast) and `/monitoring` (RBAC-gated to operator/model_admin/sys_admin).

#### Scenario: Unauthorized access to monitoring
- **WHEN** a user with role `viewer` navigates to `/monitoring`
- **THEN** the RBACGate component MUST display a "权限不足" message and prevent monitoring content from rendering

#### Scenario: SPA fallback
- **WHEN** a user directly navigates to `/monitoring` via URL
- **THEN** FastAPI MUST serve `index.html` (SPA fallback), and React Router MUST render the MonitoringPage

### Requirement: Vite dev proxy
The system SHALL configure Vite dev server to proxy `/api/*` requests to `http://localhost:8000`.

#### Scenario: API proxy in development
- **WHEN** the frontend calls `fetch('/api/v1/health')` during development
- **THEN** the request MUST be proxied to the FastAPI backend on port 8000

