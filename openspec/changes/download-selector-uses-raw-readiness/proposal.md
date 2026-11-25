# The download selector judges a cycle by its files, and serves the latest cycle first

## Why

#3238. The automatic cycle selector of the node-27 download job
(`scripts/node27_download_cycles.py`) treats "`manifest.json` exists" as
"cycle downloaded". Both adapters write the manifest before they download a
file, and no failure path removes it. A cycle whose download started but did
not finish is re-selected only while it is the latest allowed cycle; once a
newer cycle becomes the latest allowed one, nothing on the automatic path
selects it again. The compute-side scheduler keeps reading that cycle as not
ready, no alert targets the state, and the gap stays until an operator passes
an explicit cycle time.

The selector also returns the first gap it finds, oldest first. A cycle the
upstream can no longer serve (today: an IFS cycle that never gets a manifest)
is therefore selected on every pass and newer cycles are never reached.

## What Changes

- **Completeness predicate.** A cycle counts as downloaded for a source when
  `services.orchestrator.source_cycle_raw_manifest.nfs_raw_manifest_readiness`
  answers `ready` for it: the manifest's identity matches and every entry file
  is a non-empty regular file. This is the rule the scheduler applies to the
  same directory, so the selector and its consumer share one definition.
- **Selection order.** A cycle is ready when every selected source is
  ready for it. Over the window from the chain start to the latest allowed
  cycle: if the latest allowed cycle is not ready, select it; otherwise
  select the oldest cycle that is not ready; otherwise the latest allowed
  cycle. Before this change the oldest gap always won.
- **Observability.** The pass summary and the preflight-only output list the
  cycles judged not ready, with the status and reason per source, bounded in
  length.
- `node27-download-orchestration`: one requirement added.
- Runbook text that describes the selection (`infra/env/README.md`,
  `infra/env/node27-download.example`).

## Decisions

- **Reuse the scheduler's predicate, do not copy it.** The function reads the
  manifest and stats its entry files, has no side effects and needs no
  configuration. Its module imports only the standard library and
  `packages.common` (importing it also loads the `services.orchestrator`
  package init and what that pulls in); other scripts already import
  `services.*`; it imports from the download job's interpreter on node-27
  in 0.05 s.
- **Measured on the live store before writing this** (read-only scan on
  node-27, 2026-11-25 UTC, 29 cycles × 2 sources of the 14-day window): every
  cycle is `ready` for both sources under the new rule, exactly where the old
  rule says "manifest exists"; the directory spelling in use (`gfs`, `IFS`)
  and the manifest URI prefix are accepted; the old scan takes 0.004 s, the
  readiness scan 0.8 s. Deploying the change therefore selects the same
  cycle as today on a healthy chain.
- **The chain start is unchanged**: the oldest cycle in the lookback window
  that has a manifest for any selected source, or the explicit
  `NODE27_DOWNLOAD_CONTINUITY_START_TIME`. Raw retention removes whole cycle
  directories, manifest included, so it does not create "manifest without
  files" inside the window.
- **Latest allowed cycle first.** It is stateless and it removes the
  starvation: an older cycle that can never complete no longer keeps the
  newest cycle from being downloaded. The cost is that catch-up after an
  outage downloads the newest cycle before the older ones.
- **No retry cap and no age cap here.** A retry cap needs state across
  passes; an age cap needs an upstream-retention figure nobody has measured.
  Both belong with #3237, which reworks the same selection for per-source
  runs and delay.
- **The whole window is scanned on every pass** (no stop at the first gap),
  so the summary can name every not-ready cycle. At 0.8 s per pass against a
  30-minute timer this needs no caching.
- **A readiness call that raises counts as not ready** with reason
  `readiness_error`, so one unreadable directory cannot fail the pass before
  it selects anything.
- **The manifest URI prefix is the producer's.** The probe passes the
  `OBJECT_STORE_PREFIX` value the adapters of the same pass write manifests
  with (the environment value, empty when unset), not the readiness
  function's default. The selector asks whether its own download finished;
  it does not check that producer and consumer agree on the prefix.
- **One selection per pass.** One function returns the selected cycle and
  the not-ready list; the pass calls it once, so the list always belongs to
  the selection that was made.
- **Sources are not skipped individually.** A pass still runs every selected
  source for the selected cycle, as today. Per-source selection is #3237.

## Known limits

- **A re-drive of an older cycle can hold the job for a long time.** An
  older cycle the upstream no longer serves is retried whenever the latest
  allowed cycle is ready. Sources run one after the other, each with its own
  6 h subprocess timeout, and the GFS adapter polls a missing file for up to
  6 h, so one such pass can take up to 12 h with the job lock held; a newly
  allowed cycle waits for it. This repeats until the cycle leaves the
  lookback window (14 days by default). The summary names the cycle on every
  pass. Before this change the same cycle was either never retried (manifest
  present) or blocked every newer cycle for the whole window (no manifest).
- **The way past such a cycle is `NODE27_DOWNLOAD_CONTINUITY_START_TIME`**,
  which moves the chain start. It ignores the lookback window, so it has to
  be unset again once the cycle has left the window; left in place, cycles
  after it that retention later removes would be selected again.
- **A re-drive runs every selected source, ready ones included.**
  - The IFS command asks the upstream for the cycle before it looks at local
    files and fails when the upstream no longer lists it, so a re-drive of an
    old cycle can end with a failure for IFS although IFS is ready locally.
  - The GFS adapter trusts existing files only when the cycle's database row
    says the raw download completed; otherwise it downloads every file again,
    and a failed fetch removes the file it was replacing.
  Both are existing adapter behaviour that this change reaches more often.
- The predicate cannot tell a truncated file from a complete one: the
  manifests carry no expected sizes. This is the scheduler's rule too.
- A retry cap or a time budget for older-cycle re-drives would bound the
  first limit; both belong with #3237.

## Impact

- Code: `scripts/node27_download_cycles.py`.
- Tests: `tests/test_node27_download_cycles.py`. Three existing tests write
  empty manifests and need real fixtures.
- Docs: `infra/env/README.md`, `infra/env/node27-download.example`.
- Deploy: `git pull --ff-only` on node-27; the timer picks the change up on
  its next pass. The compute nodes do not run this script.
