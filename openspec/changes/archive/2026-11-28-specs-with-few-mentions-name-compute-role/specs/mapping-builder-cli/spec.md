## MODIFIED Requirements

### Requirement: Input authority is the object-store release-frozen package
The CLI SHALL resolve the baseline model package only from the object-store release-frozen shape `<object-store-root>/models/basins_<basin>_shud/<release>/package/`, where the object-store root defaults to `/home/ghdc/nwm/object-store` and MAY be changed only via an explicit `--object-store-root` option; any non-default root SHALL be recorded in the evidence package (the same recording discipline as the dev-workspace override) — this is the sanctioned channel for test runs (e.g. the keliya fixture staged under a tmp root). The CLI SHALL reject any `Basins` dev-workspace path unless an explicit `--allow-dev-workspace` operator flag is supplied, in which case the override SHALL be recorded in evidence with a rationale. A path that is neither object-store-shaped under the configured root nor a recognized `Basins` dev-workspace path SHALL fail closed.

#### Scenario: Object-store release path is accepted
- **WHEN** the CLI input path is an object-store release-frozen package path under the configured root
- **THEN** the CLI proceeds with the build.

#### Scenario: Overridden object-store root is accepted and recorded
- **WHEN** the CLI is invoked with `--object-store-root` pointing at a non-default root (e.g. a tmp root where the keliya fixture is staged as `models/basins_keliya_shud/<release>/package/`) and the input path is object-store-shaped under that root
- **THEN** the CLI proceeds with the build and records the non-default root in the evidence package.

#### Scenario: Dev-workspace path is rejected without the override flag
- **WHEN** the CLI input path is a `Basins` dev-workspace path (node-27 `/home/ghdc/nwm/Basins/...` or `/volume/nwm/Basins/...` on the compute hosts) and no override flag is set
- **THEN** the CLI fails closed and does not read the package or write any output.

#### Scenario: Dev-workspace override is explicit and recorded
- **WHEN** the CLI input path is a dev-workspace path and the explicit override flag is set
- **THEN** the CLI proceeds but records the override path and rationale in the evidence package.

#### Scenario: A path that is neither object-store-shaped nor dev-workspace fails closed
- **WHEN** the CLI input path is neither shaped `<object-store-root>/models/basins_<basin>_shud/<release>/package/` under the configured root nor a recognized `Basins` dev-workspace path
- **THEN** the CLI fails closed and does not read the package or write any output.
