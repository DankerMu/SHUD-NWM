#!/usr/bin/env bash
# node-27 generic unit-failure alert wrapper (issue #1664, design D6).
#
# Invoked by systemd as the `OnFailure=` handler of a node-27 user unit:
#
#   OnFailure=nhms-node27-unit-failure-alert@%n.service
#
# `%n` expands to the FULL unit name, so $1 already carries the `.service`
# suffix. Deliberately DUMB: no database, no state file, no deduplication, no
# Python. A scheduled unit fails at most once per tick, so a repeated mail is
# not a problem worth a state machine (that complexity belongs to the frontier
# stall lane, which alerts on a continuous condition).
#
# The mail channel is the frontier lane's, verbatim: `$NHMS_FRONTIER_SENDMAIL
# -t -i` with NHMS_ALERT_EMAIL_TO / NHMS_ALERT_EMAIL_FROM, proven live by a
# tick every 30 min. No second channel is introduced, and NONE of the three is
# defaulted: the transport is the authenticated-SMTP shim
# (scripts/node27_frontier_smtp_sendmail.py), which refuses a From that is not
# the authenticated account (exit 64), and /usr/sbin/sendmail on this box
# accepts with exit 0 and then asynchronously bounces (null-routed postfix,
# observed live 2026-08-13). A derived default for either value would turn a
# misconfiguration into a failed unit or into a "SENT" that never arrived, so
# both are required and unset is a soft exit.
#
# Exit discipline: a MISCONFIGURED alerter must not add a second failed unit
# on top of the failure it is reporting, so every unconfigured / unusable
# configuration path logs the reason and exits 0. A transport that is present
# and REJECTS the message is not a soft failure — it exits non-zero so a
# silently broken alert lane is itself visible.

set -euo pipefail

HOST=$(hostname 2>/dev/null || echo node-27)
SENDMAIL="${NHMS_FRONTIER_SENDMAIL:-}"
RECIPIENT="${NHMS_ALERT_EMAIL_TO:-}"
SENDER="${NHMS_ALERT_EMAIL_FROM:-}"
JOURNAL_LINES="${NHMS_UNIT_FAILURE_JOURNAL_LINES:-30}"

note() { echo "node27-unit-failure-alert: $*" >&2; }

# Fail-closed header hygiene, mirroring `_header_safe` in
# scripts/node27_frontier_stall_alert.py: a CR/LF in an operator-edited env
# value opens a new header, and the shim's recipient extraction would fold an
# injected `Bcc:` into the envelope while the delivered copy shows no trace of
# it. Checked on the RAW value; deliberately NOT stripped — concatenating
# `ops@x` and `Bcc: attacker@y` into one malformed address is worse than
# refusing to mail at all.
has_header_break() {
  case "$1" in
  *$'\r'* | *$'\n'*) return 0 ;;
  *) return 1 ;;
  esac
}

FAILED_UNIT="${1:-}"
if [ -z "$FAILED_UNIT" ]; then
  # Broken OnFailure= wiring (no %n). Report and stay green: turning this into
  # a failed unit would bury the original failure under a second one.
  note "SKIPPED reason=NO_UNIT_ARGUMENT"
  exit 0
fi
# Header hygiene: the value comes from systemd's instance name, but a CR/LF
# must never be able to open a new header.
SAFE_UNIT=$(printf '%s' "$FAILED_UNIT" | tr -d '\r\n')

if [ -z "$RECIPIENT" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_TO_UNSET"
  exit 0
fi
if [ -z "$SENDER" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_FROM_UNSET"
  exit 0
fi
# The tainted value is NEVER echoed back into this log line: a newline in it
# would mangle the very line reporting the problem. Name the variable only.
if has_header_break "$RECIPIENT"; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_TO_UNSAFE"
  exit 0
fi
if has_header_break "$SENDER"; then
  note "SKIPPED unit=$SAFE_UNIT reason=NHMS_ALERT_EMAIL_FROM_UNSAFE"
  exit 0
fi
if [ -z "$SENDMAIL" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=SENDMAIL_UNCONFIGURED"
  exit 0
fi
if [ ! -x "$SENDMAIL" ]; then
  note "SKIPPED unit=$SAFE_UNIT reason=SENDMAIL_UNAVAILABLE path=$SENDMAIL"
  exit 0
fi

# Journal context is best effort: on a host without journalctl (or with the
# unit's journal already rotated away) the alert must still go out naming the
# failed unit.
JOURNAL=$(journalctl --user -u "$FAILED_UNIT" -n "$JOURNAL_LINES" --no-pager 2>&1 || true)
if [ -z "$JOURNAL" ]; then
  JOURNAL="(no journal context available for $SAFE_UNIT)"
fi

# `|| rc=$?` (not `if ! ...`): after a negated condition `$?` is the status of
# the negation, i.e. always 0 inside the branch, which would swallow the
# failure. With `pipefail` this rc is sendmail's own.
#
# Header set mirrors `build_message` in the frontier lane: neither the SMTP
# shim nor `smtplib.SMTP.send_message` injects anything, so Date / MIME-Version
# / Content-Type must be written HERE. The charset is load-bearing — journal
# lines can be non-ASCII, and an undeclared 8-bit body renders as mojibake.
rc=0
printf '%s\n' \
  "To: $RECIPIENT" \
  "From: $SENDER" \
  "Subject: [NHMS node-27] unit failed: $SAFE_UNIT" \
  "Date: $(date -R)" \
  "MIME-Version: 1.0" \
  'Content-Type: text/plain; charset="utf-8"' \
  "" \
  "systemd unit $SAFE_UNIT entered the failed state on $HOST at $(date -u +'%Y-%m-%dT%H:%M:%SZ')." \
  "" \
  "Last $JOURNAL_LINES journal lines:" \
  "$JOURNAL" \
  | "$SENDMAIL" -t -i || rc=$?
if [ "$rc" -ne 0 ]; then
  note "SEND_FAILED unit=$SAFE_UNIT rc=$rc"
  exit "$rc"
fi

note "SENT unit=$SAFE_UNIT to=$RECIPIENT"
