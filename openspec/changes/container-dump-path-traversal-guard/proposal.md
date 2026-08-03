# Close the `..` traversal hole in the container dump path gates (#1269)

## Why

Four gates admit the container dump path into the forensic lane, and
all four express "the path is inside the DB container data mount" —
the messages' own phrase for the pinned prefix `/var/lib/postgresql/`,
kept verbatim throughout this document and denoting nothing more than
that prefix — as a bare string prefix — `startswith("/var/lib/postgresql/")` — which
constrains the literal opening of the string, not path containment
(all anchors re-verified at master 49b7b452):

1. `scripts/node27_timeseries_compression_live_evidence.py:744-749` —
   the pg_restore list argv gate (`argv[:5]` + `len == 6` + prefix on
   `argv[-1]`), refusing with `EvidenceError("pg_restore list argv
   differs")`.
2. `scripts/node27_timeseries_compression_live_evidence.py:1887-1892`
   — the captured schema-dump-list listing gate, same prefix predicate
   on `list_argv[-1]`, refusing with `EvidenceError("schema forensic
   dump/list identity is not verifiable")`.
3. `scripts/node27_timeseries_compression_supervisor.py:350-364` — the
   supervisor's mirror argv gate (`_assert_exact_argv`, prefix at
   :362), refusing with `SupervisorError("pg_restore list argv/output
   ownership differs")`.
4. `scripts/node27_timeseries_compression_supervisor.py:1055-1059` —
   `resolve_container_pg_restore_identity`, whose refusal message
   claims exactly the property the prefix does not enforce:
   `SupervisorError("pg_restore dump path is outside the DB container
   data mount")`.

`/var/lib/postgresql/../../../etc/shadow` satisfies the prefix at all
four gates, yet normalizes to `/etc/shadow` — outside the prefix. The
value flows verbatim from the plan's pg_restore list argv tail
(supervisor.py:1768 `str(list_command["argv"][-1])`) into gate 4,
which then really executes `docker exec <container> /usr/bin/sha256sum
<realpath> <dump_path>` (:1087-1100) and records the digest as
`dump_sha256` in the forensic bundle; the same path is handed to
`pg_restore --list`.

There is a FIFTH route to the same side effect, caught by this
change's fixture review: the `schema_dump_list` CAPTURE argv also
carries `--schema-dump-container`, and nothing gates that value before
the spawn — the supervisor's pre-spawn capture gate
(`_assert_capture_producer_argv`, supervisor.py:519-551, called at
:661 and :964) pins only producer/kind/anchored-option abbreviations,
and the verifier deliberately leaves the option value unpinned
(live_evidence.py:124-128). A plan whose pg_restore_list command tail
is clean but whose capture argv binds a traversal value still reaches
a real in-container `docker exec pg_restore --list` on the escaped
path (capture.py:531-535 builds and executes the argv from
`ctx.schema_dump_container`); gate 2 only refuses the bundle
afterwards. Two spelling holes ride along: the abbreviation form
(`--schema-dump-c=…`, expanded by capture's argparse but invisible to
an exact-base value scan) and the dangling-flag form. Upper bound of
impact is container-internal
read-only disclosure (an arbitrary in-container file's sha256 plus a
pg_restore --list success/failure signal), gated behind an
already-authorized/pinned run plan (`NODE27_COMPRESSION_RUN_PLAN_SHA256`,
supervisor.py:1817-1821) — the severity is bounded, but the gate is
lying: it is the lane's only containment assertion over this path, and
`tests/test_node27_timeseries_compression_supervisor.py:2511-2516`
stamps the false claim with a `/tmp/schema.dump` case that only
exercises the prefix miss. Introduced by 71125485 (2026-07-16), all
four sites at once, same spelling. Found during #1268's fixture-review
consumer sweep; pre-existing, orthogonal to #1268's authoring-guard
domain, and explicitly routed here by that PR's records.

## What Changes

Adopted route (the issue's recommendation, extended by fixture-review
P1): **one shared containment predicate, five call sites** — the
existing prefix conjunct gains a `..`-parts conjunct, shaped like the
plan_author guard's own `..` check, plus one new pre-spawn value gate
on the capture-argv route.

1. **`packages/common/node27_container_contract.py`** (the lane's
   cross-plane single-source contract module — its docstring's stated
   purpose is exactly this defect class: "an external-contract value
   hard-coded identically in two planes"; already imported by
   supervisor.py:43, live_evidence.py:49 and capture.py:42, and
   snapshot-audited by `scripts/node27_external_contract_snapshot.py`
   whose contract mapping is explicit per-constant, so an additive
   symbol needs no snapshot refresh) gains

   ```python
   CONTAINER_DB_MOUNT_PREFIX = "/var/lib/postgresql/"

   def container_dump_path_within_mount(value: str) -> bool:
       return value.startswith(CONTAINER_DB_MOUNT_PREFIX) and (
           ".." not in PurePosixPath(value).parts
       )
   ```

   (module gains `from pathlib import PurePosixPath`; it imports no
   pathlib today). This follows the existing precedent that both
   planes import cross-plane contract values from this module
   (`CONTAINER_PG_RESTORE_REALPATH`), which is the recorded exception
   to the verifier's restate-literals posture (live_evidence.py:
   104-111). Rejected homes: `compression_terminal_state` is the
   terminal-state/receipt-identity module — wrong cohesion; a generic
   parameterized helper in `evidence_io` would re-duplicate the
   mount-prefix literal at every call site, recreating exactly the
   multi-copy drift this change exists to kill; a new one-function
   module is a heavier artifact for the same effect.
