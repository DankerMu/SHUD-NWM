# Spec Delta: node27-raw-retention

## ADDED Requirements

### Requirement: A lane's traversal failure retires only that lane or source

The node-27 retention runner (`scripts/node27_raw_retention.py`) SHALL treat an
`OSError` raised while probing or listing a lane root (`raw/`, `canonical/`,
`<cache>/precip/`) or a per-source root beneath it (`raw/<source>`,
`canonical/<storage_source>`, `<cache>/precip/<storage_source>`) as a skip of
that lane or source only. The run SHALL continue to collect and delete targets
of every other lane and source on the same tick, SHALL write its summary to the
configured summary path, and SHALL return exit code `0` when no deletion failed.
A listing that raised SHALL NOT be reported as an empty directory.

#### Scenario: canonical root not traversable by the runner account

- **WHEN** `<object-store>/canonical` exists but the runner account lacks `x`
  on it, and aged targets exist in the raw and precip-cache lanes
- **THEN** the raw and precip-cache targets are deleted
- **AND** `skipped[]` carries one entry per configured source with reason
  `canonical_source_unsafe` and detail `path_unavailable`
- **AND** the summary file is written and `exit_code == 0`

#### Scenario: raw root readable but not traversable

- **WHEN** `<object-store>/raw` is listable (`r`) but not traversable (`x`)
- **THEN** `skipped[]` carries `raw_root_unsafe` / `path_unavailable` for the
  raw lane and no raw target is deleted
- **AND** the canonical and precip-cache lanes still delete their aged targets
  on the same tick

#### Scenario: one source root unreadable inside a traversable lane

- **WHEN** `<object-store>/canonical` is traversable and
  `<object-store>/canonical/IFS` is listable but not traversable
- **THEN** `skipped[]` carries `canonical_source_unsafe` / `path_unavailable`
  for `canonical/IFS` only
- **AND** the aged `canonical/gfs`, raw and precip-cache targets are deleted

#### Scenario: one raw source directory unreadable inside a traversable raw lane

- **WHEN** `<object-store>/raw` is traversable and `<object-store>/raw/gfs` is
  listable but not traversable
- **THEN** `skipped[]` carries `raw_source_unsafe` / `path_unavailable` with
  key `raw/gfs` and no raw target is deleted
- **AND** the aged canonical and precip-cache targets are deleted

#### Scenario: one precip-cache source directory unreadable inside a traversable cache lane

- **WHEN** `<cache>/precip` is traversable and `<cache>/precip/IFS` is mode `0o444`
- **THEN** `skipped[]` carries one entry with key `precip-cache/IFS`, reason
  `precip_cache_source_unsafe`, detail `path_unavailable` and `error_type`
  `PermissionError`
- **AND** aged cycles under `<cache>/precip/gfs` are still deleted

### Requirement: Unavailable-path skip entries carry the errno

Skip entries with detail `path_unavailable` SHALL carry `error` (the `OSError`
text) and `error_type` (the exception class name) in addition to `key`,
`reason`, `path` and `detail`. This applies to lane-root entries
(`<lane>_root_unsafe`) and per-source entries (`<lane>_source_unsafe`,
`raw_source_unsafe`). The `(reason, detail)` pair of existing entries MUST NOT
change.

#### Scenario: an unreadable object-store ancestor

- **WHEN** `<object-store>` itself is not traversable
- **THEN** both `raw_root_unsafe` and `canonical_root_unsafe` skip entries have
  detail `path_unavailable`, a non-empty `error` and `error_type` equal to
  `PermissionError`

#### Scenario: a stale lane-root handle

- **WHEN** resolving `<object-store>/raw` raises `ESTALE`
- **THEN** the raw lane skip is `raw_root_unsafe` / `path_unavailable` with
  `error` naming the stale handle and `error_type` equal to `OSError`

### Requirement: The documented operator check reports every unsafe skip

The documented operator check SHALL exit non-zero when `skipped[]` contains
any entry whose reason ends with `_unsafe` (the `jq` program in
`infra/env/node27-raw-retention.example`), and SHALL exit zero on a summary
the check otherwise accepts that carries no such entry. What the check
accepts in `failed[]` is not fixed by this requirement: this change leaves
the shipped clause as it found it, and #2100 owns that dimension.

#### Scenario: a per-source skip is not silently green

- **WHEN** the documented `jq` program runs on the summary of the
  canonical-root-untraversable tick
- **THEN** it exits `1`
- **AND** on a healthy production-shaped summary it exits `0`
