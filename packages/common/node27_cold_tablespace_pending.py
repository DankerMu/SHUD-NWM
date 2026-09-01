"""Write-ahead pending-action identities and exact pre/post classification."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

INSTALL_ACTIONS = frozenset(
    {
        "create_host_path",
        "stop_prior",
        "rename_prior",
        "create_replacement",
        "create_catalog",
    }
)
ROLLBACK_ACTIONS = frozenset(
    {
        "drop_catalog",
        "remove_replacement",
        "rename_prior_back",
        "start_prior",
        "remove_host_path",
    }
)
ALL_ACTIONS = INSTALL_ACTIONS | ROLLBACK_ACTIONS

PendingClass = Literal["pre", "post", "mixed"]


def pending_is_consistent(phase: str, ownership: Mapping[str, Any], action: str | None) -> bool:
    if action is None:
        return True
    if action not in ALL_ACTIONS:
        return False
    host = bool(ownership.get("host_path_created"))
    stopped = bool(ownership.get("prior_stopped"))
    renamed = bool(ownership.get("prior_renamed"))
    created = bool(ownership.get("installer_container_created"))
    catalog = bool(ownership.get("catalog_created"))
    if action == "create_host_path":
        return phase == "prepared" and not host
    if action == "stop_prior":
        return phase in {"prepared", "path_created"} and not stopped
    if action == "rename_prior":
        return phase == "prior_stopped" and stopped and not renamed
    if action == "create_replacement":
        return phase == "prior_renamed" and renamed and not created
    if action == "create_catalog":
        return phase == "replacement_created" and created and not catalog
    if action == "drop_catalog":
        return catalog and phase in {"ddl_created", "terminal_pending_cleanup"}
    if action == "remove_replacement":
        return created and phase in {"replacement_created", "ddl_created", "terminal_pending_cleanup"}
    if action == "rename_prior_back":
        return renamed and not created
    if action == "start_prior":
        return stopped and not renamed
    if action == "remove_host_path":
        return host
    return False


def classify_pending(
    action: str,
    *,
    current: Any | None,
    prior: Any | None,
    topology: str | None,
    path_exists: bool | None,
    path_matches: bool,
    path_empty: bool,
    identity_container: str,
    identity_prior: str,
    prior_matches_current: bool,
    prior_matches_prior: bool,
    expected_matches_current: bool,
    has_cold_bind_current: bool,
) -> PendingClass:
    """Return exact precondition, exact postcondition, or mixed/unknown."""

    current_name = None if current is None else current.name
    prior_name = None if prior is None else prior.name
    current_running = None if current is None else current.running
    if action == "create_host_path":
        if path_exists is True and path_matches and path_empty:
            return "post"
        if path_exists is False:
            return "pre"
        return "mixed"
    if action == "stop_prior":
        if current is None or current_name != identity_container or not prior_matches_current:
            return "mixed"
        if current_running is False:
            return "post"
        if current_running is True:
            return "pre"
        return "mixed"
    if action == "rename_prior":
        if (
            current is None
            and prior is not None
            and prior_name == identity_prior
            and prior_matches_prior
        ):
            return "post"
        if (
            current is not None
            and current_name == identity_container
            and prior_matches_current
            and prior is None
        ):
            return "pre"
        return "mixed"
    if action == "create_replacement":
        if (
            current is not None
            and current_name == identity_container
            and expected_matches_current
            and has_cold_bind_current
        ):
            return "post"
        if current is None and prior is not None and prior_name == identity_prior and prior_matches_prior:
            return "pre"
        return "mixed"
    if action == "create_catalog":
        if topology == "expected" and expected_matches_current and has_cold_bind_current:
            return "post"
        if topology == "absent" and expected_matches_current and has_cold_bind_current:
            return "pre"
        return "mixed"
    if action == "drop_catalog":
        if topology == "absent":
            return "post"
        if topology == "expected":
            return "pre"
        return "mixed"
    if action == "remove_replacement":
        if current is None:
            return "post"
        if current_name == identity_container and expected_matches_current and has_cold_bind_current:
            return "pre"
        return "mixed"
    if action == "rename_prior_back":
        if (
            current is not None
            and current_name == identity_container
            and prior_matches_current
            and prior is None
        ):
            return "post"
        if (
            current is None
            and prior is not None
            and prior_name == identity_prior
            and prior_matches_prior
        ):
            return "pre"
        return "mixed"
    if action == "start_prior":
        if current is None or current_name != identity_container or not prior_matches_current:
            return "mixed"
        if current_running is True:
            return "post"
        if current_running is False:
            return "pre"
        return "mixed"
    if action == "remove_host_path":
        if path_exists is False:
            return "post"
        if path_exists is True and path_matches and path_empty:
            return "pre"
        return "mixed"
    return "mixed"
