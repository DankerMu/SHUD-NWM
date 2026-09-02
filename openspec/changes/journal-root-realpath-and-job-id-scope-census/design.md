# Design

Line numbers reference `services/orchestrator/file_orchestration_journal.py`
at `origin/master` `6ca2e0b3` unless another file is named. Symbol names are
authoritative; a line number is a locator only.

## Risk triage

```text
Issue type: deployment-trap hardening + docs (#1943) · read-only ops tooling + runbook +
  live receipt (#1944); one PR, two issues, one shared root seam
Project profile: NHMS (openspec/project-profile.md)
Blast radius: medium — a raise added at the db-free scheduler's repository factory (every
  db-free start passes it); a new CLI that reads the production journal root on node-22;
  the demotion CLI's root check re-routed; no journal writer or reader changed
Fixture level: expanded
Upstream suggested level: absent (follow-ups filed by hand from PR #1939's review) —
  expanded is mandatory: `cli`, `symlink`/`path`, and legacy on-disk compatibility all
  trigger; a live production root is read
Repair intensity: high — construction seam on the db-free main lane; a read-only invariant
  over a tree the live scheduler writes between timer ticks
Why:
- #1943's failure today is silent-then-misleading (preflight PASS, blocked rows, no
  "symlink" in the text); the fix moves it to a typed start-time refusal — a new
  fail-closed path on the production lane that must pass node-22's real root
- #1944's census must count the ONE row shape that aborts the reconcile scan — a
  segment-resident row with an anchor and no flat direct — which a flat-only scan
  misses; and it must not itself take the reconcile path, which locks, migrates,
  restores and prunes
- the receipt is produced on node-22 under the pre-maintenance-window interpreter rule
  from a checkout that predates the gate — the census window — so the method itself is
  part of the deliverable
Selected risk packs: File IO/path safety; Legacy compatibility; Error handling/rollback/
  partial outputs; Security/authz (light)
Not selected: Concurrency/shared state (no lock held; runs between ticks; torn read fails
  loud and is rerun); Resource limits/discovery (existing budgets reused, overridable)
OpenSpec change: journal-root-realpath-and-job-id-scope-census
Evidence floor: tasks.md §0
```

## D1 — #1943: one journal-root authority seam, verified at construction

### Today

`FileOrchestrationJournalRepository.__init__` (`:1076`) keeps the configured
root as `Path(journal_root)`; the db-free factory
`_db_free_orchestration_repository_from_config`
(`services/orchestrator/scheduler_core.py:44-49`) passes
`str(config.scheduler_journal_root)` straight through and is the only place a
db-free `ProductionScheduler` obtains its repository (`scheduler_core.py:111`,
reached from `from_env` via `cls(config=config)` at `:186` after both db-free
preflights). Every hardened read then walks from the filesystem anchor with
`O_NOFOLLOW` per component (`packages/common/safe_fs.py:849`
`_open_directory_no_follow` → `_open_child_dir`, ENOTDIR on darwin, ELOOP on
Linux), so a symlink in any ancestor of the root makes every read a blocked
row: `file_journal_unsafe_scanned_entry` on the cross-model lane
(`model_id=None`, scanned-entry discipline) and `file_journal_unreadable` on
the model-scoped lane. `_db_free_path_check`
(`scheduler_config/db_free.py:221`) rejects only a symlinked leaf (`:325`) or
direct parent (`:305-320`); the realpath it computes (`:283`) is used for
containment comparison and evidence only. So the config passes preflight and
the operator meets a token that says nothing about symlinks. Reproduced
locally with a `tempfile` root under macOS `/var` (alias of `/private/var`):
preflight blocker `None`; alias reads blocked with the two tokens above;
`verify_directory_no_follow` on the alias →
`SafeFilesystemError: Path component is not a directory: /var/…/journal`;
the realpath root reads legally and verifies to itself.

