"""Enter-resend gate vocabulary and pure predicates (Redmine #13322, #15202).

The turn-start rail (``domain/turn_start_rail``) may, after a first wait that neither
confirmed a start nor proved the pane gone, re-send **Enter and only Enter** — never the
body. Deciding whether it may is a self-contained question with its own closed
vocabulary and two pure predicates, so it lives here rather than in the orchestrator,
exactly as the provider registry split its startup-blocker schema out of the oversized
profile config (the module-health gate).

Dependency direction: this is a **leaf**. It imports nothing from the rail, so the rail
imports it at top level and re-exports every name — every existing importer of
``composer_retains_body`` is unchanged.

What is here, and why each piece is shaped the way it is:

- :data:`RESEND_SKIP_REASONS` — the closed set of reasons a *candidate* resend did not
  happen. A ``delivered_not_started`` with ``enter_resends=0`` is ambiguous on its own:
  it can mean the rail never wanted a resend, or that it wanted one and refused, and an
  operator reading a stalled lane must be able to tell those apart (#15202 requirement
  4). Tokens only — a startup screen renders a workspace path and this lands verbatim in
  a pasteable durable record.
- :data:`ResendScreenGuard` — the injected pane classifier the WAIT_ERROR gate requires.
  Keeping it an injected *callable* rather than a registry lookup is what keeps the rail
  provider-neutral: not one provider string exists in the domain, matching the #13760
  boundary that every provider-specific literal stays in packaged profile data.
- :func:`composer_retains_body` — the stuck-composer signature both gates share.
- :func:`screen_guard_detects` — the fail-closed reduction of a guard's token to a
  verdict.

Nothing here can *send* anything: there is no transport in reach. A guard's only
possible effect is to STOP an Enter, never to press one — declaring a screen never
authorises answering it (#13760 境界).
"""

from __future__ import annotations

from typing import Callable, Optional

# --- resend-skip vocabulary (core-owned, closed set) -------------------------
RESEND_SKIP_NONE = ""  # a resend ran, or none was ever warranted
RESEND_SKIP_DISABLED = "resend_disabled"  # a resend was warranted but the budget is 0
RESEND_SKIP_BUDGET_EXHAUSTED = "budget_exhausted"  # the bound was spent and the wait still did not confirm
RESEND_SKIP_PANE_UNREADABLE = "pane_unreadable"  # read_pane failed / blank — never "clear"
RESEND_SKIP_BODY_ABSENT = "body_absent"  # the composer no longer holds the injected body
RESEND_SKIP_STARTUP_SCREEN = "startup_screen"  # the guard matched a declared startup screen
RESEND_SKIP_SCREEN_GUARD_UNBOUND = "screen_guard_unbound"  # no classifier — cannot rule #13760 out
RESEND_SKIP_RECEIVER_BLOCKED = "receiver_blocked"  # a runtime permission prompt is on screen
RESEND_SKIP_ENTER_SEND_FAILED = "enter_send_failed"  # the resend's send_keys transport step failed

RESEND_SKIP_REASONS: frozenset[str] = frozenset(
    {
        RESEND_SKIP_NONE,
        RESEND_SKIP_DISABLED,
        RESEND_SKIP_BUDGET_EXHAUSTED,
        RESEND_SKIP_PANE_UNREADABLE,
        RESEND_SKIP_BODY_ABSENT,
        RESEND_SKIP_STARTUP_SCREEN,
        RESEND_SKIP_SCREEN_GUARD_UNBOUND,
        RESEND_SKIP_RECEIVER_BLOCKED,
        RESEND_SKIP_ENTER_SEND_FAILED,
    }
)

#: The injected pane classifier the WAIT_ERROR resend gate requires (Redmine #15202).
#: Takes the pane's rendered text; returns a fixed blocker token when a declared startup
#: / permission / selection screen is on it, or ``None`` when it is clear. Production
#: binds this to the receiver profile's declared ``startup_blockers``
#: (``...application.herdr_startup_admission.make_resend_screen_guard``). The rail reads
#: the returned token only as a boolean verdict.
ResendScreenGuard = Callable[[str], Optional[str]]


def _strip_all_ws(text: str) -> str:
    """Remove every whitespace character (a wrapping-insensitive match key).

    ``str.split()`` with no argument splits on any whitespace run, so ``"".join``
    of the pieces drops all whitespace — spaces, tabs, and the newlines a rendered
    composer inserts at line wraps, whether at a word boundary or *mid-token*.
    """
    return "".join(text.split())


def composer_retains_body(content: object, text: object) -> bool:
    """True when the injected ``text`` still appears in the pane ``content`` (pure).

    The Enter-resend gate (PoC E14): the rail re-sends Enter only when the injected
    body is still sitting in the composer — the stuck-Enter signature. The match is
    whitespace-INSENSITIVE: all whitespace is removed from both sides before the
    substring test.

    Why remove all whitespace rather than collapse runs to a single space: a
    rendered composer hard-wraps a long line to the pane width, and it wraps even
    *mid-token* for an unbroken token. The real handoff marker
    ``[mozyo:handoff:...:journal=73136:kind=...]`` renders as ``journal=7313`` +
    newline + ``  6:kind`` (Redmine #13322, confirmed against the live codex TUI); a
    whitespace-*collapse* would fold that wrap to a spurious space
    (``journal=7313 6:kind``) and miss the injected ``journal=73136:kind`` — the rail
    would then refuse to resend and report ``delivered_not_started`` with
    ``enter_resends=0`` even though the body is plainly retained. Dropping all
    whitespace makes the wrap — mid-token or at a word boundary — vanish, so a
    retained body matches regardless of how the TUI folded it.

    A non-empty body is still required; anything non-string, or an empty body, is
    ``False`` — a read that could not confirm retention must not authorise a resend
    (an empty / cleared composer therefore never matches, keeping the resend gate
    fail-closed). Never raises.
    """
    if not isinstance(content, str) or not isinstance(text, str):
        return False
    body = _strip_all_ws(text)
    if not body:
        return False
    return body in _strip_all_ws(content)


def screen_guard_detects(screen_guard: ResendScreenGuard, content: str) -> bool:
    """True when the guard reports a startup / permission / selection screen (pure).

    Fail-closed and total: the caller reduces the guard's token to a boolean verdict and
    never raises. A guard that itself raises (a malformed profile, a provider registry
    fault) is read as "screen detected" — the guard exists to let the rail rule #13760
    OUT, so a guard that cannot answer has ruled nothing out, and the resend is refused.
    Only the verdict crosses back; the pane's own text never leaves this call.
    """
    try:
        return bool(screen_guard(content))
    except (Exception, SystemExit):
        return True


__all__ = (
    "RESEND_SKIP_BODY_ABSENT",
    "RESEND_SKIP_BUDGET_EXHAUSTED",
    "RESEND_SKIP_DISABLED",
    "RESEND_SKIP_ENTER_SEND_FAILED",
    "RESEND_SKIP_NONE",
    "RESEND_SKIP_PANE_UNREADABLE",
    "RESEND_SKIP_REASONS",
    "RESEND_SKIP_RECEIVER_BLOCKED",
    "RESEND_SKIP_SCREEN_GUARD_UNBOUND",
    "RESEND_SKIP_STARTUP_SCREEN",
    "ResendScreenGuard",
    "composer_retains_body",
    "screen_guard_detects",
)
