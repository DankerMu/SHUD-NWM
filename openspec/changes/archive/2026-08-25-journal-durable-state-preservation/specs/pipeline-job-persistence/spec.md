## ADDED Requirements

### Requirement: Preservation writes SHALL merge from durable truth and render only at public boundaries

A private repository lookup used as the base of a durable update SHALL return the exact durable row, not a public projection whose paths or object URIs have already been replaced by display placeholders. Public redaction SHALL remain at explicit query and return boundaries, so internal preservation logic never treats redacted display text as persisted truth and callers still never receive raw protected paths or URIs.

A hydro-run status update in a non-clearing state SHALL preserve the durable error code and message when the caller omits them. The existing clearing states (`pending`, `created`, `succeeded`, `complete`, `parsed`, and `published`) SHALL continue to clear an omitted error family. The public return value SHALL describe the same row that was written, with its protected values rendered through the public projection.

When the file-journal retry service marks an accepted-submit master permanently failed from a public job snapshot, an `error_message` identical to the freshly read current public message SHALL be treated as round-tripped display evidence, not as a new durable override. The typed transition SHALL receive no message override and SHALL preserve the current durable message. A non-empty source message that differs from the current public message SHALL remain an explicit new override and SHALL be persisted through the existing durable sanitizer. The service SHALL NOT attempt to reverse redaction or reconstruct hidden paths.

#### Scenario: A non-clearing hydro status update preserves a whole object URI

- **WHEN** a hydro run durably records `error_message="s3://nhms/logs/run.log"` and a later `staged`, `submitted`, `running`, or `failed` status update supplies no error arguments
- **THEN** the durable error message remains exactly `s3://nhms/logs/run.log`, while the returned public row contains only the existing redacted projection

#### Scenario: A non-clearing hydro status update preserves an embedded URI

- **WHEN** a hydro run durably records `error_message="SHUD aborted; see s3://nhms/logs/run.log for detail"` and a later non-clearing status update supplies no error arguments
- **THEN** the complete durable message remains byte-for-byte unchanged and no `[object-uri]` substring is written into it

#### Scenario: A successful hydro state still clears stale errors

- **WHEN** a hydro run with a durable error code and message is updated to `succeeded` without error arguments
- **THEN** both durable error fields are `None`, preserving the existing successful-state clearing contract

#### Scenario: A public retry snapshot cannot overwrite richer durable attribution

- **WHEN** an accepted-submit master has a durable message containing a real local path or object URI and permanent-failure handling receives the public projection of that same message
- **THEN** the durable message remains exact after the typed permanent-failure transition and is not replaced by `[local-path]` or `[object-uri]`

#### Scenario: A genuinely new permanent-failure message still overrides

- **WHEN** permanent-failure handling receives a non-empty source message different from the freshly read current public message
- **THEN** that new message is persisted through the existing durable sanitizer while status, error code, completion time, event details, and transition outcome semantics remain unchanged

## MODIFIED Requirements

### Requirement: A cohort master's explicit terminal mark SHALL keep its attribution while observational evidence keeps refreshing

`permanently_failed` and `cancelled` are externally assigned terminal truths, not values the cohort task projection can derive. When the projection encounters a master already carrying either status, it SHALL preserve that status instead of overwriting it with `succeeded`, `partially_failed`, or `failed`. For a `permanently_failed` row, the projection SHALL also preserve the error code and error message the row already carries; a row whose status says "permanently failed" while its attribution is rewritten on every reconcile pass is self-contradictory and SHALL NOT be produced. A `cancelled` row receives status stickiness only: its per-task projection and currently derived error family SHALL continue to update, because this requirement must not turn status preservation into a whole-row freeze or invent a new cancellation-attribution contract.

Observational evidence about the master Slurm job — completion time, exit code, log location — and `candidate_projections` SHALL continue to refresh under either sticky status. The projection-owned terminal domain SHALL remain exactly `succeeded`, `partially_failed`, and `failed`; pinning any of those values on its first computation would disable the projection rather than protect external truth. `submission_failed` and `reservation_lost` SHALL remain outside this projection through their existing submit-outcome, accounting-tuple, and reconcile-inventory gates: in particular a released reservation whose decision is valid only with `reservation_lost` SHALL NOT be rewritten through a `matched_bound` projection.

An evidence field SHALL be treated as supplied by the caller only when it carries a real value: a display placeholder is a withheld value, not an instruction to overwrite, and SHALL NOT displace a real value already persisted on the row. That rule SHALL hold on every durable write path that admits caller-supplied evidence — not only the cohort projection paths — because a placeholder that displaces a real value and is then withheld destroys evidence that survived before. A write path MAY be exempt only where the rule does not apply to it — where it never reads the persisted row, where its outcome comparison does not consider the field, or where the caller's value is by contract the authoritative one — and each such exemption SHALL be enumerated with its reason at the resolution helper, so that the exempt set is readable in one place rather than inferred from omissions.

#### Scenario: Attribution survives a later reprojection

- **WHEN** a cohort master marked `permanently_failed` is reprojected by a later resume or reconcile pass that derives a different error code
- **THEN** the persisted status, error code, and error message are all unchanged

#### Scenario: A cancelled master keeps cancellation while evidence refreshes

- **WHEN** a schema-valid accepted-submit master already persisted as `cancelled` is reprojected with complete task accounting
- **THEN** its durable status remains `cancelled`, while `candidate_projections`, completion time, exit code, log URI, and the currently derived error family update exactly as they do for the same unmarked projection

#### Scenario: Observational evidence still refreshes under the mark

- **WHEN** a reprojection of a `permanently_failed` master carries a real completion time, exit code, or log URI
- **THEN** those fields are updated on the durable row

#### Scenario: A withheld value does not displace a real one

- **WHEN** a reprojection carries an object-URI placeholder for a field whose durable row already holds a real object URI
- **THEN** the durable row still holds the real object URI afterwards — neither the placeholder nor `None` replaces it

#### Scenario: The deferred path protects a real value the same way

- **WHEN** a deferred cohort projection has already recorded a real log URI on a non-terminal row, and a later deferred pass for the same row carries an object-URI placeholder
- **THEN** the durable row still holds the real log URI, because the deferred path guards its evidence overwrite by the same rule as the batched path

#### Scenario: Derived terminal statuses are not made sticky

- **WHEN** a cohort master whose status is any of the three projection-derived terminal values is reprojected with a different task outcome and error code
- **THEN** its status and error family are overwritten as before, because those values remain owned by task projection

#### Scenario: Restart reconcile reports preserved cancellation truth

- **WHEN** complete restart accounting projects a supported historical accepted-submit master whose durable status is `cancelled`
- **THEN** the durable/public master and the operator-facing reconcile outcome both report `cancelled`, and no resubmission is triggered

#### Scenario: Projection-excluded terminal states keep their routing guards

- **WHEN** a current-contract master is `submission_failed` or `reservation_lost`
- **THEN** its existing submit-outcome/accounting and reconcile-inventory gates keep it out of complete task projection, so no `matched_bound` rewrite is attempted

#### Scenario: Stickiness produces no empty write

- **WHEN** stickiness suppresses the only field that would otherwise have changed — a unit-constructed geometry, since a production row reaching this path always carries a changed per-task projection alongside
- **THEN** the projection detects no change and writes no record
