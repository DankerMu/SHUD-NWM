"""Unit pins for the authenticated-SMTP sendmail shim (issue #1368).

The shim replaces node-27's null-routed local ``/usr/sbin/sendmail`` for the
frontier stall alert lane. Every test injects a fake SMTP object and a fake
environment — zero network, zero credentials. The one subprocess test
deliberately fails at config time, before any socket exists. The only sleeps
are in the B34 session-budget section, where a blocking peer IS the subject;
they run against a sub-second budget set through the env override and are meant
to be interrupted, never waited out (issue #1375).

Invariant pinned on EVERY failure path: the value of ``NHMS_SMTP_PASS`` never
appears in stdout or stderr.
"""

from __future__ import annotations

import ast
import base64
import email.policy
import io
import os
import signal
import smtplib
import ssl
import subprocess
import sys
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from email.generator import BytesGenerator
from email.parser import BytesParser
from pathlib import Path
from typing import Any

import pytest

from scripts import node27_frontier_smtp_sendmail as shim
from scripts import node27_frontier_stall_alert as alerter

PASSWORD = "AUTHCODE-do-not-log-9f2a"
USER = "alerts@example.invalid"
SENDER = f"NHMS Frontier Alert <{USER}>"
REPO_ROOT = Path(__file__).resolve().parents[1]
SHIM_PATH = REPO_ROOT / "scripts/node27_frontier_smtp_sendmail.py"


# ---------------------------------------------------------------------------
# Fakes / helpers.
# ---------------------------------------------------------------------------


class _MustNotHappen(BaseException):
    """BaseException on purpose: the shim contains every ``Exception`` as an
    internal failure, which would quietly absorb a "must not connect" guard
    violation and turn a real leak into a green test."""


def _wire_bytes(message: Any) -> bytes:
    """Exactly what ``smtplib.send_message`` would put on the wire."""

    buffer = io.BytesIO()
    BytesGenerator(buffer).flatten(message, linesep="\r\n")
    return buffer.getvalue()


