## ADDED Requirements

### Requirement: The safe-filesystem primitives SHALL stay within the large-file guard without changing their import surface

`packages/common/safe_fs.py` and every module split from it SHALL each be at or under the large-file guard's 1000-line
limit without a guard exclusion. Splitting SHALL NOT remove a name from `packages.common.safe_fs` unless the change retires that name
explicitly. `SafeFilesystemError` SHALL be a single class object
regardless of import path. A helper that is patched as a `safe_fs` module attribute SHALL stay in `safe_fs.py`
together with all of its in-module callers, so that the patch keeps governing them. Split-out leaf modules SHALL NOT
import `safe_fs`.

#### Scenario: Importers after the split
- **WHEN** any existing importer imports `SafeFilesystemError`, `_expand_path` or another existing name from
  `packages.common.safe_fs`
- **THEN** the import succeeds
- **AND** `packages.common.safe_fs.SafeFilesystemError is packages.common.safe_fs_paths.SafeFilesystemError`

#### Scenario: A refusal raised by a moved helper
- **WHEN** a moved path helper refuses a path, for example a `..` component or an undeterminable home directory
- **THEN** the caller catches it with `except packages.common.safe_fs.SafeFilesystemError` and its `kind` is unchanged

#### Scenario: A patched identity check
- **WHEN** a test patches `packages.common.safe_fs._verify_fd_matches_path` and calls a write-side primitive
- **THEN** the primitive observes the patched function, as before the split
