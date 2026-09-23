# Design — provider-mode test hygiene, umask-0002 marker receipt, ACL-mask ruling, journal-root convergence

## D1 — #2403: the backfill test creates both provider-gated paths through `tests.provider_mode_helpers`

In `test_db_free_unreadable_index_is_not_memoized_and_resolves_once_repaired` (`tests/test_scheduler_backfill.py`, locate by name): replace `object_root.mkdir(parents=True, exist_ok=True)` with `make_directory_with_explicit_mode(object_root)` (it pins every component it creates, including the lock parent `root`, to `0o755`), and `index_path.write_text("{not json", encoding="utf-8")` with `write_provider_destination(index_path, "{not json")` (pins the destination to `SHARED_PROVIDER_MODE`). Both, because fixing only the directory moves the failure to the destination-mode gate. The sibling `test_db_free_never_published_index_is_quiet_no_lineage` publishes nothing and takes no lock — out of scope per the issue. `git diff` on `packages/common/provider_atomic.py` and `packages/common/safe_fs.py` MUST be comment-only or empty (#2403 AC3, #1513 D3).

Evidence: RED on node-27 at master under `umask 002` (`provider_lock_parent_unsafe`) and GREEN at the PR head under both `umask 002` and `umask 022`.

## D2 — #1632: the marker suites of the provider_atomic closure, measured once under `umask 0002`

File set = the output of `receipts/provider_atomic_closure.py` (AST reverse-import closure of `packages/common/provider_atomic.py`; edges from `import a.b.c`, `from a.b import c` including `c` as a submodule, and relative imports; run `uv run --no-sync python openspec/changes/provider-mode-and-journal-root-convergence/receipts/provider_atomic_closure.py .`). At master `de4d1d182`: 421 modules, 248 test files, **21 marker-carrying files** (listed in the receipt). The first cut listed 17 (a resolver that missed submodule edges); the #1513 baseline counted 9 and recorded no names, so no per-file diff to it is possible.

Opt-ins per file (the conftest markers plus file-level gates):
- `integration` files: `NHMS_RUN_INTEGRATION=1` + `NHMS_INTEGRATION_DATABASE_URL` (disposable scratch PG). The two live-PostGIS files set `NHMS_ENABLE_LIVE_POSTGIS_MVT` themselves via `monkeypatch`.
- `e2e`: `NHMS_RUN_E2E=1`.
- `grib`: `NHMS_RUN_GRIB=1`. pytest does not wire in node-27's ecCodes: it lives in the GRIB env `/home/nwm/nhms-grib`, which `ldconfig` cannot see. The receipt's re-measure section therefore also runs these cases with `LD_LIBRARY_PATH` / `ECCODES_DIR` / `ECCODES_DEFINITION_PATH` injected, following the `scripts/run_qhh_cycle.sbatch` shape.
- `real_disk` (`test_object_store_forcing_real_disk.py`): `NHMS_RUN_REAL_DISK=1`, `DATABASE_URL`, `OBJECT_STORE_ROOT`. It is run in its own invocation, read-only:
  - the `nhms_display_ro` DSN with `statement_timeout`/`lock_timeout`;
  - the test's own `NHMS_SERVICE_ROLE=display_readonly`;
  - the NFS object store `/home/ghdc/nwm/object-store`.
- `test_basins_registry_import_db.py`'s real smoke: `NHMS_RUN_REAL_BASINS_IMPORT=1` plus `data/Basins` under the cwd. Not enabled, because it is a full basin import. Recorded as not measured.

Run (orchestrator, node-27):
- isolated detached worktree; disposable scratch PG from the `nhms-db` image;
- explicit `umask 002`, echoed in the log;
- one pytest invocation per file, with `-rs`.

Receipt:
- Per file: passed / failed / skipped, with each skip's reason.
- A skip, or a failure that is identical under `umask 022`, is **"not measured on node-27, reason X"** and is never counted as green.
- A failure caused by a provider gate (`provider_lock_parent_unsafe`, `provider_destination_access_invalid`) is fixed in this PR the same way as D1.
- Any other failure is recorded and routed.

Static census alongside: which files reference a provider publish or lock entry.

## D3 — #1631 ruling: deliberate — keep the gate and the `0o755` pin; the tension is recorded with a trigger

Where the tension bites, exactly: a directory that is BOTH (a) a provider lock's direct parent (`provider_lock_path(dest).parent` — the gate at `provider_atomic.py` refusing any `0o022` bit or a foreign owner) AND (b) a cross-uid share that depends on a default-ACL mask. No mode-bit policy satisfies both. Directories that are only (b) are served today by mask-preserving creation: `run_tree_copyback.py`'s bare `mkdir` (documented "ACL-mask-preserving BY CONSTRUCTION") and `state_manager._ensure_copyback_state_parent` (safe_fs create, then `chmod 0o775` on the components it created). The latter's directories receive `atomic_write_bytes_no_follow` state objects, not provider publishes, so they are not lock parents — consistent with the spec scenario "a state copyback parent keeps its explicit shared mode". The `0o755` pin (`safe_fs.ensure_directory_no_follow`) clamps a mask only on directories safe_fs creates.

Inert today on two external facts, re-verified on node-27 2026-09-23T13:09:24Z (`receipts/2026-09-23-issue-1631-acl-recheck.txt`, command below): (1) zero `nwm`-owned entries under `object-store/forcing`, `runs`, `states` although each carries `user:nwm:rwx` / `default:user:nwm:rwx` (node-22 is the sole writer, node-27 reads); (2) on node-27, groups `nwm` (1005) and `nfsdata` (1078) have no supplementary members. Their only primary-gid users are `nwm` (1005) and `frd_muziyao` (1078), who is the owner of the shared trees. **Correction, measured 2026-09-23T13:41Z (`receipts/2026-09-23-issue-1631-group-membership.txt`):** fact (2) as the issue states it holds on node-27 only. On node-22 — the writer host — gid 1078 is `huser`, with 23 supplementary members and 64 primary-gid users (unrelated HPC accounts; `frd_muziyao` is one of them). A `0o775 → 0o755` clamp there does take group write away from those accounts. But none of them is an NHMS writer: the NHMS writers are `frd_muziyao` (node-22, owner) and `nwm` (node-27, not in gid 1078 on either host). So the clamp tightens access for non-NHMS principals and removes nothing from any NHMS lane. The inert claim is restated in those terms: no NHMS principal loses access.

The one group both NHMS accounts share is gid 1107 `nwmuser` (members `nwm`, `frd_muziyao`), measured 2026-09-23T13:46:56Z (`receipts/2026-09-23-issue-1631-group-1107.txt`):
- It is load-bearing under `object-store/canonical/`. `canonical/gfs` and `canonical/IFS` are `2775 frd_muziyao:nwmuser` (setgid group share), and 290 group-1107 entries sit within depth 4.
- 25 cycle directories there are `755` instead of `2775`: `gfs` 2026091300–2026091900 (12) and `IFS` 2026091300–2026091900 (13), created 2026-09-14 to 2026-09-20. Cycles before and after that window are `2775`. The per-cycle listing and mode histogram are in `receipts/2026-09-23-issue-1631-canonical-cycle-modes.txt` (re-measured read-only on node-27; see its first line for the timestamp). The cause is traced in #2597, and it is NOT the #1631 clamp. The window is the gap cycle that #2100 predicted: before #2100 was deployed, the node-22 producer created mirror directories via `copyback_guard.ensure_traversable_copyback_directory`, which chmods newly created components to `0o755` and so clears setgid. The post-deployment re-sweep, which #2100 made mandatory, was never run. (The safe_fs `0o755` pin cannot produce this shape: under a setgid parent Linux keeps `S_ISGID`, giving `2755`, not `755`.)
- No NHMS lane loses needed access through it. node-27 `nwm` neither writes nor deletes under `canonical/`: the canonical retention lane runs in the system unit `nhms-node27-canonical-retention.service` as the copyback root's owner (`scripts/node27_raw_retention.py` module docstring; `infra/systemd/nhms-node27-raw-retention.service` runs only `raw,precip-cache` as `nwm`), and an owner can delete its own `755` directories. Drift since #1513: `object-store/models/direct_grid_variants` is now `1755 frd_muziyao:nfsdata` (the issue records `1777`) — it no longer admits a second uid by mode at all, so it is not a dual-uid subtree today; recorded, not relied on.

Ruling: no code change. The gate is a fail-closed security property (#1513 D3, spec "the provider lock-parent gate stays fail-closed"); the pin is what makes safe_fs deterministic under a permissive umask. **Reopen trigger**: any of the following.
- A second NHMS uid (node-27 `nwm`, or a new service account) gains a WRITE requirement under an ACL-shared subtree (`forcing/`, `runs/`, `states/`).
- The same uid gains a write requirement under `raw/`. `raw/<src>/<cycle>/` is an in-tree provider lock parent inside the mode-shared `raw/` (777) subtree, and the gate refuses a foreign-owned or group-writable lock parent regardless of group membership.
- An NHMS lane comes to depend on group access through gid 1005, 1078 or 1107. For 1107, the concrete case is node-27 `nwm` taking over any write or delete under `canonical/`, for example the canonical retention lane moving out of the owner's system unit. **Convergence direction when triggered** (preferred first): (i) keep provider DESTINATIONS — and therefore their locks — out of ACL-shared subtrees. The lock path is fixed next to the destination (`provider_lock_path(dest) = dest.with_name('.<name>.lock')`, no override at either consumer), and `_refuse_identical_copyback_lockfiles` in `state_manager.py` depends on that placement. Relocating the lock independently of the destination would therefore be a `provider_atomic.py` change and a cross-process lock-protocol change: during a mixed-version rollout, old and new processes would flock different files and lose mutual exclusion. That is not a no-code option. Keeping destinations out needs no code, and it already holds in the shipped configuration: the `scheduler/{registry,canonical-readiness,state-index}/*-last.json` destinations, `raw/<src>/<cycle>/manifest.json`, and the scheduler-refresh lock/receipt paths all sit outside `forcing/`, `runs/` and `states/`. This "not a lock parent" property holds because of the configured destination URIs, not structurally. (ii) for non-lock directories that must carry the mask, create with the `_ensure_copyback_state_parent` pattern (safe_fs create, then restore mode on created components). Never relax the gate. Code: one comment each at the gate and at the pin citing #1631 and this ruling; `git diff` on those files comment-only. Re-check command (read-only):

```bash
ssh -p 32099 nwm@210.77.77.27 'getent group nwm nfsdata; getent passwd | awk -F: '"'"'$4==1005||$4==1078'"'"'; for d in forcing runs states; do echo -n "$d: "; find /home/ghdc/nwm/object-store/$d -user nwm -print -quit; echo; done; stat -c "%a %U:%G %n" /home/ghdc/nwm/object-store/raw /home/ghdc/nwm/object-store/models/direct_grid_variants; for d in forcing runs states; do getfacl -p /home/ghdc/nwm/object-store/$d | grep -E "nwm|mask"; done'
```

plus `find /home/ghdc/nwm/object-store -maxdepth 4 -group 1107 -printf "%m %u:%g %p\n" | head` on node-27, and on the writer host: `ssh -p 32099 frd_muziyao@210.77.77.22 'getent group 1078 1005; getent passwd | awk -F: '"'"'$4==1078||$4==1005'"'"' | wc -l'`.

## D4 — #2384 ruling: converge — one journal-root decision, the retention vocabulary kept as a translation

Why converge rather than declare a deliberate second authority: the two authorities already DIFFER (authority #2 leaks a bare `RuntimeError` for `~nosuchuser/...` out of `config_from_env` — whose only handler catches `RetentionFailure` — and out of `verify_and_restore`, whose script caller catches only `(RetentionFailure, ValueError)`), and any future journal-root hardening would have to be made twice. The obstacle the issue names — the reason strings are a receipt contract (`preflight_blocked` stdout JSON; `tests/test_scheduler_journal_retention_archive.py` pins `journal_root_unavailable`) — is removed by translating, not by changing the vocabulary.

Shape. A retention-module adapter (e.g. `_verified_journal_root(value) -> Path`) used at BOTH journal-root sites — `config_from_env` (`scheduler_journal_retention.py`, today `_safe_existing_directory(journal_raw, field="journal_root")`) and `verify_and_restore` (`scheduler_journal_restore.py`, today `_safe_existing_directory(journal_root, field="journal_root")`):

1. Accept/reject is decided ONLY by `verify_journal_root_authority(value, setting=...)`, and on success the adapter returns that call's path. The path is `Path(value).expanduser()`, unresolved, identical to what `_safe_existing_directory` returned, so `_path_under`, the allowed-roots check and the archive-overlap check are unchanged. `setting` names the knob actually read:
   - `--journal-root` when `args.journal_root` was given on the retention side, and always for restore (the `verify-restore` subcommand takes `--journal-root`);
   - `NHMS_SCHEDULER_JOURNAL_ROOT` otherwise.
   It survives in the chained `__cause__`.
2. On its `OrchestratorError("FILE_JOURNAL_INVALID_ROOT")`, translate to `RetentionFailure` without re-deciding: `error_type in {"UnexpandableJournalRoot", "RelativeJournalRoot"}` → `journal_root_not_absolute`; otherwise label from one `lstat` of the configured (expanded) path — `lstat` raises → `journal_root_unavailable`; symlink → `journal_root_symlink`; not a directory → `journal_root_not_directory`; else (an unsafe ancestor, an I/O refusal on a component) → `journal_root_unsafe`. The label step cannot turn a refusal into acceptance; it only names it. This reproduces today's reason for every shape `_safe_existing_directory` could produce (missing → unavailable, leaf symlink → symlink, file → not_directory, symlinked ancestor → unsafe, relative → not_absolute) — pinned by a parametrized parity test run BEFORE and AFTER the change.
3. Chain with `OrchestratorError` (`raise RetentionFailure(...) from error`).

`_safe_existing_directory` stays for its other fields (`evidence_root`, `allowed_root`, restore's `archive_root`) and `_safe_archive_root` stays for retention's archive root; both gain `except RuntimeError` around their `expanduser()` → `f"{field}_not_absolute"` / `archive_root_not_absolute` (the same defect class; decides the issue's "who owns `:121`"). `restore.py` already has a typed stage vocabulary (`RetentionFailure("stage_root_not_absolute")`), so its live bare `expanduser()` (the site that takes the raw `stage_root`; the other site receives an already-expanded path) gains the same catch → `stage_root_not_absolute`. Every new `except RuntimeError` wraps ONLY the `Path(value).expanduser()` call. `RetentionFailure` and `SafeFilesystemError` both subclass `RuntimeError`, so a wider `try` would relabel `*_symlink` / `*_unsafe` refusals as `*_not_absolute`.

New reason strings: none. `~nosuchuser/...` becomes `journal_root_not_absolute` (an unexpandable `~` root is not an absolute path; matches authority #1's grouping of Unexpandable with Relative).

Detection successor (replaces the archived matrix row 4 grep and the refined grep of `openspec/changes/archive/2026-09-15-journal-root-lane-adoption-and-full-tree-budget-contract/design.md:230-272`): `grep -rn '_safe_existing_directory(' services scripts | grep 'field="journal_root"'` → **zero hits** after this change, and `grep -rn 'verify_journal_root_authority(' services scripts` lists the retention adapter among the consumers. The issue's dependency note ("#1955 not yet on master") is stale — that change is archived.

Tests (existing modules `tests/test_scheduler_journal_retention_archive.py` / `_planning.py`; no new module):
- `~nosuchuser_zz/...` `--journal-root`: retention (`config_from_env` → `blockers == ["journal_root_not_absolute"]`, and through the script `main` → exit 2 `preflight_blocked` JSON) and restore (`verify-restore` subcommand → exit 2 `{"reason": "journal_root_not_absolute", ...}`); RED before (bare `RuntimeError`).
- Parity: parametrized shapes, each giving the same reason before and after at both entries: missing, deep missing, leaf symlink, dangling symlink, regular file, path under a file, symlinked ancestor, `..` component, relative, and a permission-denied ancestor (skipped when running as root). The reviewer's macOS probe found the adapter and `_safe_existing_directory` agree on 21 shapes, differing only on `~nosuchuser`; node-27 (Linux) closes the platform question in 5.3. The retention cases set a valid `NHMS_SCHEDULER_ALLOWED_ROOTS`, because `config_from_env` stops at `nhms_scheduler_allowed_roots_missing` first.
- `--stage-root ~nosuchuser_zz/x` on `verify-restore` → exit 2 `{"reason": "stage_root_not_absolute", ...}` (RED before).
- Delegation pin: monkeypatch `verify_journal_root_authority` in the retention module to refuse a valid root → retention reports a `journal_root_*` blocker (proves the decision is the authority's).
- The existing `journal_root_unavailable` pin unchanged.

## Stated consequences (PR body)

- #1632 is a receipt: skips are "not measured, reason", not green. The earlier node-27 oracle runs (G1/G2, ~12k passes) ran under an implicit `umask 0002` for unmarked suites — supporting evidence, not this receipt.
- #1631 stays inert only while the two facts hold; the re-check command is in D3.
- #2384 adds a `lstat` after a refusal (diagnostic only) — one extra syscall on an already-failing path.

## PR-body checklist

- #1631 ruling (D3) and #2384 ruling (D4) — written before code, per the batch instruction.
- #1632 per-file receipt table.
- #2403 RED (umask 002, master) / GREEN (002 and 022, head).
- Out-of-scope findings:
  - 20 test files call a provider publish entry without importing `provider_mode_helpers` (census candidates, not verified).
  - `direct_grid_variants` mode has drifted.
  - 25 `755` cycle directories sit inside the `2775` `canonical/` group share.
  - node-27's pytest does not wire in its GRIB env (`/home/nwm/nhms-grib`). Once it is wired in, the 8 GRIB cases decode but fail on a fixture/decoder mismatch (#2594).
  - The `test_object_store_forcing_real_disk.py` fixture cycle is past retention.
  - `services/orchestrator/retention.py` `_sanitize_root_candidate` calls bare `expanduser()`.
