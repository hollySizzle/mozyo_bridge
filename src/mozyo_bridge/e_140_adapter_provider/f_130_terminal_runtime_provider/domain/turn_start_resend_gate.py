"""Enter-resend gate vocabulary and pure predicates (Redmine #13322, #15202).

The turn-start rail (``domain/turn_start_rail``) may, after a first wait that neither
confirmed a start nor proved the pane gone, re-send **Enter and only Enter** — never the
body. Deciding whether it may is a self-contained question with its own closed
vocabulary and small pure predicates, so it lives here rather than in the orchestrator,
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
- :func:`composer_retains_body` — the standard-rail-compatible whole-pane
  stuck-composer signature.
- :func:`current_composer_retains_body` — the stricter queue-enter signature that
  accepts only the structurally current composer tail, never matching scrollback alone.
- :func:`screen_guard_detects` — the fail-closed reduction of a guard's token to a
  verdict.

Nothing here can *send* anything: there is no transport in reach. A guard's only
possible effect is to STOP an Enter, never to press one — declaring a screen never
authorises answering it (#13760 境界).

The WAIT_ERROR gate conditions (Redmine #15202, audit j#102755)
---------------------------------------------------------------
The rail's timeout-armed resend asks two questions; the error-armed resend asks six,
and the asymmetry is the point. A timeout is a *positive* observation (the wait ran
and saw no transition); an error is the **absence** of an observation, so before
pressing Enter into a pane it cannot characterise, the rail must positively establish
who holds the target and what is on it. Each condition is fail-closed — "could not
determine" is always a refusal, never a pass:

1. ``screen_guard`` is bound. Without a classifier the rail cannot rule out a trust /
   login / update-selection screen, and #13760 is exactly what a blind Enter into one
   costs (the request body is destroyed while the transport reports ``sent``).
2. ``identity_probe`` is bound, and a conservative live-target token was established
   before injection **and** is byte-identical now. Every outer identity gate (target
   resolution, ``--target-repo``, startup admission) runs *before* the drive and none
   re-runs mid-drive, so nothing else guarantees the locator still addresses the same
   process 8–15s later — a pane can be killed and its id reused, or a lane relaunched,
   inside the wait window. The built-in Herdr token joins assigned name + terminal id +
   locator + row revision. Terminal id is the server-owned terminal identity; revision
   is only a conservative mutation fence and is not a process-generation id. Missing /
   malformed terminal-id or revision evidence refuses the resend. Runtime status is
   not directly part of the token. Identity is checked *before* the pane read because
   re-reading a pane a different process now owns is already reading the wrong thing.
3. The pane reads, and is not blank. An unreadable or blank pane is never "clear" —
   #13760's live lane saw an empty pane *after* a dialog ate the body.
4. The guard finds no declared startup screen. Checked before the body check, so a
   modal rendering over a still-visible body is reported as the screen it is.
5. The injected body is still in the composer (:func:`composer_retains_body`) — the
   stuck-Enter signature. A cleared composer means the turn already consumed it.
6. A runtime re-snapshot **positively** confirms an injectable receiver — not merely
   "did not say blocked". ``AgentStateResult`` forces ``state=unknown`` on a mechanical
   read failure, so a bare ``!= blocked`` test admits a resend on a read that never
   happened (the fail-OPEN this gate's first version shipped with). The read must
   succeed *and* the state must be injectable: ``blocked`` is a permission prompt Enter
   would answer, ``busy`` means a turn is already running, ``unknown`` is not an
   observation.
"""

from __future__ import annotations

import re
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
RESEND_SKIP_STATE_UNREADABLE = "state_unreadable"  # the runtime re-snapshot read failed
RESEND_SKIP_STATE_NOT_INJECTABLE = "state_not_injectable"  # observed busy / unknown — not a confirmed idle receiver
RESEND_SKIP_IDENTITY_PROBE_UNBOUND = "identity_probe_unbound"  # no way to re-verify who holds the target
RESEND_SKIP_IDENTITY_UNCONFIRMED = "identity_unconfirmed"  # the probe could not establish an identity
RESEND_SKIP_IDENTITY_DRIFT = "identity_drift"  # a DIFFERENT agent now holds the target locator
RESEND_SKIP_WAIT_UNARMED = "wait_unarmed"  # no causal wait could be armed before the extra Enter

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
        RESEND_SKIP_STATE_UNREADABLE,
        RESEND_SKIP_STATE_NOT_INJECTABLE,
        RESEND_SKIP_IDENTITY_PROBE_UNBOUND,
        RESEND_SKIP_IDENTITY_UNCONFIRMED,
        RESEND_SKIP_IDENTITY_DRIFT,
        RESEND_SKIP_WAIT_UNARMED,
    }
)

