## ADDED Requirements

### Requirement: Scheduler journal retention and restore verify the journal root through the journal-root authority

The scheduler journal retention preflight (`config_from_env`) and the journal restore entry (`verify_and_restore`) SHALL decide whether the configured journal root is acceptable only through `verify_journal_root_authority`, the same check the DB-free scheduler and the other journal lanes use. They SHALL keep reporting a refusal in their existing `journal_root_*` blocker vocabulary: a missing root as `journal_root_unavailable`, a symlinked root as `journal_root_symlink`, a non-directory as `journal_root_not_directory`, a root below a symlinked ancestor as `journal_root_unsafe` (an ancestor that is a regular file or not searchable reports `journal_root_unavailable`), and a relative root as `journal_root_not_absolute`. A root whose `~` cannot be expanded (for example `~nosuchuser/...`) SHALL be refused as `journal_root_not_absolute` and SHALL NOT escape either entry as an uncaught exception. The other roots these entries validate (`evidence_root`, `allowed_root`, `archive_root`, and restore's `stage_root`) SHALL likewise report an unexpandable `~` as their own `*_not_absolute` blocker. This requirement is the recorded vocabulary-translation exception to "The DB-free scheduler SHALL verify its journal root as a chain of real directories…", whose lanes answer with one error code (`FILE_JOURNAL_INVALID_ROOT`). Retention and restore share that requirement's decision but keep their per-field receipt vocabulary, because their blocker strings are a published receipt contract.

#### Scenario: An unexpandable journal root is a typed retention blocker

- **WHEN** the retention CLI runs with valid allowed roots and `--journal-root ~nosuchuser_zz/journal`
- **THEN** it prints the `preflight_blocked` JSON with `preflight_blockers` containing `journal_root_not_absolute` and exits `2`
- **AND** no traceback is printed

#### Scenario: An unexpandable journal root is a typed restore refusal

- **WHEN** the `verify-restore` subcommand runs with `--journal-root ~nosuchuser_zz/journal`
- **THEN** it prints `{"reason": "journal_root_not_absolute", "status": "blocked", ...}` and exits `2`

#### Scenario: The existing blocker vocabulary is unchanged

- **WHEN** the configured journal root is missing, a symlink, a regular file, below a symlinked ancestor, or relative
- **THEN** retention and restore report `journal_root_unavailable`, `journal_root_symlink`, `journal_root_not_directory`, `journal_root_unsafe` and `journal_root_not_absolute` respectively, exactly as before this requirement

#### Scenario: The accept/reject decision belongs to the authority

- **WHEN** `verify_journal_root_authority` refuses a root that is otherwise a real directory
- **THEN** retention reports a `journal_root_*` blocker and does not proceed
