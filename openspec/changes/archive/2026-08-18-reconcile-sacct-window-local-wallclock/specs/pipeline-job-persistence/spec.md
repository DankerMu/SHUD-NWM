## ADDED Requirements

### Requirement: Reconcile sacct scan windows are rendered in the host's local wall clock

The restart-reconciliation comment scan SHALL render its sacct page
boundaries in the host's local wall-clock representation of the
UTC-computed instants, because sacct interprets bare timestamps in the
host's local timezone: page arithmetic stays on the monotonic UTC axis,
and only the rendered `--starttime`/`--endtime` strings are converted, so
the scanned interval equals the intended interval on every host timezone
instead of being silently translated by the host offset (which on an
east-of-UTC host shifted the whole window into the past and made every
absence verdict for a job younger than the offset vacuous). The
per-session page freeze and the page-cache identity keyed by the rendered
strings keep their existing semantics, and on a UTC host the rendered
strings are byte-for-byte what they were before. On DST-observing hosts
the once-yearly fall-back hour renders an ambiguous local timestamp that
sacct's timezone-less interface may resolve up to an hour off, so the
coverage-complete gate can over-claim its scanned interval by at most one
hour once a year (inherent to sacct, not to this conversion;
spring-forward is safe because a UTC-to-local conversion never emits a
skipped wall clock, and adjacent page boundaries sit twelve hours apart
so rendered page keys can never collide).

#### Scenario: an east-of-UTC host scans the intended window

WHEN the reconcile comment scan runs on a host east of UTC
THEN the rendered page boundaries are the local wall-clock forms of the
UTC page instants, so a job submitted minutes ago falls inside the newest
page instead of beyond it

#### Scenario: a UTC host renders the same strings as before

WHEN the host timezone is UTC
THEN every rendered page boundary string is byte-for-byte identical to
the pre-change output

#### Scenario: page freeze and cache identity are unchanged

WHEN a querier session renders its pages under any host timezone that
stays stable for the session's duration
THEN the page set is frozen once per session and the page-cache keys
deduplicate exactly as before (a mid-session timezone change re-renders
the same frozen UTC page to a different key, costing only a redundant
sacct re-query — never a wrong verdict)
