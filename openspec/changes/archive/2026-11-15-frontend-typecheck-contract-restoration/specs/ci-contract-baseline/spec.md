## ADDED Requirements

### Requirement: Frontend type check MUST gate the frontend build

The `frontend-build` CI job SHALL execute a full-program TypeScript check (`tsc --noEmit` over the application project) as its own named step, ordered before the bundle build, and that step SHALL fail the job on any type error. The check SHALL NOT be reduced to a warning, folded into another step's command line, or satisfied by `vite build` (which transpiles per file and surfaces no whole-program type errors) or by `check:api-types` (which only diffs regenerated types against the static schema and never compiles application code).

#### Scenario: Type error fails the frontend job

- **WHEN** a pull request touching `apps/frontend/**` introduces a TypeScript error anywhere in the type-checked project
- **THEN** the `frontend-build` job's type-check step MUST fail and the job MUST be red

#### Scenario: The gate is observably executed

- **WHEN** the `frontend-build` job runs on a pull request
- **THEN** the type-check step MUST appear in the run as executed and passed, not skipped and not collapsed into the install/build/test step

#### Scenario: Contract drift between backend and frontend is caught

- **WHEN** a backend change removes or renames a named response schema that a frontend module consumes
- **THEN** regenerating the frontend types and running the gate MUST fail, rather than the stale reference silently degrading to `unknown`
