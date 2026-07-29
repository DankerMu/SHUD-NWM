# strict-warm-start — delta for scheduler-completion-verdict-absence-tolerance

## ADDED Requirements

### Requirement: Terminal init-state comparison SHALL distinguish absence from conflict via a single shared helper

The scheduler SHALL evaluate a terminal decision's recorded init-state identity against the strict warm-start resolution through one shared helper returning exactly one of `match`, `absent`, `conflict`. Comparison SHALL be per-present-field: `absent` means the terminal evidence carries no init-state identity fields at all; when `init_state_id` is present, every additionally present field (`checksum`, `uri`, `valid_time`; redaction placeholders skipped as today) SHALL agree for `match`; any present field in disagreement SHALL classify as `conflict`. A record carrying only `init_state_id` SHALL retain today's match semantics unchanged; a record carrying other identity fields without `init_state_id` SHALL classify as `conflict`. The helper SHALL be a pure field comparison: the candidate path's existing special branches — the `candidate_state` terminal-source branch and the `COLD_START_QUARANTINED` escape — SHALL remain candidate-side short-circuits ahead of the helper and SHALL NOT enter the verdict path; when the strict resolution carries no `candidate_state`, the verdict path SHALL bypass the helper and keep today's gap behavior. Both paths SHALL consume this helper for the init-identity field comparison only; the candidate path's remaining admission segments are out of scope.

#### Scenario: Absence is distinguished from conflict

- **WHEN** a terminal-success row carries no init-state identity fields
- **THEN** the helper returns `absent`, not `conflict`

#### Scenario: Legacy id-only records keep matching

- **WHEN** a terminal row carries only `init_state_id` and it equals the strict resolution's state id
- **THEN** the helper returns `match`, byte-identical in effect to today's behavior

#### Scenario: Any present-field disagreement is conflict

- **WHEN** a terminal row carries `init_state_id` plus at least one further identity field and any present field disagrees with the strict resolution
- **THEN** the helper returns `conflict`

#### Scenario: Candidate-path special branches stay candidate-side

- **WHEN** the candidate ladder evaluates a terminal whose evidence takes the `candidate_state` terminal-source branch or the `COLD_START_QUARANTINED` escape
- **THEN** the candidate-side wrapper short-circuits to match ahead of the helper, the emitted candidate decision shapes are unchanged from today, and the cycle-completion verdict path never inherits these escapes — it classifies the same shapes through the plain helper (`absent`), so their completion still requires proven successor continuity (the design's named cold-seed residual risk)

#### Scenario: Strict resolution without candidate_state keeps gap

- **WHEN** the strict warm-start resolution is ready but carries no `candidate_state` (cold-start generation shapes)
- **THEN** the verdict path bypasses the helper and the cycle verdict remains `gap` as today
