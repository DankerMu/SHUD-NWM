# Close the three residual non-production capture-argv shapes after the anchor series (#1263)

## Why

The anchor series (#1250 seam scan → #1259 producer identity → #1261/PR #1262
tool-path value pins) leaves three argv shapes that still verify PASS while
being provably non-production (all facts measured by issue-scribe at
ec45d76a, content-identical to current master fed5a60a):

1. **Help-token early exit.** capture.py's parser runs with argparse's
   default `add_help=True` (capture.py:756, no `add_help=False`), so `-h`,
   `--help` and — because no other registered flag starts with `--h` — the
   unambiguous abbreviations `--h`/`--he`/`--hel` all make `main()`
   (capture.py:782-806, `parse_args` first) print ~2.6 KB of help and
   `SystemExit(0)` before a single capture runs. An identity-anchored,
   value-pinned, seam-free argv carrying one such trailing token passes every
   existing gate (four executed PASS proofs in the issue body), yet the argv
   it records could never have produced the snapshot it claims: the recorded
   producer exits before collecting anything. The forensic claim "this argv
   proves the committed producer collected this snapshot" is false for the
   whole help-token family.
2. **argv[0] trust root under-recorded.** The comment at
   live_evidence.py:1098-1100 records WHY argv[0] is unpinned ("an
   environment fact, not a committed identity") but not the capability
   consequence: argv[0] is a residual trust root — a plan may record any
   interpreter path there, and the interpreter (plus the repo checkout the
   producer script is loaded from) decides what the pinned argv[1] script
   actually does. The closure path is producer-side hardening (#1261
   alternative 2), not a verifier gate; the record should say so.
3. **`--evidence-dir` is a measurement input, recorded as merely
   "run-scoped".** capture.py `_free_bytes` (:490-502) runs
   `os.statvfs(ctx.evidence_dir)` (:501); the snapshot's `free_bytes` (:472)
   feeds the verifier's `MIN_FREE_BYTES` (300 GiB) hard gates
   (live_evidence.py:2046-2047 and :2201-2203). The #1250 work closed the
   `--self-test-free-bytes` SEAM route to faking headroom, but the
   directory-identity route stayed open: point `--evidence-dir` at any
   roomy filesystem and the statvfs measurement is about THAT filesystem,
   not the data volume the plan claims. The deliberately-absent rationale at
   live_evidence.py:113-118 ("run-scoped, varies per run") under-records
   this identity. Meanwhile production `plan_author` derives BOTH
   `--evidence-dir = f"{root}/capture-artifacts"` (plan_author.py:219) and
   `output_path = f"{root}/capture-{kind}.json"` (:239) from the same
   `root`, and `output_path` is already verifier-bound (:1086-1092 absolute
   string; :1449 ledger ref equality) — so a purely relational binding is
   available without pinning any run-varying literal.

## What Changes

One PR, three shapes, all in `scripts/node27_timeseries_compression_live_evidence.py`
and its test suite; every other file frozen.

1. **Help-token rejection branch** in the existing per-token scan
   (:1169-1186), alongside the seam branch: refuse any token whose base
   (`token.split("=", 1)[0]`) is `-h`, or has `len(base) >= 3` and is a
   prefix of `--help` (covers `--h`, `--he`, `--hel`, `--help` itself and
   the `--help=x` spelling). `-h` has length 2, outside the `len >= 3`
   mechanism the other branches use, so it gets the explicit equality case.
   Distinct `EvidenceError` message naming the offending token and stating
   the refusal class: an argparse help early-exit token means the recorded
   producer exits before collecting anything. The message stays
   spelling-safe (measured: the bare spellings print help and
   `SystemExit(0)`; `--help=x` is an argparse usage error, `SystemExit(2)`,
   no help printed — do not claim "prints help and exits 0" for the whole
   family). Zero-collision premise
   (measured: capture parser registers no `--h*` business flag and no
   single-dash flag at all beyond argparse's auto `-h`) is recorded in a
   comment and pinned by a structural test against the real parser.
2. **argv[0] comment expansion** at :1098-1100: keep the existing sentences
   verbatim, append the capability consequence (argv[0] + the repo checkout
   remain the residual trust roots of the forensic claim) and the closure
   route (producer-side hardening per #1261 alternative 2 — explicitly NOT
   a verifier gate, since the verifier cannot know the production
   interpreter path without pinning an environment fact). No new gate, no
   runtime behavior change for this shape.
3. **Relational `--evidence-dir` binding** (the issue's stated preference,
   adopted): a sixth capture gate immediately after the tool-value loop
   (:1135-1141): `_argv_option_values(capture_argv, "--evidence-dir") ==
   [expected]` where `expected = output_path.rsplit("/", 1)[0] +
   "/capture-artifacts"` — the exact textual inverse of plan_author's two
   same-`root` f-strings, no filesystem normalization (even a
   trailing-slash root round-trips consistently through both f-strings, so
   plan_author output is never refused). One equality refuses all four
   shapes (absent/duplicated/dangling/mismatched); the message names the
   option, the observed bindings and the derived expected value. Like
   `--database`, the value is dynamic, so it joins
   `PINNED_CAPTURE_VALUE_OPTIONS` (abbreviation closure: a trailing
   `--ev /elsewhere` — or `--e`, len 3 — must not rebind last-wins) but NOT
   `EXPECTED_CAPTURE_TOOL_VALUES` (no literal exists). Zero-collision
   premise (measured: `--evidence-dir` is the only registered `--e*` flag)
   recorded and structurally pinned. The deliberately-absent rationale at
   :113-118 is rewritten: `--evidence-dir` moves out of the absent list
   with its statvfs-measurement-input identity recorded; the
   `--schema-dump-*` sentence stays.
4. **Test templates stay single-field-corruption honest**: `_bundle`'s
   capture template and `_producer_argv` both gain a `--evidence-dir`
   binding derived consistently with the capture `output_path` they pair
   with (in `_bundle`, all capture output_paths live directly under
   `tmp_path`, so the derived value is `f"{tmp_path}/capture-artifacts"`).
   Without this, every existing negative would be refused for the missing
   `--evidence-dir` instead of the field it corrupts — silent attribution
   loss across the whole matrix. `_producer_argv` keeps `--mutation-head-sha`
   caller-supplied via `*extra` (the `[pair_missing]` red capability from
   #1261 must survive).

### Out of scope (verbatim-preserve surface)

- The #1250 seam branch, #1259 anchor gates and #1262 value-pin gate code
  and messages: byte-identical (the `PINNED_CAPTURE_VALUE_OPTIONS` tuple
  gaining one element is a recorded data-domain widening, the same move
  #1261 made on the anchored→pinned axis; the rejection loop code does not
  change).
- `capture.py`, `plan_author.py`, `supervisor.py`, `bundle_author.py`,
  `schemas/**`, `tests/test_node27_timeseries_compression_supervisor.py`,
  `tests/test_node27_timeseries_compression_capture.py`: zero diff. The
  supervisor stays help-token/evidence-dir agnostic by the recorded
  asymmetry (executor runs hermetic plans; forensic claims are
  verifier-owned).
- No argv[0] gate, no per-kind evidence subdirectories, no whole-argv
  parser-viability simulation, no producer-side (`capture.py`) hardening —
  that is #1261 alternative 2's own track.

## Impact

- Affected spec: `hypertable-compression` (one ADDED requirement + one
  MODIFIED requirement — the #1262 tool-value requirement's
  "`--evidence-dir` … stay[s] deliberately unpinned" sentence must move
  with this change, or the archived capability spec would carry two
  mutually contradictory normative sentences about `--evidence-dir`).
- Affected code: `scripts/node27_timeseries_compression_live_evidence.py`
  (one new scan branch, one new gate, two comment/rationale updates, one
  tuple extension), `tests/test_node27_timeseries_compression_live_evidence.py`
  (two template updates + new negatives/positives/structural tests).
- Verification is fully hermetic (pytest + ruff + openspec); no node-27 or
  node-22 access. Runtime behavior change is refusal-only: strictly more
  bundles are rejected, and the twelve-kind plan_author positive control
  plus the e2e (whose plans derive both fields from the same tmp root)
  prove no production-shaped or hermetic-e2e bundle is newly refused.
