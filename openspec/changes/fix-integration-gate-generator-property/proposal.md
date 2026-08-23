# Express "the integration database name is not PID-derived" as a generator property, not a sampling accident

## Why

`tests/test_integration_gate.py:50` asserts:

```python
assert str(os.getpid()) not in first_name
```

against `first_name = conftest._integration_database_name()`, which is
`f"nhms_it_{uuid.uuid4().hex}"` (`tests/conftest.py:224-225`).

The **intent** is a property of the generator: the database name must not be
derived from the PID. The **implementation** is a property of one sample: does
this particular 32-char random hex string happen to contain the PID's decimal
digits as a substring. Every decimal digit is a valid hex digit, so a correct
generator fails this assertion at a rate set entirely by how many digits the PID
happens to have — the assertion's verdict is decided by the PID allocator, not
by the code under test.

Measured failure rates (issue #1747, 200k-3M samples per row): 1-digit PID
**0.857**, 2-digit **0.157**, 3-digit **0.0094**, 4-digit ~4-6e-4, 5-digit
~3e-5. On bare metal (4-5 digit PIDs) this is a rare ghost red; inside a PID
namespace — any `docker run` pytest, which is how node-27 regressions are run —
PIDs are 1-2 digits and the assertion is **~86% / ~16% red per round**, with no
information content when it fires.

Same defect class as the merged #1717: the assertion does not express the
property it claims, so its verdict is decided by something other than the
behavior under test.

## What Changes

- The probabilistic substring assertion at `tests/test_integration_gate.py:50`
  is removed from `test_integration_database_name_uses_high_entropy_uuid`.
- The intent it carried is re-expressed as a **deterministic generator-property
  test**: with `uuid4` stubbed to a fixed sentinel hex, `_integration_database_name()`
  must equal `"nhms_it_" + sentinel_hex` character for character. Exact equality
  proves the PID — and every other ambient input — contributes exactly nothing
  to the result, independent of how many digits the PID has.
- The surviving assertions in the original test (distinctness, the
  `nhms_it_[0-9a-f]{32}` shape, uuid-parseability) are unchanged.
- `tests/conftest.py::_integration_database_name` is **not** changed: the
  generator is already correct. No production file changes.

## Fixture level

`compact`. `design.md` is exempt at this level per the workflow's
issue-risk-contract; the risk-pack selections and the design decision (sentinel
stub over plain deletion) are recorded in `tasks.md` and in `## What Changes`
above.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

- `real-integration-test-matrix`: adds the rule that the integration lane's
  per-run database name is asserted as a property of its generator, not as a
  property of one draw from it.

## Impact

- `tests/test_integration_gate.py` only.
- Removes a ~86%-per-round false red from any containerized run of the test
  suite, which is how node-27 regressions are executed.

## Out of scope

- `tests/conftest.py::_integration_database_name` itself — the generator is
  correct and the issue explicitly places it out of scope.
- `#1671` (the `Unit Tests (full)` 45-minute timeout), tracked separately.
- Any repo-wide sweep for other probabilistic assertions. The issue records that
  this defective assertion has **no sibling copy**: the two other consumers of
  `_integration_database_name()` (`tests/conftest.py:144`, `:172`) only use the
  return value to create and drop a database.
