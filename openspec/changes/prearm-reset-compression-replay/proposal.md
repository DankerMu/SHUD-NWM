# Committed pre-arm reset for the node-27 compression replay (#1088)

## Why

The replay supervisor's trust boundary refuses to overwrite any
pre-existing supervisor-owned artifact: `run_child` raises
`"{kind} output {label} exists before spawn"` when any plan
artifact-association path already exists
(supervisor.py:789-794), and `_artifact_ref` raises
`"checkpoint artifact path already exists"` (supervisor.py:1050-1058).
That refusal is correct anti-evidence-shadow design — but there is no
committed pre-arm reset that clears the PREVIOUS arm's residue, so
every re-arm relies on the operator hand-deriving the sweep list. The
2026-07-17 arm session burned three one-shot replay launches on
exactly this (launch 9: stale `preflight-activity.json`; launch 10:
stale `schema-before.dump` — recorded in the gitignored
`.workplans/1069/live-arming-log.md`, cited as unverifiable field
anecdote). `grep prearm|pre-arm|sweep` over `scripts/`,
`docs/runbooks/`, `infra/` finds nothing.

## Premise corrections vs the issue text (explorer-verified)

1. Neither the working directory nor the schema-dump path is
   env-driven. `infra/env/node27-timeseries-compression-replay.example`
   holds only DB credentials + `NODE27_COMPRESSION_EXPECTED_STALE_SHA256`
   + `NODE27_COMPRESSION_RUN_PLAN_SHA256`. The workdir is implicit in
   the systemd unit's `--ledger-path` parent
   (`/home/nwm/node27-timeseries-compression-replay/`,
   infra/systemd/nhms-node27-timeseries-compression-replay.service:12),
   and the schema-dump path is PLAN-AUTHORED — it is whatever
   `run-plan.json`'s `artifact_associations["schema_dump"]` says
   (validated at supervisor.py:321/:330), not a fixed convention. So
   the prearm script reads association paths from `run-plan.json` and
   takes the workdir from a `--workdir` flag whose default is the
   production literal; it does NOT invent env vars.
2. The issue's trust-boundary anchors (:781-786/:1042-1048) drifted;
   current anchors are :789-794/:1050-1058 (byte-identical semantics).
3. The finalizer "三件套" is keyed off the RECEIPT path, not the
   finalizer-state path: `.terminal-evidence.json.publish.lock`,
   `.terminal-evidence.json.intent-gate.lock`,
   `.terminal-evidence.json.intent-gate.json`, plus the
   `.terminal-evidence.json.failure-intent/` directory
   (packages/common/compression_terminal_state.py:83-108), alongside
   `finalizer-state.json` and `.finalizer-state.json.<run_id>.consumed`.
4. THE ISSUE'S OWN WHITELIST IS WRONG (fixture-review P1×2, verified
   against code). Keeping `finalizer-state.json` kills the next arm:
   `_write_finalizer_state` refuses an existing state path
   (supervisor.py:1465-1478), and unpublished-failure paths
   (finalize_from_state :1565-1567, hard-kill, reboot) leave it
   behind. Keeping a stale `supervisor-ledger.jsonl` kills the next
   arm the same way (`AppendOnlyLedger` opens with `O_CREAT|O_EXCL`,
   supervisor.py:269-270). And the intent-gate family is a
   single-cycle state machine: keeping a pending
   `.failure-intent/` + gate state poisons the next publish
   (`_contexts_allow_reconcile` run_id mismatch,
   compression_terminal_state.py:1620/:1796) and makes the receipt
   non-authoritative (:1670), while sweeping only PART of the family
   (e.g. `.failure-intent.consumed-<hex>`, :1443, while keeping the
   gate json) manufactures an unrecoverable
   "ambiguous durable location" state (:1018). Correct dispositions
   are encoded below.

## What Changes

New `scripts/node27_timeseries_compression_prearm.py` (single file +
one test file; supervisor and all other production files zero-diff):

1. Inputs: `--workdir` (default
   `/home/nwm/node27-timeseries-compression-replay`), `--replay-env-path`
   (default `infra/env/node27-timeseries-compression-replay.env`
   resolved against the repo root via `Path(__file__).parents[1]`,
   never the CWD; parsed with a strict KEY=VALUE parser
   mirroring validate_two_node_docker_runtime.py:808-826 — refuse
   symlinked or non-0600 env file, duplicate keys), `--systemctl`
   (default `/usr/bin/systemctl`; the test seam, mirroring the fake-
   systemctl precedent of tests/test_scheduler_file_provider_refresh.py:2410-2440).
   `if __name__ == "__main__": raise SystemExit(main())` so the
   issue's `uv run python -m scripts.node27_timeseries_compression_prearm`
   invocation form works.
