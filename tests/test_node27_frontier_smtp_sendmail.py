"""Unit pins for the authenticated-SMTP sendmail shim (issue #1368).

The shim replaces node-27's null-routed local ``/usr/sbin/sendmail`` for the
frontier stall alert lane. Every test injects a fake SMTP object and a fake
environment — zero network, zero credentials, no sleeps. The one subprocess
test deliberately fails at config time, before any socket exists.

Invariant pinned on EVERY failure path: the value of ``NHMS_SMTP_PASS`` never
appears in stdout or stderr.
"""

from __future__ import annotations

import ast
import base64
import email.policy
import io
import os
import smtplib
import ssl
import subprocess
import sys
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
