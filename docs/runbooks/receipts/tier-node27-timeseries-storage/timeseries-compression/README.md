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
persist: one supervisor-owned append-only ledger for both migration applies,
decompression, dry-run, enforce, dump/container `pg_restore`, and benchmarks;
a dump-bound container `pg_restore --list` artifact
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

## Version-3 qualifying contract

The second invariant review confirmed that the first hardening pass still
allowed false PASS through global chronology reversal, output/input aliasing,
authored dump lists, incomplete invocation/service namespaces, unrelated Git
objects, plan text decoys, unbounded connection/result phases, v1-labeled v2
failures, secrets in otherwise benign text, contradictory compressed-relation
lists, stale terminal outputs and a self-asserted timeout.

The current verifier therefore accepts task 4.5 only from a version `3.0`
terminal with `qualifies_task_4_5=true`. It derives invocation identity and
chronology from the supervisor ledger rather than the legacy authored
invocation JSON. It retains the descriptor-bound PG15 dump-list proof, unique snapshot
IDs, one global chronology and the complete source manifest. The reviewed
repository is exactly `/home/nwm/NWM`, remote identity `DankerMu/SHUD-NWM`,
and the authorization-pinned remote-tracking SHA. Query plans use exact
structural relation fields; the seven-day curve uses half-open overlap
semantics. The recurring service remains the finite bounded wrapper `--enforce`
lane. The one-off supervisor is isolated in the separate no-timer replay
service, whose explicit reviewed paths, finite `TimeoutStartSec`, process-group
hard wall and single-use CAS finalizer in `ExecStopPost` cannot leak into the
timer lane. A qualifying replay records exactly one replay-service activation
while the recurring timer/service activation count stays zero.

The qualifying v3 producer additionally requires an external SHA-256 pin for
the immutable run plan, exact per-kind argv, and clean checkout/origin/SHA
lineage before the first spawn. Semantic outputs are in one producer
bijection: children own only files their argv writes; all catalog, selector,
size, preflight and cleanup facts are explicit supervisor capture steps at
their real state-machine positions. Each owner requires an absent destination
and every ledger association stores path/bytes/hash plus device/inode for
later identity revalidation. Raw checkpoints are supervisor-produced under
the same rule. The sole-DB-user
attestation is the explicitly selected operating model; it remains a stated
trust limit rather than an invented database audit.

The single replay activation is derived from canonical `systemctl show`: the
`Type=oneshot` unit is executing as `activating/start`, its `MainPID` equals the supervisor, its non-empty
`INVOCATION_ID` matches every ledger event, and its UTC/monotonic start stamps
are present. Cursor-bounded journal proves only that no second replay ID and no
recurring activation occurred; same-ID log noise is ignored. Real user-journal
records evaluate `_SYSTEMD_USER_UNIT` and `USER_UNIT` independently: either
exact governed value is classified, differing governed values conflict, and
`_SYSTEMD_USER_UNIT=init.scope` cannot hide a governed `USER_UNIT`.
`_SYSTEMD_UNIT=user@<uid>.service` is only manager context; exact
`_SYSTEMD_UNIT`/`UNIT` fallback is allowed only when both user-unit fields are
absent. This shape must be checked on node-27 before live start. It is never
injected as a scalar count. Git probes and all CAS publication locks share the
finite hard wall, including reserved finalizer budget. Provenance-bound finalizer
state and failure tombstones retain the mutation SHA even through
`ExecStopPost`, repeat, timeout, and publish-race handling.
Once closure/disjointness is proven and the old terminal identity is frozen,
failure to establish provenance replaces a prior PASS with a schema-valid v3
nonqualifying `provenance_state=unavailable` tombstone. It carries only the
safe stage/reason, expected old identity, and independently trusted verifier
SHA when one exists; it never fabricates run or mutation identity. A failure
before that safety boundary leaves the terminal and all adjacent state absent
or unchanged.

The recovery child is the committed bounded Python producer, not plain `psql`
output: its pinned argv names the exact target, mutation SHA and receipt path;
it verifies compressed pre-state/positive rows, performs one decompression,
reconciles relation/state/row parity, and atomically writes the receipt. The
main wall gives every ordinary step only a shorter operation wall, reserving
TERM/KILL/drain and terminal publication time. Lock timeout keeps finalizer or
verifier failure intent retryable; only successful replacement or proof of a
newer terminal consumes that state.

A terminal is not authoritative while its adjacent active intent directory is
pending. Consumers, failure invalidation, and publishers all acquire the
bounded intent gate before any terminal lock. The active directory contains
mode-0600 canonical `intent.json` plus `identity.json`; file and parent fsync,
parent identity revalidation, and gate↔sidecar↔intent inode/digest cross-binding
make the state durable across fresh processes. The sidecar also pins the exact
failure-payload digest, schema and run/verifier/mutation identities. Same-byte
inode replacement, sidecar tampering/replacement, secrets, links, or evidence
input aliases remain fail-closed. A verified failure/PASS atomically renames
the exact pair out of the active namespace, fsyncs the parent, removes only the
validated pair, and durably returns the gate to idle. Immediately before
unlink, the consumer first persists a fsynced `committed_cleanup` gate phase
that binds the durable terminal identity, expected predecessor, both entry
names/full identities, consumed-directory inode, digest, and provenance
context. Deletion is strictly
intent then sidecar, with a child-directory fsync after each unlink; directory
removal is parent-fsynced before the gate can become durably idle. Fresh
processes recover both-files, sidecar-only, zero-files, and directory-absent
prefixes. The reverse one-file prefix is unreachable by construction and is
rejected. Every survivor is descriptor-read for exact identity and canonical
binding immediately before deletion, so tampering or foreign entries remain
untouched. A changed terminal permits cleanup only under schema-valid explicit
newer-wins provenance. Terminal locks are
opened by basename through the gate's already anchored parent descriptor and
validated as no-follow, regular, single-link, mode-0600 files with matching
inode; swapping the visible parent namespace cannot redirect lock creation.
The implementation is singular:
`packages/common/compression_terminal_state.py` serves the verifier,
supervisor and `ExecStopPost` finalizer. The supervisor never opens the
publication lock or atomically replaces the terminal itself. Its retry state
binds the exact stale device/inode/bytes/digest and run/mutation SHA. Shared
reconciliation permits an unavailable verifier intent to become a bound
finalizer tombstone only for that same expected terminal identity, retains
pending/finalizer state on timeout or ambiguity, and consumes state only after
a complete publication or a schema-valid authoritative newer terminal.

Version-3 failure branches are disjoint in the schema:
`provenance_state=unavailable` requires the exact safe `failure_context` and
forbids run/mutation
identity, while `provenance_state=bound` requires `run_id` plus
`mutation_head_sha` and forbids `failure_context`. A qualifying version-3 PASS
explicitly forbids every failure-only field. Historical version-2 JSON remains
readable and nonqualifying.

All committed 2026-07-15 JSON remains byte-identical and schema-readable as
superseded version-2 history, but it cannot set the qualifying discriminator.
It also lacks the new producer-owned global chronology, raw quiescence
checkpoints, resolved launcher provenance and descriptor-bound dump inspection. Those facts
cannot be reconstructed by editing old envelopes. Task 4.5 remains open and a
new controlled mutation replay still requires separate authorization.
