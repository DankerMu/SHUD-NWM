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
