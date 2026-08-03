# real-integration-test-matrix — delta for portable-hermetic-test-oracles (#1274)

## ADDED Requirements

### Requirement: Hermetic test oracles express their intent platform-portably

Hermetic tests SHALL express the same oracle on every platform the
suite supports (Linux CI and macOS development machines) — this
binds tests that execute embedded shell snippets, construct
filesystem fixtures, or trigger interpreter-version-sensitive
behavior: an embedded
snippet using a tool dialect unavailable on the running platform is
executed through a probed, pinned dialect substitution of exactly the
affected tool invocations — never by skipping the test and never by
weakening what the guard's control flow judges — while any
doc-equality assertion keeps comparing the canonical published
snippet text; a fixture path SHALL NOT depend on platform path
topology (such as a symlinked system tempdir) to reach the gate it
asserts, and where two refusal gates could answer, each gate gets its
own fixture row; a test that needs an interpreter-triggered failure
(such as `RecursionError`) SHALL pin inputs measured to trigger it
deterministically on every supported interpreter version rather than
on one version's internal limits. Green-for-the-wrong-reason is
treated as red: assertions name the specific refusal branch they
exercise, so a platform that diverts the control flow into a
different branch fails loudly instead of passing vacuously.

#### Scenario: A GNU-only snippet runs on a BSD-userland machine

- **WHEN** a hermetic test executes a guard snippet that invokes
  `stat -c` and the running platform's stat lacks `-c`
- **THEN** the test executes a copy with the pinned BSD-equivalent
  invocations substituted, the guard's control flow is otherwise
  byte-identical, the named refusal branch is still the one
  exercised, and the canonical GNU text remains what doc-equality
  assertions compare

#### Scenario: Each refusal gate has its own fixture row

- **WHEN** a fixture path could be refused by more than one gate
  (symlink-component refusal versus approved-root refusal)
- **THEN** the suite carries one row per gate — a resolved
  symlink-free path outside the approved root asserting the
  root-approval refusal, and an explicit symlink-bearing path
  asserting the symlink refusal — and neither row's outcome depends
  on the platform's tempdir topology

#### Scenario: Interpreter-version-sensitive triggers are pinned deterministically

- **WHEN** a test needs `RecursionError` from JSON parsing to
  exercise a never-raises error branch
- **THEN** the input depth is one measured to raise on every
  supported CPython version, the payload stays within the production
  size limit asserted in-test, and the adjacent non-recursive
  malformed shape (such as a top-level list) is pinned by its own
  independent case

#### Scenario: Wrong-branch passes are impossible

- **WHEN** a guard test's platform diverts execution into a
  different refusal branch than the one the test names
- **THEN** the test fails — its assertions bind the specific
  refusal message or error code of the named branch, not a generic
  refusal shape
