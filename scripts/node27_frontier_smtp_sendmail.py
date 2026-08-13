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

``scripts/node27_frontier_stall_alert.py`` is NOT modified: it still runs
``<binary> -t -i`` with the RFC-822 message on stdin, exit 0 == sent.

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
import sys
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
from typing import Any

DEFAULT_HOST = "smtp.163.com"
DEFAULT_PORT = 465
SMTP_TIMEOUT_SEC = 30.0

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_UNAVAILABLE = 69
EXIT_INTERNAL = 70

SmtpFactory = Callable[[str, int, float], Any]


class _UsageError(Exception):
    """Bad argv / bad env / no recipients — nothing was connected to."""


def _oneline(value: object) -> str:
    """Collapse to a single stderr line. The lane logs stderr verbatim."""

    return " ".join(str(value).split())


def default_smtp_factory(host: str, port: int, timeout: float) -> Any:
    return smtplib.SMTP_SSL(host, port, timeout=timeout)


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
            stage = "login"
            smtp.login(user, password)
            stage = "send"
            # Envelope sender must be the authenticated account: 163 rejects a
            # mismatch. The From HEADER is left exactly as the lane built it —
            # operator config (NHMS_ALERT_EMAIL_FROM) owns making them agree.
            refused = smtp.send_message(message, from_addr=user, to_addrs=recipients)
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
