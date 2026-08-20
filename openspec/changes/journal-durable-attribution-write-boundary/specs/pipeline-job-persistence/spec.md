# pipeline-job-persistence Spec Delta

## ADDED Requirements

### Requirement: Display redaction placeholders SHALL NOT reach durable journal state through any write path

Object-URI display placeholders (`[object-uri]`, `[uri]`) are produced by public query projection so that callers never see raw object URIs, and a caller that round-trips a public row back into a write SHALL NOT launder those placeholders into durable state. The anti-laundering strip SHALL therefore be applied inside the single function through which journal records are constructed, rather than at individual write call sites, so that a write path added later inherits the guarantee instead of having to re-declare it. The strip SHALL act only on payloads supplied by a caller and SHALL NOT re-process the journal's own public rendering: the pipeline-event lane deliberately renders real paths and URIs into display placeholders as the durable public value, so that lane is stripped at its caller boundary — before the rendering — and the record constructor SHALL leave its already-rendered output intact. Consequently the event lane's structural guarantee is the weaker one, and an event write path that bypasses the caller boundary would carry no strip; that is a declared limit, not a covered case. A placeholder SHALL never be persisted as the literal placeholder text. Where the write path can decline — a guard that only overwrites when the caller supplied a value — a withheld value SHALL leave the persisted value untouched; where the write is unconditional, a withheld value SHALL resolve to the value already persisted on the row; and only where no value was previously persisted SHALL the durable result be `None`. The governing rule is single and uniform: **a withheld value never changes durable state**. The strip SHALL match a placeholder only as a whole value, never as a substring, so that placeholder text embedded inside a longer message survives. The strip SHALL remain narrowly scoped to the object-URI placeholder set: `[local-path]` and `[redacted]` are deliberately persisted evidence for runtime-root and secret redaction and SHALL continue to be stored verbatim. Where a durable write is already governed by a stricter remedy — the accepted-submit master row's frozen identity evidence, which rejects a divergent write loudly — that remedy SHALL continue to take precedence; silent withholding applies to the non-frozen evidence fields. Because the strip is idempotent, applying it both inside the record constructor and at a pre-existing outer call site is permitted and SHALL NOT change semantics — this mirrors the existing placement of the sibling durable-error-message sanitizer.

#### Scenario: A cohort projection write cannot launder a placeholder

- **WHEN** the accepted-submit cohort terminal projection writes a row whose evidence field carries an object-URI placeholder, through either the batched projection path or the deferred single-row path
- **THEN** the durable journal record and the direct row file store `None` for that field — the row held no real value for it beforehand — and no literal placeholder text is persisted by either path. Where the row did already hold a real value, the withheld-value rule below governs and the real value survives instead, whether the write path declines the overwrite or resolves the withheld value against the row

#### Scenario: Deliberately persisted placeholders survive

- **WHEN** a durable payload carries `[local-path]` or `[redacted]`
- **THEN** those values are persisted verbatim, unchanged by the anti-laundering strip

#### Scenario: Stored literals are normalized only by ordinary writes, never by a sweep

- **WHEN** the journal already contains rows in which a literal placeholder was persisted before this change
- **THEN** no migration, backfill, or sweep rewrites them, and reading them is unchanged; a later ordinary write to such a row normalizes the stored literal to `None`, which is the intended remedy rather than a rewrite pass

#### Scenario: The journal's own public rendering is not stripped away

- **WHEN** the pipeline-event lane deliberately renders a real object URI into a display placeholder as the durable public value of an event record
- **THEN** that rendered placeholder is persisted intact, because the strip runs on the caller's payload upstream of the rendering and never re-processes the rendering's output

#### Scenario: Placeholder text inside a longer message survives

- **WHEN** a durable field's value merely contains placeholder text as a substring of a longer message
- **THEN** the value is stored verbatim, because the strip matches whole values only

### Requirement: A cohort master's explicit terminal mark SHALL keep its attribution while observational evidence keeps refreshing

