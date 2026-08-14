#!/usr/bin/env python3
"""``sendmail -t -i`` compatible shim that submits over authenticated SMTPS.

Why this exists (issue #1368, live receipt 2026-08-13): node-27's local
postfix is deliberately null-routed (``default_transport = error``,
loopback-only). ``/usr/sbin/sendmail`` therefore ACCEPTS every message with
exit 0 and asynchronously bounces it (``dsn=5.0.0 status=bounced``), so the
frontier stall lane's "sendmail exit 0" evidence proves nothing about
delivery. This shim replaces the binary — point ``NHMS_FRONTIER_SENDMAIL`` at
it — and turns the acceptance into a SYNCHRONOUS 250 from the destination
provider's submission server, printed as one ``SMTP-ACCEPTED`` evidence line.

The lane's contract is unchanged: ``scripts/node27_frontier_stall_alert.py``
still runs ``<binary> -t -i`` with the RFC-822 message on stdin, exit 0 ==
sent. Its only concession to this shim is that a successful send now keeps the
last stderr line — the ``SMTP-ACCEPTED`` evidence above — in the receipt and
event log, so "the destination said 250" is provable from the deployed
artifacts instead of dying in the pipe.

Config is environment-only (``NHMS_SMTP_HOST`` / ``PORT`` / ``USER`` /
``PASS``); credentials are never accepted on argv, and ``NHMS_SMTP_PASS`` is
never interpolated into any output on any path — ``login()`` is its only
reader.

Exit codes: 0 accepted · 64 usage/config · 69 delivery failure ·
70 contained internal failure (whole-class, never a traceback — the lane
parses stderr, and a traceback on a monitoring lane is a silent monitor).
"""

from __future__ import annotations

import email.policy
import os
import smtplib
import ssl
import sys
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from typing import Any

DEFAULT_HOST = "smtp.163.com"
DEFAULT_PORT = 465
SMTP_TIMEOUT_SEC = 30.0

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_UNAVAILABLE = 69
EXIT_INTERNAL = 70

SmtpFactory = Callable[[str, int, float], Any]

# CTEs that already put 7-bit clean bytes on the wire, whatever the payload is.
_SEVEN_BIT_CTES = frozenset({"base64", "quoted-printable"})


class _UsageError(Exception):
    """Bad argv / bad env / no recipients — nothing was connected to."""


def _oneline(value: object) -> str:
    """Collapse to a single stderr line. The lane logs stderr verbatim."""

    return " ".join(str(value).split())


def default_smtp_factory(host: str, port: int, timeout: float) -> Any:
    """SMTPS with a VERIFIED TLS context.

    ``smtplib.SMTP_SSL`` defaults to ``ssl._create_stdlib_context()``, i.e.
    ``check_hostname=False`` / ``verify_mode=CERT_NONE``: the authorization code
    would be handed to whatever answers the connection, and the synchronous 250
    this whole shim exists to obtain could be forged by anything on the path.
    ``ssl.create_default_context()`` (CERT_REQUIRED + hostname check) is what
    makes "250 from the destination provider" mean anything.
    """

    return smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())


def parse_message(stdin_bytes: bytes) -> EmailMessage:
    """Seam kept module-level so tests can force an arbitrary failure class."""

    return BytesParser(policy=email.policy.default).parsebytes(stdin_bytes)


def _check_argv(argv: Sequence[str]) -> None:
    """Tolerate sendmail flags; refuse positional recipients.

    The lane only ever passes ``-t -i``. A positional argument means someone
    rewired the caller to pass recipients on the command line, which this shim
    silently would not deliver to (recipients come from the headers) — fail
    loud instead.
    """

    if not any(arg == "-t" for arg in argv):
        raise _UsageError("missing -t: recipients are read from To/Cc/Bcc headers only")
    for arg in argv:
        if not arg.startswith("-"):
            raise _UsageError(f"unexpected positional argument {arg!r}: recipients must come from headers (-t)")


def _config(env: Mapping[str, str]) -> tuple[str, int, str, str]:
    host = (env.get("NHMS_SMTP_HOST") or DEFAULT_HOST).strip()
    raw_port = (env.get("NHMS_SMTP_PORT") or str(DEFAULT_PORT)).strip()
    try:
        port = int(raw_port)
    except ValueError:
        raise _UsageError(f"NHMS_SMTP_PORT is not an integer: {raw_port!r}") from None
    if not 1 <= port <= 65535:
        raise _UsageError(f"NHMS_SMTP_PORT out of range 1-65535: {port}")
    user = (env.get("NHMS_SMTP_USER") or "").strip()
    if not user:
        raise _UsageError("missing required environment variable NHMS_SMTP_USER")
    if not (env.get("NHMS_SMTP_PASS") or ""):
        raise _UsageError("missing required environment variable NHMS_SMTP_PASS")
    return host, port, user, env["NHMS_SMTP_PASS"]


