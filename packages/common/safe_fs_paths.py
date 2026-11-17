# Leaf of packages.common.safe_fs (#3155): the error type and the path-expansion
# helpers. safe_fs re-exports every name here; this module must not import safe_fs.
from __future__ import annotations

from pathlib import Path


class SafeFilesystemError(RuntimeError):
    """Raised when a filesystem operation cannot be completed safely.

    ``kind`` discriminates the refusal structurally so callers can branch on
    the field rather than on message text:

    - ``"unsafe"`` (default) -- tamper-shaped refusal: symlinked target or
      path component, non-regular target, containment violation.
    - ``"io"`` -- an ``OSError`` wrapped into this module's contract.
    - ``"indeterminate"`` -- an operation that may or may not have taken
      effect (durable-replace dir-fsync / parent-identity uncertainty).
    - ``"identity_changed"`` -- the target's inode was replaced between the
      pre-open stat and the post-open fstat: the target was swapped for a
      DIFFERENT REGULAR FILE while it was being opened.  An ordinary atomic
      ``os.replace`` and a hostile swap are indistinguishable at this layer,
      so this is a CONSISTENCY refusal, not the symlink defense: a symlink
      appearing in that window is refused by ``O_NOFOLLOW`` (ELOOP) and the
      symlink mode checks and never reaches the identity comparison.  Callers
      that own a concurrent-replace relationship may absorb this kind with a
      bounded retry; callers that must reject any inode movement keep
      rejecting it.  The primitive itself never retries.
    """

    def __init__(self, message: str, *, kind: str = "unsafe") -> None:
        super().__init__(message)
        self.kind = kind


def _anchor_for(path: Path, *, containment_root: Path | None) -> tuple[Path, tuple[str, ...]]:
    target = _expand_path(path)
    if containment_root is not None:
        root = _expand_path(containment_root)
        return root, _relative_parts_under_root(target, root)
    root = Path(target.anchor)
    if not root.anchor:
        raise SafeFilesystemError(f"No filesystem anchor for path: {target}")
    return root, _absolute_parts(target)


def _expand_path(path: Path) -> Path:
    """Expand a leading ``~`` and anchor the result at the current directory.

    ``Path.expanduser()`` throws a bare, errno-less ``RuntimeError`` when no home
    directory can be determined -- ``~<unknown user>``, or no passwd entry with
    ``HOME`` unset.  This prelude is shared by every public entry point of this
    module, so letting that throw out would defeat the module's advertised
    contract on all of them at once; it is converted here into the module's own
    structured error instead.

    Mind the direction: ``SafeFilesystemError`` **is** a ``RuntimeError``
    subclass, not the reverse.  Callers that already write
    ``except SafeFilesystemError`` therefore catch this with no change of their
    own, while the bare throw sailed past them -- and past every caller reading
    ``error.kind``, which a bare ``RuntimeError`` does not carry.

    The failure reuses the existing ``unsafe`` classification rather than adding
    a new one, so callers branching on the existing values keep a total set of
    branches.  Degrading to a permissive pass that keeps the literal ``~``
    component is not an option: the write-side primitives would then create or
    delete a path the operator never named.
    """

    try:
        expanded = Path(path).expanduser()
    except RuntimeError as error:
        raise SafeFilesystemError(
            f"Cannot determine the home directory for path: {path}",
            kind="unsafe",
        ) from error
    return expanded if expanded.is_absolute() else Path.cwd() / expanded


def _absolute_parts(path: Path) -> tuple[str, ...]:
    parts = tuple(part for part in path.parts if part not in {path.anchor, ""})
    for part in parts:
        if part in {".", ".."}:
            raise SafeFilesystemError(f"Unsafe path component: {part!r}")
    return parts


def _relative_parts_under_root(path: Path, root: Path) -> tuple[str, ...]:
    target = _expand_path(path)
    containment_root = _expand_path(root)
    try:
        relative = target.relative_to(containment_root)
    except ValueError as error:
        raise SafeFilesystemError(f"Path must stay under containment root: {target}") from error
    parts = tuple(relative.parts)
    for part in parts:
        if part in {"", ".", ".."}:
            raise SafeFilesystemError(f"Unsafe path component under containment root: {part!r}")
    return parts
