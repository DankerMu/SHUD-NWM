## 1. Fixture and invariant

- [x] 1.1 Record stable ownership, multi-owner completeness, runtime non-goals, and focused evidence.

## 2. Implementation

- [x] 2.1 Replace numeric citations with reason-to-one-or-more-function ownership.
- [x] 2.2 Preserve both copyback lock reasons and every owner represented by the old index.

## 3. Regression evidence

- [x] 3.1 Assert exact reason-key equality and non-empty owner tuples.
- [x] 3.2 Pin all seven known multi-owner reason sets.
- [x] 3.3 Assert each named owner function contains the corresponding reason literal.
- [x] 3.4 Run the focused replay suite, strict OpenSpec validation, markdown lint, `git diff --check`, and child path-isolation check.
