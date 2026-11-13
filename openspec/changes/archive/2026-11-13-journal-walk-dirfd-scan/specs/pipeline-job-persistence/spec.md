## ADDED Requirements

### Requirement: Journal directory scans SHALL stat entries relative to one no-follow directory descriptor
A non-strict file-journal directory scan SHALL open each visited directory once through the no-follow descriptor chain and SHALL stat every entry of that directory relative to the held descriptor without following symlinks, so the number of directory opens is bounded by the number of directories visited rather than by the number of entries. Error codes and evidence, disappearance handling under strict and non-strict walks, path-segment validation, entry and depth limits, sorted yield order and the strict-authority before/after directory checks SHALL be unchanged. Strict-disappearance scans SHALL keep their per-entry no-follow stat behavior unchanged.

#### Scenario: Directory opens do not grow with entry count
- **WHEN** a directory holding N regular journal files under a root nested D levels deep is scanned, for two different values of N
- **THEN** the count of `os.open` calls is the same bounded per-directory constant for both values of N

#### Scenario: A symlink entry is still refused
- **WHEN** a scanned directory contains a symlink whose name matches the scanned suffix
- **THEN** the scan raises `file_journal_unsafe_scanned_entry` and does not follow the link, for matching and non-matching names and for links to directories

#### Scenario: An entry vanishing mid-scan keeps today's outcome
- **WHEN** an entry listed by the scan is removed before it is stat'd
- **THEN** a strict-disappearance walk raises the same code it raises today and a non-strict walk skips the entry
