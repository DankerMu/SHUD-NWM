# Issue #1069 controlled compression receipts

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
4.628 ms to 5.053 ms. Both remained warm-cache. The live command checked all
seven in-memory after plans, but persisted only one plan per query and omitted
the cold/warmup/activity records and several actual bind values. Those facts
are therefore operational observations, not sufficient terminal evidence.

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
The first terminal attempt was rejected during final cross-review and is not
committed as acceptance evidence. Three provenance gaps are confirmed:

- the mutation ran at `2f1fa6a52cce456c4d2ce3b9b263ec3e22ad4ddf`, while a
  later verifier-only head was written over the preflight artifact;
- the independent selector was executed again immediately before enforce, but
  no second complete timestamped snapshot was persisted;
- the benchmark artifact lacks complete bind, cold/warmup/activity and seven-
  plan records.

The dry-run/enforce receipts and successful database outcome remain immutable.
They are runner schema version `1.0` and do not contain a runner-frozen Git
SHA, so they do not close task 4.5 under the hardened live-evidence v2
contract. A new controlled evidence replay would require
separate human authorization for the exact decompression/recompression scope;
it must use the hardened verifier contract and may not relabel or overwrite
these historical receipts. Node-27-local generated artifacts and all
credentials remain uncommitted.

That separate authorization was granted on 2026-07-15 for one decompression
and one bound-1 recompression of only
`_timescaledb_internal._hyper_3_7_chunk`. Authorization is not completion:
until distinct recovery preflight/receipt artifacts prove the exact target,
mutation SHA, node/database, at least 300 GiB free space, compressed-to-
uncompressed transition, returned relation, zero exit and row parity—and the
fresh compression evidence reselects the same target—task 4.5 remains open.
The accepted v2 terminal will truthfully record `decompress_run=true`; it may
not preserve the historical “no decompression” flag after recovery occurs.
