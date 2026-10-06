# The provision step checks the parent of its output registry before it does anything

Issue: #2753 (found in the 2026-10-06 tailanhe rehearsal; a gap of #2737). Fixture level: **compact**.
Risk packs: **Ordering fail-closed**.

## Why

`scripts/provision_direct_grid_scheduler_registry.py` publishes `--output-registry` through
`publish_scheduler_registry_manifest`, whose destination lock (`_provider_destination_file_lock` in
`packages/common/provider_atomic.py`) refuses with `provider_lock_parent_unsafe` unless the parent directory
is owned by the effective user and is neither group- nor other-writable. An apply reaches that check after
its database transaction was committed. A dry-run never reaches it, so it passes and writes
`provision-dry-run.json` for a path the apply cannot publish to; the apply must name the same path as its
dry-run, so unless the operator owns that directory and can `chmod 755` it, the way on is a new
`--succession-id`.

The runbook also does not say how to give a basin a new model generation when the package content is
unchanged: the baseline version is derived from the content, so publishing the same tree again is
`already_done` and yields no new `package_checksum`, hence no new variant `model_id`.

## What changes

1. `packages/common/provider_atomic.py` gets the check as public code, so that the provision step does not
   copy it:
   - a predicate on an `os.stat_result`: owner is the effective user and the mode has no group or other
     write bit. `_provider_destination_file_lock` uses it in place of its inline condition; its behaviour
     and error code do not change.
   - a function that takes the directory of a provider destination and reaches it the way the lock does:
     `open_directory_no_follow` (every component from the root opened without following symlinks, `.` / `..`
     refused), `os.fstat`, then the predicate. It creates nothing. When the directory does not exist it
     opens the nearest existing ancestor the same way and requires only that the effective user may write
     and search it (`os.access`, with `effective_ids=True` where the platform supports it); this branch is
     best-effort, the answer on NFS is the client's. The apply creates the missing components itself with
     mode 0755 and never chmods them, so a directory it creates passes the predicate under any umask.
     It reports what is wrong (the directory, owner uid, mode, or the path component that could not be
     opened) so that the caller can say it.

2. `provision_direct_grid_registry` calls that function for the parent of `--output-registry` in the part it
   already marks as "a refusal that has done nothing": before the dry-run receipt of an apply is loaded,
   before the receipt target is prepared and before the database is opened, in a dry-run and in an apply.
   The path is taken as the publisher takes a plain path (`Path(value).expanduser().parent`). A failing
   check is a `DirectGridProvisionError` naming the directory and what is wrong, what is required, and both
   ways on: for a directory of the operator's own, `chmod 755` and the same command; otherwise a directory
   of their own (`mkdir -m 755`) and, because an apply must name the path of its dry-run, a new
   `--succession-id` with its own dry-run.
   An `--output-registry` that is an `s3://` or `published://` URI is resolved by the publisher under the
   object store root; it is not checked here and behaves as today. Any other scheme (as `urlparse` sees it,
   which includes a relative path whose first component contains a colon) is one the publisher refuses with
   `provider_destination_unsupported` after the commit: it is refused here, in the same place, saying to
   name a plain path.
   For an existing directory the function of item 1 also requires that the effective user may write and
   search it, as it does for the ancestor of a missing one.

3. Runbooks: `docs/runbooks/production-ops/recalibration-and-archive.md` 5.7.1 says how to publish an
   unchanged package as a new generation (hop 1 with `--package-version-template` and a suffix, a dedicated
   `--registry-manifest`; without the suffix the result is `already_done` and there is nothing to succeed
   to), and that the provision dry-run now refuses an unsafe `--output-registry` parent.
   `docs/runbooks/production-ops/service-bringup.md` is 989 lines of 1000: its hop 3 pitfall may be reworded
   in place, adding no line.

## Must preserve

- Every existing test of the provision tool and of `provider_atomic` passes unchanged.
- A dry-run still writes nothing but its receipt; no new writes anywhere.
- Python 3.11 and 3.12; no file over 1000 lines.

## Out of scope

The apply ordering itself (registry published after the commit); other callers of the provider lock; URI
destinations; the other conditions the publisher enforces after the commit and a dry-run still does not
predict: an existing destination file must be a regular file of the effective user with mode 0644, the
state of an existing lock file, validation of the registry content, filesystem errors.

## Evidence

Tests in `tests/test_provision_direct_grid_dry_run_and_receipt.py` (or a second file beside it if that one
would pass 1000 lines). "Refused, nothing done" is asserted as: the fake database was never connected, and
the tree of the object store and of the output directory is identical before and after (which also shows
that no succession directory was created).

- dry-run with the parent group-writable, and with it other-writable: refused, nothing done.
- apply with the parent group-writable, without any dry-run receipt: refused by this check, not by the
  missing dry-run receipt, nothing done. (The check runs before the dry-run receipt is loaded.)
- apply after a compliant dry-run whose parent was made group-writable in between: refused, nothing done,
  the database never connected.
- a symlinked parent, and a symlink in the ancestry of the parent: refused.
- existing compliant parent (created by the test with mode 0755), and parent missing under a writable
  ancestor: dry-run and apply proceed as today.
- parent missing under an ancestor that is not writable (skipped when run as root, as the suite already
  does elsewhere): refused.
- unit test of the predicate with a stat result of a foreign uid: unsafe.
- `--output-registry a:b/registry.json` and `file:///x/registry.json`: refused before the database, nothing
  done; an `s3://` output registry is not refused by this check.
- the function of item 1 called directly: `None` for a 0755 directory of the user, a problem for 0775, for
  0555 (skipped as root), and for a symlinked component; for each existing-directory case the destination
  lock on a file in that directory agrees (succeeds exactly where the function returned `None`).
- `_provider_destination_file_lock` still raises `provider_lock_parent_unsafe` for a group-writable parent
  (existing test, unchanged).