2. **Four admission gates swap their inline prefix check for the
   predicate** — gates 1-3 keep their existing refusal messages
   verbatim (the argv still "differs"); gate 4 keeps "pg_restore dump
   path is outside the DB container data mount" byte-for-byte, and the
   check behind it now enforces containment rather than a string
   opening. No message churn, no reordering: gate 4's check stays
   the function's first statement, ahead of every
   `_run_capture_argv`, so refusal happens with **zero docker
   invocations**.
3. **Gate 5 — pre-spawn capture-argv containment** (closes the fifth
   route): in `_assert_capture_producer_argv`, when the declared kind
   is `schema_dump_list`, every value bound to
   `--schema-dump-container` (via the existing
   `_capture_option_values`, which handles both argparse forms,
   position-independently, and returns the `""` sentinel for a
   dangling flag — a sentinel the predicate naturally refuses) must
   satisfy the shared predicate; an absent option binds no value, so
   there is nothing to judge — and capture itself fails such a run
   (the option is registered `default=None`, capture.py:769, and
   `_capture_schema_dump_list` raises `CaptureError` at :515-516; the
   in-prefix default belongs to plan_author, not capture).
   `--schema-dump-container` additionally joins
   `ANCHORED_CAPTURE_OPTIONS` — in BOTH planes, supervisor.py:76 and
   live_evidence.py:98, because the tuples' equality is pinned by an
   existing cross-plane drift test — so abbreviation spellings
   (`--schema-dump-c=…`) cannot smuggle the binding past the
   exact-base value scan — the same "abbreviation is a rebinding
   technique" rationale already recorded in that gate's docstring.
   The verifier's plan-capture gate thereby also newly refuses such
   abbreviations (live_evidence.py:1259-1264) — a deliberate part of
   the change, recorded in the behavior surface below.
   The docstring gains the adjudication sentence distinguishing this
   VALUE check (an execution-safety property of what the spawned
   capture will docker-exec) from the deliberately unchecked
   `--mutation-head-sha` value (a forensic claim the verifier owns).
   capture.py itself stays frozen: the refusal happens before its
   process exists, and the verifier's listing gate (gate 2) already
   refuses the recorded bundle post-hoc regardless of spelling.
4. **Verbatim posture untouched** (the issue's hard boundary, #1265's
   adjudicated forensic posture): the predicate judges and never
   rewrites. Admitted values keep being recorded and compared as the
   plan's original strings — no `posixpath.normpath`, no `Path()`
   write-back, no widening of `EXPECTED_CAPTURE_TOOL_VALUES`.
   Interior-double-slash in-prefix values
   (`/var/lib/postgresql//evidence/…`) stay admitted:
   `PurePosixPath(...).parts` drops empty components without ever
   seeing a `..`, so the #1268 adjudication pin scenario (interior
   `//` authors and lands verbatim) is untouched, as is plan_author
   itself (the authoring non-guard ruling stands; these gates exist
   precisely for hand-crafted plans and bundles that never pass
   through plan_author).
5. **Tests**: the false-stamp test
   (`test_resolve_container_pg_restore_identity_rejects_out_of_mount_dump`)
   is strengthened into a parametrized battery (prefix miss
   `/tmp/schema.dump` plus the two traversal shapes); each of the
   five gates gains its own independent traversal negatives (no gate
   relies on an upstream gate having refused; gate 5 additionally
   covers the abbreviation and dangling-flag spellings); a
   zero-docker-exec proof covers gate 4's refusal ordering (gate 5's
   refusal-before-spawn is structural: the pure function is called
   before spawn at supervisor.py:661/:964, and a direct unit test
   covers it); direct predicate unit tests pin the accept/reject
   table (default, interior `//`, bare prefix root, trailing-slash
   in-prefix, `a..b` filename component vs `..` segment); a
   source-scan drift guard refuses the old inline mount-prefix
   spellings in either gate module (a scan can only refuse the
   spellings it enumerates — the per-gate behavioural refusals are
   the load-bearing guarantee).