Three sibling lanes disagree today: `operator_reserved_demotion.py:71-82`
asserts the root through `verify_directory_no_follow` and raises
`OrchestratorError("FILE_JOURNAL_INVALID_ROOT", "journal root failed safe
filesystem verification", {"error_type"})`; `file_orchestration_migration.py:738`
hands `Path(journal_root).expanduser().resolve()` to its child process; the
scheduler main lane does neither.

### Change

- New module `services/orchestrator/journal_root_authority.py`:

  ```python
  JOURNAL_ROOT_INVALID_MESSAGE = (
      "journal root failed safe filesystem verification: every path component of "
      "the configured journal root must be a real directory, none a symlink; "
      "set it to the realpath (readlink -f)"
  )

  def verify_journal_root_authority(journal_root: str | Path, *, setting: str) -> Path:
      try:
          return verify_directory_no_follow(Path(journal_root))
      except (OSError, SafeFilesystemError) as error:
          raise OrchestratorError(
              "FILE_JOURNAL_INVALID_ROOT",
              JOURNAL_ROOT_INVALID_MESSAGE,
              {"error_type": type(error).__name__, "journal_root": str(journal_root), "setting": setting},
          ) from error
  ```

  The two callers set different knobs — the scheduler reads
  `NHMS_SCHEDULER_JOURNAL_ROOT`, the demotion CLI takes `--journal-root` — so
  the constant message speaks of "the configured journal root" and the
  setting name travels in `details["setting"]`; the runbook, not the message,
  names the environment variable.

  ```python
  ```

  The returned path is what `verify_directory_no_follow` returns: tilde-expanded,
  **not** resolved — the same authority location the demotion lane already
  uses for its repository, so a root that is a realpath verifies to itself and
  a literal `~` expands once. The message is constant: no path, no traceback,
  no module name (the configured value rides in `details`), which is what the
  demotion CLI's leak assertions already require of this error.
- `_db_free_orchestration_repository_from_config` calls the helper and
  constructs the repository on the verified path. Callers: `scheduler_core.py:111`
  only. Surfacing: `plan-production` catches `OrchestratorError` and prints
  `code: message` to stderr with exit 1 on both entrypoints — under the node-22
  oneshot unit that is the journal line the operator reads, and the unit fails
  instead of running a pass whose every cycle is a blocked row.
- `operator_reserved_demotion.py:71-82` calls the helper (two callers of one
  seam, no third copy). The migration lane's `.resolve()` stays: a realpath
  handed to the child satisfies the constraint by construction; recorded, not
  changed.
- Strictness: the helper raises for a missing root as the demotion lane does
  (`FileNotFoundError` is an `OSError`). **Corrected during implementation**
  (implementer deviation 1): the fixture's original premise — "the db-free
  preflight blocks a missing root before the factory runs" — was wrong in one
  respect. `from_env`'s two preflight-blocked branches (`scheduler_core.py:186`
  and `:202`) passed `active_repository=None`, which `__init__` reads as "not
  supplied" and answers by calling the factory — so a blocked pass still
  constructed a repository from the very value the preflight had rejected
  (harmless before: a dead repository with root `Path("None")`; with the
  verification in the factory it would raise, and the `missing` shape would
  surface as a bare `TypeError`, breaking #1627's redacted-blocker contract —
  six `test_production_scheduler` cases proved it). Fix: a module-level
  sentinel `_DB_FREE_REPOSITORY_BLOCKED` that only those two branches pass,
  checked before the existing `is not None` chain; `active_repository=None`
  keeps its meaning for every other constructor caller (396 test-side
  constructions audited, 168 without an injected repository, zero failures).
  A blocked pass now carries `active_repository=None` instead of a dead
  repository; `scheduler_runtime` early-returns on blocked passes (`:616`,
  `:661`, `:701`) before its first repository read (`:744`), so nothing
  observes the difference. Task 1.4b is the audit; its expectation held for
  this corrected reason.
- `_db_free_path_check` is untouched: #1627 is adjudicating that lane's
  realpath/ENOENT family (`runtime-evidence-and-operations` requirement
  "DB-free scheduler config path adjudication survives symlink loops"), and the
  issue scopes any change there to that ruling. The factory seam is
  downstream of preflight and independent of it.

