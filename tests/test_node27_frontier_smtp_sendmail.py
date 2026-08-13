"""Unit pins for the authenticated-SMTP sendmail shim (issue #1368).

The shim replaces node-27's null-routed local ``/usr/sbin/sendmail`` for the
frontier stall alert lane. Every test injects a fake SMTP object and a fake
environment — zero network, zero credentials, no sleeps. The one subprocess
test deliberately fails at config time, before any socket exists.

Invariant pinned on EVERY failure path: the value of ``NHMS_SMTP_PASS`` never
appears in stdout or stderr.
"""

from __future__ import annotations

import os
import smtplib
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_frontier_smtp_sendmail as shim

PASSWORD = "AUTHCODE-do-not-log-9f2a"
USER = "alerts@example.invalid"
REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM_PATH = REPO_ROOT / "scripts/node27_frontier_smtp_sendmail.py"


# ---------------------------------------------------------------------------
# Fakes / helpers.
# ---------------------------------------------------------------------------


class _MustNotHappen(BaseException):
    """BaseException on purpose: the shim contains every ``Exception`` as an
    internal failure, which would quietly absorb a "must not connect" guard
    violation and turn a real leak into a green test."""


class FakeSMTP:
    """Records login/send_message/quit; raises or refuses on demand."""

    def __init__(
        self,
        *,
        login_error: BaseException | None = None,
        send_error: BaseException | None = None,
        refused: dict[str, Any] | None = None,
    ) -> None:
        self.login_error = login_error
        self.send_error = send_error
        self.refused = refused or {}
        self.logins: list[tuple[str, str]] = []
        self.sent: list[tuple[Any, str, list[str]]] = []
        self.quit_calls = 0

    def login(self, user: str, password: str) -> None:
        self.logins.append((user, password))
        if self.login_error is not None:
            raise self.login_error

    def send_message(self, message: Any, *, from_addr: str, to_addrs: Sequence[str]) -> dict[str, Any]:
        self.sent.append((message, from_addr, list(to_addrs)))
        if self.send_error is not None:
            raise self.send_error
        return dict(self.refused)

    def quit(self) -> None:
        self.quit_calls += 1


def _factory(smtp: FakeSMTP, calls: list[tuple[str, int, float]]):
    def factory(host: str, port: int, timeout: float) -> FakeSMTP:
        calls.append((host, port, timeout))
        return smtp

    return factory


def _exploding_factory(error: BaseException | None = None):
    def factory(host: str, port: int, timeout: float):  # pragma: no cover - must not run
        raise error or _MustNotHappen(f"must not connect to {host}:{port} (timeout={timeout})")

    return factory


def _env(**overrides: str) -> dict[str, str]:
    env = {"NHMS_SMTP_USER": USER, "NHMS_SMTP_PASS": PASSWORD}
    env.update(overrides)
    return {key: value for key, value in env.items() if value != ""}


def _message(
    *,
    to: str = "ops@example.invalid",
    cc: str | None = None,
    bcc: str | None = None,
    body: str = "frontier stalled since 2026-08-13T00:00:00+00:00\n",
) -> bytes:
    headers = [
        "From: NHMS Frontier Alert <nwm@node-27>",
        f"To: {to}",
    ]
    if cc is not None:
        headers.append(f"Cc: {cc}")
    if bcc is not None:
        headers.append(f"Bcc: {bcc}")
    headers += [
        "Subject: [NHMS] frontier-stalled: no progress for 4h",
        "X-NHMS-Alert-Event: frontier-stalled",
        "MIME-Version: 1.0",
        'Content-Type: text/plain; charset="utf-8"',
    ]
    return ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")


def _output(capsys: pytest.CaptureFixture[str]) -> tuple[str, str]:
    captured = capsys.readouterr()
    assert PASSWORD not in captured.err
    assert PASSWORD not in captured.out
    return captured.out, captured.err


def _run(
    argv: Sequence[str],
    stdin_bytes: bytes,
    env: Mapping[str, str],
    factory: Any,
) -> int:
    return shim.main(list(argv), stdin_bytes, dict(env), factory)


# ---------------------------------------------------------------------------
# Happy path.
# ---------------------------------------------------------------------------


