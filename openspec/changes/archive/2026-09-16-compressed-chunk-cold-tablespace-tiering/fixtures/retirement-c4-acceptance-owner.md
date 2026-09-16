# R1.6 Bringup-C4 production-acceptance owner fixture

Issue #1895 / epic #1891; R1.6 plus C4 portion of R1.4/R1.5. Suggested expanded
accepted, effective high. This is an independent owner-transfer PR after
PRs #2311/#2318; retained SQL/API workload transfer remains the next R1.4 slice, not
silently dropped.

Minimal mergeable slice: deliver
`services/production_closure/c4_production_acceptance.py` (split owner-local IO
if size requires), public `scripts/node27_c4_production_acceptance.py`, tests
and actual runbook entrypoints. Compose the existing C4 binder; no changes to
its CLI, core return contract, closed receipt schema, producer or river-click
publisher. Old G0/C3 families remain unchanged until later R3; the new
production acceptance has no `node27_issue1895_*` import/runtime dependency.

## Public contract and provenance

Three immutable stages, one private outer acceptance chain, not a new
generalized receipt platform:

1. `freeze --approved-record PATH --reviewed-sha SHA --receipt PATH --frontend-origin ORIGIN --api-origin ORIGIN --basin-id ID --segment-id ID --output FREEZE`:
   before C4 execution, read the independently approved delivery/C1 record,
   verify its selected `status=PASS`, `head_sha` and `reviewed_sha` fields match
   the supplied approved 40-hex SHA, then publish the freeze. The source is an
   already-approved operator-supplied artifact, not a file this CLI
   self-approves. It may have other bounded fields; do not import/reimplement
   the full old C1 validator or imply all C1 checks passed here. Freeze binds
   source path/raw SHA-256/full file identity, expected SHA, five C4 inputs and
   current UTC time. Receipt target must not already exist; its private parent
   exists before this command.
2. `bind --freeze FREEZE --reviewed-sha SHA --cmd-start SECONDS --cmd-end SECONDS --output BINDING`:
   read original freeze, verify supplied SHA and frozen source still match;
   require freeze time no later than the C4 command-start second. The bracket
   comes from the actual producer invocation, not the binder invocation. Run the
   unchanged real Node C4 binder against frozen five inputs + supplied bracket.
   Capture C4 raw SHA-256 and full POSIX identity before/after that call and
   reject drift, then publish immutable binding containing original freeze
   identity/hash and accepted C4 identity/hash/inputs/bracket.
3. `verify --freeze FREEZE --binding BINDING --reviewed-sha SHA --output ACCEPTANCE`:
   validate closed outer records and their original identities, frozen approval
   source and supplied SHA; rerun the unchanged local binder with the ORIGINAL
   inputs/bracket and recompare exact C4 bytes/identity. Publish the private
   acceptance result only if all checks agree. Never rewrite/rederive expected
   facts from the final observation or refresh the original binding to make it
   pass.

The approved record is a trust input from its delivery/C1 owner; status/SHA
extraction validates binding only, not approval authorization or the full
independent C1-C3 proof. C1 currently emits status/head_sha/reviewed_sha
(`node27_issue1895_display_runtime.py`); future delivery records may use this
same selected-field contract without depending on retired C1 code. Basin/segment
and origins come from the operator's approved C4 configuration, supplied
explicitly at freeze (C1 does not currently emit basin/segment). Receipt path
comes from the operator; execution bracket is recorded once at bind after the
real producer run.

Outer freeze/binding/acceptance schemas are owner-local closed versioned JSON
objects with bounded depth/nodes/bytes and strict scalar types; no extra
SHA/digest fields in the C4 document. Origins allow http/https and empty or `/`
path only, no credentials/query/fragment; leave semantic origin comparison to
the real binder. Reject malformed/empty SHA/IDs/brackets, duplicate JSON keys
and nonfinite numbers. Errors are stable owner-coded refusals with no raw
input/URL/credential leakage.
Prefer static refusal codes/messages, never interpolated raw inputs or native
exceptions (including argparse errors). If redaction is needed, use existing
`packages/common/redaction.py`, not a second redaction implementation.

## Reuse and filesystem boundary