### Docs

The constraint is stated where the value is set and where it is read
(tasks 1.5). The runbook subsection gives the symptom triad (preflight PASS ·
blocked rows every cycle · a diagnostic that says "not a directory" / ELOOP),
the one-line check (`readlink -f` equals the configured value) and the
remedy; since this PR the scheduler refuses to start with
`FILE_JOURNAL_INVALID_ROOT` instead.

## D2 — #1944: the census reuses the gate's predicate and reads every surface without the reconcile path

### What must be counted

The gate `_require_job_id_cycle_scope` (`:9558`) derives
`_cycle_scope_from_job_id(payload["job_id"])` (`:12588`; `None` for an
unrecognised shape → pass) and compares it, normalised, with the row's own
`(_source_id_from_job, format_cycle_time(_cycle_time_from_job))`; mismatch →
`FileOrchestrationJournalError("file_journal_job_id_scope_mismatch",
field="job_id", evidence={"expected", "actual"})`. A row that trips it is
"divergent". The census calls **this method** per row with
`{"payload": row}` and `record_type="pipeline_job"` and classifies on the
reason token — no second implementation of the comparison exists in the
module (a second implementation is the "census 0 / gate fires" false green
#1944 names; the test pins it by neutering `_cycle_scope_from_job_id` and
watching the count drop to zero).

The reconcile-abort shape (consequence (a)) is a row that exists in journal
segments (or a latest view) with an anchor in `reconcile-inventory/` and no
flat direct file; `_iter_reconcile_inventory_records` (`:7020`) then calls
`_restore_derived_master_direct_unlocked`, whose outgoing-record validation
trips the gate inside a generator with no `try`, so the scan stops before
later anchors and before the anchor prune at `:7064`. A flat-only scan counts
zero of these.

### Surfaces and their readers

