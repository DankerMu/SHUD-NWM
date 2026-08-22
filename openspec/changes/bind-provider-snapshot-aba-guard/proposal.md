# Bind the provider-snapshot ABA guard to a real test oracle

## Why

`tests/test_scheduler_file_provider_refresh.py::test_provider_snapshot_rejects_replacement_between_metadata_and_read`
is deterministically red on node-27 (Linux/ext4) and green on macOS (APFS).
Issue #1717 established the root cause and it is a **test defect, not a
production defect**.

`read_provider_snapshot` (`packages/common/provider_atomic.py:116-145`) makes
three filesystem reads through the module-level
`read_bytes_limited_no_follow` name:

1. inside `capture_provider_preimage(before)` at `:99`
2. the payload read at `:135`
3. inside `capture_provider_preimage(after)` at `:99`

The test's monkeypatch fires its replacement on the **first** call, i.e. inside
the `before` capture. `before.sha256` therefore already hashes `generation-b`,
and the `:142` content-hash comparison is trivially equal. The test never
constructs the divergence it claims to assert.

macOS goes green only because APFS records nanosecond `mtime_ns`, so the
`before != after` disjunct fires on the timestamp field. node-27's ext4 has a
4 ms timestamp tick (`CONFIG_HZ=250`; measured in #1717: 73 distinct mtimes
over 2000 writes, minimum tick ~4000062 ns), so both writes land inside one
tick, `before == after`, and nothing raises.

Consequences:

- node-27's backend pytest oracle carries a permanent known-red, which is what
  this change removes.
- The macOS green is worthless as evidence: deleting the `:142` content-hash
  comparison outright leaves the test green on macOS.
- The `provider_preimage_changed` guard's "replaced during the read" branch
  therefore has **no real coverage on either platform**.

## What Changes

Test-only. `packages/common/provider_atomic.py` is **not** modified — the
production guard is already correct, and touching it would destroy the
oracle-integrity story of this change.

- Rewrite the existing test as a deterministic **ABA** scenario: replace the
  bytes before the `:135` payload read, then restore the original bytes *and*
  the original `mtime_ns` before the `after` capture. `before == after` holds
  exactly, so the content-hash disjunct is the only thing that can raise.
- Add a second, smaller test for the replaced-and-left-replaced case, with a
  different-length replacement that is not restored. It does **not** isolate a
  disjunct — with the replacement left in place both `before != after` and the
  content-digest comparison fire — but its divergence rides on `size` rather
  than on timestamp granularity, so it is deterministic on both platforms.
  Isolating `before != after` on its own is tracked separately in #1733.
- Both tests assert the observed call count so a future refactor of the read
  sequence fails loudly instead of degrading back into a vacuous green.

## Impact

- Affected specs: `scheduler-registry-refresh` (one ADDED requirement pinning
  the snapshot-read guard contract that was previously implicit)
- Affected code: `tests/test_scheduler_file_provider_refresh.py` only
- Not affected: `packages/common/provider_atomic.py`, any production path, any
  other test
