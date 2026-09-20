"""Compatibility facade guard: forwarder classification (#1823 partition).

Non-forwarding implementations (sync and async), the four local/forwarder
transition directions, the follow-up-issue semantics required of an inventory
entry, and the import-family growth signal. The alias half is in
``tests/test_entropy_audit_facade_guard_aliases.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

from scripts.governance import audit_repo_entropy
from tests.entropy_audit_helpers import (
    _append_inventory_line,
    _assert_compatibility_facade_report_only_finding,
    _commit_all,
    _compatibility_facade_guard,
    _compatibility_facade_signals,
    _git_rev_parse,
    _setup_compatibility_facade_guard_fixture,
    _write,
)


def test_compatibility_facade_guard_follow_up_issue_semantics_requires_concrete_issue_ref() -> None:
    assert not audit_repo_entropy._compatibility_inventory_has_follow_up_issue_semantics(
        "owner module cannot host local glue; follow-up issue; removal condition."
    )
    assert audit_repo_entropy._compatibility_inventory_has_follow_up_issue_semantics(
        "owner module cannot host local glue; follow-up issue #999; removal condition."
    )
    assert audit_repo_entropy._compatibility_inventory_has_follow_up_issue_semantics(
        "owner module cannot host local glue; follow-up issue /issues/999; removal condition."
    )
    assert audit_repo_entropy._compatibility_inventory_has_follow_up_issue_semantics(
        "owner module cannot host local glue; issues/999 follow-up removal condition."
    )


def test_compatibility_facade_guard_requires_non_forwarding_inventory_metadata(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8")
        + "\n"
        + "def metadata_required_chain_policy(value: object) -> dict[str, str]:\n"
        + "    normalized = str(value).strip()\n"
        + "    return {\"value\": normalized}\n",
    )

    message_key = "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required"
    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(
            tmp_path,
            base_ref,
            "new-non-forwarding-implementation",
        )
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- metadata_required_chain_policy",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(
            tmp_path,
            base_ref,
            "new-non-forwarding-implementation",
        )
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- metadata_required_chain_policy local removal condition.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(
            tmp_path,
            base_ref,
            "new-non-forwarding-implementation",
        )
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- metadata_required_chain_policy follow-up issue #999 removal condition.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(
            tmp_path,
            base_ref,
            "new-non-forwarding-implementation",
        )
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- metadata_required_chain_policy owner module cannot host removal condition.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(
            tmp_path,
            base_ref,
            "new-non-forwarding-implementation",
        )
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- metadata_required_chain_policy owner module cannot host local glue; "
        "follow-up issue; removal condition after owner extraction.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(
            tmp_path,
            base_ref,
            "new-non-forwarding-implementation",
        )
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- metadata_required_chain_policy owner module cannot host local glue; "
        "follow-up issue #999; removal condition after owner extraction.",
    )

    assert _compatibility_facade_signals(
        tmp_path,
        base_ref,
        "new-non-forwarding-implementation",
    ) == []


def test_compatibility_facade_guard_reports_async_non_forwarding_implementation(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8")
        + "\n"
        + "async def new_async_chain_policy(value: object) -> dict[str, str]:\n"
        + "    normalized = str(value).strip()\n"
        + "    return {\"value\": normalized}\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-non-forwarding-implementation")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required"
    ]
    assert signals[0]["inventory_tokens"] == ["new_async_chain_policy", "new_async_chain_policy"]
    assert "new_async_chain_policy" in str(signals[0]["detail"])


def test_compatibility_facade_guard_reports_existing_sync_local_changed_to_forwarder_until_inventory_updates(
    tmp_path: Path,
) -> None:
    _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    local_function = (
        "\n"
        "def unlisted_existing_local_chain_policy(value: object) -> dict[str, str]:\n"
        "    normalized = str(value).strip()\n"
        "    return {\"value\": normalized}\n"
    )
    _write(chain_path, chain_path.read_text(encoding="utf-8") + local_function)
    _commit_all(tmp_path, "add unlisted existing local chain policy")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8").replace(
            local_function,
            "\n"
            "def unlisted_existing_local_chain_policy(value: object) -> object:\n"
            "    return chain_manifests.unlisted_existing_local_chain_policy(value)\n",
        ),
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required"
    ]
    assert signals[0]["path"] == "services/orchestrator/chain.py"
    assert signals[0]["inventory_tokens"] == [
        "unlisted_existing_local_chain_policy",
        "unlisted_existing_local_chain_policy",
    ]
    assert signals[0]["line"] is not None
    assert "changed to forwarding facade path" in str(signals[0]["detail"])
    assert "unlisted_existing_local_chain_policy" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "unlisted_existing_local_chain_policy owner services.orchestrator.chain_manifests "
        "retention forwarding facade until removal condition; verification command: "
        "`uv run pytest -q tests/test_orchestration_chain.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_existing_async_local_changed_to_forwarder_until_inventory_updates(
    tmp_path: Path,
) -> None:
    _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    local_function = (
        "\n"
        "async def unlisted_existing_async_local_chain_policy(value: object) -> dict[str, str]:\n"
        "    normalized = str(value).strip()\n"
        "    return {\"value\": normalized}\n"
    )
    _write(chain_path, chain_path.read_text(encoding="utf-8") + local_function)
    _commit_all(tmp_path, "add unlisted existing async local chain policy")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8").replace(
            local_function,
            "\n"
            "async def unlisted_existing_async_local_chain_policy(value: object) -> object:\n"
            "    return await chain_manifests.unlisted_existing_async_local_chain_policy(value)\n",
        ),
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required"
    ]
    assert signals[0]["inventory_tokens"] == [
        "unlisted_existing_async_local_chain_policy",
        "unlisted_existing_async_local_chain_policy",
    ]
    assert signals[0]["line"] is not None
    assert "changed to forwarding facade path" in str(signals[0]["detail"])
    assert "unlisted_existing_async_local_chain_policy" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "unlisted_existing_async_local_chain_policy owner services.orchestrator.chain_manifests "
        "retention async forwarding facade until removal condition; verification command: "
        "`uv run pytest -q tests/test_orchestration_chain.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_existing_sync_forwarder_changed_to_non_forwarding_until_inventory_updates(
    tmp_path: Path,
) -> None:
    _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    forwarding_function = (
        "\n"
        "def unlisted_existing_chain_forwarder(value: object) -> object:\n"
        "    return chain_manifests.unlisted_existing_chain_forwarder(value)\n"
    )
    _write(chain_path, chain_path.read_text(encoding="utf-8") + forwarding_function)
    _commit_all(tmp_path, "add unlisted existing chain forwarding facade")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8").replace(
            forwarding_function,
            "\n"
            "def unlisted_existing_chain_forwarder(value: object) -> dict[str, str]:\n"
            "    normalized = str(value).strip()\n"
            "    return {\"value\": normalized}\n",
        ),
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-non-forwarding-implementation")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required"
    ]
    assert signals[0]["path"] == "services/orchestrator/chain.py"
    assert signals[0]["inventory_tokens"] == [
        "unlisted_existing_chain_forwarder",
        "unlisted_existing_chain_forwarder",
    ]
    assert "changed to non-forwarding facade implementation" in str(signals[0]["detail"])
    assert "unlisted_existing_chain_forwarder" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "unlisted_existing_chain_forwarder owner module cannot host local glue; "
        "follow-up issue #999; removal condition after owner extraction.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_existing_async_forwarder_changed_to_non_forwarding_until_inventory_updates(
    tmp_path: Path,
) -> None:
    _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    forwarding_function = (
        "\n"
        "async def unlisted_existing_async_chain_forwarder(value: object) -> object:\n"
        "    return await chain_manifests.unlisted_existing_async_chain_forwarder(value)\n"
    )
    _write(chain_path, chain_path.read_text(encoding="utf-8") + forwarding_function)
    _commit_all(tmp_path, "add unlisted existing async chain forwarding facade")
    base_ref = _git_rev_parse(tmp_path, "HEAD")
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8").replace(
            forwarding_function,
            "\n"
            "async def unlisted_existing_async_chain_forwarder(value: object) -> dict[str, str]:\n"
            "    normalized = str(value).strip()\n"
            "    return {\"value\": normalized}\n",
        ),
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-non-forwarding-implementation")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required"
    ]
    assert signals[0]["inventory_tokens"] == [
        "unlisted_existing_async_chain_forwarder",
        "unlisted_existing_async_chain_forwarder",
    ]
    assert "changed to non-forwarding facade implementation" in str(signals[0]["detail"])
    assert "unlisted_existing_async_chain_forwarder" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "unlisted_existing_async_chain_forwarder owner module cannot host local glue; "
        "follow-up issue #999; removal condition after owner extraction.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_requires_import_family_inventory_metadata(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8") + "import apps.api.main as api_main\n",
    )

    message_key = "compatibility-facade-growth.new-import-family.inventory-required"
    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-import-family")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- apps/api",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-import-family")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "- apps/api import family justified; it does not invert ownership.",
    )

    assert _compatibility_facade_signals(tmp_path, base_ref, "new-import-family") == []


def test_compatibility_facade_guard_reports_chain_import_family_growth_until_inventory_updates(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8") + "import apps.api.main as api_main\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-import-family")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-import-family.inventory-required"
    ]
    assert signals[0]["path"] == "services/orchestrator/chain.py"
    assert signals[0]["inventory_tokens"] == ["apps/api"]
    assert "apps/api" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-import-family.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "apps/api import family justified; it does not invert ownership.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0