def _check_sender(message: EmailMessage, user: str) -> None:
    """Fail closed when the From header is not the authenticated account.

    163 (and every other submission service worth using) refuses a From header
    that differs from the account that authenticated, and this shim deliberately
    does NOT rewrite the header — the operator's ``NHMS_ALERT_EMAIL_FROM`` owns
    it. Detecting the mismatch here, before the socket exists, turns a recurring
    stage=send 550 into one config error the operator can act on. Only the
    address part is compared: ``NHMS Frontier Alert <acct@host>`` is the shipped
    (and correct) shape. Never mentions the password.
    """

    raw = message.get("From")
    if raw is None:
        raise _UsageError(f"message has no From header: it must carry the authenticated account {user}")
    _display, sender = parseaddr(str(raw))
    if sender.lower() != user.lower():
        raise _UsageError(
            f"From header address {sender or '(unparseable)'} does not match the authenticated"
            f" account {user}: point NHMS_ALERT_EMAIL_FROM at that address"
            " (a display name in front of it is fine)"
        )


def _normalize_headers(message: EmailMessage) -> None:
    """Re-set non-ASCII headers so they flatten as ``=?utf-8?...?=``.

    A header parsed from raw 8-bit bytes carries an ``unknown-8bit`` charset,
    and the generator then emits ``=?unknown-8bit?b?...?=`` on the wire — a
    charset no client can render, produced from bytes that were plain UTF-8.
    Re-assigning the decoded value makes the generator encode it as UTF-8.
    """

    items = [(name, str(value)) for name, value in message.items()]
    if all(value.isascii() for _name, value in items):
        return
    for name in {name.lower() for name, _value in items}:
        del message[name]
    for name, value in items:
        message[name] = value


def _reencode_7bit(message: EmailMessage) -> None:
    """Give every 8-bit part an explicit Content-Transfer-Encoding."""

    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None or payload.isascii():
            continue
        maintype, subtype = part.get_content_maintype(), part.get_content_subtype()
        if maintype == "text":
            charset = part.get_content_charset() or "utf-8"
            part.set_content(
                payload.decode(charset, "replace"), subtype=subtype, charset="utf-8", cte="quoted-printable"
            )
        else:
            part.set_content(payload, maintype=maintype, subtype=subtype, cte="base64")


def _declare_8bit(message: EmailMessage) -> None:
    """Give every 8-bit part the ``8bit`` CTE that RFC 6152 requires.

    ``BODY=8BITMIME`` on MAIL FROM declares the TRANSPORT only. Per RFC 2045 an
    ABSENT Content-Transfer-Encoding means ``7bit``, so a part carrying raw UTF-8
    with no CTE header tells the receiver "these bytes are 7-bit" while they
    demonstrably are not: a conforming gateway is entitled to strip the high bit
    and the operator's only alert arrives as mojibake. Unlike ``_reencode_7bit``
    this adds the declaration ONLY — the payload bytes cross byte-identical,
    which is the entire point of taking the 8BITMIME branch.
    """

    for part in message.walk():
        if part.is_multipart():
            continue
        payload = part.get_payload(decode=True)
        if payload is None or payload.isascii():
            continue
        if (part.get("Content-Transfer-Encoding") or "").strip().lower() in _SEVEN_BIT_CTES:
            # Already encoded 7-bit clean on the wire (the decoded payload being
            # 8-bit is exactly what base64/QP exist for). Its declaration is
            # already true; overwriting it with ``8bit`` would corrupt the part.
            continue
        del part["Content-Transfer-Encoding"]
        part["Content-Transfer-Encoding"] = "8bit"


def _prepare_for_wire(message: EmailMessage, *, eightbit_ok: bool) -> tuple[str, ...]:
    """Never push undeclared 8-bit bytes; return the MAIL FROM options to use.

    Every mail this lane sends contains non-ASCII (the runbook pointer in the
    body is Chinese). Raw 8-bit on a plain SMTP session is a protocol violation:
    a strict server rejects it (visible), a lenient one strips the high bit
    (mojibake in the operator's only alert). So: when the server advertises the
    extension, declare ``BODY=8BITMIME`` on the envelope AND ``8bit`` on each
    8-bit part (both halves are required — see ``_declare_8bit``); otherwise
    re-encode the body with an explicit CTE and stay 7-bit clean.
    """

    _normalize_headers(message)
    if message.as_bytes().isascii():
        return ()
    if eightbit_ok:
        _declare_8bit(message)
        return ("BODY=8BITMIME",)
    _reencode_7bit(message)
    return ()