- Invoke fixed repository `apps/frontend/scripts/c4-receipt-binder.mjs` through
  direct argv and the existing bounded-command primitive; do not invoke the core
  as a CLI or create a fake PASS path. Use
  `packages/common/node27_pgdata_command.run_bounded_command` and translate its
  error at the display acceptance boundary. Fixed trusted binder path, finite
  timeout/output ceiling; no user-supplied executable/runner CLI options.
- Python tests may inject the process boundary; the default CLI always calls the
  real Node binder. Require its successful exit and actual PASS output, not
  substring/exit-only acceptance. Do not duplicate C4 semantic validation.
- Owner-local descriptor reader uses `safe_fs.open_file_no_follow` /
  `open_directory_no_follow`; full fstat signature is
  dev/ino/uid/mode/nlink/size/mtime_ns/ctime_ns plus SHA-256 read from the SAME
  held fd. Hold/read/recheck across binder invocation, recheck pathname and
  pinned parent identities, close every fd. C4's own binder supplies its
  semantic/private-file checks; outer reader still enforces euid-owned 0700
  parent, 0600 regular single-link bounded file. Shared
  `evidence_io.FileIdentity` lacks uid/mode/nlink/mtime/ctime, so do not extend
  it or mistake it for this stricter C4 signature; reuse evidence_io JSON
  complexity bounds where suitable.
- Private output publication follows old commit owner's temp-sibling pattern,
  composed from EXISTING
  `safe_fs_publication.write_bytes_no_follow_exclusive(mode=0o600, require_durable_create=True)`
  then `move_regular_file_no_follow_exclusive`. No bare final-path write,
  overwrite, newly weakened common helper or wholesale old commit-module
  migration. Verify private parent, created temp inode, final inode/bytes,
  durable parent and readback. On uncertainty refuse; never delete an existing
  winner or object not proved created by this invocation.
- Paths of approval/freeze/binding/C4/output must not alias; no-follow all path
  components, parent swaps, symlink/nonregular/hardlink, unsafe ownership/mode
  and oversized/complex input refuse. Input records remain immutable even after
  refusal.

## Migration/deletion census

| Surface                                                                   | This slice                                                                                                           | Later R3 / other R1 slice                                                                                   |
| ------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `node27_issue1895_publication_current.bind_c3_receipt` SHA/C4 hash checks | Re-establish only this necessary outer guarantee in the new real owner, using current C4 binder                      | Old broader C3 registry/GFS-IFS/frontier/job/log checks and wrappers retire later                           |
| `node27_issue1895_commit` / `private_receipt`                             | Reuse their private temp/publish design through existing shared safe_fs primitives, no imports or copied full stacks | Old performance marker/private-receipt owners retain current callers until R3                               |
| Existing C4 binder/core/schema/producer and river-click                   | Unchanged semantic/protocol/publication owners                                                                       | Not retired                                                                                                 |
| Bringup C3/C4 and retained PGDATA workload docs                           | Register new freeze/bind/verify paths, record source semantics and explicit no-C1-C3-substitution                    | Withdrawn G0-G8 examples remain historical until R3 deletion; SQL/API/ingest workload transfer remains R1.4 |
| Old C1/C2/C3/performance CLI callers and their tests                      | Unchanged in this slice; no broken intermediate imports                                                              | Remove only after their actual remaining consumer transfer                                                  |

Concrete registration targets: `docs/runbooks/node-27-bringup-checklist.md`
§C4 and `docs/runbooks/tier-node27-timeseries-storage.md` retained-workloads
section. Keep their independent C1-C3 proof and withdrawn G0-G8 status intact.

Import census: old C3 owner is used by its observer/binder scripts, c14 owner
map and C3 tests; private_receipt/commit additionally serve
census/reconcile/timer/performance wrappers. None is a dependency of standalone
C4 core/CLI. New owner therefore MUST compose the standalone binder without
importing that old graph. Do not preserve the old graph merely for the new
entrypoint. Synthetic positional query compatibility and four-lane G7 protocol
are explicitly NOT carried; query work is a separate pending slice.

## Risk packs / invariant matrix

Selected core packs: CLI/script entry; config/setup; file IO/path
safety/overwrite; schema/fields; auth/secrets; concurrency/ordering; resource
limits; legacy compatibility/examples; error/rollback/partial output;
packaging/dependency; documentation/migration notes. All are selected because
the new owner accepts/publishes private production evidence across process and
filesystem boundaries. Domain selected: published NHMS/display identity. Not
selected: geospatial/CRS, hydro-met temporal query semantics, SHUD
runtime/numerics, Slurm, external providers, manifest/QC, PostGIS/Timescale
engine (this slice does not query DB or change SQL).

