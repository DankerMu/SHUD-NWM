## ADDED Requirements

### Requirement: Declared state checkpoint hours SHALL be structurally reachable under the configured restart cadence

Forecast manifest assembly SHALL reject, with a stable typed error code, any
combination where a declared state checkpoint hour is not an integer multiple
of `update_ic_step_minutes` — at every manifest production site, not just one
— because SHUD writes a restart file only when the elapsed model time divides
the configured `Update_IC_STEP`, including at END, so a checkpoint hour that
does not divide the cadence can never be produced. The checkpoint recovery
rerun SHALL set the cadence it needs for the hour it is recovering, so
recovery stays correct independently of that
configuration. The cadence written for a recovery rerun applies to that
scratch rerun only; the main run's published configuration is restored
afterwards.

#### Scenario: a misaligned cycle configuration is refused instead of silently unreachable

WHEN allowed cycle hours produce checkpoint hours that are not integer
multiples of the derived restart cadence
THEN forecast manifest assembly fails with a stable typed error code at each
manifest production site, instead of emitting a manifest whose checkpoint
hours can never be written

#### Scenario: recovery rerun makes its target hour reachable

WHEN the checkpoint recovery rerun shortens the run to a missing forecast
hour
THEN the configuration it writes sets the restart cadence to that hour's
minute count, and after the rerun the main run's published configuration text
is byte-for-byte what it was before

#### Scenario: aligned configurations are unaffected

WHEN the configured cycle hours are evenly spaced, including the default
configuration
THEN manifest assembly and checkpoint behavior are unchanged; analysis
manifests, which declare no state checkpoint hours, are outside this
requirement