class FakeSMTP:
    """Records ehlo/login/send_message/quit; raises or refuses on demand."""

    def __init__(
        self,
        *,
        login_error: BaseException | None = None,
        send_error: BaseException | None = None,
        refused: dict[str, Any] | None = None,
        extensions: Sequence[str] = ("8bitmime", "size"),
        ehlo_code: int = 250,
        ehlo_error: BaseException | None = None,
    ) -> None:
        self.login_error = login_error
        self.send_error = send_error
        self.refused = refused or {}
        self.extensions = {name.lower() for name in extensions}
        self.ehlo_code = ehlo_code
        self.ehlo_error = ehlo_error
        self.logins: list[tuple[str, str]] = []
        self.sent: list[tuple[Any, str, list[str]]] = []
        self.mail_options: list[tuple[str, ...]] = []
        self.wire: list[bytes] = []
        self.ehlo_calls = 0
        self.helo_calls = 0
        self.quit_calls = 0

    def ehlo(self, name: str = "") -> tuple[int, bytes]:
        self.ehlo_calls += 1
        if self.ehlo_error is not None:
            raise self.ehlo_error
        if self.ehlo_code != 250:
            self.extensions = set()  # a non-ESMTP server advertises nothing
        return self.ehlo_code, b"fake-smtp"

    def helo(self, name: str = "") -> tuple[int, bytes]:
        self.helo_calls += 1
        return 250, b"fake-smtp"

    def has_extn(self, opt: str) -> bool:
        return opt.lower() in self.extensions

    def login(self, user: str, password: str) -> None:
        self.logins.append((user, password))
        if self.login_error is not None:
            raise self.login_error

    def send_message(
        self,
        message: Any,
        *,
        from_addr: str,
        to_addrs: Sequence[str],
        mail_options: Sequence[str] = (),
    ) -> dict[str, Any]:
        self.sent.append((message, from_addr, list(to_addrs)))
        self.mail_options.append(tuple(mail_options))
        self.wire.append(_wire_bytes(message))
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
    sender: str = SENDER,
    to: str = "ops@example.invalid",
    cc: str | None = None,
    bcc: str | None = None,
    body: str = "frontier stalled since 2026-08-13T00:00:00+00:00\n",
) -> bytes:
    headers = [
        f"From: {sender}",
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


@pytest.fixture(autouse=True)
def sigalrm_stays_pristine() -> Any:
    """Autouse for this whole module: no test may leave SIGALRM state behind.

    The budget tests arm a real one-shot ``setitimer`` in the pytest process. A
    leaked alarm does not fail the test that leaked it — it fires minutes later,
    inside whatever unrelated test happens to be running (in a full-suite run
    this file is followed by ~98 more), and lands there as a
    ``_SessionBudgetExceeded`` naming a stage from a session that ended long
    ago. Per-file CI stays green while master's full run breaks somewhere else.

    Repair AND report, in that order: repairing alone would quietly absorb the
    next real leak, and reporting alone would leave the alarm ticking through
    the rest of the session.
    """

    baseline = signal.getsignal(signal.SIGALRM)
    yield
    timer = signal.getitimer(signal.ITIMER_REAL)
    handler = signal.getsignal(signal.SIGALRM)
    if timer != (0.0, 0.0):
        signal.setitimer(signal.ITIMER_REAL, 0)
    if handler != baseline:
        signal.signal(signal.SIGALRM, baseline)
    assert timer == (0.0, 0.0), f"test left a live SIGALRM timer: {timer}"
    assert handler == baseline, f"test left SIGALRM handler {handler!r}, not {baseline!r}"


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
    assert message["From"] == SENDER
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
    stdin = f"From: {USER}\r\nSubject: orphan\r\n\r\nbody\r\n".encode()

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
    systemd lane's environment, not from the repo venv.

    Parsed with ``ast`` rather than by line prefix: a lazy import inside a
    function is indented, and the prefix scan waved every one of those through
    — the exact shape a "just import requests where it is needed" edit takes.
    """

    tree = ast.parse(SHIM_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            # A relative import means the shim stopped being self-contained.
            imported.add(node.module.split(".")[0] if node.level == 0 and node.module else ".")
    imported.discard("__future__")

    assert imported, "the ast scan found no imports at all — it stopped scanning"
    assert imported <= set(sys.stdlib_module_names), imported


# ---------------------------------------------------------------------------
# B30 — the shipped factory speaks VERIFIED TLS.
# ---------------------------------------------------------------------------


def test_b30_default_factory_uses_smtps_with_a_verifying_context(monkeypatch: pytest.MonkeyPatch) -> None:
    """``SMTP_SSL``'s default context is ``ssl._create_stdlib_context()``:
    ``CERT_NONE`` + no hostname check. Under it the 163 authorization code goes
    to whoever answers the connection and the synchronous 250 — the entire
    evidence claim of this shim — is forgeable by anything on the path."""

    recorded: dict[str, Any] = {}

    class _RecordingSMTPSSL:
        def __init__(self, host: str, port: int, *args: Any, **kwargs: Any) -> None:
            recorded["args"] = (host, port, args)
            recorded["kwargs"] = kwargs

    def _plaintext_is_forbidden(*_args: Any, **_kwargs: Any):  # pragma: no cover - must not run
        raise _MustNotHappen("the shim must not open a plaintext SMTP session")

    monkeypatch.setattr(shim.smtplib, "SMTP_SSL", _RecordingSMTPSSL)
    monkeypatch.setattr(shim.smtplib, "SMTP", _plaintext_is_forbidden)

    shim.default_smtp_factory("smtp.163.com", 465, 30.0)

    assert recorded["args"] == ("smtp.163.com", 465, ())
    assert recorded["kwargs"]["timeout"] == 30.0
    context = recorded["kwargs"]["context"]
    assert isinstance(context, ssl.SSLContext)
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True


def test_b30_the_default_factory_is_what_main_uses(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pin above is only load-bearing if nothing else is wired in by
    default: ``main`` without an injected factory must reach it."""

    calls: list[tuple[str, int, float]] = []

    def _factory_spy(host: str, port: int, timeout: float) -> FakeSMTP:
        calls.append((host, port, timeout))
        return FakeSMTP()

    monkeypatch.setattr(shim, "default_smtp_factory", _factory_spy)

    assert shim.main(["-t", "-i"], _message(), _env()) == 0
    assert calls == [("smtp.163.com", 465, shim.SMTP_TIMEOUT_SEC)]


# ---------------------------------------------------------------------------
# B33 — From must be the authenticated account (fail closed, pre-connect).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "sender",
    [
        USER,
        f"<{USER}>",
        f"NHMS Frontier Alert <{USER}>",
        f"NHMS Frontier Alert <{USER.upper()}>",  # domains are case-insensitive
        f"前沿停摆告警 <{USER}>",  # non-ASCII display name is still just a display name
    ],
)
def test_b33_from_whose_addr_spec_is_the_account_is_accepted(
    sender: str, capsys: pytest.CaptureFixture[str]
) -> None:
    smtp = FakeSMTP()

    rc = _run(["-t", "-i"], _message(sender=sender), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    assert len(smtp.sent) == 1


@pytest.mark.parametrize(
    "sender",
    [
        "NHMS Frontier Alert <nwm@node-27>",  # the derived default: never deliverable
        "other@example.invalid",
        "NHMS Frontier Alert <>",
        "not-an-address",
    ],
)
def test_b33_from_that_is_not_the_account_exits_64_without_connecting(
    sender: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """163 refuses a From that differs from the authenticated account and the
    shim never rewrites the header, so this misconfiguration is a permanent
    stage=send failure — every 30 min, forever. Name both addresses once, at
    config time, before any socket exists."""

    rc = _run(["-t", "-i"], _message(sender=sender), _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert err.strip().count("\n") == 0
    assert err.startswith("SMTP-CONFIG-ERROR ")
    assert USER in err
    assert "NHMS_ALERT_EMAIL_FROM" in err


def test_b33_a_missing_from_header_exits_64_without_connecting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    stdin = b"To: ops@example.invalid\r\nSubject: no sender\r\n\r\nbody\r\n"

    rc = _run(["-t", "-i"], stdin, _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 64
    assert "no From header" in err


# ---------------------------------------------------------------------------
# B32 — the REAL lane message crosses the shim without raw 8-bit.
# ---------------------------------------------------------------------------


def _lane_message() -> bytes:
    """The genuine article: ``build_message`` from the alerter, with the body
    lines the stalled branch produces (Chinese runbook pointer + em dash),
    encoded exactly the way ``default_sendmail_runner`` encodes it."""

    config = alerter.config_from_env(
        {
            "DATABASE_URL": "postgresql://nhms_display_ro:pw@127.0.0.1:55432/nhms",
            "NHMS_ALERT_EMAIL_TO": "ops@example.invalid",
            "NHMS_ALERT_EMAIL_FROM": SENDER,
        }
    )
    body_lines = [
        "No directional frontier progress for 4h30m (threshold 4h).",
        "Last observed change: 2026-08-13T00:00:00+00:00",
        "",
        "Current observation:",
        "  - gfs: frontier=2026-08-12T18:00:00+00:00 cycles=40"
        " latest_created=2026-08-12T23:30:00+00:00",
        "",
        "The stall clock keeps running across query failures — the alerter"
        " never resets it in the operator's favour.",
        "",
        f"Runbook: {alerter.RUNBOOK_REFERENCE}",
    ]
    text = alerter.build_message(
        config,
        event=alerter.EVENT_STALLED,
        subject="[NHMS] frontier-stalled: no progress for 4h30m",
        now=datetime(2026, 8, 13, 4, 30, tzinfo=UTC),
        body_lines=body_lines,
    )
    return text.encode("utf-8")


def _split_wire(wire: bytes) -> tuple[bytes, bytes]:
    headers, _sep, body = wire.partition(b"\r\n\r\n")
    return headers, body


def test_b32_lane_message_declares_8bitmime_when_the_server_offers_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    smtp = FakeSMTP(extensions=("8bitmime",))

    rc = _run(["-t", "-i"], _lane_message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    assert smtp.ehlo_calls == 1 and smtp.helo_calls == 0
    _message_obj, from_addr, to_addrs = smtp.sent[0]
    assert from_addr == USER
    assert to_addrs == ["ops@example.invalid"]
    assert smtp.mail_options == [("BODY=8BITMIME",)]
    headers, body = _split_wire(smtp.wire[0])
    assert headers.isascii()
    assert b"unknown-8bit" not in headers
    assert alerter.RUNBOOK_REFERENCE.encode("utf-8") in body  # the 8-bit body IS declared
    # BODY=8BITMIME is the ENVELOPE half of the declaration. The part still needs
    # its own Content-Transfer-Encoding: RFC 2045 says an absent CTE means 7bit,
    # so raw UTF-8 with no CTE tells the receiver "these bytes are 7bit" while
    # they demonstrably are not — a conforming gateway may then strip the high
    # bit and the operator's only alert arrives as mojibake (RFC 6152 requires
    # the 8bit declaration).
    assert b"Content-Transfer-Encoding: 8bit" in headers
    assert headers.count(b"Content-Transfer-Encoding:") == 1  # declared once, not appended
    # And declaring it must NOT re-encode: on this branch the 8-bit bytes cross
    # byte-identical (only the line separator is normalised to CRLF).
    assert body == _lane_message().partition(b"\r\n\r\n")[2].replace(b"\n", b"\r\n")
    assert "SMTP-ACCEPTED" in err


def test_b32_lane_message_is_reencoded_when_8bitmime_is_absent(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A server without the extension gets a 7-bit clean message with an
    explicit CTE. Pushing the raw UTF-8 anyway is a protocol violation: strict
    servers reject it, lenient ones strip the high bit and the operator's only
    alert arrives as mojibake."""

    smtp = FakeSMTP(extensions=())

    rc = _run(["-t", "-i"], _lane_message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    assert smtp.mail_options == [()]
    wire = smtp.wire[0]
    assert wire.isascii(), "raw 8-bit pushed to a server that never advertised 8BITMIME"
    assert b"unknown-8bit" not in wire
    assert b"Content-Transfer-Encoding: quoted-printable" in wire
    # And it is a re-encoding, not a mutilation: the Chinese survives the trip.
    delivered = BytesParser(policy=email.policy.default).parsebytes(wire)
    assert alerter.RUNBOOK_REFERENCE in delivered.get_content()
    assert delivered["Subject"] == "[NHMS] frontier-stalled: no progress for 4h30m"


def test_b32_an_already_encoded_part_keeps_its_own_cte_on_the_8bitmime_branch(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The 8bit declaration is for parts that actually put 8-bit bytes on the
    wire. A base64 part's DECODED payload is 8-bit by construction while its
    wire bytes are ASCII — relabelling it ``8bit`` would tell the receiver to
    read base64 text as raw content, i.e. corrupt the part to fix nothing."""

    attachment = base64.b64encode("中文附件".encode()).decode("ascii")
    stdin = (
        f"From: {SENDER}\r\n"
        "To: ops@example.invalid\r\n"
        "Subject: mixed\r\n"
        "MIME-Version: 1.0\r\n"
        'Content-Type: multipart/mixed; boundary="B"\r\n'
        "\r\n"
        "--B\r\n"
        'Content-Type: text/plain; charset="utf-8"\r\n'
        "\r\n"
        "中文正文\r\n"
        "--B\r\n"
        'Content-Type: application/octet-stream; name="a.bin"\r\n'
        "Content-Transfer-Encoding: base64\r\n"
        "\r\n"
        f"{attachment}\r\n"
        "--B--\r\n"
    ).encode("utf-8")

    smtp = FakeSMTP(extensions=("8bitmime",))
    rc = _run(["-t", "-i"], stdin, _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    assert smtp.mail_options == [("BODY=8BITMIME",)]
    wire = smtp.wire[0]
    assert wire.count(b"Content-Transfer-Encoding: base64") == 1
    assert wire.count(b"Content-Transfer-Encoding: 8bit") == 1  # the text part only
    delivered = BytesParser(policy=email.policy.default).parsebytes(wire)
    text_part, bin_part = delivered.get_payload()
    assert text_part.get_content() == "中文正文"  # the CRLF before the boundary is the boundary's
    assert bin_part.get_payload(decode=True).decode("utf-8") == "中文附件"


def test_b32_non_ascii_headers_are_utf8_encoded_not_unknown_8bit(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A raw 8-bit header byte parses as charset ``unknown-8bit`` and flattens
    to ``=?unknown-8bit?b?...?=`` — a charset no client can render, made out of
    plain UTF-8. Operator-set display names are the live path for this."""

    smtp = FakeSMTP()
    stdin = _message(sender=f"前沿停摆告警 <{USER}>", body="中文正文\n")

    rc = _run(["-t", "-i"], stdin, _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    headers, _body = _split_wire(smtp.wire[0])
    assert b"unknown-8bit" not in headers
    assert b"=?utf-8?" in headers
    delivered = BytesParser(policy=email.policy.default).parsebytes(smtp.wire[0])
    assert str(delivered["From"]) == f"前沿停摆告警 <{USER}>"


def test_b32_ascii_only_message_negotiates_nothing_extra(capsys: pytest.CaptureFixture[str]) -> None:
    """No gratuitous BODY=8BITMIME on a message that does not need it."""

    smtp = FakeSMTP()

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _output(capsys)
    assert rc == 0
    assert smtp.mail_options == [()]
    assert smtp.wire[0].isascii()


def test_b32_undecodable_header_bytes_do_not_become_unknown_8bit_either(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Non-UTF-8 garbage in a header (the shape ``os.environ`` surrogateescape
    can produce) must still leave through a declared charset, not crash and not
    ``unknown-8bit``."""

    smtp = FakeSMTP(extensions=())
    stdin = (
        b"From: NHMS \xff\xfe alert <alerts@example.invalid>\r\n"
        b"To: ops@example.invalid\r\nSubject: s\r\n\r\nbody\xff\n"
    )

    rc = _run(["-t", "-i"], stdin, _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    assert b"unknown-8bit" not in smtp.wire[0]
    assert smtp.wire[0].isascii()


def test_b32_a_helo_only_server_still_gets_its_greeting(capsys: pytest.CaptureFixture[str]) -> None:
    """``ehlo()`` records its response even when it fails, which switches off
    smtplib's own HELO fallback inside ``send_message`` — so the explicit EHLO
    added for extension detection must carry the fallback itself."""

    smtp = FakeSMTP(ehlo_code=500, extensions=("8bitmime",))

    rc = _run(["-t", "-i"], _lane_message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0, err
    assert smtp.helo_calls == 1
    assert smtp.mail_options == [()]
    assert smtp.wire[0].isascii()


def test_b32_an_ehlo_failure_is_a_delivery_failure(capsys: pytest.CaptureFixture[str]) -> None:
    smtp = FakeSMTP(ehlo_error=smtplib.SMTPServerDisconnected("connection lost"))

    rc = _run(["-t", "-i"], _lane_message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 69
    assert "SMTP-FAILED stage=ehlo" in err
    assert smtp.logins == []
    assert smtp.quit_calls == 1


# ---------------------------------------------------------------------------
# B34 (#1375) — the SESSION is bounded, not just each socket operation.
# ---------------------------------------------------------------------------
#
# The budget is scaled down through NHMS_SMTP_SESSION_BUDGET_SEC so these tests
# run in fractions of a second: nothing here waits 45 s, and every sleep is
# meant to be interrupted, so a test that starts sleeping out its full duration
# is itself the failure signal (each one asserts its own wall clock).

BUDGET_SEC = 0.30
BUDGET = str(BUDGET_SEC)
#: The scaled-down stand-in for "blocks past the lane's wall": 16 budgets long,
#: which no working alarm ever waits out, but short enough that a shim WITHOUT
#: the alarm fails these tests in seconds instead of stalling the suite.
FOREVER_SEC = 5.0


class SleepySMTP(FakeSMTP):
    """Blocks INSIDE one call, the way a real socket read blocks on a peer that
    dribbles: it never returns to a stage boundary, so nothing but an interrupt
    can end it. ``close`` is recorded because the budget path must prefer it
    over another ``quit`` round trip."""

    def __init__(self, *, block_at: str, seconds: float = FOREVER_SEC, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.block_at = block_at
        self.seconds = seconds
        self.close_calls = 0

    def _block(self, stage: str) -> None:
        if self.block_at in (stage, "every"):
            time.sleep(self.seconds)

    def ehlo(self, name: str = "") -> tuple[int, bytes]:
        self._block("ehlo")
        return super().ehlo()

    def login(self, user: str, password: str) -> None:
        self._block("login")
        super().login(user, password)

    def send_message(self, message: Any, **kwargs: Any) -> dict[str, Any]:
        self._block("send")
        return super().send_message(message, **kwargs)

    def quit(self) -> None:
        self._block("quit")
        super().quit()

    def close(self) -> None:
        self.close_calls += 1


def test_b34_session_budget_stays_clear_of_the_lane_wall() -> None:
    """The two constants live in different files and were introduced by
    different commits with incompatible dimensions (per-op vs wall clock).
    Either one drifting alone must go red here, not on node-27 at 03:00.

    The ceiling is the same mirror for the ENV OVERRIDE: the shim deliberately
    does not import the alerter (it is exec'd by absolute path, and the
    dependency runs lane → shim), so this test is the only place the two files
    are compared."""

    assert shim.SESSION_BUDGET_SEC + 10 <= alerter.SENDMAIL_TIMEOUT_SEC
    assert shim.SESSION_BUDGET_CEILING_SEC <= alerter.SENDMAIL_TIMEOUT_SEC
    assert shim.SESSION_BUDGET_SEC < shim.SESSION_BUDGET_CEILING_SEC


def test_b34_accumulated_per_operation_waits_hit_the_session_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No single operation exceeds its own timeout; their SUM exceeds the
    session budget. That is the shape the per-op timeout cannot see."""

    smtp = SleepySMTP(block_at="every", seconds=BUDGET_SEC / 2)

    started = time.monotonic()
    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))
    elapsed = time.monotonic() - started

    _, err = _output(capsys)
    assert rc == 69
    assert err.strip().count("\n") == 0
    assert "SMTP-FAILED stage=" in err
    assert "reason=session-budget" in err
    assert f"budget={BUDGET_SEC:g}s" in err
    assert elapsed < BUDGET_SEC * 6, "the session ran past its own budget"


def test_b34_a_peer_blocked_mid_operation_is_interrupted_by_the_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The dribbling-peer case: one blocking call that would outlive the lane's
    60 s wall. The shim must end it ITSELF — no external SIGKILL — and still
    name the stage it died in."""

    smtp = SleepySMTP(block_at="send")

    started = time.monotonic()
    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))
    elapsed = time.monotonic() - started

    _, err = _output(capsys)
    assert rc == 69
    assert "SMTP-FAILED stage=send" in err
    assert "reason=session-budget" in err
    assert "host=smtp.163.com" in err
    assert "elapsed=" in err  # how long the peer held us is half the diagnosis
    assert elapsed < FOREVER_SEC / 2.5, "the blocking call was waited out instead of interrupted"
    assert smtp.sent == []  # the fake never got past its own sleep


@pytest.mark.parametrize("stage", ["ehlo", "login", "send"])
def test_b34_the_budget_line_names_the_stage_it_expired_in(
    stage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Stage attribution is the entire point: rc=124 from the lane's SIGKILL
    cannot tell 'cannot reach 163' from 'message half-pushed'."""

    smtp = SleepySMTP(block_at=stage)

    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 69
    assert f"SMTP-FAILED stage={stage} " in err
    assert "reason=session-budget" in err


def test_b34_budget_expiry_closes_the_socket_instead_of_quitting(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """QUIT is another round trip against the same unresponsive peer, and the
    lane's wall is only 15 s away by then."""

    smtp = SleepySMTP(block_at="send")

    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))

    _output(capsys)
    assert rc == 69
    assert smtp.close_calls == 1
    assert smtp.quit_calls == 0


def test_b34_a_hanging_quit_after_a_250_is_bounded_and_stays_accepted(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The alarm stays armed through teardown, so a peer that never answers QUIT
    cannot walk the shim into the lane's SIGKILL. And the fired timer must not
    unsay the 250 that was already printed: this is a successful send."""

    smtp = SleepySMTP(block_at="quit")

    started = time.monotonic()
    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))
    elapsed = time.monotonic() - started

    _, err = _output(capsys)
    assert rc == 0
    assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
    assert "session-budget" not in err
    assert elapsed < FOREVER_SEC / 2.5


def test_b34_a_trip_between_the_wire_outcome_and_the_evidence_line_keeps_the_250(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A signal can be delivered between ``send_message`` returning and the
    evidence line being written — a window of a few bytecodes that no sleeping
    fake can schedule, so the delivery is injected at exactly that instruction.
    RFC 5321 already transferred delivery responsibility at the final dot: the
    shim must report the 250 it holds, not a budget failure."""

    real_report = shim._report_wire_outcome
    entries: list[str] = []

    def trip_on_first_entry(host: str, refused: Any, recipients: Any) -> int:
        entries.append(host)
        if len(entries) == 1:
            raise shim._SessionBudgetExceeded(shim._SessionBudget(BUDGET_SEC, host))
        return real_report(host, refused, recipients)

    monkeypatch.setattr(shim, "_report_wire_outcome", trip_on_first_entry)
    smtp = FakeSMTP()

    rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0
    assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
    assert len(entries) == 2  # the recovery arm re-reported, it did not re-raise


@pytest.mark.parametrize("budget", ["abc", "0", "-1", "nan", "inf", "45s"])
def test_b34_a_bad_budget_override_is_a_config_error_and_nothing_connects(
    budget: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silently falling back to the default would strand an operator who set the
    budget to re-align it with a changed lane wall."""

    rc = _run(
        ["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=budget), _exploding_factory()
    )

    _, err = _output(capsys)
    assert rc == 64
    assert err.strip().count("\n") == 0
    assert err.startswith("SMTP-CONFIG-ERROR ")
    assert "NHMS_SMTP_SESSION_BUDGET_SEC" in err


def test_b34_without_an_override_the_default_budget_is_armed_and_disarmed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    real_setitimer = signal.setitimer
    timers: list[tuple[int, float]] = []

    def recording_setitimer(which: int, seconds: float, *rest: float) -> Any:
        timers.append((which, seconds))
        return real_setitimer(which, seconds, *rest)

    monkeypatch.setattr(shim.signal, "setitimer", recording_setitimer)

    rc = _run(["-t", "-i"], _message(), _env(), _factory(FakeSMTP(), []))

    _output(capsys)
    assert rc == 0
    assert shim.SESSION_BUDGET_SEC == 45.0
    assert timers[0] == (signal.ITIMER_REAL, shim.SESSION_BUDGET_SEC)
    assert timers[-1] == (signal.ITIMER_REAL, 0)


@pytest.mark.parametrize("blocked", [None, "send"])
def test_b34_the_process_is_left_pristine_whether_the_budget_trips_or_not(
    blocked: str | None, capsys: pytest.CaptureFixture[str]
) -> None:
    """The shim runs inside the lane's process tree (and inside this pytest
    process). A leaked handler or a still-armed timer would fire into whatever
    ran next — the shim's own containment would never see it."""

    before = signal.getsignal(signal.SIGALRM)
    smtp = FakeSMTP() if blocked is None else SleepySMTP(block_at=blocked)

    rc = shim.main(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))

    _output(capsys)
    assert rc == (0 if blocked is None else 69)
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_b34_off_the_main_thread_the_budget_degrades_instead_of_crashing(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``signal.signal`` raises ValueError outside the main thread. The shim is
    a CLI, but nothing may turn that into an SMTP-INTERNAL-ERROR (or a stderr
    line the lane would read as SMTP evidence) if it is ever imported."""

    results: list[int] = []
    thread = threading.Thread(
        target=lambda: results.append(shim.main(["-t", "-i"], _message(), _env(), _factory(FakeSMTP(), [])))
    )
    thread.start()
    thread.join(timeout=30)

    _, err = _output(capsys)
    assert results == [0]
    assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"


def test_b34_a_budget_trip_in_the_disarm_window_is_still_the_structured_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``main``'s backstop. The one window ``_run`` cannot cover from the inside
    is the alarm landing inside its own disarm sequence — a few instructions
    wide, so the escape is injected at the ``parse_message`` seam instead. What
    is pinned is the containment: a budget expiry that reaches ``main`` must
    still read as a delivery failure naming its stage, never as
    SMTP-INTERNAL-ERROR rc=70."""

    budget = shim._SessionBudget(BUDGET_SEC, "smtp.163.com")
    budget.stage = "send"

    def escaping(_stdin_bytes: bytes) -> Any:
        raise shim._SessionBudgetExceeded(budget)

    monkeypatch.setattr(shim, "parse_message", escaping)

    rc = shim.main(["-t", "-i"], _message(), _env(), _exploding_factory())

    _, err = _output(capsys)
    assert rc == 69
    assert err.strip().count("\n") == 0
    assert "SMTP-FAILED stage=send host=smtp.163.com reason=session-budget" in err
    assert "SMTP-INTERNAL-ERROR" not in err


# ---------------------------------------------------------------------------
# B34/R1 — round-1 findings: the disarm window, the override ceiling, and the
# pins the first pass left the mechanism without.
# ---------------------------------------------------------------------------


def _widen_the_disarm_window(monkeypatch: pytest.MonkeyPatch, seconds: float = 1.0) -> None:
    """Hold the shim inside ``disarm`` long enough for the alarm to land there.

    The real window is the handful of instructions between "the timer is still
    armed" and "the timer is off" — deliberately armed, so a hanging QUIT stays
    bounded. Stretching the ``setitimer(…, 0)`` call is the only way to schedule
    an expiry into it; the sleep is interrupted by the very alarm it is waiting
    for, so it costs microseconds when the containment works.
    """

    real_setitimer = signal.setitimer

    def stalling_setitimer(which: int, value: float, *rest: float) -> Any:
        if value == 0:
            time.sleep(seconds)
        return real_setitimer(which, value, *rest)

    monkeypatch.setattr(shim.signal, "setitimer", stalling_setitimer)


def test_b34_an_expiry_inside_the_disarm_does_not_unsay_the_250(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-1 CORR-1: the disarm ran with the alarm still armed but outside the
    arm that handles it, so an expiry there escaped to ``main``'s backstop and
    printed SMTP-FAILED + rc=69 AFTER the SMTP-ACCEPTED — the lane then records
    a failure for a message the server took, and retries it next tick."""

    before = signal.getsignal(signal.SIGALRM)
    _widen_the_disarm_window(monkeypatch)
    smtp = SleepySMTP(block_at="send", seconds=BUDGET_SEC / 2)

    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(smtp, []))

    _, err = _output(capsys)
    assert rc == 0
    assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
    assert "session-budget" not in err
    assert signal.getsignal(signal.SIGALRM) == before
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)


def test_b34_the_disarm_hands_sigalrm_back_through_sig_ign(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The SIG_IGN step is invisible from inside the process — deleting it keeps
    every other test green while re-opening the race it exists for: restoring
    the previous handler (SIG_DFL, in the lane) while the timer expires
    concurrently kills the shim with 142 and ZERO stderr, which is total
    evidence loss on a monitoring lane."""

    before = signal.getsignal(signal.SIGALRM)
    real_signal = signal.signal
    installed: list[Any] = []

    def recording_signal(signum: int, handler: Any) -> Any:
        installed.append(handler)
        return real_signal(signum, handler)

    monkeypatch.setattr(shim.signal, "signal", recording_signal)

    rc = _run(["-t", "-i"], _message(), _env(), _factory(FakeSMTP(), []))

    _output(capsys)
    assert rc == 0
    assert signal.SIG_IGN in installed, "the handler was restored without the SIG_IGN interposition"
    assert installed.index(signal.SIG_IGN) < installed.index(before)  # IGN first, then the restore
    assert installed[-1] == before


@pytest.mark.parametrize("budget", ["60", "60.0", "90", "3600", "1e9"])
def test_b34_a_budget_at_or_above_the_lane_wall_is_refused(
    budget: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Round-1 INT-1: positivity alone let an operator set 90 and quietly
    restore the pre-#1375 geometry — the lane's SIGKILL lands first, the stage
    is lost again, and the constants-only guard test cannot see it because no
    constant moved."""

    rc = _run(
        ["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=budget), _exploding_factory()
    )

    _, err = _output(capsys)
    assert rc == 64
    assert err.strip().count("\n") == 0
    assert err.startswith("SMTP-CONFIG-ERROR ")
    assert "NHMS_SMTP_SESSION_BUDGET_SEC" in err
    assert f"{shim.SESSION_BUDGET_CEILING_SEC:g}s" in err  # the operator is told the ceiling


def test_b34_a_budget_just_under_the_ceiling_is_accepted(capsys: pytest.CaptureFixture[str]) -> None:
    """The ceiling refuses '>= the wall', not 'anything the operator raised':
    stretching the budget to 59 s is a legitimate deployment choice."""

    calls: list[tuple[str, int, float]] = []

    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC="59"), _factory(FakeSMTP(), calls))

    _, err = _output(capsys)
    assert rc == 0
    assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
    assert calls[0][2] == shim.SMTP_TIMEOUT_SEC  # 30 < 59, so the per-op cap is the binding one


def test_b34_a_small_budget_caps_the_per_operation_timeout(capsys: pytest.CaptureFixture[str]) -> None:
    """The min() is a no-op at the shipped values, which is where the existing
    factory pin looks — so it needs pinning in the regime where it is not: a
    30 s socket timeout under a sub-second budget would let one operation
    outlive the whole session."""

    calls: list[tuple[str, int, float]] = []

    rc = _run(["-t", "-i"], _message(), _env(NHMS_SMTP_SESSION_BUDGET_SEC=BUDGET), _factory(FakeSMTP(), calls))

    _output(capsys)
    assert rc == 0
    assert 0 < calls[0][2] <= BUDGET_SEC


def test_b34_an_exhausted_budget_still_hands_the_factory_a_positive_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The clock starts before the message is parsed, so a tiny budget can be
    spent before the socket is even asked for. A non-positive timeout is a
    ValueError out of ``socket.create_connection`` — i.e. a BUDGET path
    arriving as SMTP-INTERNAL-ERROR rc=70, which the contract forbids. The
    alarm is armed by then and ends the session on its own terms."""

    monkeypatch.setattr(shim._SessionBudget, "remaining", lambda _self: -5.0)
    calls: list[tuple[str, int, float]] = []

    rc = _run(["-t", "-i"], _message(), _env(), _factory(FakeSMTP(), calls))

    _, err = _output(capsys)
    assert rc == 0, err
    assert calls[0][2] == 0.001
    assert "SMTP-INTERNAL-ERROR" not in err


def test_b34_an_expiry_at_the_disarm_call_itself_keeps_the_250(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``disarm`` contains the expiry from its own first instruction onward; the
    instruction that CALLS it is in the caller's frame, one bytecode earlier and
    outside that reach. Nothing can schedule a signal onto a single instruction,
    so the delivery is injected as ``disarm`` raising. The 250 already printed,
    and the exit code already in flight for it, must both stand."""

    def raising_disarm(self: Any) -> None:
        raise shim._SessionBudgetExceeded(self)

    monkeypatch.setattr(shim._SessionBudget, "disarm", raising_disarm)
    smtp = FakeSMTP()
    previous = signal.getsignal(signal.SIGALRM)

    try:
        rc = _run(["-t", "-i"], _message(), _env(), _factory(smtp, []))

        _, err = _output(capsys)
        assert rc == 0
        assert err.strip() == "SMTP-ACCEPTED host=smtp.163.com code=250 recipients=1"
        assert "session-budget" not in err
    finally:
        # Patching ``disarm`` away removed the only code that stops the timer,
        # so this run really does leave a live 45 s alarm and the discarded
        # budget's handler behind — the one test in this file that must clean up
        # after itself, or the alarm fires into some later test's process.
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