2. Fail-closed refusals, ALL evaluated before any move:
   (a) `systemctl --user is-active nhms-node27-timeseries-compression-replay.service`
   output must be exactly `inactive` or `failed` — anything else
   (`active`, `activating`, `unknown`, invocation failure) refuses;
   rc alone is NOT the gate because a running oneshot reports
   `activating` with nonzero rc.
   (b) if `<workdir>/terminal-evidence.json` exists, its sha256 (via
   `packages.common.compression_terminal_state.terminal_identity`,
   the same helper the supervisor consumes at :1766-1773) must equal
   the pinned `NODE27_COMPRESSION_EXPECTED_STALE_SHA256` (64-hex
   required); mismatch or malformed digest refuses. A missing receipt
   is fine (first arm).
   (c) UNRESOLVED FAILURE-INTENT refuses: if the
   `.terminal-evidence.json.failure-intent/` directory or any
   `.terminal-evidence.json.failure-intent.consumed-*` sibling
   exists, or `.terminal-evidence.json.intent-gate.json` exists with
   state != idle (or unreadable), the script refuses and tells the
   operator to resolve the intent first (e.g. supervisor
   `--finalize-only`) — sweeping a decided-but-unpublished failure
   would itself be evidence-shadowing, and partial sweeps of this
   family manufacture the unrecoverable ambiguous-location state
   (compression_terminal_state.py:1018).
   (d) `run-plan.json` present but unreadable/invalid JSON refuses
   (fail-closed; the operator authored it, so breakage needs eyes).
   A MISSING run-plan.json is allowed (sweep-only mode: the workdir
   sweep still runs, association archiving is skipped with a printed
   notice — systemd's ConditionPathExists will still block arming
   until the operator authors a plan).
3. Archive, never delete: `mkdir -p <workdir>/prearm-archive/<UTC-iso>/`
   then `shutil.move` every DIRECT child of the workdir that is not
   whitelisted and not `prearm-archive` itself. Whitelist (exact
   names, deliberately minimal): `run-plan.json` (systemd
   ConditionPathExists needs it, .service:3) and
   `terminal-evidence.json` (the supervisor's expected-stale CAS
   anchor, :1766-1773). EXPLICITLY SWEPT, with named test
   assertions: `finalizer-state.json` and `supervisor-ledger.jsonl`
   (both would kill the next arm via O_EXCL refusals —
   supervisor.py:1465-1478 and :269-270), stale
   `.finalizer-state.json.*.consumed` markers, and — only after
   refusal (d) has established the intent family is resolved (idle
   or absent) — the inert `.terminal-evidence.json.publish.lock` /
   `.intent-gate.lock` / `.intent-gate.json` residue as a family
   (locks are created on demand with O_CREAT|O_EXCL,
   compression_terminal_state.py:670/:1523, so they are not
   cross-run anchors).
4. Association/output residue: for every `artifact_associations`
   value across the plan's COMMANDS and every capture `output_path`
   (captures have no artifact_associations — validate_run_plan
   enforces the exact key set {capture_id, kind, argv, output_path},
   supervisor.py:573-581, and the capture-side trust boundary is
   "capture {kind} output exists before its owner", :891-898) that
   exists on disk (lstat, no follow), move it into
   `<archive>/associations/` under a collision-safe name (label +
   original basename + counter). This covers the exact set the
   supervisor refuses on, including out-of-workdir paths like
   `/home/nwm/nhms-evidence/schema-before.dump` and capture outputs
   under a non-default plan `--root`. Whitelisted workdir files are
   exempt even if a plan association names them
   (`terminal-evidence.json` is the expected-stale CAS anchor and the
   supervisor requires it in place).
5. Forensics: write `<archive>/prearm-manifest.json` (via
   `packages.common.safe_fs.atomic_write_bytes_no_follow`) recording
   UTC timestamp and every from→to pair; print the archive path and
   the next arm command
   (`systemctl --user start nhms-node27-timeseries-compression-replay.service`)
   on stdout. Exit 0 also when there was nothing to move (rerun-safe).

## Non-goals

- NO change to supervisor.py — the refusals at :789-794/:1050-1058
  stay byte-identical (zero-diff proof required).
- NO change to the systemd unit, the env example, runbooks, or the
  bringup checklist: the issue's optional `.service ExecStartPre` /
  runbook-checklist integrations carry a conditional AC requiring a
  node-27 live rehearsal receipt, which is operator-gated — routed as
  an operator follow-up, not silently dropped.
- NO deletion path anywhere in the script (no `unlink`/`rmtree`).
- NO automatic invocation from any production flow; the script is a
  committed operator tool.
- #1089 (external-contract snapshot probe) surfaces.
