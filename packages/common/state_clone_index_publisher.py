"""State-clone index publisher for the lifecycle post-commit tail.

Epic #982 SUB-6 (``mapping-variant-state-compatibility`` task 3.3):
publishes each committed cloned ``(M1, source, t*)`` ``hydro.state_snapshot``
row into the scheduler file state index through the existing
:class:`packages.common.state_manager.FileStateSnapshotIndexRepository`
entry shape. Runs on the lifecycle post-commit tail, ORDERED BEFORE the
Change 4 ``publish_scheduler_registry_manifest`` re-publish (D7 fact
anchor A-i).

Design rationale
----------------
node-22 is DB-free. Its only source of truth for warm-start evidence is
the scheduler file state index, and its only source of truth for the
current active model is the scheduler registry manifest. If the manifest
names ``M1`` active BEFORE the file state index carries ``M1``'s
successor checkpoint, node-22 dispatches on stale evidence and either
falls off warm-start onto cold spin-up or picks the wrong lineage.

Ordering the index publish BEFORE the manifest publish, with a raising
index publisher HOLDING BACK the manifest re-publish, guarantees the
compute plane sees the two authorities in monotone order: index carries
the successor first, then the manifest names it active. A publish
failure keeps the previous manifest as the compute-plane authority so
node-22 is never routed to ``M1`` without ``M1``'s successor checkpoint.

Cold-start route interaction (SUB-5)
------------------------------------
A source covered by an explicit cold-start approval (SUB-5 task 3.2)
gets NO clone row committed under ``(M1, source_id)`` at ``t*``. This
publisher iterates ``ctx.source_scope`` and does a lookup per source; a
``None`` return means "no clone committed" (approved cold-start OR
hook-skipped fresh basin OR legacy target). The publisher SKIPS such
sources — it never fabricates an index entry.

Byte-boundary
-------------
Read-only consumer of the SUB-2/3/4/5 clone-write side. Does not touch
``packages/common/state_clone.py`` or ``packages/common/state_clone_hook.py``;
they stay byte-frozen. Uses
:meth:`packages.common.state_manager.PsycopgStateSnapshotRepository.get_latest_clone_row_for_model_source`
— the newly-added minimal read method — to fetch the committed clone
row for each source without needing to thread ``cutover_valid_time``
through the hook path.

Shadow-proof lookup (SUB-6 Round-1 fold): the read method filters by
``clone_gate_fingerprint IS NOT NULL`` so prior SHUD forecast /
save-state rows (which never populate ``clone_gate_fingerprint``)
cannot shadow a fresh clone written at a backdated ``t*``. Clone rows
ALWAYS carry a non-NULL ``clone_gate_fingerprint`` (SUB-2 core write
path); non-clone rows ALWAYS carry NULL. The filter isolates clone
lineage unambiguously without threading ``cutover_valid_time`` through
:class:`PostCommitPublishContext`.
"""

from __future__ import annotations

from packages.common.model_registry import (
    PostCommitPublishContext,
    PostCommitStateIndexPublisher,
)
from packages.common.state_manager import (
    FileStateSnapshotIndexRepository,
    StateSnapshotRepository,
)

__all__ = [
    "StateIndexPublishFailedError",
    "build_default_state_index_publisher",
]


class StateIndexPublishFailedError(RuntimeError):
    """Raised when the state-index upsert fails for a cloned source.

    Carries the surrounding cutover scope so the retry blocker record
    can locate the blocked ``(basin_version_id, target_model_id,
    source_scope)`` tuple without re-parsing the exception message.
    The root-cause exception is kept as ``.cause`` and chained via
    ``raise ... from`` so tracebacks retain the original.

    A raise from this publisher HOLDS BACK the Change 4 manifest re-
    publish on the lifecycle post-commit tail — the previous manifest
    stays the compute-plane authority (D7 fact anchor A-i).
    """

    def __init__(
        self,
        *,
        basin_version_id: str,
        target_model_id: str,
        source_scope: tuple[str, ...] | None,
        source_id: str,
        cause: BaseException,
    ) -> None:
        super().__init__(
            "state clone index publish failed for "
            f"basin_version_id={basin_version_id!r} "
            f"target_model_id={target_model_id!r} "
            f"source_id={source_id!r}: {cause!r}"
        )
        self.basin_version_id = basin_version_id
        self.target_model_id = target_model_id
        self.source_scope = source_scope
        self.source_id = source_id
        self.cause = cause


def build_default_state_index_publisher(
    *,
    state_snapshot_repo: StateSnapshotRepository,
    file_state_index_repo: FileStateSnapshotIndexRepository,
) -> PostCommitStateIndexPublisher:
    """Return the SUB-6 state-index publisher bound to injected repos.

    The returned callable is registered via
    :meth:`packages.common.model_registry.PsycopgModelRegistryStore.register_post_commit_state_index_publisher`
    at bootstrap time. It walks ``ctx.source_scope``, reads the newly
    committed clone row from ``state_snapshot_repo``, and upserts that
    row into ``file_state_index_repo`` verbatim so lineage byte-matches
    the DB source of truth.

    Parameters
    ----------
    state_snapshot_repo
        Read side of ``hydro.state_snapshot``. The publisher uses
        :meth:`get_latest_clone_row_for_model_source` — the newest
        CLONE row (``clone_gate_fingerprint IS NOT NULL``) for
        ``(target_model_id, source_id)`` is the just-committed clone
        row. The clone-provenance filter guarantees that prior
        forecast / save-state rows at higher ``valid_time`` cannot
        shadow a fresh clone written at a backdated ``t*`` (SUB-6
        Round-1 fold — shadow-proof lookup).
    file_state_index_repo
        Write side of the scheduler file state index.
        :meth:`upsert_state_snapshot` is the exact seam node-22 reads
        against; the publisher writes ONE entry per committed
        ``(M1, source_id)`` row.

    Behavior
    --------
    * ``ctx.source_scope is None`` — legacy IDW target, no clone rows
      to publish. Returns without touching the index.
    * A source with no committed clone row (approved cold-start OR
      hook-skipped) — the read returns ``None`` and the publisher
      skips that source. No fabricated index entry.
    * A file-state-index upsert raise for any source — wrapped in
      :class:`StateIndexPublishFailedError` and re-raised so the
      post-commit tail short-circuits BEFORE the manifest re-publish.
    """

    def _publish(ctx: PostCommitPublishContext) -> None:
        if ctx.source_scope is None:
            return
        for source_id in ctx.source_scope:
            snapshot = state_snapshot_repo.get_latest_clone_row_for_model_source(
                model_id=ctx.target_model_id,
                source_id=source_id,
            )
            if snapshot is None:
                # No clone row committed for this source (approved cold-
                # start via SUB-5 task 3.2, or the hook took a skip path
                # — fresh basin / legacy target). Never fabricate an
                # index entry.
                continue
            try:
                file_state_index_repo.upsert_state_snapshot(snapshot)
            except Exception as error:
                raise StateIndexPublishFailedError(
                    basin_version_id=ctx.basin_version_id,
                    target_model_id=ctx.target_model_id,
                    source_scope=ctx.source_scope,
                    source_id=source_id,
                    cause=error,
                ) from error

    # Return as-is; ``PostCommitStateIndexPublisher`` is a Callable
    # alias so the inner function satisfies the type structurally.
    _publish.__qualname__ = "state_clone_index_publisher"
    return _publish
