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
reader. The whole session is additionally bounded by ``SESSION_BUDGET_SEC``
(45 s, overridable with ``NHMS_SMTP_SESSION_BUDGET_SEC``) — see
``_SessionBudget`` for why a per-operation socket timeout is not a bound.

Exit codes: 0 accepted · 64 usage/config · 69 delivery failure (including
session-budget expiry) · 70 contained internal failure (whole-class, never a
traceback — the lane parses stderr, and a traceback on a monitoring lane is a
silent monitor).
"""

from __future__ import annotations

import email.policy
import math
import os
import signal
import smtplib
import ssl
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses, parseaddr
from typing import Any

DEFAULT_HOST = "smtp.163.com"
DEFAULT_PORT = 465
SMTP_TIMEOUT_SEC = 30.0

#: Wall-clock ceiling for the whole SMTP session, kept strictly below the
#: lane's ``SENDMAIL_TIMEOUT_SEC = 60`` SIGKILL wall in
#: ``scripts/node27_frontier_stall_alert.py`` so the shim always reports its own
#: stage instead of dying anonymously as rc=124. Overridable per deployment
#: with ``NHMS_SMTP_SESSION_BUDGET_SEC`` — that env var is the single point
#: where the two constants are re-aligned; a test pins the margin.
SESSION_BUDGET_SEC = 45.0

#: Hard ceiling for that override, mirroring the lane's ``SENDMAIL_TIMEOUT_SEC``
#: (60 s). A budget at or above the wall is not a budget: the SIGKILL lands
#: first and the pre-#1375 geometry — rc=124, no stage, message possibly
#: delivered — is silently back. Refused as a config error instead. The lane
#: constant is mirrored, not imported: this shim is exec'd by absolute path
#: with no package context, and the dependency direction is lane → shim. A test
#: importing both modules pins the mirror.
SESSION_BUDGET_CEILING_SEC = 60.0

EXIT_OK = 0
EXIT_USAGE = 64
EXIT_UNAVAILABLE = 69
EXIT_INTERNAL = 70

SmtpFactory = Callable[[str, int, float], Any]

# CTEs that already put 7-bit clean bytes on the wire, whatever the payload is.
_SEVEN_BIT_CTES = frozenset({"base64", "quoted-printable"})


class _UsageError(Exception):
    """Bad argv / bad env / no recipients — nothing was connected to."""


class _SessionBudgetExceeded(Exception):
    """The session budget expired. Deliberately NOT an ``OSError`` subclass:
    the stage ladder's ``except OSError`` arms would swallow it and report the
    expiry as an ordinary per-stage failure, losing ``reason=session-budget``.
    Carries its budget so the containment net in ``main`` can print the same
    structured line without re-deriving stage/host."""

    def __init__(self, budget: _SessionBudget) -> None:
        super().__init__(f"session budget {budget.seconds:g}s exceeded at stage {budget.stage}")
        self.budget = budget


class _SessionBudget:
    """One-shot ``SIGALRM`` deadline for the entire session (issue #1375).

    ``SMTP_TIMEOUT_SEC`` is PER OPERATION: every blocking read restarts it, so a
    session with its ~8 round trips can legitimately run 8×30 s, and a peer that
    dribbles one byte every 29 s resets it forever. Only the lane's 60 s SIGKILL
    then ends the session — anonymously, mid-DATA, leaving the operator an
    rc=124 with no stage. ``setitimer(ITIMER_REAL)`` raises in-band from
    whatever blocking call is in flight, so the ``stage`` this object carries is
    still available to report.
    """

    def __init__(self, seconds: float, host: str) -> None:
        self.seconds = seconds
        self.host = host
        self.stage = "connect"
        self.tripped = False
        self._started = time.monotonic()
        self._previous: Any = None
        self._installed = False  # this object owns the SIGALRM handler
        self._armed = False  # ... and the timer is running

    def elapsed(self) -> float:
        return time.monotonic() - self._started

    def remaining(self) -> float:
        return self.seconds - self.elapsed()

    def failure_line(self) -> str:
        return (
            f"SMTP-FAILED stage={self.stage} host={self.host} reason=session-budget"
            f" elapsed={self.elapsed():.3f}s budget={self.seconds:g}s"
        )

    def arm(self) -> None:
        """Best effort. ``signal.signal`` raises ``ValueError`` off the main
        thread; degrade to the per-operation cap alone rather than crashing, and
        stay silent about it — the lane logs this stderr verbatim, so a warning
        line here would be indistinguishable from an SMTP evidence line."""

        try:
            self._previous = signal.signal(signal.SIGALRM, self._fire)
        except (ValueError, OSError):
            return
        self._installed = True
        try:
            signal.setitimer(signal.ITIMER_REAL, self.seconds)
        except (ValueError, OSError):
            self._restore()
            return
        self._armed = True

    def _fire(self, _signum: int, _frame: Any) -> None:
        self.tripped = True
        raise _SessionBudgetExceeded(self)

    def _restore(self) -> None:
        """Hand SIGALRM back to whoever had it, exactly once, and only if this
        object ever took it — an unarmed budget (off the main thread) must not
        install ``SIG_DFL`` over a handler it never owned."""

        if not self._installed:
            return
        self._installed = False
        signal.signal(signal.SIGALRM, self._previous if self._previous is not None else signal.SIG_DFL)

    def disarm(self) -> None:
        """Stop the timer BEFORE touching the handler, and step through
        ``SIG_IGN`` on the way back: restoring ``SIG_DFL`` while the timer
        expires concurrently kills the process with 142 and zero stderr — the
        one failure shape this whole mechanism exists to prevent.

        Deliberately exception-safe end to end. The alarm is still armed while
        this runs — that is the point, a hanging ``quit()`` must stay bounded —
        so the expiry can land INSIDE the disarm itself. Letting it out would
        print a session-budget failure after the caller had already printed the
        250 the server gave us (round-1 CORR-1). It has nothing left to
        interrupt anyway: the timer is one-shot and has, by definition, just
        fired. The restore runs on every path, including that one.
        """

        try:
            if self._armed:
                self._armed = False
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, signal.SIG_IGN)
        except _SessionBudgetExceeded:
            pass
        finally:
            self._restore()


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


def _budget_seconds(env: Mapping[str, str]) -> float:
    """Validated like ``NHMS_SMTP_PORT``: a typo is a config error, not a
    silent fallback to the default — an operator who set the budget to align it
    with a changed lane wall must hear that the value did not take."""

    raw = (env.get("NHMS_SMTP_SESSION_BUDGET_SEC") or "").strip()
    if not raw:
        return SESSION_BUDGET_SEC
    try:
        seconds = float(raw)
    except ValueError:
        raise _UsageError(f"NHMS_SMTP_SESSION_BUDGET_SEC is not a number: {raw!r}") from None
    if not math.isfinite(seconds) or seconds <= 0:
        raise _UsageError(f"NHMS_SMTP_SESSION_BUDGET_SEC must be a positive number of seconds: {raw!r}")
    if seconds >= SESSION_BUDGET_CEILING_SEC:
        raise _UsageError(
            f"NHMS_SMTP_SESSION_BUDGET_SEC must stay below the alerter's"
            f" {SESSION_BUDGET_CEILING_SEC:g}s sendmail wall, otherwise the SIGKILL arrives first and"
            f" the failure loses its stage: {raw!r}"
        )
    return seconds


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


def _report_wire_outcome(host: str, refused: Mapping[str, Any], recipients: Sequence[str]) -> int:
    """What the server said, once ``send_message`` has returned."""

    if refused:
        detail = " ".join(f"{addr}={_oneline(code)}" for addr, code in sorted(refused.items()))
        print(f"SMTP-PARTIAL-REFUSAL host={host} refused={len(refused)} {detail}", file=sys.stderr)
        return EXIT_UNAVAILABLE
    print(f"SMTP-ACCEPTED host={host} code=250 recipients={len(recipients)}", file=sys.stderr)
    return EXIT_OK


def _run(argv: Sequence[str], stdin_bytes: bytes, env: Mapping[str, str], smtp_factory: SmtpFactory) -> int:
    _check_argv(argv)
    host, port, user, password = _config(env)
    budget = _SessionBudget(_budget_seconds(env), host)
    message = parse_message(stdin_bytes)
    recipients = _recipients(message)
    if not recipients:
        raise _UsageError("no recipients: the message carries no To/Cc/Bcc address")
    _check_sender(message, user)
    del message["Bcc"]  # blind carbon copy stays in the envelope, not on the wire

    # ``None`` until ``send_message`` returns, the refused map afterwards. A
    # budget expiry with this bound must NOT unsay the 250 that already came
    # back over the wire — same invariant as the teardown's blanket except.
    wire_outcome: dict[str, Any] | None = None
    reported: int | None = None
    budget.arm()  # after config: a usage error needs no alarm
    try:
        try:
            # Per-op cap kept under the session budget. At the shipped values
            # this is just SMTP_TIMEOUT_SEC (30 < 45) — the min() only bites
            # when the env override sets a budget below the per-op timeout.
            # SIGALRM, not this, is the enforcement. The floor matters for the
            # same small budgets: a non-positive timeout is a ValueError out of
            # socket.create_connection, i.e. a budget path surfacing as
            # SMTP-INTERNAL-ERROR rc=70. The alarm is already armed and will
            # end the session on its own terms.
            smtp = smtp_factory(host, port, max(0.001, min(SMTP_TIMEOUT_SEC, budget.remaining())))
        except OSError as error:  # smtplib.SMTPException subclasses OSError
            print(f"SMTP-FAILED stage={budget.stage} host={host} error={type(error).__name__}: {_oneline(error)}",
                  file=sys.stderr)
            return EXIT_UNAVAILABLE
        try:
            try:
                budget.stage = "ehlo"
                # Explicit EHLO before AUTH: ``esmtp_features`` (8BITMIME) must be
                # known before the body is prepared. ``ehlo()`` records its response
                # even when it fails, which disables smtplib's own HELO fallback —
                # so mirror that fallback here instead of losing it.
                code, _greeting = smtp.ehlo()
                if not 200 <= code <= 299:
                    smtp.helo()
                budget.stage = "login"
                smtp.login(user, password)
                budget.stage = "send"
                mail_options = _prepare_for_wire(message, eightbit_ok=bool(smtp.has_extn("8bitmime")))
                # Envelope sender must be the authenticated account: 163 rejects a
                # mismatch. The From HEADER is left exactly as the lane built it —
                # operator config (NHMS_ALERT_EMAIL_FROM) owns making them agree,
                # and ``_check_sender`` already refused the config where they don't.
                wire_outcome = smtp.send_message(
                    message, from_addr=user, to_addrs=recipients, mail_options=mail_options
                )
            except OSError as error:
                print(f"SMTP-FAILED stage={budget.stage} host={host} error={type(error).__name__}: {_oneline(error)}",
                      file=sys.stderr)
                return EXIT_UNAVAILABLE
            reported = _report_wire_outcome(host, wire_outcome, recipients)
            return reported
        finally:
            try:
                if budget.tripped:
                    # The peer already proved it will not answer in time; QUIT
                    # is another round trip against the same wall. Drop the
                    # socket instead. The alarm stays armed either way, so a
                    # hanging quit() on the normal path is bounded too — and
                    # this blanket except is what keeps a printed 250 said.
                    smtp.close()
                else:
                    smtp.quit()
            except Exception:  # noqa: BLE001 - a botched teardown must not unsay a 250
                pass
    except _SessionBudgetExceeded:
        if reported is not None:
            return reported
        if wire_outcome is not None:
            return _report_wire_outcome(host, wire_outcome, recipients)
        print(budget.failure_line(), file=sys.stderr)
        return EXIT_UNAVAILABLE
    finally:
        try:
            budget.disarm()
        except _SessionBudgetExceeded:
            # ``disarm`` contains its own expiry; this covers the instruction
            # that CALLS it, where the alarm would land in this frame instead.
            # Swallowing keeps the return already in flight — and the evidence
            # line already printed for it — exactly as it was.
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
    except _SessionBudgetExceeded as error:
        # ``_run`` handles its own expiry; this covers the one window it cannot
        # cover from the inside — the alarm firing inside its own disarm
        # sequence. A budget expiry must never read as SMTP-INTERNAL-ERROR.
        print(error.budget.failure_line(), file=sys.stderr)
        return EXIT_UNAVAILABLE
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
