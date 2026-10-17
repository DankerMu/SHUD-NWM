# Spec Delta

## ADDED Requirements

### Requirement: The stale-display-route check SHALL skip archived changes
`_check_stale_route_tokens` SHALL not scan paths under `openspec/changes/archive/`; `_path_is_route_authority_expanded_scope` SHALL keep its treatment of `docs/archived/` and `docs/runbooks/`. The report's `skipped_path_families` metadata SHALL name the archive skip as scoped to `stale-display-route-token`.

#### Scenario: Archived change is not a finding
- **WHEN** an archived change under `openspec/changes/archive/` mentions a retired display route
- **THEN** the report contains no `stale-display-route-token` finding for that path

#### Scenario: Active change still is
- **WHEN** a non-archived change under `openspec/changes/` mentions a retired display route without an archive marker
- **THEN** the report contains a `stale-display-route-token` finding for it