#: The injected read-only target-identity probe the WAIT_ERROR resend gate requires
#: (Redmine #15202, audit j#102755 finding 3). Given the target locator it returns an
#: opaque conservative fingerprint of the live target — under Herdr, an injective
#: encoding of assigned name + stable terminal id + locator + row revision from a FRESH
#: ``agent list`` snapshot — or ``None`` when that cannot be established. Terminal id
#: separates terminal instances; revision is a mutation fence, not a process-generation
#: id. The rail captures one token before injecting and requires an exact match before
#: it re-sends Enter, so a pane that was recycled, relaunched, or reassigned in the wait
#: window cannot receive the extra Enter. A runtime status change alone leaves the
#: token stable unless it coincides with some separately revisioned presentation change.
#: A memoised probe would make the comparison vacuous: it MUST re-read.
ResendIdentityProbe = Callable[[str], Optional[str]]

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


_COMPOSER_PROMPT_RE = re.compile(r"^\s*[›❯>]\s*(?P<body>.*)$")


def current_composer_retains_body(content: object, text: object) -> bool:
    """True only when ``text`` is retained in the *current* composer tail.

    ``composer_retains_body`` intentionally preserves the older standard-rail
    whole-pane match.  That is not strong enough for the Herdr queue-enter retry:
    a prior copy of the same handoff in scrollback must never authorise a fresh
    Enter.  This predicate finds the last rendered composer prompt and searches
    only that prompt plus its wrapped continuation lines.  Whitespace is removed
    inside that tail so a hard wrap in the marker or body remains matchable.

    Missing/blank prompt evidence, non-string input, and an empty body all fail
    closed.  The composer text is never returned or persisted.
    """
    if not isinstance(content, str) or not isinstance(text, str):
        return False
    body = _strip_all_ws(text)
    if not body:
        return False
    lines = content.splitlines()
    prompt_index = -1
    prompt_body = ""
    for index, line in enumerate(lines):
        match = _COMPOSER_PROMPT_RE.match(line)
        if match:
            prompt_index = index
            prompt_body = match.group("body").strip()
    if prompt_index < 0 or not prompt_body:
        return False
    # A submitted prompt remains in scrollback while unindented receiver output is
    # rendered below it.  Such a historical prompt is not a current composer even
    # when no newer prompt is visible (notably while a receiver is busy).  Wrapped
    # composer continuations and TUI footer/status rows are indented; fail closed on
    # any non-empty, non-indented row after the candidate prompt.
    if any(line.strip() and not line[:1].isspace() for line in lines[prompt_index + 1 :]):
        return False
    composer_tail = _strip_all_ws("".join(lines[prompt_index:]))
    return body in composer_tail


def probe_identity(probe: ResendIdentityProbe, target: str) -> Optional[str]:
    """The probe's token for ``target``, or ``None`` when it cannot be established.

    Fail-closed and total: a probe that raises (a listing failure, an unresolvable
    workspace, a herdr fault) is indistinguishable from "I cannot tell you who holds
    this pane", which is exactly the case the resend must refuse. A blank or
    non-string token is normalised to ``None`` for the same reason — an empty identity
    would otherwise compare equal to another empty identity and pass the drift check.
    """
    try:
        token = probe(target)
    except (Exception, SystemExit):
        return None
    if not isinstance(token, str):
        return None
    token = token.strip()
    return token or None


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
    "RESEND_SKIP_IDENTITY_DRIFT",
    "RESEND_SKIP_IDENTITY_PROBE_UNBOUND",
    "RESEND_SKIP_IDENTITY_UNCONFIRMED",
    "RESEND_SKIP_NONE",
    "RESEND_SKIP_PANE_UNREADABLE",
    "RESEND_SKIP_REASONS",
    "RESEND_SKIP_RECEIVER_BLOCKED",
    "RESEND_SKIP_SCREEN_GUARD_UNBOUND",
    "RESEND_SKIP_STARTUP_SCREEN",
    "RESEND_SKIP_STATE_NOT_INJECTABLE",
    "RESEND_SKIP_STATE_UNREADABLE",
    "RESEND_SKIP_WAIT_UNARMED",
    "ResendIdentityProbe",
    "ResendScreenGuard",
    "composer_retains_body",
    "current_composer_retains_body",
    "probe_identity",
    "screen_guard_detects",
)
