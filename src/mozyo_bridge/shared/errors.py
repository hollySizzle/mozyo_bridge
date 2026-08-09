from __future__ import annotations

import sys


class CommandAbort(SystemExit):
    """A fail-closed :func:`die` abort that *carries* its message (Redmine #15149).

    ``die`` has always printed the operator message to stderr and raised a bare
    ``SystemExit(code)``, so the message existed only as CLI text. An in-process
    caller that is not the CLI — the shared application API a local MCP server
    calls (#15149) — could therefore only recover the reason by parsing stdout /
    stderr, which is exactly the dependency the CLI/MCP boundary must not have.

    This subclass keeps the abort a ``SystemExit`` (same type for every existing
    ``except SystemExit`` handler, same ``.code``, same interpreter exit
    behaviour, so the CLI contract is unchanged) while exposing the message as a
    typed attribute. It is the fail-closed *carrier*, never a new decision: the
    gate that raised it already made the call.
    """

    def __init__(self, message: str, code: int = 2) -> None:
        super().__init__(code)
        self.message = message

    def __str__(self) -> str:  # pragma: no cover - debugging affordance only
        return self.message


def die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise CommandAbort(message, code)


def warn(message: str) -> None:
    """Emit a non-fatal warning to stderr (deprecation / drift notices)."""
    print(f"warning: {message}", file=sys.stderr)
