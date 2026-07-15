# Issue #1069 controlled compression receipts

This directory preserves the immutable node-27 receipts for the controlled
initial TimescaleDB compression work on 2026-07-15. The first run applied
migration `000047` twice successfully, proved byte-identical D3 catalog state
with no compression policy, and invoked the runner once in dry-run mode and
exactly once with `--enforce` under bound 1 and a 900-second external timeout.

## Historical v1 run (rejected terminal evidence)

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

### Historical v1 receipts

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

The v1 dry-run/enforce receipts and successful database outcome remain
immutable. They are runner schema version `1.0` and do not contain a
runner-frozen Git SHA, so they do not close task 4.5 under the hardened
live-evidence v2 contract. They have not been relabeled or overwritten by the
later replay.

That separate authorization was granted on 2026-07-15 for one decompression
and one bound-1 recompression of only
`_timescaledb_internal._hyper_3_7_chunk`.

## Recorded v2 replay (terminal acceptance superseded)

The authorized replay ran from
`/home/nwm/NWM/.nhms-issue1069-live/replay-20260715T113531Z` at mutation SHA
`8e77db55e47034c426147b4627b7af2099145389`. It performed exactly one
decompression and one v2 `--enforce` recompression with `bound=1`, a
604800-second (7-day) compression lag, and the 900-second external timeout.
The separately authorized decompression target was
`_timescaledb_internal._hyper_3_7_chunk`, covering
`[2026-05-28T00:00:00Z, 2026-06-04T00:00:00Z)`. Its row count remained
5,738,400 before and after decompression, and the fresh dry-run and both
selector snapshots reselected that exact target before enforce.

The direct `hypertable_size(regclass)` and compressed-chunk-count evidence is:

| Hypertable | Before bytes | After bytes | Compressed chunks after |
| --- | ---: | ---: | ---: |
| `hydro.river_timeseries` | 180,168,245,248 | 177,214,144,512 | 1 |
| `met.forcing_station_timeseries` | 85,404,286,976 | 85,404,286,976 | 0 |
| **Combined** | **265,572,532,224** | **262,618,431,488** | **1** |

The combined size strictly decreased by 2,954,100,736 bytes and the total
compressed-chunk count is 1 (> 0). The selected chunk itself decreased from
3,088,285,696 to 134,119,424 bytes.

Production curve results were identical before/after; median changed from
0.435 ms to 0.432 ms and p95 from 0.499 ms to 0.434 ms. Production MVT bytes
and hash were identical before/after; median changed from 4.583 ms to 4.951 ms
and p95 from 4.618 ms to 4.972 ms. Both queries passed the pinned thresholds,
and all seven after-phase plans for each query bound the selected
`DecompressChunk`.

Cleanup restored the autopipe timer to enabled/active. The compression timer
is enabled/inactive, its service is inactive with zero activations, and both
installed units are byte-identical to the repository. Retention, node-22,
drill, and role mutation remained out of scope and false. The terminal
truthfully records `decompress_run=true` for the separately authorized
recovery.

### Recorded v2 receipts

- `dry-run-replay-20260715T114310Z.json` —
  `5804660072c640634de6200c42b9cecd46308ca66c9ae85c20adc7b73e64ed5b`
- `enforce-replay-20260715T114420Z.json` —
  `634205e8f367bae88bd72cd476046e1bef3aa86bf43afe6bbca055d60a5c573c`
- `terminal-replay-20260715T114625Z.json` —
  `f4b1cbf9a0a8f60a30ddb8b4787584542aabeec35799fa7a9dd1de7242deff65`

The then-current independent verifier generated the terminal receipt at
`2026-07-15T11:46:25.062814Z` with verdict `PASS_TASK_4_5`. The replay's
database outcome and three committed receipts remain immutable historical
facts, but full cross-review subsequently expanded the evidence fixture and
found reusable false-PASS paths. The terminal verdict is therefore no longer
task-4.5 acceptance evidence; it is not edited or relabeled.

The strengthened contract requires evidence that the recorded replay did not
persist: ordered immutable invocation ledgers for both migration applies,
decompression, dry-run, and enforce; a dump-bound `pg_restore --list` artifact
and exact pre-migration catalog shape; timestamped pre/post/catalog snapshot
identities; prior-to-final systemd state, installed/repository unit bytes,
resolved `ExecStart`, activation-window journals; production-owner request
identity inside the selected chunk; independent session-identity samples and
per-statement/phase timeout records. These are not derivable retroactively
from asserted scalar fields or from the committed terminal envelope.

Current unit files, source blobs, dump readability, catalog state, and
production queries can be recaptured read-only. Migration execution order,
the original pre-state/activation window, the before-compression query plans,
and the exact decompression/recompression invocations require a newly
authorized controlled replay. No new node-27 mutation was performed during
the invariant-closure fix. Task 4.5 remains open pending that fresh live
evidence; retention, node-22, drill, and role mutation remain out of scope.
