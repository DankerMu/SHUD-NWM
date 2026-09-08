"""C1/C2/C3 exact-SHA receipt contracts. No live DB, API, browser, or network."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from packages.common.node27_issue1895_display_runtime import (
    DISPLAY_ENV_PATH,
    EXPECTED_RUNTIME,
    bind_c1_receipt,
    observe_display_runtime,
    parse_display_port,
)
from packages.common.node27_issue1895_http import fetch_local_expected_status
from packages.common.node27_issue1895_private_receipt import (
    read_held_private_bytes,
    read_held_private_json,
    read_held_private_text,
    read_private_receipt,
)
from packages.common.node27_issue1895_readonly_accept import accept_c2_evidence, bind_c2_receipt, validate_c2_receipt
from packages.common.node27_issue1895_types import Issue1895ReadinessError
from services.production_closure.readonly_db_validation import AUTHORITATIVE_EVIDENCE_FILENAMES

ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
WRONG_SHA = "b" * 40
INVALID_SHA = "not-a-sha"
PAST_BRACKET = ("2020-01-01T00:00:00Z", "2020-01-01T00:00:01Z")
REGISTRY_SCHEMA_VERSION = "nhms.scheduler.file_model_registry.v1"
NOW = "2026-09-06T12:00:00Z"
LATER = "2026-09-06T12:00:01Z"

def _current_bracket() -> tuple[str, str]:
    now = datetime.now(UTC)
    start = (now - timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    end = (now + timedelta(seconds=5)).isoformat().replace("+00:00", "Z")
    return start, end


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.body = body
        self.status = status
        self.code = status
        self.headers = headers or {}
        self.offset = 0
        self.closed = False

    def read(self, size: int = -1) -> bytes:
        if size < 0:
            size = len(self.body) - self.offset
        output = self.body[self.offset : self.offset + size]
        self.offset += len(output)
        return output

    def close(self) -> None:
        self.closed = True


class FakeOpener:
    def __init__(self, responses: dict[str, FakeResponse | Exception]) -> None:
        self.responses = responses
        self.requests: list[Any] = []

    def open(self, request: Any, timeout: int) -> FakeResponse:
        assert timeout in {5, 10}
        self.requests.append(request)
        response = self.responses[request.full_url]
        if isinstance(response, Exception):
            raise response
        return response


def _private(parent: Path) -> None:
    parent.mkdir(parents=True, exist_ok=True)
    os.chmod(parent, 0o700)


def _now_values() -> Any:
    return lambda: datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _c1_show(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[],
        returncode=0,
        stdout=(
            "MainPID=71\nActiveState=active\n"
            "ControlGroup=/user.slice/nhms-display-api.service\nId=nhms-display-api.service\n"
        ),
        stderr="",
    )


def _c1_opener() -> FakeOpener:
    origin = "http://127.0.0.1:18080"
    return FakeOpener(
        {
            f"{origin}/health": FakeResponse(b'{"status":"ok","service":"nhms-api"}'),
            f"{origin}/api/v1/runtime/config": FakeResponse(
                json.dumps({"status": "ok", "data": EXPECTED_RUNTIME}).encode()
            ),
            f"{origin}/api/v1/slurm/health": HTTPError(
                f"{origin}/api/v1/slurm/health",
                404,
                "not found",
                hdrs={},
                fp=FakeResponse(b'{"detail":"Not Found"}', status=404),
            ),
        }
    )


def test_c1_port_parser_refuses_ambiguous_assignment_forms() -> None:
    assert parse_display_port("DATABASE_URL=postgresql://ignored\n") == 8080
    assert parse_display_port("NHMS_DISPLAY_API_PORT=18080\n") == 18080
    for bad in (
        "NHMS_DISPLAY_API_PORT=08080\n",
        "NHMS_DISPLAY_API_PORT='8080'\n",
        "NHMS_DISPLAY_API_PORT=$PORT\n",
        "NHMS_DISPLAY_API_PORT=8080 # comment\n",
        "NHMS_DISPLAY_API_PORT=8080\nNHMS_DISPLAY_API_PORT=8081\n",
        "NHMS_DISPLAY_API_PORT=65536\n",
    ):
        with pytest.raises(Issue1895ReadinessError):
            parse_display_port(bad)


def test_c1_reader_refuses_nonabsolute_or_wrong_basename_before_any_read(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    env = private / "display.env"
    env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    from packages.common.node27_issue1895_display_runtime import read_display_port

    with pytest.raises(Issue1895ReadinessError) as relative:
        read_display_port(Path("display.env"))
    assert relative.value.code == "C1_DISPLAY_ENV_PATH"
    with pytest.raises(Issue1895ReadinessError) as mismatched_absolute:
        read_display_port(env)
    assert mismatched_absolute.value.code == "C1_DISPLAY_ENV_PATH"
    with pytest.raises(Issue1895ReadinessError) as wrong_basename:
        read_display_port(private / "other.env", expected_display_env=private / "other.env")
    assert wrong_basename.value.code == "C1_DISPLAY_ENV_PATH"
    assert DISPLAY_ENV_PATH == Path("/home/nwm/NWM/infra/env/display.env")


@pytest.mark.parametrize("mode", (0o640, 0o666))
def test_c1_reader_refuses_nonprivate_display_env(tmp_path: Path, mode: int) -> None:
    private = tmp_path / "private"
    _private(private)
    env = private / "display.env"
    env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(env, mode)
    from packages.common.node27_issue1895_display_runtime import read_display_port

    with pytest.raises(Issue1895ReadinessError) as nonprivate:
        read_display_port(env, expected_display_env=env)
    assert nonprivate.value.code == "C1_DISPLAY_ENV_INVALID"


def test_c1_reader_refuses_symlinked_display_env_parent(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    _private(real_parent)
    real_env = real_parent / "display.env"
    real_env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(real_env, 0o600)
    linked_parent = tmp_path / "linked"
    linked_parent.symlink_to(real_parent, target_is_directory=True)
    from packages.common.node27_issue1895_display_runtime import read_display_port

    with pytest.raises(Issue1895ReadinessError) as symlinked_parent:
        read_display_port(linked_parent / "display.env", expected_display_env=linked_parent / "display.env")
    assert symlinked_parent.value.code == "C1_DISPLAY_ENV_UNREADABLE"


def test_c1_owner_binds_process_http_and_private_pass_receipt(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    env = private / "display.env"
    env.write_text("DATABASE_URL=postgresql://nhms_display_ro:secret@db/nhms\nNHMS_DISPLAY_API_PORT=18080\n")
    os.chmod(env, 0o600)
    receipt = private / "c1.json"
    opener = _c1_opener()
    document = observe_display_runtime(
        display_env=env,
        receipt_path=receipt,
        head_sha=SHA,
        reviewed_sha=SHA,
        opener=opener,
        run_systemctl=_c1_show,
        read_cgroup=lambda pid: f"0::/user.slice/nhms-display-api.service/{pid}",
        now=_now_values(),
        expected_display_env=env,
    )
    assert document["checks"]["slurm_health_status"] == 404
    assert "postgresql" not in receipt.read_text()
    assert receipt.stat().st_mode & 0o777 == 0o600
    cmd_start, cmd_end = _current_bracket()
    bind_c1_receipt(receipt, reviewed_sha=SHA, expected_port=18080, cmd_start=cmd_start, cmd_end=cmd_end)
    raw = json.loads(receipt.read_text())
    raw["checks"]["runtime_exact"] = False
    receipt.write_text(json.dumps(raw), encoding="utf-8")
    os.chmod(receipt, 0o600)
    with pytest.raises(Issue1895ReadinessError):
        bind_c1_receipt(receipt, reviewed_sha=SHA, expected_port=18080, cmd_start=cmd_start, cmd_end=cmd_end)


def test_private_receipt_and_http_json_readers_close_deep_json_without_traceback(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    receipt = private / "deep.json"
    receipt.write_text("{" * 1100 + "}" * 1100, encoding="utf-8")
    os.chmod(receipt, 0o600)
    with pytest.raises(Issue1895ReadinessError) as receipt_error:
        read_private_receipt(receipt, code_prefix="TEST_RECEIPT")
    assert receipt_error.value.code == "TEST_RECEIPT_JSON"
    wide_receipt = private / "wide.json"
    wide_receipt.write_text(json.dumps({str(index): index for index in range(10_001)}), encoding="utf-8")
    os.chmod(wide_receipt, 0o600)
    with pytest.raises(Issue1895ReadinessError) as width_error:
        read_private_receipt(wide_receipt, code_prefix="TEST_RECEIPT")
    assert width_error.value.code == "TEST_RECEIPT_JSON"
    from packages.common.node27_issue1895_http import read_bounded_json_body

    response = FakeResponse(("[" * 1100 + "]" * 1100).encode())
    with pytest.raises(Issue1895ReadinessError) as http_error:
        read_bounded_json_body(response, body_limit=10_000, stage="test")
    assert http_error.value.code == "API_BODY_INVALID"


def test_held_private_reader_refuses_parent_mode_0755(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    target = private / "input.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    os.chmod(target, 0o600)
    os.chmod(private, 0o755)
    with pytest.raises(Issue1895ReadinessError) as refused:
        read_held_private_bytes(target, label="private input")
    assert refused.value.code in {"READINESS_INPUT_IDENTITY", "READINESS_INPUT_INVALID", "INPUT_PARENT_MODE"}
    with pytest.raises(Issue1895ReadinessError):
        read_held_private_text(target, label="private input")
    with pytest.raises(Issue1895ReadinessError):
        read_held_private_json(target, label="private input")


def test_held_private_reader_refuses_parent_inode_swap_during_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from packages.common import node27_issue1895_commit as commit

    private = tmp_path / "private"
    _private(private)
    target = private / "input.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    os.chmod(target, 0o600)
    seen = {"n": 0}
    real = commit._parent_facts

    def swap_on_restat(path: Path, *, code_prefix: str):
        info = real(path, code_prefix=code_prefix)
        seen["n"] += 1
        if seen["n"] != 2:
            return info
        parent = path.parent
        moved = parent.with_name(parent.name + ".moved")
        name = path.name
        parent.rename(moved)
        parent.mkdir()
        os.chmod(parent, 0o700)
        os.rename(moved / name, parent / name)
        os.rmdir(moved)
        return real(path, code_prefix=code_prefix)

    monkeypatch.setattr(commit, "_parent_facts", swap_on_restat)
    with pytest.raises(Issue1895ReadinessError) as refused:
        read_held_private_json(target, label="private input")
    assert refused.value.code in {
        "READINESS_INPUT_IDENTITY",
        "READINESS_INPUT_INVALID",
        "READINESS_INPUT_TOCTOU",
        "INPUT_PARENT_DRIFT",
        "INPUT_PARENT_MODE",
    }
    assert seen["n"] >= 1


def test_held_private_reader_accepts_mode_0700_parent_and_0600_file(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    target = private / "input.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    os.chmod(target, 0o600)
    raw, info = read_held_private_bytes(target, label="private input")
    assert json.loads(raw) == {"ok": True}
    assert info.st_nlink == 1
    assert read_held_private_text(target, label="private input") == '{"ok":true}'
    _raw, document, facts = read_held_private_json(target, label="private input")
    assert document == {"ok": True}
    assert isinstance(facts["st_mtime_ns"], int) and not isinstance(facts["st_mtime_ns"], bool)


def test_held_private_json_facts_mtime_ns_is_from_held_descriptor_not_later_path(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    target = private / "input.json"
    target.write_text('{"ok":true}', encoding="utf-8")
    os.chmod(target, 0o600)
    before = os.lstat(target)
    _raw, document, facts = read_held_private_json(target, label="private input")
    assert document == {"ok": True}
    assert facts["st_mtime_ns"] == int(before.st_mtime_ns)
    replacement = 2_000_000_000 if int(before.st_mtime_ns) != 2_000_000_000 else 3_000_000_000
    os.utime(target, ns=(replacement, replacement))
    after = os.lstat(target)
    assert int(after.st_mtime_ns) != facts["st_mtime_ns"]
    assert facts["st_dev"] == int(before.st_dev)
    assert facts["st_ino"] == int(before.st_ino)


def test_expected_status_http_reader_keeps_no_proxy_no_redirect_contract() -> None:
    url = "http://127.0.0.1:18080/api/v1/slurm/health"
    error = HTTPError(url, 404, "not found", hdrs={}, fp=FakeResponse(b"{}", status=404))
    opener = FakeOpener({url: error})
    status, body = fetch_local_expected_status(
        url=url, expected_status=404, opener=opener, timeout_seconds=10, body_limit=64, stage="test"
    )
    assert (status, body) == (404, b"{}")
    request = opener.requests[0]
    assert request.get_header("Accept-encoding") == "identity"
    assert not request.has_header("Authorization")


def _canonical_c2_files(root: Path, run_id: str) -> None:
    lane = root / run_id / "db" / "readonly-db-boundary"
    _private(lane)
    role = {"current_user": "nhms_display_ro", "role_type": "readonly_candidate"}
    routes = [{"name": "health", "status": "PASS"}]
    probes = [{"target": "hydro.hydro_run", "operations": [{"name": "INSERT", "status": "PASS"}]}]
    summary = {
        "schema": "nhms.readonly_db_boundary.evidence.v1",
        "status": "PASS",
        "run_id": run_id,
        "validation_provenance": {
            "mode": "live",
            "live_readonly_proof": True,
            "merged_source_evidence": True,
            "declared_sources": ["GFS", "IFS"],
            "reduced_scope": False,
            "source_bundle_count": 2,
            "source_artifacts": [{"sources": ["GFS"]}, {"sources": ["IFS"]}],
        },
        "display_identity": {"GFS": {"source": "GFS"}, "IFS": {"source": "IFS"}},
        "role": role,
        "route_smoke": routes,
        "permission_probes": probes,
        "runtime": {"service_role": "display_readonly", "control_mutations_expected": False},
    }
    for name, value in {
        "summary.json": summary,
        "role.json": role,
        "route_smoke.json": routes,
        "permission_probes.json": probes,
    }.items():
        path = lane / name
        path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(path, 0o600)


def test_c2_acceptance_binds_all_canonical_files_and_rejects_tamper(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    _private(root)
    run_id = "issue1895-readonly-now"
    _canonical_c2_files(root, run_id)
    receipt_parent = tmp_path / "private"
    _private(receipt_parent)
    receipt = receipt_parent / "c2.json"
    accepted = accept_c2_evidence(
        evidence_root=root, run_id=run_id, receipt_path=receipt, head_sha=SHA, reviewed_sha=SHA, now=_now_values()
    )
    assert set(accepted["authoritative_files"]) == set(AUTHORITATIVE_EVIDENCE_FILENAMES)
    assert accepted["checks"]["summary_full_gfs_ifs_scope"] == {
        "merged_source_evidence": True,
        "declared_sources": ["GFS", "IFS"],
        "reduced_scope": False,
        "source_bundle_count": 2,
        "source_artifact_sources": [["GFS"], ["IFS"]],
        "display_identity_sources": ["GFS", "IFS"],
    }
    cmd_start, cmd_end = _current_bracket()
    bind_c2_receipt(receipt, evidence_root=root, run_id=run_id, reviewed_sha=SHA, cmd_start=cmd_start, cmd_end=cmd_end)
    summary = root / run_id / "db" / "readonly-db-boundary" / "summary.json"
    summary.write_text(summary.read_text() + " ", encoding="utf-8")
    os.chmod(summary, 0o600)
    with pytest.raises(Issue1895ReadinessError) as tampered:
        bind_c2_receipt(
            receipt, evidence_root=root, run_id=run_id, reviewed_sha=SHA, cmd_start=cmd_start, cmd_end=cmd_end
        )
    assert tampered.value.code == "C2_BIND_TAMPER"


def test_c2_receipt_refuses_boolean_or_inconsistent_full_scope_facts(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    _private(root)
    run_id = "issue1895-readonly-scope"
    _canonical_c2_files(root, run_id)
    receipt_parent = tmp_path / "private"
    _private(receipt_parent)
    receipt = receipt_parent / "c2.json"
    document = accept_c2_evidence(
        evidence_root=root, run_id=run_id, receipt_path=receipt, head_sha=SHA, reviewed_sha=SHA, now=_now_values()
    )
    boolean_scope = {**document, "checks": {**document["checks"], "summary_full_gfs_ifs_scope": True}}
    with pytest.raises(Issue1895ReadinessError) as boolean_rejected:
        validate_c2_receipt(boolean_scope)
    assert boolean_rejected.value.code == "C2_RECEIPT_SCOPE"
    inconsistent_scope = {
        **document,
        "checks": {
            **document["checks"],
            "summary_full_gfs_ifs_scope": {
                **document["checks"]["summary_full_gfs_ifs_scope"],
                "declared_sources": ["IFS", "GFS"],
            },
        },
    }
    with pytest.raises(Issue1895ReadinessError) as inconsistent_rejected:
        validate_c2_receipt(inconsistent_scope)
    assert inconsistent_rejected.value.code == "C2_RECEIPT_SCOPE"
    receipt_scope = json.loads(receipt.read_text(encoding="utf-8"))
    receipt_scope["checks"]["summary_full_gfs_ifs_scope"]["declared_sources"] = ["IFS", "GFS"]
    receipt.write_text(json.dumps(receipt_scope), encoding="utf-8")
    os.chmod(receipt, 0o600)
    cmd_start, cmd_end = _current_bracket()
    with pytest.raises(Issue1895ReadinessError) as binder_rejected:
        bind_c2_receipt(
            receipt,
            evidence_root=root,
            run_id=run_id,
            reviewed_sha=SHA,
            cmd_start=cmd_start,
            cmd_end=cmd_end,
        )
    assert binder_rejected.value.code == "C2_RECEIPT_SCOPE"


@pytest.mark.parametrize("mode,status", [("simulated", "PASS"), ("live", "FAIL")])
def test_c2_acceptance_refuses_nonlive_or_nonpass_canonical_summary(tmp_path: Path, mode: str, status: str) -> None:
    root = tmp_path / "artifacts"
    _private(root)
    run_id = "issue1895-readonly-bad"
    _canonical_c2_files(root, run_id)
    summary_path = root / run_id / "db" / "readonly-db-boundary" / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["status"] = status
    summary["validation_provenance"]["mode"] = mode
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    os.chmod(summary_path, 0o600)
    receipt_parent = tmp_path / "private"
    _private(receipt_parent)
    with pytest.raises(Issue1895ReadinessError):
        accept_c2_evidence(
            evidence_root=root,
            run_id=run_id,
            receipt_path=receipt_parent / "c2.json",
            head_sha=SHA,
            reviewed_sha=SHA,
            now=_now_values(),
        )


def test_c2_acceptance_refuses_reduced_single_source_canonical_summary(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    _private(root)
    run_id = "issue1895-readonly-reduced"
    _canonical_c2_files(root, run_id)
    summary_path = root / run_id / "db" / "readonly-db-boundary" / "summary.json"
    summary = json.loads(summary_path.read_text())
    summary["validation_provenance"].update(
        {
            "declared_sources": ["GFS"],
            "reduced_scope": True,
            "source_bundle_count": 1,
            "source_artifacts": [{"sources": ["GFS"]}],
        }
    )
    summary["display_identity"] = {"GFS": {"source": "GFS"}}
    summary_path.write_text(json.dumps(summary), encoding="utf-8")
    os.chmod(summary_path, 0o600)
    receipt_parent = tmp_path / "private"
    _private(receipt_parent)
    with pytest.raises(Issue1895ReadinessError) as reduced:
        accept_c2_evidence(
            evidence_root=root,
            run_id=run_id,
            receipt_path=receipt_parent / "c2.json",
            head_sha=SHA,
            reviewed_sha=SHA,
            now=_now_values(),
        )
    assert reduced.value.code == "C2_SUMMARY_SCOPE"


def test_c2_acceptance_refuses_permission_probe_that_is_not_denied_write_pass(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    _private(root)
    run_id = "issue1895-readonly-probe-fail"
    _canonical_c2_files(root, run_id)
    lane = root / run_id / "db" / "readonly-db-boundary"
    summary_path = lane / "summary.json"
    probes_path = lane / "permission_probes.json"
    summary = json.loads(summary_path.read_text())
    probes = json.loads(probes_path.read_text())
    probes[0]["operations"][0]["status"] = "FAIL"
    summary["permission_probes"] = probes
    for path, payload in ((summary_path, summary), (probes_path, probes)):
        path.write_text(json.dumps(payload), encoding="utf-8")
        os.chmod(path, 0o600)
    receipt_parent = tmp_path / "private"
    _private(receipt_parent)
    with pytest.raises(Issue1895ReadinessError) as failed_probe:
        accept_c2_evidence(
            evidence_root=root,
            run_id=run_id,
            receipt_path=receipt_parent / "c2.json",
            head_sha=SHA,
            reviewed_sha=SHA,
            now=_now_values(),
        )
    assert failed_probe.value.code == "C2_SUMMARY_PROBES"


def test_c1_owner_refuses_invalid_or_mismatched_sha_without_publishing(tmp_path: Path) -> None:
    receipt = tmp_path / "c1.json"
    with pytest.raises(Issue1895ReadinessError) as invalid:
        observe_display_runtime(
            display_env=tmp_path / "display.env",
            receipt_path=receipt,
            head_sha=INVALID_SHA,
            reviewed_sha=INVALID_SHA,
        )
    assert invalid.value.code == "READINESS_SHA_INVALID"
    assert not receipt.exists()
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        observe_display_runtime(
            display_env=tmp_path / "display.env",
            receipt_path=receipt,
            head_sha=SHA,
            reviewed_sha=WRONG_SHA,
        )
    assert mismatch.value.code == "READINESS_SHA_MISMATCH"
    assert not receipt.exists()


def test_c1_binder_refuses_wrong_invalid_sha_and_out_of_bracket(tmp_path: Path) -> None:
    private = tmp_path / "private"
    _private(private)
    env = private / "display.env"
    env.write_text("NHMS_DISPLAY_API_PORT=18080\n", encoding="utf-8")
    os.chmod(env, 0o600)
    receipt = private / "c1.json"
    observe_display_runtime(
        display_env=env,
        receipt_path=receipt,
        head_sha=SHA,
        reviewed_sha=SHA,
        opener=_c1_opener(),
        run_systemctl=_c1_show,
        read_cgroup=lambda pid: f"0::/user.slice/nhms-display-api.service/{pid}",
        now=_now_values(),
        expected_display_env=env,
    )
    original = receipt.read_bytes()
    cmd_start, cmd_end = _current_bracket()
    with pytest.raises(Issue1895ReadinessError) as wrong:
        bind_c1_receipt(receipt, reviewed_sha=WRONG_SHA, expected_port=18080, cmd_start=cmd_start, cmd_end=cmd_end)
    assert wrong.value.code == "C1_BIND_SHA"
    assert receipt.read_bytes() == original
    with pytest.raises(Issue1895ReadinessError) as invalid:
        bind_c1_receipt(receipt, reviewed_sha=INVALID_SHA, expected_port=18080, cmd_start=cmd_start, cmd_end=cmd_end)
    assert invalid.value.code == "READINESS_SHA_INVALID"
    assert receipt.read_bytes() == original
    with pytest.raises(Issue1895ReadinessError) as bracket:
        bind_c1_receipt(
            receipt,
            reviewed_sha=SHA,
            expected_port=18080,
            cmd_start=PAST_BRACKET[0],
            cmd_end=PAST_BRACKET[1],
        )
    assert bracket.value.code == "READINESS_BRACKET"
    assert receipt.read_bytes() == original


def test_c2_owner_refuses_invalid_or_mismatched_sha_without_publishing(tmp_path: Path) -> None:
    receipt = tmp_path / "c2.json"
    with pytest.raises(Issue1895ReadinessError) as invalid:
        accept_c2_evidence(
            evidence_root=tmp_path,
            run_id="issue1895-readonly-now",
            receipt_path=receipt,
            head_sha=INVALID_SHA,
            reviewed_sha=INVALID_SHA,
        )
    assert invalid.value.code == "READINESS_SHA_INVALID"
    assert not receipt.exists()
    with pytest.raises(Issue1895ReadinessError) as mismatch:
        accept_c2_evidence(
            evidence_root=tmp_path,
            run_id="issue1895-readonly-now",
            receipt_path=receipt,
            head_sha=SHA,
            reviewed_sha=WRONG_SHA,
        )
    assert mismatch.value.code == "READINESS_SHA_MISMATCH"
    assert not receipt.exists()


def test_c2_binder_refuses_wrong_invalid_sha_and_out_of_bracket(tmp_path: Path) -> None:
    root = tmp_path / "artifacts"
    _private(root)
    run_id = "issue1895-readonly-now"
    _canonical_c2_files(root, run_id)
    receipt_parent = tmp_path / "private"
    _private(receipt_parent)
    receipt = receipt_parent / "c2.json"
    accept_c2_evidence(
        evidence_root=root, run_id=run_id, receipt_path=receipt, head_sha=SHA, reviewed_sha=SHA, now=_now_values()
    )
    original = receipt.read_bytes()
    cmd_start, cmd_end = _current_bracket()
    with pytest.raises(Issue1895ReadinessError) as wrong:
        bind_c2_receipt(
            receipt, evidence_root=root, run_id=run_id, reviewed_sha=WRONG_SHA, cmd_start=cmd_start, cmd_end=cmd_end
        )
    assert wrong.value.code == "C2_BIND_SHA"
    assert receipt.read_bytes() == original
    with pytest.raises(Issue1895ReadinessError) as invalid:
        bind_c2_receipt(
            receipt, evidence_root=root, run_id=run_id, reviewed_sha=INVALID_SHA, cmd_start=cmd_start, cmd_end=cmd_end
        )
    assert invalid.value.code == "READINESS_SHA_INVALID"
    assert receipt.read_bytes() == original
    with pytest.raises(Issue1895ReadinessError) as bracket:
        bind_c2_receipt(
            receipt,
            evidence_root=root,
            run_id=run_id,
            reviewed_sha=SHA,
            cmd_start=PAST_BRACKET[0],
            cmd_end=PAST_BRACKET[1],
        )
    assert bracket.value.code == "READINESS_BRACKET"
    assert receipt.read_bytes() == original