def test_happy_path_sends_once_with_authenticated_envelope(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP()
    calls: list[tuple[str, int, float]] = []

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, calls))

    _, err = _output(capsys)
    assert rc == 0
    assert calls == [("smtp.163.com", 465, 30.0)]
    assert smtp.logins == [(USER, PASSWORD)]
    assert len(smtp.sent) == 1
    message, from_addr, to_addrs = smtp.sent[0]
    # Envelope sender is the authenticated account (163 rejects a mismatch);
    # the From header is left exactly as the lane built it.
    assert from_addr == USER
    assert to_addrs == ["ops@example.invalid"]
    assert message["From"] == "NHMS Frontier Alert <nwm@node-27>"
    assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
    assert smtp.quit_calls == 1


def test_bcc_is_stripped_from_the_message_but_kept_in_the_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    smtp = FakeSMTP()
    stdin = _message(to="ops@example.invalid", cc="dba@example.invalid", bcc="audit@example.invalid")

    rc = _run(["-t", "-i"], stdin, _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0
    message, _from_addr, to_addrs = smtp.sent[0]
    assert to_addrs == ["ops@example.invalid", "dba@example.invalid", "audit@example.invalid"]
    assert message["Bcc"] is None
    assert message["Cc"] == "dba@example.invalid"
    assert "recipients=3" in err


def test_host_and_port_come_from_the_environment(capsys: pytest.CaptureFixture[str]) -> None:
    calls: list[tuple[str, int, float]] = []

    rc = _run(
        ["-t", "-i"],
        _message(),
        _env(NHMS_SMTP_HOST="smtp.example.invalid", NHMS_SMTP_PORT="1465"),
        _factory(FakeSMTP(), calls),
    )

    _, err = _output(capsys)
    assert rc == 0
    assert calls == [("smtp.example.invalid", 1465, 30.0)]
    assert "host=smtp.example.invalid" in err


# ---------------------------------------------------------------------------
# Config / usage failures (exit 64, nothing connected).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("missing", ["NHMS_SMTP_USER", "NHMS_SMTP_PASS"])
def test_missing_credential_variable_is_named_and_nothing_connects(
    missing: str, capsys: pytest.CaptureFixture[str]
) -> None:
    env = _env(**{missing: ""})

    rc = _run(["-t", "-i"], _message(), env, _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert err.strip().count("\n") == 0
    assert err.startswith("SMTP-CONFIG-ERROR ")
    assert missing in err


@pytest.mark.parametrize("port", ["abc", "0", "70000", "-1", "465.0"])
def test_bad_port_is_a_config_error(port: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_PORT=port), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert "NHMS_SMTP_PORT" in err


def test_message_without_recipients_never_connects(capsys: pytest.CaptureFixture[str]) -> None:
    stdin = b"From: nwm@node-27\r\nSubject: orphan\r\n\r\nbody\r\n"

    rc = _run(["-t", "-i"], stdin, _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert "no recipients" in err


def test_positional_recipient_argument_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    """The lane never passes recipients positionally; if a caller starts to,
    the shim would silently deliver to the headers instead. Fail loud."""

    rc = _run(["-t", "-i", "someone@example.invalid"], _message(), _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert "someone@example.invalid" in err


def test_missing_dash_t_is_rejected(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["-i"], _message(), _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert "-t" in err


def test_unknown_flags_are_tolerated(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP()

    rc = _run(["-t", "-i", "-oi", "-v"], _message(), _env(), _factory(smtp, []))

    _output(capsys)
    assert rc == 0
    assert len(smtp.sent) == 1


def test_undecodable_stdin_is_handled_without_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    """Raw non-UTF-8 garbage must not become an unhandled exception; it parses
    into a headerless message and lands on the no-recipients arm."""

    rc = _run(["-t", "-i"], b"\xff\xfe\x00garbage\r\n\r\n\x80\x81", _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert "Traceback" not in err


# ---------------------------------------------------------------------------
# Delivery failures (exit 69).
# ---------------------------------------------------------------------------


def test_connect_failure_reports_stage_connect(capsys: pytest.CaptureFixture[str]) -> None:
    rc = _run(["-t", "-i"], _message(), _env(), _exploding_factory(OSError("connection refused")))

    _, err = _output(capsys)
    assert rc == 69
    assert err.strip().count("\n") == 0
    assert "SMTP-FAILED stage=connect host=smtp.163.com" in err
    assert "connection refused" in err


def test_login_failure_reports_stage_login_and_never_sends(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP(login_error=smtplib.SMTPAuthenticationError(535, b"535 Error: authentication failed"))

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 69
    assert "SMTP-FAILED stage=login" in err
    assert "SMTPAuthenticationError" in err
    assert smtp.sent == []
    assert smtp.quit_calls == 1


def test_send_failure_reports_stage_send(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP(send_error=smtplib.SMTPException("connection unexpectedly closed"))

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 69
    assert "SMTP-FAILED stage=send" in err
    assert smtp.quit_calls == 1


def test_socket_timeout_during_send_is_a_delivery_failure(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP(send_error=TimeoutError("timed out"))

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 69
    assert "stage=send" in err


def test_refused_recipients_are_a_failure_not_a_success(capsys: pytest.CaptureFixture[str]) -> None:
    """send_message returns normally for a partially refused envelope; treating
    that as success would re-create the very blind spot this shim closes."""

    smtp = FakeSMTP(refused={"ops@example.invalid": (550, b"550 recipient rejected")})

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 69
    assert err.startswith("SMTP-PARTIAL-REFUSAL host=smtp.163.com refused=1 ")
    assert "ops@example.invalid" in err
    assert "550" in err
    assert "SMTP-ACCEPTED" not in err


# ---------------------------------------------------------------------------
# Whole-class containment (exit 70).
# ---------------------------------------------------------------------------


def test_arbitrary_internal_failure_is_contained_as_exit_70(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exception class nobody enumerated (here MemoryError, which is neither
    OSError nor SMTPException) must still exit structurally. The lane records
    stderr verbatim; a traceback from a monitoring lane is a silent monitor."""

    def boom(_stdin_bytes: bytes):
        raise MemoryError("parser exploded")

    monkeypatch.setattr(shim, "parse_message", boom)

    rc = _run(["-t", "-i"], _message(), _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 70
    assert err.strip().count("\n") == 0
    assert err.startswith("SMTP-INTERNAL-ERROR MemoryError: ")
    assert "Traceback" not in err


def test_containment_also_covers_a_failure_after_connect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    smtp = FakeSMTP(send_error=MemoryError("out of memory mid-send"))

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 70
    assert "SMTP-INTERNAL-ERROR MemoryError" in err
    assert smtp.quit_calls == 1  # teardown still runs


def test_a_broken_quit_does_not_unsay_an_accepted_message(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP()

    def broken_quit() -> None:
        raise OSError("socket already gone")

    smtp.quit = broken_quit  # type: ignore[method-assign]

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0
    assert "SMTP-ACCEPTED" in err


# ---------------------------------------------------------------------------
# Entry wiring (real file, real interpreter, no socket).
# ---------------------------------------------------------------------------


def test_file_is_executable_with_a_python3_shebang() -> None:
    assert os.access(SHIM_PATH, os.X_OK)
    assert SHIM_PATH.read_bytes().startswith(b"#!/usr/bin/env python3\n")


def test_subprocess_without_credentials_exits_64_with_one_clean_line() -> None:
    """Proves the shebang + ``_entrypoint`` wiring, without a socket: config
    validation happens before anything is connected to."""

    result = subprocess.run(
        [str(SHIM_PATH), "-t", "-i"],
        input=_message(),
        env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        capture_output=True,
        timeout=60,
        check=False,
    )

    stderr = result.stderr.decode("utf-8", "replace")
    assert result.returncode == 64, stderr
    assert result.stdout == b""
    assert stderr.strip().count("\n") == 0
    assert "NHMS_SMTP_USER" in stderr
    assert "Traceback" not in stderr


def test_module_is_stdlib_only() -> None:
    """No third-party import may creep in: on node-27 the shim runs from the
    systemd lane's environment, not from the repo venv."""

    source = SHIM_PATH.read_text(encoding="utf-8")
    imported = {
        line.split()[1].split(".")[0]
        for line in source.splitlines()
        if line.startswith(("import ", "from ")) and "__future__" not in line
    }
    assert imported <= set(sys.stdlib_module_names), imported