6. **Prose de-staling**: four surfaces describe the gates as
   prefix-only and would become false — the container dump path
   comment block in plan_author.py ("asserts the same `startswith`
   mount containment"), the #1268 adjudication pin test's docstring
   ("still prefix-compatible with the gates"), the MODIFIED
   requirement's own residual sentence ("the verifier's prefix/shape
   argv gates"), and the runbook's `--schema-dump-container`
   paragraph. The first two were named by fixture review; the last two
   came from the grep sweep that review's finding forced. All get
   comment/docstring/prose-only updates; assertions and pinned
   behavior stay byte-identical. The de-staling also re-points those blocks'
   cross-file anchors at symbol names rather than line numbers,
   because this change's own line growth invalidates numbers written
   in the same commit (the #1268 depth-retro rule, extended from
   self-file to cross-file anchors).
7. **Spec**: one ADDED requirement (the five gates judge containment
   through the single shared predicate, refuse independently, refuse
   before container side effects, and rewrite nothing) and one
   MODIFIED requirement (the #1268 residual paragraph notes that the
   consuming gates now also refuse `..` components — a containment
   judgment at the gates that does not reopen the authoring
   adjudication).

Explicitly not adopted (per the issue): parts-prefix rewrite of all
gates (changes leading-`//` behavior, bigger diff, no added safety
over the conjunct); plan_author-side guarding alone (gates 1-2 never
see plan_author output when a bundle is hand-crafted);
normalize-then-compare (weakens "bundle matches the reviewed plan
byte-for-byte").

Recorded residuals (out of scope, bounded by the same
already-authorized-plan trust boundary):

- The predicate is string-level, so a symlink planted inside the
  bind-mounted evidence directory and pointing outside it still
  escapes `docker exec sha256sum` (which follows symlinks).
  Correction to this change's first draft of this paragraph, made
  after review challenged it: planting such a symlink does NOT
  require access inside the DB container. MEASURED on node-27
  (read-only, `docker inspect nhms-db --format '{{json .Mounts}}'`,
  2026-08-02):

  ```json
  {"Type":"bind","Source":"/home/nwm/nhms-evidence",
   "Destination":"/var/lib/postgresql/evidence","RW":true}
  ```

  so host-side write access as the plan-authoring user suffices —
  exactly the actor class the pinned-plan boundary already admits —
  and the reachable region is the `evidence` subtree, narrower than
  the `/var/lib/postgresql/` prefix the predicate spans (the rest of
  that prefix carries no other host bind mount in that same listing;
  the DB's own data directory is mounted outside the prefix entirely,
  at `/home/postgres/pgdata/data`). What the predicate
  closes is the pure-string traversal, which needs no filesystem
  foothold at all; closing the symlink route needs a container-side
  no-follow check, deliberately not attempted here.
- `PurePosixPath` keeps `..\x00` as a single component, so a
  NUL-bearing value such as `/var/lib/postgresql/..\x00/etc` is
  admitted by the predicate although `execve` would truncate it to
  `/var/lib`. Not reachable as an escape — CPython refuses the argv
  with `ValueError` before any spawn — and the argv token model's
  lack of a NUL check predates this change.

## Impact

- Affected code: `packages/common/node27_container_contract.py`
  (additive: one const + one predicate + `PurePosixPath` import),
  `scripts/node27_timeseries_compression_live_evidence.py` (:744-749,
  :1887-1892, :98 `ANCHORED_CAPTURE_OPTIONS` mirror),
  `scripts/node27_timeseries_compression_supervisor.py`
  (:350-364, :1055-1059, :519-551 gate 5, :76
  `ANCHORED_CAPTURE_OPTIONS`),
  `tests/test_node27_timeseries_compression_supervisor.py`,
  `tests/test_node27_timeseries_compression_live_evidence.py`;
  comment/docstring-only: `scripts/node27_timeseries_compression_plan_author.py`
  (the container dump path comment block, zero non-comment changed
  lines), the #1268 pin test's docstring, and
  `docs/runbooks/tier-node27-timeseries-storage.md` (the
  `--schema-dump-container` paragraph). That is the complete
  seven-file non-spec set; it is enumerated from
  `git diff --name-only`, not from memory, because earlier rounds of
  this change added files without updating this list.
- Frozen surfaces (zero diff):
  `scripts/node27_timeseries_compression_capture.py`,
  `scripts/node27_timeseries_compression_prearm.py`,
  `scripts/node27_timeseries_compression_bundle_author.py`,
  `packages/common/safe_fs.py`, `packages/common/evidence_io.py`,
  `packages/common/compression_terminal_state.py`,
  `scripts/node27_external_contract_snapshot.py` (its contract mapping
  is per-constant; the additive const needs no snapshot change),
  `schemas/**`, `db/**`.
- Affected specs: `hypertable-compression` (1 ADDED, 1 MODIFIED).
- Behavior change surface: previously-admitted `..`-bearing container
  dump paths are now refused at every gate. No legitimate producer
  ever emits such a path (plan_author's default and any sane override
  are `..`-free), so the only observable change is that hand-crafted
  traversal plans/bundles fail closed — with the existing messages at
  gates 1-4, and one NEW refusal message at gate 5 (the pre-spawn
  capture-argv value check, a refusal that simply did not exist
  before). Capture argvs carrying an abbreviation of
  `--schema-dump-container` are also newly refused — on BOTH planes,
  since the anchored tuple is mirrored (supervisor's pre-spawn gate
  and the verifier's plan-capture gate at live_evidence.py:1259-1264)
  — previously admitted and expanded by argparse; no committed
  producer emits abbreviations.
