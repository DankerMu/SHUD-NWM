## MODIFIED Requirements

### Requirement: shadcn/ui component library
The system SHALL install shadcn/ui and generate the base components the application uses: Button, Card, Table, Dialog, Badge, Select, Toast. A generated component that no application module imports SHALL NOT be kept, and neither SHALL its Radix dependency.

#### Scenario: Component availability
- **WHEN** a page imports `<Button>` from `@/components/ui/button`
- **THEN** the component renders with Tailwind styling consistent with the project theme

### Requirement: Zustand state management
The system SHALL use Zustand stores for auth (role state) and monitoring (stages/jobs/metrics). Forecast series for the river popup are loaded by `src/lib/hydroMet/riverForecast.ts` and held by the popup component; there is no forecast store.

#### Scenario: Store reactivity
- **WHEN** the monitoring store's `stages` array is updated via `fetchAll()`
- **THEN** all components subscribed to `useMonitoringStore(s => s.stages)` MUST re-render with the new data
