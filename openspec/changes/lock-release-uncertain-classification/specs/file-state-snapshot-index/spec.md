# Delta: file-state-snapshot-index

## ADDED Requirements

### Requirement: Provider lock-release failure after the destination commit MUST be classified as commit-uncertain

A provider destination-lock release failure SHALL be raised as a classified
error carrying a phase that identifies writes inside the lock scope as
already completed (release-uncertain semantics), never as an unclassified
OS-level exception, and a release failure SHALL NOT mask an exception
already propagating from the lock body. The state-index copyback merge's
callers SHALL treat that classification as committed-or-uncertain, never as
a provable refusal: the operator replay tool SHALL report it through its
existing commit-uncertain path (non-refusal exit code, merge summary on
stdout, receipt recording the uncertain commit state and the release-failure
reason), and the natural orchestration copyback path SHALL surface it as a
structured copyback error whose code is distinct from the fail-closed
merge-failure code, reaching the copyback pipeline event with that distinct
code rather than escaping as a bare exception with no event. Sibling users
of the same provider lock keep their existing exception contracts, with the
release failure now arriving classified instead of bare. This requirement
governs release-period error classification only: lock acquisition
semantics, blocking behavior, merge collision semantics, and
compare-and-swap preimage semantics remain unchanged (the byte-identical
locking clause of the copyback-scope requirement is to be read as covering
acquisition and CAS preimage semantics, which this requirement does not
touch), and a release failure never leaks the lock or parent file
descriptors — a subsequent same-process lock acquisition on the same path
succeeds.

#### Scenario: merge caller can prove the commit happened despite the release failure

- **WHEN** the destination compare-and-swap has published the merged index
  and the subsequent lock release fails with an OS-level error
- **THEN** the merge raises a classified provider error with
  release-uncertain semantics and the destination index bytes are the merged
  content, so a caller can assert the commit as a fact rather than infer
  from the exception type

#### Scenario: replay reports commit-uncertain, not a refusal and not a bare crash

- **WHEN** the operator replay tool runs enforce and the merge fails only in
  the lock-release period after the commit
- **THEN** the tool exits with its committed/uncertain exit code and status,
  emits the known merge summary on stdout, writes the receipt with the
  uncertain commit state naming the release-failure reason, and never exits
  with an unclassified traceback, an empty stdout, or a refusal status

#### Scenario: natural copyback path emits an event with a distinct code

- **WHEN** the orchestration copyback stage hits the same release-period
  failure
- **THEN** the copyback raises a structured error whose code differs from
  the fail-closed merge-failure code and the stage writes the copyback
  pipeline event carrying that code, instead of a bare exception with no
  event

#### Scenario: release failure never masks the in-flight body error

- **WHEN** the lock body raises a pre-commit classified error and the lock
  release also fails during unwinding
- **THEN** the propagated exception is the body's pre-commit error, with the
  release failure suppressed rather than replacing it

## MODIFIED Requirements

### Requirement: A receipted idempotent copyback replay SHALL exist for failed state-index copybacks