Governing invariant: production acceptance can only validate the original
approved SHA, original C4 execution inputs/bracket and exact previously accepted
file bytes/identity; a later observation can refuse but cannot redefine those
expectations.

| Surface             | Scenario                                                                                                         | Proof                                                                                                   |
| ------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| Approval producer   | Valid approved source then freeze before C4; missing/mismatched SHA or source swap                               | Freeze binds original source/facts, no output on refusal, no self-derived approval                      |
| Validators/process  | Binder PASS/FAIL/nonzero/timeout/malformed output                                                                | Real binder required; no exit-only/fake PASS, bounded static errors and original arguments              |
| Storage/identity    | C4 bytes change, inode replacement, same-size content change, mode/nlink/mtime/ctime drift                       | Same-held-fd hash/full facts and before/after/path-parent checks refuse every drift                     |
| Entry/order         | freeze→producer→bind→verify; missing freeze/binding or freeze after bracket start                                | Correct sequence succeeds, absent/late/stale/mismatched records refuse, no mutable refresh              |
| Consumers           | Existing local C4 and river-click interfaces                                                                     | Original five-input/bracket CLI and schema unchanged; old C3 tests still work until removal             |
| Failure/publication | Existing output, symlink parent/leaf, nonregular, oversized/duplicate/complex JSON, write/fsync/readback failure | No clobber, no cleanup of winner/unowned object, no accepted partial result or fd leak                  |
| Evidence/docs       | Actual new CLI runs and registered Bringup-C4 owner                                                              | Positive/negative runtime proof; local C4 PASS never labeled independent C1-C3 or deployment completion |

## Evidence Floor and selectors

- Proposed test owners `tests/test_node27_c4_production_acceptance.py` and, if
  separated, `tests/test_node27_c4_production_acceptance_io.py` cover real
  public freeze/bind/verify and filesystem refusal, with process boundary
  injection only. No Node dependency in ordinary backend CI unit tests; no
  source/prose pins.
- Explicit PathTestRules for new owner/IO/CLI select all new suites; matching
  expected maps in `test_select_ci_tests.py` ship together. Every actual shared
  dependency of the new owner/IO/CLI must select the new consumer suites:
  at minimum `node27_pgdata_command.py`, `safe_fs.py`,
  `safe_fs_publication.py`, `evidence_io.py` and, when imported,
  `redaction.py`. Do not shrink existing producer-rule consumers.
  Existing frontend binder
  suites remain selected by their normal frontend path scope.
- Local: full Ruff, strict active and matching canonical C4 OpenSpec, changed
  Markdown; existing frontend C4 binder/producer/private-publisher
  suites/typecheck as applicable. Main applies only the C4 MODIFIED delta.
- Node-27 isolated checkout/venv/private owned directories: new targeted backend
  suites plus selector/unchanged C3 suites; default backend regression. New
  boundary tests need a batched discriminating red proof/mutation where baseline
  lacks the new API; no import-error-as-red claim.
- Run ACTUAL new CLI freeze/bind/verify around the REAL Node binder with a valid
  closed C4 receipt and negative byte/SHA/identity/binder cases, not only mocks
  or --help. Synthetic fixture receipts must be labeled fixture proof, never
  live production PASS.
- Exercise existing readonly live-C4 producer on node27 when producing live
  evidence; use approved configuration and private outputs, no redeploy/write
  operations. Preserve truthful PASS/BLOCKED/FAIL and never substitute synthetic
  PASS for unavailable browser facts. Current source-pin/foreign holds remain
  untouched; final production handoff remains R5.
- Before merge: four-seat high-risk cross-review, independent candidate
  verification, final exact-head gap sweep, CI and Chinese summary. Record
  actual public entrypoint, code owner and record provenance before marking R1.6
  done; do not mark all R1.4/R1.5/epic done.

Non-goals: broader old C3 checks, SQL/API/ingest workload transfer in this PR,
cold family deletion, new signatures/trust model/receipt platform, modifying the
local C4 closed protocol, changing production units/env/data, node22, archive or
epic closure.
