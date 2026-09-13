"""Inert exact-container snapshot, recreation argv, and rollback decisions.

The production installer hands raw ``docker inspect`` JSON to this module.  It
never shell-sources that data.  Unsupported non-default fields are refusals: a
recreate is safer to reject than to silently normalize an old container away.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace

from packages.common import node27_pgdata_container as container
from packages.common.node27_cold_tablespace_identity import (
    CONTAINER_COLD_PATH,
    PRODUCTION_CONTAINER,
    PRODUCTION_HOST_PATH,
    PRODUCTION_IDENTITY,
    ColdTablespaceIdentity,
    validate_identity_for_action,
)

# Compatibility exports.  New state-machine code carries ``ColdTablespaceIdentity``
# instead of selecting these production literals independently.
LIVE_CONTAINER = PRODUCTION_CONTAINER
COLD_HOST_PATH = str(PRODUCTION_HOST_PATH)
COLD_CONTAINER_PATH = CONTAINER_COLD_PATH
COLD_BIND = PRODUCTION_IDENTITY.cold_bind


def with_cold_bind(
    snapshot: container.ContainerSnapshot, *, identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY
) -> container.ContainerSnapshot:
    validate_identity_for_action(identity)
    if identity.cold_bind in snapshot.binds:
        return snapshot
    return replace(snapshot, binds=tuple(sorted((*snapshot.binds, identity.cold_bind))))


@dataclass(frozen=True)
class ContainerDiff:
    approved: bool
    changed_fields: tuple[str, ...]
    blockers: tuple[str, ...]


@dataclass(frozen=True)
class RollbackPlan:
    remove_catalog: bool
    remove_installer_container: bool
    restore_prior: bool
    remove_host_path: bool
    blockers: tuple[str, ...]


def build_recreate_argv(
    snapshot: container.ContainerSnapshot,
    *,
    replacement_name: str | None = None,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> tuple[str, ...]:
    """Build direct argv from the immutable identity contract.

    ``replacement_name`` remains only as a compatibility assertion for existing
    callers.  It cannot redirect a production invocation and a disposable test
    must pass an identity issued by ``make_disposable_identity``.
    """

    validate_identity_for_action(identity)
    expected_name = identity.container_name
    if replacement_name is not None and replacement_name != expected_name:
        raise container.ContainerContractError(
            "replacement container name differs from the immutable identity contract"
        )
    return container.serialize_container_argv(
        with_cold_bind(snapshot, identity=identity), name=expected_name, docker_bin=identity.docker_bin
    )


def diff_container_config(
    before: container.ContainerSnapshot,
    after: container.ContainerSnapshot,
    *,
    identity: ColdTablespaceIdentity = PRODUCTION_IDENTITY,
) -> ContainerDiff:
    """Accept a new runtime container ID but no config drift beyond one bind."""

    validate_identity_for_action(identity)
    if before.resolved_image_id != after.resolved_image_id:
        return ContainerDiff(
            approved=False,
            changed_fields=("resolved_image_id",),
            blockers=("resolved image identity drifted",),
        )
    before_payload = before.config_payload()
    after_payload = after.config_payload()
    changed = tuple(sorted(key for key in before_payload if before_payload[key] != after_payload.get(key)))
    expected_binds = tuple(sorted((*before.binds, identity.cold_bind)))
    approved = changed == ("binds",) and after.binds == expected_binds
    blockers = () if approved else ("container diff is not exactly the one cold bind",)
    return ContainerDiff(approved=approved, changed_fields=changed, blockers=blockers)


def rollback_plan(
    *,
    installer_container: str,
    prior_container: str,
    installer_created_catalog: bool,
    catalog_dependents: int,
    pg_tblspc_references: Sequence[str],
    current_bind_references: Sequence[str],
    stopped_bind_references: Sequence[str],
    host_path_identity_matches: bool,
    host_path_empty: bool,
    installer_container_created: bool = True,
) -> RollbackPlan:
    if not installer_container or not prior_container:
        raise container.ContainerContractError("rollback container identities are required")
    blockers: list[str] = []
    if catalog_dependents:
        blockers.append("catalog has dependents")
    if pg_tblspc_references:
        blockers.append("pg_tblspc still references host state")
    if current_bind_references:
        blockers.append("live container still references host state")
    if stopped_bind_references:
        blockers.append("stopped container has a stale host bind")
    if not host_path_identity_matches:
        blockers.append("host path identity is uncertain")
    if not host_path_empty:
        blockers.append("host path is not empty")
    return RollbackPlan(
        remove_catalog=installer_created_catalog and catalog_dependents == 0,
        remove_installer_container=installer_container_created,
        restore_prior=True,
        remove_host_path=not blockers,
        blockers=tuple(blockers),
    )