An operator-invoked replay tool SHALL re-run the state-index copyback for an explicit run-id set or for the runs of one or more explicit cycles, resolved from the source index by matching each entry's flat optional `cycle_id` field after normalizing the requested cycle identifier to the production lowercase-source form. It SHALL expose exactly two object-store roots (private reference and shared destination, defaulting to the production environment variables) with the index paths derived from them, and SHALL refuse equal or overlapping roots. It SHALL default to dry-run — a read-only preview that does not invoke the merge, changes no index content, and copies no objects — and require an explicit enforce flag to invoke the real merge code path used by production copyback. An empty run-id resolution SHALL exit non-zero with a structured reason and SHALL NOT invoke the merge. An enforce run SHALL refuse to proceed — exiting non-zero before any merge invocation, index write, or object copy — when the derived destination index file does not exist, unless bootstrap is explicitly allowed by a dedicated flag. Enforce runs SHALL be idempotent (a repeated enforce run publishes no new entries and copies no objects) and SHALL write a JSON receipt (schema-versioned, recording mode, resolved run ids, entry counts before and after, and per-checkpoint outcomes) under the receipt root named by its environment variable. Refusal semantics SHALL be limited to failures that provably left the destination index uncommitted: a merge-raised error may be reported as a refusal only when its reason is on an explicit pre-commit allowlist (preimage-changed, validation, and collision classes whose raise point precedes the destination compare-and-swap); any merge-raised error not on that allowlist SHALL be classified as commit-uncertain and SHALL be reported with a distinct non-refusal reason, so that unknown future failure modes fail safe as uncertain rather than as refusals. Every committed or commit-uncertain outcome — including a post-merge destination read-back failure or a receipt failure — SHALL exit non-zero with a distinct post-merge failure reason that does not claim refusal, SHALL run the post-merge evidence chain (destination read-back, entry-preservation verification, and receipt write) as far as the failure allows, and SHALL emit the merge summary, as far as known, on stdout. An enforce run SHALL verify after the merge that the published destination index contains every entry identity the pre-merge destination read observed, and SHALL report a loss as a distinct post-merge failure rather than success; when a loss verdict and a receipt failure occur in the same run, the loss reason SHALL take precedence in the reported failure with the receipt failure recorded in its details. The tool SHALL NOT touch the orchestration journal, the registry, or canonical-readiness providers.

#### Scenario: Backlogged entries are recovered idempotently

- **WHEN** the replay tool is enforced for a cycle whose earlier copyback failed closed
- **THEN** the missing entries enter the destination index with their objects copied, a receipt records the before/after counts, and a second enforce run reports zero new entries and copies no objects

#### Scenario: Dry-run changes nothing

- **WHEN** the replay tool runs without the enforce flag
- **THEN** no index content change and no object copy occurs, and the receipt/preview reports the resolved run ids and would-be new entry count

#### Scenario: Empty resolution fails closed

- **WHEN** the requested cycles or run ids resolve to no source-index entries
- **THEN** the tool exits non-zero with a structured reason and the destination index is not written

#### Scenario: Missing destination index refuses enforce

- **WHEN** the replay tool is enforced against a destination root whose derived index file does not exist and bootstrap has not been explicitly allowed
- **THEN** the tool exits non-zero with a structured reason before any merge invocation, and no index or object is written under the destination root

#### Scenario: Receipt failure after a successful merge is reported distinctly

- **WHEN** the merge succeeds but the receipt cannot be written
- **THEN** the tool exits non-zero with a post-merge failure reason that does not claim refusal and the merge summary is emitted on stdout

#### Scenario: Post-merge read-back failure is reported distinctly

- **WHEN** the merge succeeds but the post-merge destination read-back fails
- **THEN** the tool exits non-zero with a post-merge failure reason that does not claim refusal, and the merge summary as far as known is emitted on stdout

#### Scenario: Destination entries lost across the merge are reported as failure

- **WHEN** the destination index observed before the merge vanishes or loses entries before the merge commits, so the published index no longer contains every previously observed entry identity
- **THEN** the enforce run exits non-zero with a distinct post-merge failure reason instead of reporting success

#### Scenario: Untyped merge exceptions are commit-uncertain

- **WHEN** the merge call raises an exception that is not one of the known typed error classes carrying a reason, such as a bare OSError raised from inside the merge internals without a classifying wrapper (lock-teardown failures are no longer an example: they now arrive typed with release-uncertain semantics and take the commit-uncertain path with a real reason)
- **THEN** the tool classifies the outcome as commit-uncertain, runs the post-merge evidence chain, writes the receipt, and exits with the distinct non-refusal reason carrying a synthetic error identifier instead of crashing with an unclassified traceback

#### Scenario: Commit-uncertain merge failures do not claim refusal

- **WHEN** the merge raises an error whose reason is not on the pre-commit allowlist, such as a durable-replace or post-read uncertainty where the destination index may already hold the new content
- **THEN** the tool exits non-zero with a distinct non-refusal reason, runs the post-merge evidence chain as far as the failure allows, and does not report the run as refused

#### Scenario: Loss verdict outranks receipt failure

- **WHEN** an enforce run detects lost destination entries and the receipt also cannot be written
- **THEN** the reported failure reason is the entry-loss reason, with the receipt failure recorded in its details
