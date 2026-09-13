"""Behavioral admission tests for descriptor-bound PGDATA hardware evidence."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from packages.common.node27_pgdata_evidence import (
    EvidencePolicy,
    parse_mdadm_evidence,
    parse_smart_evidence,
    verify_root_storage_evidence,
)

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
HOST = "node27-synthetic"
ARRAY = "/dev/md0"
MEMBERS = ("/dev/sdb1", "/dev/sdc1")


def _policy() -> EvidencePolicy:
    return EvidencePolicy(
        expected_hostname=HOST,
        array_device=ARRAY,
        max_age_seconds=300,
        expected_uid=os.getuid(),
        approved_modes=(0o600,),
        mdadm_argv=("/usr/sbin/mdadm", "--detail", ARRAY),
        smartctl_prefix=("/usr/sbin/smartctl",),
        backup_argv=("/usr/local/sbin/nhms-backup-inventory", "--json"),
        expected_pgdata="/home/synthetic/pgdata",
    )


def _write_evidence(path: Path, payload: dict, *, mode: int = 0o600) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.chmod(mode)
    return path


def _envelope(*, command: list[str], subject: dict, output: str, captured_at: datetime = NOW) -> dict:
    return {
        "schema_version": "1.0",
        "captured_at": captured_at.isoformat().replace("+00:00", "Z"),
        "hostname": HOST,
        "command": {"argv": command},
        "subject": subject,
        "output": output,
    }


def _healthy_mdadm() -> str:
    return """/dev/md0:
           Version : 1.2
        Raid Level : raid1
        Raid Devices : 2
       Total Devices : 2
         State : clean
 Active Devices : 2
Working Devices : 2
 Failed Devices : 0
  Spare Devices : 0

           Number   Major   Minor   RaidDevice State
              0       8       17        0      active sync   /dev/sdb1
              1       8       33        1      active sync   /dev/sdc1
"""


def _raid_file(tmp_path: Path, output: str, **changes: object) -> Path:
    payload = _envelope(
        command=["/usr/sbin/mdadm", "--detail", ARRAY],
        subject={"array_device": ARRAY},
        output=output,
    )
    payload.update(changes)
    return _write_evidence(tmp_path / "mdadm.json", payload)


def _smart_file(tmp_path: Path, device: str, output: str) -> Path:
    return _write_evidence(
        tmp_path / f"smart-{Path(device).name}.json",
        _envelope(
            command=["/usr/sbin/smartctl", "-H", device],
            subject={"device": device},
            output=output,
        ),
    )


def test_healthy_raid_and_two_descriptor_bound_smart_passes_are_admitted(tmp_path: Path) -> None:
    policy = _policy()
    raid = _raid_file(tmp_path, _healthy_mdadm())
    smart = {
        device: _smart_file(tmp_path, device, "SMART overall-health self-assessment test result: PASSED")
        for device in MEMBERS
    }

    health = verify_root_storage_evidence(raid, smart, policy=policy, now=NOW)

    assert health.healthy is True
    assert health.members == MEMBERS
    assert {item.device for item in health.smart} == set(MEMBERS)
    assert all(item.status == "PASS" for item in health.smart)


@pytest.mark.parametrize(
    ("label", "output"),
    [
        (
            "degraded",
            _healthy_mdadm()
            .replace("Active Devices : 2", "Active Devices : 1")
            .replace("State : clean", "State : clean, degraded"),
        ),
        ("rebuilding", _healthy_mdadm().replace("State : clean", "State : clean, resyncing")),
        ("recovering", _healthy_mdadm().replace("State : clean", "State : clean, recovering")),
        ("reshaping", _healthy_mdadm().replace("State : clean", "State : clean, reshaping")),
        ("missing", _healthy_mdadm().replace("/dev/sdc1", "removed")),
        ("substituted", _healthy_mdadm().replace("active sync   /dev/sdc1", "spare rebuilding   /dev/sdc1")),
        ("unknown", _healthy_mdadm().replace("State : clean", "State : mysterious")),
    ],
)
def test_nonhealthy_raid_states_are_blockers(tmp_path: Path, label: str, output: str) -> None:
    parsed = parse_mdadm_evidence(_raid_file(tmp_path, output), policy=_policy(), now=NOW)

    assert parsed.healthy is False, label
    assert parsed.blockers, label


@pytest.mark.parametrize(
    ("first", "second", "expected"),
    [
        (
            "SMART overall-health self-assessment test result: PASSED",
            "SMART overall-health self-assessment test result: PASSED",
            True,
        ),
        (
            "SMART overall-health self-assessment test result: PASSED",
            "SMART overall-health self-assessment test result: FAILED",
            False,
        ),
        ("SMART status unavailable", "SMART overall-health self-assessment test result: PASSED", False),
    ],
)
def test_smart_requires_each_parsed_member_to_explicitly_pass(
    tmp_path: Path, first: str, second: str, expected: bool
) -> None:
    policy = _policy()
    raid = _raid_file(tmp_path, _healthy_mdadm())
    smart = {
        MEMBERS[0]: _smart_file(tmp_path, MEMBERS[0], first),
        MEMBERS[1]: _smart_file(tmp_path, MEMBERS[1], second),
    }

    health = verify_root_storage_evidence(raid, smart, policy=policy, now=NOW)

    assert health.healthy is expected


def test_descriptor_binding_rejects_stale_wrong_mode_wrong_command_and_subject(tmp_path: Path) -> None:
    policy = _policy()
    stale = _raid_file(tmp_path, _healthy_mdadm(), captured_at=(NOW - timedelta(seconds=301)).isoformat())
    # The fixture deliberately changes a descriptor-owned field after constructing
    # it; none of these fields may be trusted from the text body alone.
    for path, matcher in ((stale, "captured_at"),):
        with pytest.raises(ValueError, match=matcher):
            parse_mdadm_evidence(path, policy=policy, now=NOW)

    wrong_mode = _raid_file(tmp_path, _healthy_mdadm())
    wrong_mode.chmod(0o644)
    with pytest.raises(ValueError, match="mode"):
        parse_mdadm_evidence(wrong_mode, policy=policy, now=NOW)

    bad_command = _raid_file(tmp_path, _healthy_mdadm())
    payload = json.loads(bad_command.read_text(encoding="utf-8"))
    payload["command"] = {"argv": ["/bin/sh", "-c", "mdadm --detail /dev/md0"]}
    _write_evidence(bad_command, payload)
    with pytest.raises(ValueError, match="command"):
        parse_mdadm_evidence(bad_command, policy=policy, now=NOW)

    bad_subject = _raid_file(tmp_path, _healthy_mdadm())
    payload = json.loads(bad_subject.read_text(encoding="utf-8"))
    payload["subject"] = {"array_device": "/dev/md9"}
    _write_evidence(bad_subject, payload)
    with pytest.raises(ValueError, match="subject"):
        parse_mdadm_evidence(bad_subject, policy=policy, now=NOW)


def test_single_smart_evidence_cannot_be_reused_for_both_member_identities(tmp_path: Path) -> None:
    policy = _policy()
    member = _smart_file(tmp_path, MEMBERS[0], "SMART overall-health self-assessment test result: PASSED")

    with pytest.raises(ValueError, match="identity"):
        parse_smart_evidence(member, device=MEMBERS[1], policy=policy, now=NOW)
