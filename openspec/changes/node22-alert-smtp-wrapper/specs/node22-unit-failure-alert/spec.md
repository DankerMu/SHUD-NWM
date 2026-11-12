## ADDED Requirements

### Requirement: node-22 SHALL reach the SMTPS shim only through the exact-interpreter wrapper
The repository SHALL track `scripts/node22_smtp_sendmail.sh`, an executable sendmail-compatible transport that execs `/scratch/frd_muziyao/NWM/.venv/bin/python /scratch/frd_muziyao/NWM/scripts/node27_frontier_smtp_sendmail.py` with its own arguments, standard input and exit status passed through unchanged. When the interpreter is missing or not executable, or the shim file is missing, the wrapper SHALL write one `node22-smtp-sendmail:` line to standard error and exit 64 without invoking any other interpreter. The wrapper SHALL NOT invoke `python3`, `uv`, or read or print any `NHMS_SMTP_*` value.

#### Scenario: Message is forwarded to the shim
- **WHEN** the wrapper runs as `<wrapper> -t -i` with a message on stdin and the interpreter and shim exist
- **THEN** the interpreter receives the shim path followed by `-t -i`, the stdin bytes unchanged, and the wrapper exits with the interpreter's exit status

#### Scenario: Missing interpreter fails closed
- **WHEN** the interpreter path is absent or not executable
- **THEN** the wrapper prints `node22-smtp-sendmail: INTERPRETER_MISSING` to stderr and exits 64 without running anything else

#### Scenario: Non-executable interpreter fails closed
- **WHEN** the interpreter path exists but is not executable
- **THEN** the wrapper prints `node22-smtp-sendmail: INTERPRETER_MISSING` to stderr and exits 64 without running anything else

#### Scenario: Missing shim fails closed
- **WHEN** the interpreter exists but the shim file is absent
- **THEN** the wrapper prints `node22-smtp-sendmail: SHIM_MISSING` to stderr and exits 64 without running anything

#### Scenario: Shim constant names a tracked file
- **WHEN** the test strips `/scratch/frd_muziyao/NWM/` from the wrapper's `SHIM` constant
- **THEN** the remainder is a git-tracked, existing repository file (`scripts/node27_frontier_smtp_sendmail.py`)