def _recipients(message: EmailMessage) -> list[str]:
    """All To/Cc/Bcc addresses, de-duplicated, order preserved."""

    raw = [str(header) for name in ("to", "cc", "bcc") for header in message.get_all(name, [])]
    seen: dict[str, None] = {}
    for _display, address in getaddresses(raw):
        if address:
            seen.setdefault(address, None)
    return list(seen)


def _run(argv: Sequence[str], stdin_bytes: bytes, env: Mapping[str, str], smtp_factory: SmtpFactory) -> int:
    _check_argv(argv)
    host, port, user, password = _config(env)
    message = parse_message(stdin_bytes)
    recipients = _recipients(message)
    if not recipients:
        raise _UsageError("no recipients: the message carries no To/Cc/Bcc address")
    _check_sender(message, user)
    del message["Bcc"]  # blind carbon copy stays in the envelope, not on the wire

    stage = "connect"
    try:
        smtp = smtp_factory(host, port, SMTP_TIMEOUT_SEC)
    except OSError as error:  # smtplib.SMTPException subclasses OSError
        print(f"SMTP-FAILED stage={stage} host={host} error={type(error).__name__}: {_oneline(error)}",
              file=sys.stderr)
        return EXIT_UNAVAILABLE
    try:
        try:
            stage = "ehlo"
            # Explicit EHLO before AUTH: ``esmtp_features`` (8BITMIME) must be
            # known before the body is prepared. ``ehlo()`` records its response
            # even when it fails, which disables smtplib's own HELO fallback —
            # so mirror that fallback here instead of losing it.
            code, _greeting = smtp.ehlo()
            if not 200 <= code <= 299:
                smtp.helo()
            stage = "login"
            smtp.login(user, password)
            stage = "send"
            mail_options = _prepare_for_wire(message, eightbit_ok=bool(smtp.has_extn("8bitmime")))
            # Envelope sender must be the authenticated account: 163 rejects a
            # mismatch. The From HEADER is left exactly as the lane built it —
            # operator config (NHMS_ALERT_EMAIL_FROM) owns making them agree,
            # and ``_check_sender`` already refused the config where they don't.
            refused = smtp.send_message(
                message, from_addr=user, to_addrs=recipients, mail_options=mail_options
            )
        except OSError as error:
            print(f"SMTP-FAILED stage={stage} host={host} error={type(error).__name__}: {_oneline(error)}",
                  file=sys.stderr)
            return EXIT_UNAVAILABLE
        if refused:
            detail = " ".join(f"{addr}={_oneline(code)}" for addr, code in sorted(refused.items()))
            print(f"SMTP-PARTIAL-REFUSAL host={host} refused={len(refused)} {detail}", file=sys.stderr)
            return EXIT_UNAVAILABLE
        print(f"SMTP-ACCEPTED host={host} code=250 recipients={len(recipients)}", file=sys.stderr)
        return EXIT_OK
    finally:
        try:
            smtp.quit()
        except Exception:  # noqa: BLE001 - a botched teardown must not unsay a 250
            pass


def main(
    argv: Sequence[str],
    stdin_bytes: bytes,
    env: Mapping[str, str],
    smtp_factory: SmtpFactory | None = None,
) -> int:
    """``argv`` excludes the program name. Never raises, never tracebacks."""

    try:
        return _run(argv, stdin_bytes, env, smtp_factory or default_smtp_factory)
    except _UsageError as error:
        print(f"SMTP-CONFIG-ERROR {_oneline(error)}", file=sys.stderr)
        return EXIT_USAGE
    except Exception as error:  # noqa: BLE001 - whole-class containment, see module docstring
        print(f"SMTP-INTERNAL-ERROR {type(error).__name__}: {_oneline(error)}", file=sys.stderr)
        return EXIT_INTERNAL


def _entrypoint() -> int:
    """Thin wiring. The stdin read sits inside the containment net too: a
    broken pipe from the caller must be a structured line, not a traceback."""

    try:
        stdin_bytes = sys.stdin.buffer.read()
    except Exception as error:  # noqa: BLE001 - whole-class containment, see module docstring
        print(f"SMTP-INTERNAL-ERROR stdin unreadable: {type(error).__name__}: {_oneline(error)}", file=sys.stderr)
        return EXIT_INTERNAL
    return main(sys.argv[1:], stdin_bytes, os.environ)


if __name__ == "__main__":
    sys.exit(_entrypoint())