| surface | reader | writes? | why this one |
|---|---|---|---|
| `flat_direct` | `_iter_direct_pipeline_job_records` (`:6471`): `_iter_regular_json_files(root/"pipeline-jobs")` non-recursive → `_read_optional_json` → `_validated_direct_pipeline_job_record` | no (read cache only) | the presence set for the abort cross-match; validation fails loud on a malformed file (#1820's lane, not softened) |
| `by_cycle_direct` | `_iter_regular_json_files(root/"pipeline-jobs"/"by-cycle", recursive=True)` + the same reader/validator per file | no | the canonical lookup (`_canonical_reconcile_job_unlocked` `:8403`) reads it; a row can live here without a flat file |
| `journal_replay` | `_iter_pipeline_job_records(include_direct=False)` (`:6667` → `_replay_all_pipeline_job_records` `:6680`: `latest/**.json` views + `journal/**.jsonl` segments through `_apply_journal_record`) | no (lane tag + read cache) | where the segment-only row lives; `_validate_pipeline_job_identity` checks payload against path, not id against payload, so a divergent row replays cleanly and is seen |
| `reconcile_inventory` | `list_directory_no_follow_limited(root/"reconcile-inventory", max_entries=max_files, containment_root=root)`; the over-limit sentinel and a name that is neither a safe `.json` nor residue fail loud exactly as the journal's own listing does (`:7775-7795`); `_RECONCILE_INVENTORY_TEMP_RE` names counted as residue; `.json` → `_read_optional_json` → `_validated_reconcile_inventory_anchor(anchor, expected_job_id=stem)` (`:8356`, pure); the anchor mapping carries `job_id`/`source_id`/`cycle_time` written from the row's own pair (`:8494-8501`), so it is classified by the same gate call and an anchor of a divergent row is itself divergent — counted under this surface, never listed in a row's `surfaces` | no | **never** `_iter_reconcile_inventory_records` (`_write_lock`, inventory lock, `_locked_cycle_write` per anchor — a cycle flock against the live scheduler — then restore + prune), never `_iter_reconcile_pipeline_job_records` (runs `_ensure_reconcile_inventory_migrated`), never `_reconcile_inventory_entry_names_unlocked` (`:7761`, deletes `.tmp` residue) |
| `active_reconcile` | `_LEGACY_ACTIVE_RECONCILE_DIRECTORY` (`:428`): absent → `present: false`; present → `.json` entries through `_read_optional_json` + `_validated_direct_pipeline_job_record`, which is how `_canonical_reconcile_job_unlocked` (`:8443-8449`) reads that directory | no | the canonical lookup reads it; node-22 has no such directory today, the receipt must say so rather than omit it |

The repository is constructed through `verify_journal_root_authority` (D1),
so an alias root refuses typed before any read. `max_files` / `max_records`
overrides are exposed because the replay charges every latest, segment and
direct record against one `_RecordBudget` (`:6682`, default
`MAX_FILE_JOURNAL_RECORDS = 100_000`) and `latest/` alone holds 5,998 files on
node-22; a trip fails loud (`code: message`, exit 1), and the override is the
documented way past it.

### Receipt

`schema_version: "nhms.scheduler.job_id_scope_census.v1"`, `generated_at`,
`journal_root` (configured), `journal_root_verified`, `limits`, `surfaces`
(per surface: `present`, `files`, `rows`, `divergent`; the inventory adds
`residue`; the replay adds `latest_files` and `segment_files`),
`divergent_rows` (one entry per unique `job_id`, sorted: `surfaces` — the
row-bearing surfaces only, anchors never among them — `own_scope`,
`job_id_scope`, `anchor_present`, `flat_direct_present`, `by_cycle_present`,
`journal_present`, `reconcile_abort_trigger = anchor_present and not
flat_direct_present`; an anchor-only id has `surfaces == []`),
`divergent_total` (unique ids, anchor-only ids included),
`reconcile_abort_triggers`, `exit_code`. Exit 0 / 2 / 1 (none / found /
typed error; `OrchestratorError` prints `error_code: message`,
`FileOrchestrationJournalError` prints `reason: field` — a deliberate
departure from `cli.py:657`/`:948`'s `str(error)` + exit 2, because 2 is
"found" here). `--output` refuses a path whose realpath lies under the
verified root — the receipt must not become the first byte the census writes
into the tree.

### Zero-write proof

Files-only byte snapshots miss exactly the paths that would slip: residue
removal and directory creation. The test snapshots every directory and file
(`os.walk(followlinks=False)`, relative path, kind, bytes) before and after on
the divergent tree, and separately monkeypatches the four repository entry
points named above to raise if reached.

## D3 — #1944: recovery is manual, anchor-inclusive, and count-independent

The runbook subsection (tasks 3.1) is written before the node-22 receipt and
does not depend on its count. It states: what a divergent row is; the census
command in node-22 form and its exit codes; consequence (a); consequence (b)
with the verifier's correction recorded on #1944 — a terminal row
(`succeeded` / `failed` / `cancelled`) short-circuits toward ordinary targets
but enters the gate toward `partially_failed` / `permanently_failed`
(`terminal_guarded`'s second conjunct), so **neither terminal nor
non-terminal divergent rows can be retired through the API**, and the
production path that hits it is `FileJournalRetryService.mark_permanently_failed`
on auto-retry's decline (`retry.py:416`, `chain_forecast_orchestrator_cycle.py:294`)
plus `chain_array_accounting.py:487`'s `partially_failed` aggregation; and the
recovery — timer stopped, files backed up, the row's flat and by-cycle direct
files **and** its anchor deleted together (an anchor left behind re-aborts the
next scan at the same place), census rerun, timer restarted. A
segment-resident row is append-only history: after its anchor and direct
files are gone it no longer feeds the reconcile scan, cannot be transitioned,
and keeps appearing in the census under `journal_replay` with
`reconcile_abort_trigger: false`; that is the accepted end state, and
rewriting segments to retire it is out of scope.

If the receipt is non-zero, tasks §5 adds a guarded command whose only action
is that deletion (dry-run default, `--apply`, `unlink_no_follow` with
`containment_root`), and §8.11 prefers it over hand deletion. If zero, §5 is
not built and the PR says so.

## D4 — node-22 receipt method

node-22's active checkout is `3acea778` (pre-#1939; the gate is not live
there), Python 3.12.7 in a shared `.venv` that must not be rebuilt before the
maintenance window. The census therefore runs from a **detached worktree** of
the pushed branch (`git worktree add --detach /scratch/frd_muziyao/nhms-census-<sha> <sha>`)
with the active interpreter and `-m`, cwd inside the worktree so the worktree
shadows the checkout (verified by printing
`file_orchestration_journal.__file__` first — a module both SHAs have, so the
check proves shadowing; an import failure of the new module against the
3.12.7 `.venv` is fail-closed and reported, never repaired with `uv sync`). `uv run --no-sync` is not used: outside a project it has no effect
and picks an arbitrary interpreter. The live checkout is not pulled — pulling
would activate the gate before the census, defeating the census window. Run
between timer ticks; the receipt records timer/service state, root and
`readlink -f` equality, worktree SHA, interpreter path and version, UTC time
and every per-surface count; the worktree is removed afterwards. A torn
segment read fails loud and is rerun. A non-scope rejection of a legitimately
old row is a finding to route, never a skip to add.

## Invariant Matrix

```text
Governing invariant: The db-free scheduler's journal root is verified as a chain of real
  directories at construction, through one seam shared with the operator demotion lane,
  and refused with a typed message that names the remedy; and a legacy row whose job id
  contradicts its own scope is observable read-only on every surface the reconcile scan
  and the canonical lookup read — by the gate's own predicate, with zero bytes written and
  no reconcile lock or repair path entered — with the anchor-present / flat-direct-missing
  combination named as the reconcile-abort trigger.

| # | Invariant | Where enforced | Test / evidence | Status |
|---|---|---|---|---|
| 1 | Alias-ancestor root passes `_db_free_path_check` but `from_env` raises `FILE_JOURNAL_INVALID_ROOT` | `verify_journal_root_authority` in the factory | root-authority test: alias root (both the preflight pin and the raise) | new |
| 2 | Realpath root at node-22 depth constructs; `active_repository.root` is the verified path | same | six-real-components case; node-22 receipt (the census constructs through the same seam on the live root) | new + live |
| 3 | The message names the remedy (`readlink -f`, real directories), carries no path/traceback/module; `details` carries the setting name per caller | `JOURNAL_ROOT_INVALID_MESSAGE` constant + `setting` kwarg | CLI test stderr exact line; `details["setting"]` per lane; demotion leak assertions | new |
| 4 | Demotion and scheduler use one seam | import identity | `is` assertion across modules; grep: no second `verify_directory_no_follow` + `FILE_JOURNAL_INVALID_ROOT` pair | new |
| 4b | A preflight-blocked db-free pass builds no repository and never verifies the rejected root (missing / symlink-leaf / symlink-loop shapes) | `_DB_FREE_REPOSITORY_BLOCKED` sentinel in `from_env` + `__init__` | `test_preflight_blocked_db_free_pass_builds_no_repository`; the two `from_env` blocked branches are the only producers of the sentinel | new (deviation 1) |
| 4c | Symlink-leaf and symlink-loop roots are refused by the factory seam itself, independent of the preflight that also catches them | `verify_journal_root_authority` | factory-level test with the preflight verdict asserted `blocked` alongside | new (deviation 2) |
| 5 | `_db_free_path_check`, `safe_fs`, `file_orchestration_journal.py` unchanged | no diff | `git diff origin/master` empty on those files | pinned |
| 6 | Census classifies with `_require_job_id_cycle_scope` only | module has no second comparison | predicate-reuse pin (neutered `_cycle_scope_from_job_id` → 0) | new |
| 7 | A segment-only divergent row with an anchor is counted and flagged as the abort trigger | `journal_replay` + `reconcile_inventory` cross-match | segment-only test (`surfaces == ["journal_replay"]`, `reconcile_abort_trigger`) | new |
| 8 | One `job_id` on several surfaces counts once | dedup by `job_id` | flat + by-cycle + replay test | new |
| 9 | Anchor-only divergent id counts once with `surfaces == []`, under the inventory surface, and triggers | anchor classification | anchor-only test | new |
| 9b | An inventory entry that is neither a safe anchor name nor residue fails the census loud, as the journal's own listing would | mirrored listing rules | invalid-name test, exit 1 | new |
| 10 | Zero bytes written; no reconcile lock/migration/restore/prune path entered | readers chosen in D2 | dir+file snapshot equality; four monkeypatched entry points never reached; `.tmp` residue survives | new |
| 11 | Alias root refuses the census typed; `--output` inside the root refused with nothing written | D1 seam; realpath containment on `--output` | CLI tests | new |
| 12 | `active_reconcile` and `reconcile_inventory` absence is reported, not omitted | `present: false` | legal-tree test without the directories | new |
| 13 | Exit codes 0/2/1 | CLI | CLI tests, both entrypoints | new |
| 14 | node-22 receipt produced read-only from a detached worktree with the active interpreter; the live checkout's working tree and HEAD unchanged (`fetch`/`worktree add` touch `.git/` only), `.venv` untouched, worktree removed | D4 procedure | receipt fields + transcript; `git worktree list` clean; HEAD still `3acea778`; `git status --porcelain` unchanged | evidence |
| 15 | The runbook carries the census command in the exact-interpreter form | positive assertion added to `tests/test_node22_entrypoint_invariant.py` (task 3.3; the scanner alone only inspects `uv` lines) | test red without the runbook line, green with it | new |
| 16 | Runbook states both terminal and non-terminal rows cannot be retired via API, and anchor deletion is mandatory | §8.11 text | reviewer reads against the #1944 comment | new |
```

## Boundary-surface checklist

- Shared helper roots: new `journal_root_authority.py` (two callers), new
  `journal_scope_census.py` (CLI only). `file_orchestration_journal.py`,
  `safe_fs.py`, `db_free.py` untouched.
- Public entrypoints: `plan-production` gains a typed start-time failure for
  an invalid root (was a per-read blocked row); `demote-reserved-job` keeps its
  code, changes its message; new `census-job-id-scope`.
- Read surfaces: the five census surfaces (D2 table).
- Write/delete/overwrite surfaces: none (census); `--output` outside the root
  only. §5, if built, deletes exactly three files per id.
- Staging/publish/rollback: none.
- Producer/consumer evidence boundaries: the census receipt schema; node-22
  receipt in `.workplans/` and the PR.
- Stale-state/idempotency boundaries: the census is idempotent and lock-free;
  a run concurrent with a scheduler pass may fail loud on a torn read and is
  rerun.
- Unchanged downstream consumers: every journal reader/writer; the reconcile
  scan's propagation; `scheduler_runtime.py:1555-1595`.

## Review focus

1. D2: is every anchor read lock-free and pure — no
   `_iter_reconcile_inventory_records`, `_iter_reconcile_pipeline_job_records`,
   `_ensure_reconcile_inventory_migrated`, `_reconcile_inventory_entry_names_unlocked`?
   Is `.tmp` residue reported and left alone?
2. D2: does the module compare scopes anywhere other than through
   `_require_job_id_cycle_scope`? (Any `_cycle_scope_from_job_id` call used
   for a decision is a second implementation.)
3. Row 7: does the segment-only fixture really leave no flat/by-cycle file
   for the rewritten id, and is the anchor written in the exact shape
   `_sync_reconcile_inventory_for_row_unlocked` writes?
4. D1: does the factory raise reach stderr as `code: message` on both
   entrypoints, and does node-22's real root (six real components) pass —
   including a trailing-slash or `~` spelling, which the helper expands but
   must not resolve?
5. D3: does the runbook say both terminal and non-terminal divergent rows are
   API-unretirable and that the anchor must go with the direct files?
6. D4: is the node-22 procedure free of any `uv` invocation and any write to
   the live checkout or `.venv`?
