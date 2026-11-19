## ADDED Requirements

### Requirement: Completion scoring SHALL decide through the candidate-state ladder without building per-event evidence

Cycle completion scoring SHALL obtain each in-scope model's decision from the same candidate-state decision ladder the
submitting path uses, so a cycle is scored `complete` or `gap` exactly as that ladder's terminal decisions dictate.
Because completion scoring neither reads nor publishes the per-event evidence listing, it SHALL request the decision
without that listing: the decision's evidence SHALL then omit `pipeline_events` altogether and SHALL be otherwise
identical. The submitting path SHALL keep requesting the full evidence.

#### Scenario: The decision is the same with and without the event listing

- **WHEN** the ladder is evaluated twice for the same candidate and the same candidate state, once with and once
  without the event evidence listing
- **THEN** both decisions have the same `action` and `reason`
- **AND** the evidence of the second equals the evidence of the first with the `pipeline_events` key removed

#### Scenario: Completion scoring does not sanitise events

- **WHEN** a cycle whose candidates carry pipeline events is scored for completion
- **THEN** no event payload is sanitised for evidence during that scoring
- **AND** the verdict equals the verdict the full-evidence decision would produce, including for a cycle that scores
  `complete` because a recalibrated model is scoped out by its lineage cutover

#### Scenario: Submission still publishes the event listing

- **WHEN** a candidate is evaluated on the submitting path
- **THEN** its decision evidence carries `pipeline_events` as before
