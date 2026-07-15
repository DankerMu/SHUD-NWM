# Issue #1069 task 4.5 live compression evidence

This directory preserves the immutable node-27 receipts for the controlled
initial TimescaleDB compression run on 2026-07-15. The run applied migration
`000047` twice successfully, proved byte-identical D3 catalog state with no
compression policy, and invoked the runner once in dry-run mode and exactly
once with `--enforce` under bound 1 and a 900-second external timeout.

The selected terminal hydro chunk
`_timescaledb_internal._hyper_3_7_chunk` covers
`2026-05-28T00:00:00Z` through `2026-06-04T00:00:00Z`. Its immediate
measurement fell from 4,115,734,528 to 134,119,424 bytes. A later post
snapshot measured the compressed sibling at 134,127,616 bytes; the 8 KiB
FSM/VM measurement-time drift is within the verifier's 1 MiB bound. Combined
`hypertable_size(regclass)` for the hydro and met tables fell from
266,599,981,056 to 262,618,431,488 bytes, while compressed-chunk count rose
from 0 to 1. The met table received its D3 settings but remained uncompressed,
as required by the exact bound-1 authorization.

Production curve and MVT result hashes were byte-identical before/after. The
curve median changed from 0.475 ms to 0.413 ms and p95 from 0.860 ms to
0.432 ms. The MVT median changed from 4.614 ms to 5.044 ms and p95 from
4.628 ms to 5.053 ms. Both remained warm-cache and all seven after plans for
each query contained a `DecompressChunk` bound to the selected relation.

The autopipe timer was restored to its original enabled/active state while its
pre-existing failed service remained `MainPID=0`. The compression timer is
installed and enabled but intentionally inactive; its service has zero
activations. No retention, drill, decompression, role mutation, or node-22
operation was performed.

## Receipts

- `dry-run-live-20260715T090943Z.json` —
  `78fde19433177fd53435c1c9f09a24a509b50e7ebd0cd63ce102f4413e829c3e`
- `enforce-live-20260715T091543Z.json` —
  `0d7ca6184d0c636cb88670c24223c44bc1a9e8eacacf6029401aaff39ed3a891`
- `terminal-live-20260715T092119Z.json` —
  `c295329e3342c8f91aa4856b98f78230b26e8fe8a0edc5ad59003360ccc05339`
  with verdict `PASS_TASK_4_5`.

The terminal receipt references the node-27-local schema forensic dump,
catalog, size, benchmark, and cleanup artifacts by absolute path, byte count,
and SHA-256. Those generated artifacts and all credentials remain uncommitted.
