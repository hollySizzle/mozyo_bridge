from __future__ import annotations

import sys


def die(message: str, code: int = 2) -> None:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(code)


def warn(message: str) -> None:
    """Emit a non-fatal warning to stderr (deprecation / drift notices)."""
    print(f"warning: {message}", file=sys.stderr)
