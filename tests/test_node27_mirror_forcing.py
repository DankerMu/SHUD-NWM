from __future__ import annotations

import json

import pytest

import scripts.node27_mirror_forcing as mirror


def test_missing_explicit_node22_dsn_returns_skip_without_display_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("N22_DSN", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql://display_user:display-secret@display.example/display")

    display_env = tmp_path / "display.env"
    display_env.write_text(
        "DATABASE_URL=postgresql://display_env_user:display-env-secret@display-env.example/display\n",
        encoding="utf-8",
    )
    if hasattr(mirror, "DISPLAY_ENV"):
        monkeypatch.setattr(mirror, "DISPLAY_ENV", display_env)

    def fail_mirror_forcing(**_: object) -> dict[str, object]:
        pytest.fail("missing node-22 DSN must skip before mirror_forcing is called")

    monkeypatch.setattr(mirror, "mirror_forcing", fail_mirror_forcing)

    rc = mirror.main(["--run-id", "run-no-dsn", "--object-store-root", str(tmp_path)])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == mirror.NODE22_DSN_MISSING_REASON
    assert payload["skipped"] is True
    assert payload["mirror_boundary"]["mode"] == mirror.TRANSITIONAL_MIRROR_MODE
    assert payload["mirror_boundary"]["dsn"]["source"] is None
    assert payload["mirror_boundary"]["forbidden_sources"] == [
        "infra/env/display.env",
        "display runtime DATABASE_URL",
    ]
    rendered = json.dumps(payload)
    assert "display-secret" not in rendered
    assert "display-env-secret" not in rendered


def test_cli_node22_dsn_is_used_and_report_records_transitional_boundary(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    node22_dsn = "postgresql://n22_user:n22-secret@node22.example:55432/nhms?sslpassword=top-secret"
    seen: dict[str, object] = {}

    def fake_mirror_forcing(**kwargs: object) -> dict[str, object]:
        seen.update(kwargs)
        return {
            "run_id": kwargs["run_id"],
            "forcing_version_id": "fv-cli",
            "model_id": "model-a",
            "source_id": "ifs",
            "basin_version_id": "basin-a",
            "station_timeseries": {"local_rows": 12},
        }

    monkeypatch.delenv("N22_DSN", raising=False)
    monkeypatch.setattr(mirror, "mirror_forcing", fake_mirror_forcing)

    rc = mirror.main(
        [
            "--run-id",
            "run-cli",
            "--object-store-root",
            str(tmp_path),
            "--node22-url",
            node22_dsn,
            "--allow-archived-node22-db-rollback-mirror",
        ]
    )

    assert rc == 0
    assert seen["node22_url"] == node22_dsn
    assert seen["node22_dsn_source"] == "cli:--node22-url"
    payload = json.loads(capsys.readouterr().out)
    boundary = payload["mirror_boundary"]
    assert boundary["mode"] == mirror.TRANSITIONAL_MIRROR_MODE
    assert boundary["purpose"] == mirror.TRANSITIONAL_MIRROR_PURPOSE
    assert boundary["compatibility_only"] is True
    assert boundary["dsn"] == {
        "source": "cli:--node22-url",
        "printed": False,
        "dsn_redacted": True,
    }
    assert boundary["source_boundary"]["access"] == "read_only"
    assert boundary["destination_boundary"]["role"] == "node-27 local data-plane"
    assert boundary["current_topology"]["node22_local_postgres"] == {
        "port": ":55433",
        "status": mirror.HISTORICAL_NODE22_PG_STATUS,
        "implicit_source_allowed": False,
    }
    assert "object-store forcing-domain handoff" in boundary["sunset_condition"]
    rendered = json.dumps(payload)
    assert node22_dsn not in rendered
    assert "n22-secret" not in rendered
    assert "top-secret" not in rendered


def test_env_node22_dsn_source_and_credential_redaction(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    node22_dsn = "postgresql://n22_user:n22-secret@node22.example:55432/nhms?sslpassword=top-secret"

    def fake_mirror_forcing(**kwargs: object) -> dict[str, object]:
        assert kwargs["node22_url"] == node22_dsn
        return {
            "run_id": kwargs["run_id"],
            "forcing_version_id": "fv-env",
            "debug": {
                "raw": node22_dsn,
                "message": f"connection failed password=leaked {node22_dsn}",
            },
        }

    monkeypatch.setenv("N22_DSN", node22_dsn)
    monkeypatch.setenv(mirror.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")
    monkeypatch.setattr(mirror, "mirror_forcing", fake_mirror_forcing)

    rc = mirror.main(["--run-id", "run-env", "--object-store-root", str(tmp_path)])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mirror_boundary"]["dsn"]["source"] == "env:N22_DSN"
    rendered = json.dumps(payload)
    assert node22_dsn not in rendered
    assert "n22_user" not in rendered
    assert "n22-secret" not in rendered
    assert "sslpassword" not in rendered
    assert "top-secret" not in rendered
    assert "leaked" not in rendered
    assert "[redacted]" in rendered


def test_unexpected_mirror_failure_is_structured_and_redacted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    node22_dsn = "postgresql://n22_user:n22-secret@node22.example:55432/nhms?sslpassword=top-secret"

    def fail_mirror_forcing(**_: object) -> dict[str, object]:
        raise RuntimeError(f"connection failed password=leaked {node22_dsn}")

    monkeypatch.setenv("N22_DSN", node22_dsn)
    monkeypatch.setenv(mirror.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, "true")
    monkeypatch.setattr(mirror, "mirror_forcing", fail_mirror_forcing)

    rc = mirror.main(["--run-id", "run-fail", "--object-store-root", str(tmp_path)])

    captured = capsys.readouterr()
    assert rc == 1
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["failed"] is True
    assert payload["reason"] == mirror.NODE22_MIRROR_FAILED_REASON
    assert payload["mirror_boundary"]["dsn"]["source"] == "env:N22_DSN"
    rendered = json.dumps(payload)
    assert node22_dsn not in rendered
    assert "n22_user" not in rendered
    assert "n22-secret" not in rendered
    assert "sslpassword" not in rendered
    assert "top-secret" not in rendered
    assert "leaked" not in rendered
    assert "[redacted]" in rendered


def test_configured_node22_dsn_requires_archived_rollback_allowance(
    monkeypatch: pytest.MonkeyPatch, tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("N22_DSN", "postgresql://n22_user:n22-secret@node22.example:55432/nhms")
    monkeypatch.delenv(mirror.ARCHIVED_NODE22_DB_ROLLBACK_MIRROR_ENV, raising=False)

    def fail_mirror_forcing(**_: object) -> dict[str, object]:
        pytest.fail("configured N22_DSN must not be used without archived rollback allowance")

    monkeypatch.setattr(mirror, "mirror_forcing", fail_mirror_forcing)

    rc = mirror.main(["--run-id", "run-no-allow", "--object-store-root", str(tmp_path)])

    assert rc == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] is True
    assert payload["reason"] == mirror.NODE22_ROLLBACK_MIRROR_NOT_ALLOWED_REASON
    assert payload["mirror_boundary"]["dsn"]["source"] == "env:N22_DSN"
    rendered = json.dumps(payload)
    assert "n22-secret" not in rendered