`permanently_failed` is an externally applied terminal mark, not a value the cohort projection can derive, and when the projection encounters a master already carrying that mark it SHALL preserve both the mark and the error code and error message the row already carries, instead of overwriting them with the values derived by the current pass. A row whose status says "permanently failed" while its error code is rewritten on every reconcile pass is self-contradictory attribution and SHALL NOT be produced. Observational evidence about the master Slurm job — completion time, exit code, log location — SHALL continue to refresh under the mark, because refreshing evidence under a sticky status is the projection's intended behavior and does not contradict the mark. The stickiness SHALL be triggered only by a status that is both externally applied — not derivable by the projection, which yields only `succeeded`, `partially_failed`, and `failed` — and already protected from status overwrite today; `permanently_failed` is currently the only such status, and pinning a derived value on its first computation would disable the projection rather than protect it. Terminal statuses that are neither derived nor currently status-protected are a pre-existing gap that this requirement does not address. An evidence field SHALL be treated as supplied by the caller only when it carries a real value: a display placeholder is a withheld value, not an instruction to overwrite, and SHALL NOT displace a real value already persisted on the row. That rule SHALL hold on **every** durable write path that admits caller-supplied evidence — not only the cohort projection paths — because a placeholder that displaces a real value and is then withheld destroys evidence that survived before.

#### Scenario: Attribution survives a later reprojection

- **WHEN** a cohort master marked `permanently_failed` is reprojected by a later resume or reconcile pass that derives a different error code
- **THEN** the persisted status, error code, and error message are all unchanged

#### Scenario: Observational evidence still refreshes under the mark

- **WHEN** that same reprojection carries a real completion time, exit code, or log URI
- **THEN** those fields are updated on the durable row

#### Scenario: A withheld value does not displace a real one

- **WHEN** a reprojection carries an object-URI placeholder for a field whose durable row already holds a real object URI
- **THEN** the durable row still holds the real object URI afterwards — neither the placeholder nor `None` replaces it

#### Scenario: The deferred path protects a real value the same way

- **WHEN** a deferred cohort projection has already recorded a real log URI on a non-terminal row, and a later deferred pass for the same row carries an object-URI placeholder
- **THEN** the durable row still holds the real log URI, because the deferred path guards its evidence overwrite by the same rule as the batched path

#### Scenario: Derived terminal statuses are not made sticky

- **WHEN** a cohort master whose status is any of the three projection-derived terminal values is reprojected with a different derived error code
- **THEN** the error code is overwritten as before, because stickiness applies only to the externally applied mark

#### Scenario: Stickiness produces no empty write

- **WHEN** stickiness suppresses the only field that would otherwise have changed — a unit-constructed geometry, since a production row reaching this path always carries a changed per-task projection alongside
- **THEN** the projection detects no change and writes no record

### Requirement: A write path SHALL compare the same value it persists, so replaying an identical call converges

Several durable write paths decide their outcome by comparing the row they are about to write against the row already persisted, answering "already recorded" when the two agree. That comparison is only meaningful if the value compared is the value that will be persisted. Because caller-supplied evidence is normalized on its way to durable state, the normalization SHALL be applied before the comparison, not only before the write — otherwise an unchanged replay compares a raw caller value against a normalized durable value, the two can never agree, and the path reports a change on every call.

Replaying an identical call SHALL therefore converge: the second and later calls SHALL report that the state is already recorded, SHALL NOT append a further durable record, and SHALL NOT consume a further sequence number. A path that reports an applied change on every replay grows the journal without bound and drives it toward its segment and size limits. A path whose caller treats "already recorded" as success SHALL NOT instead report a conflict on replay, because a caller that reads a conflict as "not committed" will skip the follow-on work the first call earned.

#### Scenario: An identical replay carrying a withheld value converges

- **WHEN** a durable write path **that decides its outcome by comparison** is called twice with identical arguments in which an evidence field carries a display placeholder
- **THEN** the second call reports the state as already recorded, appends no further durable record, and consumes no further sequence number. A path that deliberately has no such comparison and appends on every call is outside this scenario

#### Scenario: A cancellation receipt replay stays committed

- **WHEN** a cancellation completion receipt carrying a withheld log location is delivered a second time for a row that already recorded it
- **THEN** the path answers "already recorded" rather than "conflict", so the caller still treats the cancellation as committed and does not drop its follow-on event
