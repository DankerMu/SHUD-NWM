## ADDED Requirements

### Requirement: The job-id scope census SHALL refuse an unresolvable `--output` with its own typed code before the census runs

When canonicalising the receipt path or the verified journal root raises — an `OSError` such as `FileNotFoundError` for a relative `--output` whose working directory has been removed, or a `ValueError` for a value carrying an embedded NUL — the census command SHALL fail with the typed code `CENSUS_OUTPUT_UNRESOLVABLE` before any census work, on both the click and the argparse entrypoint: exit status 1, empty stdout, and exactly one `<code>: <message>` line on stderr with no traceback. The code SHALL be distinct from `CENSUS_OUTPUT_UNWRITABLE`, which denotes a failure after the receipt reached stdout, and SHALL be listed in the command's help beside the other output codes. The refusal SHALL write nothing under the journal root.

#### Scenario: A relative output under a deleted working directory is a typed refusal

- **GIVEN** a verified journal root and a process whose working directory has been removed
- **WHEN** an operator runs the census with `--output receipt.json` through either entrypoint
- **THEN** the command exits 1 with empty stdout, stderr is exactly `CENSUS_OUTPUT_UNRESOLVABLE: <message>`, no traceback is printed, and the journal root is byte-identical
