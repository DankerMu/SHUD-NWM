"""Compatibility facade guard: alias detection (#1823 partition).

Owner aliases, annotated aliases, full-module dotted aliases and calls,
same-RHS multi-target aliases, sequence aliases, imported symbols, monkeypatch
aliases and the inventory metadata each of them requires before the guard stops
reporting. The forwarder-classification half is in
``tests/test_entropy_audit_facade_guard_forwarders.py``.

The shared constants, the memoized ``build_report`` accessor, the finding
selectors and the fixture builders live in ``tests/entropy_audit_helpers.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.entropy_audit_helpers import (
    _append_inventory_line,
    _assert_compatibility_facade_report_only_finding,
    _compatibility_facade_guard,
    _compatibility_facade_signals,
    _setup_compatibility_facade_guard_fixture,
    _write,
)


def test_compatibility_facade_guard_reports_scheduler_owner_alias_until_inventory_updates(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "NewSchedulerAlias = _scheduler_state.NewSchedulerAlias\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required"
    ]
    assert signals[0]["path"] == "services/orchestrator/scheduler.py"
    assert signals[0]["inventory_tokens"] == ["NewSchedulerAlias"]
    assert "NewSchedulerAlias" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "NewSchedulerAlias owner services.orchestrator.scheduler_state retention removal-condition; "
        "verification command: `uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_requires_scheduler_alias_inventory_metadata(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "MetadataRequiredSchedulerAlias = _scheduler_state.MetadataRequiredSchedulerAlias\n",
    )

    message_key = "compatibility-facade-growth.new-facade-reexport.inventory-required"
    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "- MetadataRequiredSchedulerAlias",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "- MetadataRequiredSchedulerAlias owner services.orchestrator.scheduler_state "
        "retention removal-condition; verification TBD.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "- MetadataRequiredSchedulerAlias owner services.orchestrator.scheduler_state "
        "retention removal-condition.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "- MetadataRequiredSchedulerAlias owner services.orchestrator.scheduler_state "
        "retention removal-condition; verification command: "
        "`uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport") == []


def test_compatibility_facade_guard_reports_scheduler_annotated_owner_alias_until_inventory_updates(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "AnnotatedAlias: object = _scheduler_state.AnnotatedAlias\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required"
    ]
    assert signals[0]["path"] == "services/orchestrator/scheduler.py"
    assert signals[0]["inventory_tokens"] == ["AnnotatedAlias"]
    assert (
        signals[0]["detail"]
        == "new owner-module alias `AnnotatedAlias` forwarding to "
        "`services.orchestrator.scheduler_state.AnnotatedAlias`"
    )
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "AnnotatedAlias owner services.orchestrator.scheduler_state retention removal-condition; "
        "verification command: `uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_scheduler_full_module_dotted_aliases_until_inventory_updates(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "\n"
        + "import services.orchestrator.scheduler_state\n"
        + "DottedAlias = services.orchestrator.scheduler_state.DottedAlias\n"
        + "DottedAnnotatedAlias: object = "
        + "services.orchestrator.scheduler_state.DottedAnnotatedAlias\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    ]
    assert [signal["inventory_tokens"] for signal in signals] == [
        ["DottedAlias"],
        ["DottedAnnotatedAlias"],
    ]
    assert (
        signals[0]["detail"]
        == "new owner-module alias `DottedAlias` forwarding to "
        "`services.orchestrator.scheduler_state.DottedAlias`"
    )
    assert (
        signals[1]["detail"]
        == "new owner-module alias `DottedAnnotatedAlias` forwarding to "
        "`services.orchestrator.scheduler_state.DottedAnnotatedAlias`"
    )
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "DottedAlias and DottedAnnotatedAlias owner module aliases retain with removal condition; "
        "verification command: `uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_classifies_full_module_dotted_call_as_forwarding(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "\n"
        + "import services.orchestrator.scheduler_state\n"
        + "def dotted_scheduler_forwarder(value: object) -> object:\n"
        + "    return services.orchestrator.scheduler_state.dotted_scheduler_forwarder(value)\n",
    )

    forwarding_signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    non_forwarding_signals = _compatibility_facade_signals(
        tmp_path,
        base_ref,
        "new-non-forwarding-implementation",
    )

    assert [signal["inventory_tokens"] for signal in forwarding_signals] == [
        ["dotted_scheduler_forwarder", "dotted_scheduler_forwarder"]
    ]
    assert "new forwarding facade path" in str(forwarding_signals[0]["detail"])
    assert non_forwarding_signals == []
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "dotted_scheduler_forwarder owner module services.orchestrator.scheduler_state "
        "retains forwarding facade path until removal condition; verification command: "
        "`uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_same_rhs_multi_target_aliases_until_inventory_updates(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "SameRhsAlias = SameRhsAliasCompat = _scheduler_state.SameRhsAlias\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    signals_by_token = {tuple(signal["inventory_tokens"]): signal for signal in signals}
    assert set(signals_by_token) == {
        ("SameRhsAlias",),
        ("SameRhsAliasCompat",),
    }
    assert (
        signals_by_token[("SameRhsAlias",)]["detail"]
        == "new owner-module alias `SameRhsAlias` forwarding to "
        "`services.orchestrator.scheduler_state.SameRhsAlias`"
    )
    assert (
        signals_by_token[("SameRhsAliasCompat",)]["detail"]
        == "new owner-module alias `SameRhsAliasCompat` forwarding to "
        "`services.orchestrator.scheduler_state.SameRhsAlias`"
    )
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "SameRhsAlias and SameRhsAliasCompat share owner module aliases with "
        "retention and removal condition; verification command: "
        "`uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


@pytest.mark.parametrize(
    ("opening", "closing", "name_prefix"),
    [
        ("(", ")", "Tuple"),
        ("[", "]", "List"),
    ],
)
def test_compatibility_facade_guard_reports_sequence_owner_aliases_until_inventory_updates(
    tmp_path: Path,
    opening: str,
    closing: str,
    name_prefix: str,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + (
            f"{opening}{name_prefix}Alias, {name_prefix}OtherAlias{closing} = "
            f"{opening}_scheduler_state.{name_prefix}Alias, "
            f"_scheduler_state.{name_prefix}OtherAlias{closing}\n"
        ),
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["inventory_tokens"] for signal in signals] == [
        [f"{name_prefix}Alias"],
        [f"{name_prefix}OtherAlias"],
    ]
    assert (
        signals[0]["detail"]
        == f"new owner-module alias `{name_prefix}Alias` forwarding to "
        f"`services.orchestrator.scheduler_state.{name_prefix}Alias`"
    )
    assert (
        signals[1]["detail"]
        == f"new owner-module alias `{name_prefix}OtherAlias` forwarding to "
        f"`services.orchestrator.scheduler_state.{name_prefix}OtherAlias`"
    )
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        f"{name_prefix}Alias and {name_prefix}OtherAlias sequence owner module aliases "
        "retain with removal condition; verification command: "
        "`uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_scheduler_imported_symbol(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "from services.orchestrator.scheduler_state import NewImportedSchedulerSymbol\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required"
    ]
    assert signals[0]["inventory_tokens"] == ["NewImportedSchedulerSymbol"]
    assert "new imported facade symbol" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-facade-reexport.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "NewImportedSchedulerSymbol owner services.orchestrator.scheduler_state retention "
        "removal condition; verification command: "
        "`uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_requires_chain_alias_verification_metadata(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8")
        + "NewChainAlias = chain_manifests.NewChainAlias\n",
    )

    message_key = "compatibility-facade-growth.new-facade-reexport.inventory-required"
    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "NewChainAlias owner services.orchestrator.chain_manifests retention "
        "legacy chain import until removal condition.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "NewChainAlias owner services.orchestrator.chain_manifests retention "
        "legacy chain import until removal condition; verification command: "
        "`uv run pytest -q tests/test_orchestration_chain.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_requires_chain_monkeypatch_alias_verification_metadata(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8")
        + "_new_chain_patch = chain_manifests._new_chain_patch\n",
    )

    message_key = "compatibility-facade-growth.new-monkeypatch-alias.inventory-required"
    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-monkeypatch-alias")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "_new_chain_patch owner services.orchestrator.chain_manifests retention "
        "legacy chain monkeypatch alias until removal condition.",
    )

    assert [
        signal["message_key"]
        for signal in _compatibility_facade_signals(tmp_path, base_ref, "new-monkeypatch-alias")
    ] == [message_key]

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "_new_chain_patch owner services.orchestrator.chain_manifests retention "
        "legacy chain monkeypatch alias until removal condition; verification command: "
        "`uv run pytest -q tests/test_orchestration_chain.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_ignores_inventory_token_outside_guard_hook(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8")
        + "from services.orchestrator.persistence import PipelineEvent\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-facade-reexport")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-facade-reexport.inventory-required"
    ]
    assert signals[0]["inventory_tokens"] == ["PipelineEvent"]
    assert "PipelineEvent" in str(signals[0]["detail"])


def test_compatibility_facade_guard_reports_scheduler_monkeypatch_alias(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    scheduler_path = tmp_path / "services" / "orchestrator" / "scheduler.py"
    _write(
        scheduler_path,
        scheduler_path.read_text(encoding="utf-8")
        + "_new_scheduler_patch = _scheduler_state._new_scheduler_patch\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-monkeypatch-alias")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-monkeypatch-alias.inventory-required"
    ]
    assert signals[0]["inventory_tokens"] == ["_new_scheduler_patch"]
    assert "new owner-module alias" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-monkeypatch-alias.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/SCHEDULER_COMPATIBILITY_INVENTORY.md",
        "_new_scheduler_patch owner services.orchestrator.scheduler_state retention "
        "removal condition; verification command: "
        "`uv run pytest -q tests/test_entropy_audit_script.py`.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0


def test_compatibility_facade_guard_reports_chain_non_forwarding_implementation_until_inventory_updates(
    tmp_path: Path,
) -> None:
    base_ref = _setup_compatibility_facade_guard_fixture(tmp_path)
    chain_path = tmp_path / "services" / "orchestrator" / "chain.py"
    _write(
        chain_path,
        chain_path.read_text(encoding="utf-8")
        + "\n"
        + "def new_chain_policy(value: object) -> dict[str, str]:\n"
        + "    normalized = str(value).strip()\n"
        + "    return {\"value\": normalized}\n",
    )

    signals = _compatibility_facade_signals(tmp_path, base_ref, "new-non-forwarding-implementation")

    assert [signal["message_key"] for signal in signals] == [
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required"
    ]
    assert signals[0]["path"] == "services/orchestrator/chain.py"
    assert signals[0]["inventory_tokens"] == ["new_chain_policy", "new_chain_policy"]
    assert "new_chain_policy" in str(signals[0]["detail"])
    _assert_compatibility_facade_report_only_finding(
        tmp_path,
        base_ref,
        "compatibility-facade-growth.new-non-forwarding-implementation.inventory-required",
    )

    _append_inventory_line(
        tmp_path,
        "docs/governance/CHAIN_COMPATIBILITY_INVENTORY.md",
        "new_chain_policy owner module cannot host local glue; "
        "follow-up issue #999; removal condition after owner extraction.",
    )

    assert _compatibility_facade_guard(tmp_path, base_ref)["signal_count"] == 0
