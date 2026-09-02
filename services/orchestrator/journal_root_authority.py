"""The one journal-root authority seam for the file journal's callers (#1943).

Every hardened journal read walks from the filesystem anchor with
``O_NOFOLLOW`` per component (``packages.common.safe_fs._open_directory_no_follow``),
so a symlink in *any* ancestor of the configured root turns every read into a
blocked row -- ``file_journal_unsafe_scanned_entry`` on the cross-model lane and
``file_journal_unreadable`` on the model-scoped one -- while the db-free
preflight, which only rejects a symlinked leaf or direct parent, reports no
blocker.  The operator then meets a diagnostic (darwin ``Path component is not
a directory``, Linux ELOOP) that names neither the symlink nor the remedy.

This module moves that failure to construction time: the configured root is
verified as a chain of real directories *before* the repository is built, and a
root that fails is refused with the typed ``FILE_JOURNAL_INVALID_ROOT`` and one
constant message that names the remedy.

"Invalid" here means any of: a root that is blank or otherwise not absolute
once ``~`` is expanded, a root whose home cannot be expanded at all, a root
that is missing, and a root any of whose components is not a real directory.
The non-absolute shapes are refused rather than passed on because
``packages.common.safe_fs`` anchors a relative path on ``Path.cwd()`` -- and
``Path("")`` is ``Path(".")`` -- so an unset environment variable or an empty
``--journal-root`` would silently retarget every read at the process's working
directory.  On the read-only census that shows up as a clean exit 0 over the
wrong tree, which is the one answer a census must never be able to give.

The message is deliberately one constant shared by both callers -- the db-free
scheduler factory (which reads ``NHMS_SCHEDULER_JOURNAL_ROOT``) and the
operator demotion CLI (which takes ``--journal-root``) -- so it speaks of "the
configured journal root" rather than of any one setting, and the setting name
travels in ``details["setting"]``.  It carries no path, no traceback and no
module name: the demotion CLI's stderr leak assertions already require that of
this error, and the configured value rides in ``details["journal_root"]``.

The returned path is what ``verify_directory_no_follow`` returns -- tilde
expanded, **not** resolved -- which is the same authority location the demotion
lane already uses for its repository I/O.  A root that already is a realpath
verifies to itself, and a literal ``~`` expands exactly once.
"""

from __future__ import annotations

from pathlib import Path

from packages.common.safe_fs import SafeFilesystemError, verify_directory_no_follow

from .chain_types import OrchestratorError

JOURNAL_ROOT_INVALID_MESSAGE = (
    "journal root failed safe filesystem verification: every path component of "
    "the configured journal root must be a real directory, none a symlink; "
    "set it to the realpath (readlink -f)"
)


def verify_journal_root_authority(journal_root: str | Path, *, setting: str) -> Path:
    """Verify the configured journal root as a chain of real directories.

    ``setting`` names the knob the caller read the value from
    (``NHMS_SCHEDULER_JOURNAL_ROOT`` for the scheduler, ``--journal-root`` for
    the demotion CLI); it rides in the error details, never in the message.

    A missing root raises too (``FileNotFoundError`` is an ``OSError``), which
    is what the demotion lane already did.  So does a blank or otherwise
    non-absolute root: ``safe_fs`` would anchor it on ``Path.cwd()``, making the
    caller read (and, on the demotion lane, write) a tree the operator never
    named.  The absoluteness question is asked AFTER ``~`` expansion, because a
    literal ``~/...`` root is legitimate and is not absolute before expansion;
    ``Path.expanduser`` raises a bare ``RuntimeError`` when no home can be
    determined (``~<unknown user>``, or no passwd entry with ``HOME`` unset),
    and that root is invalid for the same reason.  Both carry a stable
    ``details["error_type"]`` rather than a Python class name.

    On the scheduler's ``from_env`` path a missing or unsafe root never reaches
    this function at all -- not because the preflight runs first (it does, but
    it used to hand its rejected value straight to the factory anyway), but
    because a preflight-blocked db-free pass now passes
    ``scheduler_core._DB_FREE_REPOSITORY_BLOCKED`` and builds no repository.
    The redacted blocker of #1627 stays the operator's answer for those shapes
    (blank and relative among them); this refusal is the answer for the roots
    the preflight passes, above all a symlinked ancestor -- and for the demotion
    and census CLIs, which have no preflight in front of them at all.
    """

    def _refuse(error_type: str) -> OrchestratorError:
        return OrchestratorError(
            "FILE_JOURNAL_INVALID_ROOT",
            JOURNAL_ROOT_INVALID_MESSAGE,
            {
                "error_type": error_type,
                "journal_root": str(journal_root),
                "setting": setting,
            },
        )

    try:
        expanded = Path(journal_root).expanduser()
    except RuntimeError as error:
        raise _refuse("UnexpandableJournalRoot") from error
    if not expanded.is_absolute():
        raise _refuse("RelativeJournalRoot")
    try:
        return verify_directory_no_follow(expanded)
    except (OSError, SafeFilesystemError) as error:
        raise _refuse(type(error).__name__) from error
