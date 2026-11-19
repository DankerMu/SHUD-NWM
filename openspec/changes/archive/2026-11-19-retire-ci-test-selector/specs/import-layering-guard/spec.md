## REMOVED Requirements

### Requirement: A pytest AST guard SHALL forbid upward imports

**Reason**: Replaced by "A pytest AST guard SHALL fail on upward imports": the selector scenarios of this requirement are cut (#3219), and OpenSpec refuses a MODIFIED block that drops a scenario, so the surviving text moves to a new header.
**Migration**: See the ADDED requirement "A pytest AST guard SHALL fail on upward imports"; apart from the selector cuts its body and scenarios are unchanged.

## ADDED Requirements

### Requirement: A pytest AST guard SHALL fail on upward imports
`tests/test_import_layering_guard.py` SHALL parse every `.py` under `packages/` (scan roots are the repository's `apps packages services workers scripts tests` directories only, never `.venv`, worktrees or `openspec/`) and fail on any import whose top-level package is `workers`, `services`, `apps` or `scripts`, and every `.py` under `workers/` importing `services` or `apps`. `tests/import_layering_allowlist.json` is a list of `{"file": <repo-relative path>, "module": <full dotted module>}` entries, deduplicated per pair, and is the only tolerated exception; the guard SHALL fail when an entry's import is absent from that file's AST (stale entry). The initial allowlist lists the current upward imports; after the expand step it SHALL be `[]` (shim re-exports are downward imports and never need an entry).

#### Scenario: New upward import fails the guard
- **WHEN** a file under `packages/` gains `from workers.x import y` not present in the allowlist
- **THEN** the guard fails and names the file and the import

#### Scenario: Stale allowlist entry fails the guard
- **WHEN** the allowlist names a file and module that the file no longer imports
- **THEN** the guard fails and names the stale entry

#### Scenario: Downward import needs no entry
- **WHEN** `workers/data_adapters/gfs_adapter.py` imports `packages.common.contracts.region`
- **THEN** the guard reports nothing
