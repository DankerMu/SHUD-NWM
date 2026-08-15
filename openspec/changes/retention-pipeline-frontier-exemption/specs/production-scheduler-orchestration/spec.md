# production-scheduler-orchestration (delta)

## ADDED Requirements

### Requirement: Object-store retention respects the pipeline frontier

The scheduler's end-of-pass object-store retention SHALL NOT delete cycle-scoped
artifacts (raw, canonical, forcing) or run workspaces for any cycle at or after
the pipeline's active lower bound — the minimum of (a) the earliest cycle that
still has non-terminal work in the current pass's candidate state (selected
candidates, construction-blocked candidates, and skipped candidates whose skip
reason is not in the explicit terminal set, unknown reasons counting as
non-terminal) and (b) the current discovery window's lower bound. Wall-clock
age alone SHALL NOT be a sufficient deletion criterion while an active lower
bound is derivable.

#### Scenario: catch-up cycle exempted from retention

WHEN a replay or backfill pass is catching up and a cycle older than the
wall-clock retention cutoff is at or after the active lower bound
THEN the retention plan does not select that cycle's raw, canonical, forcing,
or run-workspace entries for deletion
AND each exempted entry is recorded in the retention receipt's skipped list
with a frontier-specific reason distinct from the not-yet-expired reason
AND the receipt carries the active lower bound, its source, and the count of
frontier-protected entries, readable both in the full receipt and after
evidence-size compaction
AND exempted entries are not size-scanned.

#### Scenario: terminal cycles remain collectable

WHEN a cycle older than the retention cutoff is before the active lower bound
(its candidates are all terminal and it is outside the discovery window)
THEN retention deletes it exactly as before this change, with unchanged
freed-bytes accounting.

#### Scenario: steady-state behavior is unchanged

WHEN the configured lookback window plus the cycle lag plus twice the largest
source cycle interval fit within the retention window
THEN the retention plan is identical, key for key, to the plan produced
without frontier awareness
AND the enabled, dry-run, and forced-dry-run gates and the protected-prefix
exemptions behave exactly as before.

#### Scenario: misconfigured windows fail safe

WHEN the configured lookback window plus cycle lag exceeds the retention
window
THEN the frontier gate exempts the affected in-window cycles instead of
allowing the produce-then-delete spin
AND the drift direction is always over-retention, never over-deletion, and is
visible in the receipt's frontier block.

#### Scenario: failed run workspace preserved for diagnosis

WHEN a run's cycle is at or after the active lower bound (including cycles in
the discovery window whose upstream data is no longer available)
THEN the run workspace under runs/ is not deleted
AND the SHUD stdout/stderr logs inside it remain readable for post-mortem.

#### Scenario: no active lower bound provided

WHEN retention planning runs without an active lower bound (for example a
direct invocation outside a scheduler pass)
THEN retention falls back to the wall-clock criterion unchanged
AND the receipt's frontier block records that no bound was applied.
