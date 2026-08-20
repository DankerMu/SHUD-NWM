"""Create test paths with explicit, umask-independent modes.

Why this exists (issue #1513).  ``provider_atomic`` guards every provider
publish with two fail-closed **mode** checks, and a test that pre-creates
either surface with a bare ``mkdir`` / ``write_text`` inherits the ambient
umask instead of pinning a mode:

* the lock's **direct parent** directory must carry no ``0o022`` bit
  (``_provider_destination_file_lock``), and
* an already-existing **destination file** must be exactly
  ``SHARED_PROVIDER_MODE`` (``atomic_replace_provider_bytes``).

A bare ``Path.mkdir`` lands ``0o777 & ~umask`` and a bare ``write_text`` lands
``0o666 & ~umask``.  On a host at umask ``0002`` -- node-27, the project's
designated backend pytest oracle -- that is ``0o775`` and ``0o664``, and both
gates refuse.  On the umask-``0022`` hosts (node-22, macOS dev, CI runners) the
same calls land ``0o755`` and ``0o644`` and everything passes, which is exactly
why the breakage went unnoticed: the outcome was a property of the environment,
not of the code.

Production directory creation is pinned by ``safe_fs``; a test that
**pre-creates** these surfaces has to pin its own modes, which is what this
module is for.  Neither gate is relaxed to accommodate the tests -- they are
security properties (design D3).

``Path.mkdir(mode=..., parents=True)`` is not a substitute for the directory
half: the ``mode`` argument applies to the **leaf only**, and the ancestors
``parents=True`` creates still take ``0o777 & ~umask``.  The mode must
therefore be set per created component.  The in-repo idiom followed here is
``packages.common.state_manager._ensure_copyback_state_parent`` -- create, then
``chmod`` only the components **this call** created, so a directory that
already existed keeps whatever mode its owner chose.

The ``chmod`` (which ``safe_fs`` itself deliberately does **not** do -- see the
change's design D2) is what makes the landed mode independent of the ambient
umask in both directions: under ``0o077`` a bare ``mkdir`` lands ``0o700`` and
the chmod restores ``0o755``.  That is correct *here* and wrong in ``safe_fs``:
these are per-test scratch paths that exist only to satisfy the gates, whereas
widening inside the shared production helper would silently loosen private
directories on strict hosts.

Scope: this is not a blanket replacement for the ~1398 mode-less ``mkdir``
calls under ``tests/`` (design D4).  Only a path that a provider gate actually
inspects -- a lock's direct parent, or a provider destination file -- needs it.
"""

from __future__ import annotations

import os
from pathlib import Path

from packages.common.provider_atomic import SHARED_PROVIDER_MODE

#: The mode ``provider_atomic``'s lock-parent gate accepts
#: (``0o755 & 0o022 == 0``) while preserving the read/traverse path the two-node
#: topology depends on.  Same value as the production pin in
#: ``packages/common/safe_fs.py`` (design D8).
TEST_DIRECTORY_MODE = 0o755


def make_directory_with_explicit_mode(path: Path) -> Path:
    """Create ``path`` and every missing ancestor at ``TEST_DIRECTORY_MODE``.

    Idempotent, and non-destructive on directories it did not create: a
    component that already exists keeps its current mode.
    """

    target = Path(path).expanduser().absolute()
    for component in (*reversed(target.parents), target):
        try:
            os.lstat(component)
        except FileNotFoundError:
            # Deliberately no ``exist_ok=True``: that would swallow a genuine
            # collision AND skip the chmod on a directory somebody else just
            # created, silently reintroducing the ambient-umask mode this
            # helper exists to eliminate.
            component.mkdir()
            os.chmod(component, TEST_DIRECTORY_MODE)
    return target


def write_provider_destination(path: Path, content: str | bytes = "{}") -> Path:
    """Pre-create a provider destination file at ``SHARED_PROVIDER_MODE``.

    ``atomic_replace_provider_bytes`` refuses to publish over an existing
    destination whose mode is not exactly ``SHARED_PROVIDER_MODE``, so a stub
    written at the ambient umask (``0o664`` under ``0002``) makes the publish
    raise ``provider_destination_access_invalid`` before it reaches whatever
    the test was actually about.  The mode is read from the production constant
    rather than restated, so the two cannot drift apart.

    Only the FIRST write needs this: ``write_bytes`` / ``write_text`` truncate
    an existing file and leave its mode alone, so a later rewrite of the same
    destination inherits the mode pinned here.
    """

    target = Path(path).expanduser().absolute()
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")
    os.chmod(target, SHARED_PROVIDER_MODE)
    return target
