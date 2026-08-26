## 1. Fixture and invariant

- [x] 1.1 Record the operation-worktree invariant, path-byte risk, fallback compatibility, merge behavior, and explicit non-goals.

## 2. Implementation

- [x] 2.1 Resolve the Git top level from raw tool-call `cwd` and bind config/Git/file/MERGE_HEAD/diagnostics to it.
- [x] 2.2 Preserve path-valued Git output as bytes, removing exactly one protocol LF before `os.fsdecode`.

## 3. Regression evidence

- [x] 3.1 Cover nested linked-worktree exclusion/block diagnostics and absent/non-Git fallback.
- [x] 3.2 Cover linked `commit -a`, same/split/fallback merge roots, and inherited/newly-authored attribution.
- [x] 3.3 Cover trailing-space, CR, and LF-suffix worktree identities and exact diagnostics.
- [x] 3.4 Run the 44-scenario hook fixture, strict OpenSpec validation, `git diff --check`, and confirm the child diff contains no #1571/#1619 paths.
