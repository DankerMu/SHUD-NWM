# Spec Delta: pipeline-job-persistence

## ADDED Requirements

### Requirement: A read-only census SHALL report job-id scope divergence on every surface the reconcile scan and the canonical lookup read, using the write gate's own predicate

The file journal SHALL offer a read-only census command that classifies every
persisted pipeline-job row by whether its job identifier's derivable
`(source, cycle)` contradicts the row's own `(source_id, cycle_time)`. The
classification SHALL be made by invoking the same write-boundary gate that
raises `file_journal_job_id_scope_mismatch`, once per row, and reading its
verdict — the census SHALL NOT carry a second comparison of the identifier
against the row, so that a row the census reports as legal is one the gate
accepts and a row the gate rejects is one the census counts. The census SHALL
read the flat direct directory, the by-cycle direct partition, the journal
replay (latest views and journal segments), the reconcile-inventory anchors,
and the legacy active-reconcile directory when present, reporting an absent
directory as absent rather than omitting it; it SHALL count each divergent
job identifier once across surfaces and, for each, report on which
row-bearing surfaces it appears (anchors are not rows and are reported on
their own surface, an anchor of a divergent row being itself divergent since
it carries the row's own pair), whether an anchor exists for it and whether a
flat direct file exists for it, and SHALL name the combination "anchor present and flat direct
missing" as the reconcile-abort trigger, because that is the shape whose
repair inside the reconcile scan trips the gate and stops the scan. The
census SHALL write nothing under the journal root — it SHALL enumerate
anchors through the no-follow directory listing and the pure anchor
validator, SHALL NOT enter the reconcile scan, the inventory migration, the
direct-file restore or the anchor prune, SHALL leave temporary residue in the
inventory in place and report it — and SHALL refuse to write its own receipt
to a location inside the root. It SHALL construct the repository through the
journal-root verification seam, so an invalid root is refused typed before
any read. Any reader fault other than a scope mismatch — including an inventory
entry that is neither a well-formed anchor name nor temporary residue — SHALL
fail the census loud with its existing reason, never be skipped. The census
SHALL exit zero when no divergent row exists, with a distinct non-zero code
when one or more exist, and with a third, distinct code on a typed failure
(printing the error's code or reason without a traceback), and SHALL emit one JSON
receipt carrying the configured and verified root, the time, the per-surface
counts, the divergent rows, the trigger count and its own `exit_code`. The
receipt SHALL be emitted before any optional receipt file is written, and a
typed failure raised after the receipt has been emitted (an unwritable receipt
path) SHALL take exit-code precedence over the divergence verdict — the
process exits with the typed-failure code while the emitted receipt's
`exit_code` still carries the verdict, which is why the receipt, not the shell
status alone, is the operator's authority. The gate itself, the
reconcile scan's exception propagation and every write lane SHALL be
unchanged by this requirement.

#### Scenario: A segment-resident divergent row with an anchor is counted as the abort trigger

- **WHEN** a journal holds a pipeline-job row whose job identifier resolves
  to a cycle different from the row's own, present only in journal segments
  and latest views, with an anchor for that identifier in the
  reconcile-inventory directory and no flat direct file for it
- **THEN** the census counts that identifier once, attributes it to the
  journal replay surface only, reports the anchor present and the flat direct
  file missing, flags it as a reconcile-abort trigger, and exits with the
  "divergent rows found" code

#### Scenario: A divergent row present on several surfaces counts once and is not a trigger

- **WHEN** the same divergent identifier has a flat direct file, a by-cycle
  direct file and journal records
- **THEN** the census reports it once, lists all three surfaces, and does not
  flag it as a reconcile-abort trigger

#### Scenario: An anchor with no row is still a divergent id and a trigger

- **WHEN** the reconcile-inventory directory holds an anchor whose job
  identifier contradicts the anchor's own pair, and no row for that
  identifier exists on any row-bearing surface
- **THEN** the census counts that identifier once with an empty list of
  row-bearing surfaces, reports the anchor present and the flat direct file
  missing, flags it as a reconcile-abort trigger, and counts it on the
  inventory surface

#### Scenario: A legal tree censuses to zero

- **WHEN** every row's identifier agrees with its own scope, or does not
  resolve to a pair
- **THEN** the census reports zero divergent rows on every surface and exits
  zero, while still reporting each surface's row and file counts and the
  presence of each directory

#### Scenario: The census writes nothing

- **WHEN** the census runs over a tree containing divergent rows, anchors and
  temporary residue in the inventory directory
- **THEN** every directory and file under the root is byte-for-byte and
  entry-for-entry identical afterwards, the residue is reported and still
  present, and no reconcile lock, migration, restore or prune path was
  entered

#### Scenario: The census uses the gate's predicate and nothing else

- **WHEN** the identifier-scope derivation the gate relies on is made to
  return "no pair" for every identifier
- **THEN** the census over a tree that previously reported divergent rows
  reports zero, because it has no comparison of its own

#### Scenario: An invalid root or an in-root receipt path is refused typed

- **WHEN** the census is pointed at a root reached through a symlinked
  ancestor, or asked to write its receipt to a path inside the root
- **THEN** it exits with the error code, prints `<code>: <message>` without a
  traceback, and writes nothing
