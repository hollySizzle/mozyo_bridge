"""herdr durable-identity mapping (Redmine #13247).

The #13245 terminal-transport seam
(:mod:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport`)
addresses a herdr target by a **handle**, and the #13175 herdr PoC
(``vibes/docs/logics/herdr-poc-13175-experiment-log.md``) proved *which* handle is
durable: experiment **E10** showed that a herdr **assigned name** (given by
``agent rename``, e.g. ``poc_claude``) survives a full ``server stop`` / restart,
while ``pane_id`` and ``terminal_id`` are regenerated per process and are
therefore disposable. The PoC learning was explicit: "mozyo-bridge の lane/Redmine
紐付けに使う handle は herdr 付与名一択".

This module is the **staged seam** that turns that learning into a contract: a
pure, deterministic mapping between a mozyo lane/workspace/role slot and a herdr
assigned name, plus a fail-closed re-bind procedure that recovers a live target
from a restart by looking the name up in ``agent list`` — never by a cached pane
id.

Relationship to the route-identity ledger
------------------------------------------
The route-identity ledger
(:mod:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.route_identity_ledger`,
spec ``vibes/docs/specs/route-identity-ledger.md``, Redmine #12553) already fixed
the tmux-side identity contract: ``pane_id`` is a *cache / snapshot only and is
never the route authority*; the authority is the stable tuple
``(workspace_id, lane_id, role, pane_name)``. This module is the **herdr analogue**
of that same rule, and it stays consistent with it on purpose:

- the stable identity components are the same slot —
  ``(workspace_id, lane_id, role)`` — normalised the same way (an empty lane maps
  to :data:`DEFAULT_LANE`, matching the ledger's ``default`` convention);
- the herdr **assigned name** plays the role that ``pane_name`` plays on the tmux
  side: it is the durable, restart-surviving label;
- ``pane_id`` / ``terminal_id`` are the disposable analogues, and this type
  **never holds them** — :class:`HerdrAgentIdentity` has no pane/terminal field,
  so a caller structurally cannot persist a session-local locator as identity.

Where the ledger re-matches a stable tuple against a live *pane inventory*, this
module re-binds a stable *name* against a live *agent list* (:func:`rebind_by_name`).
The recovered locator is transient and is labelled as such, exactly as the
ledger's ``resolved_pane_id`` is a refreshed cache, never the authority.

Naming convention (the core of this US)
---------------------------------------
:func:`encode_assigned_name` is a pure, deterministic function from the identity
slot to a herdr assigned name; :func:`decode_assigned_name` is its fail-closed
inverse. The name is built to satisfy three properties at once:

1. **Deterministic** — the same slot always yields the same name (no clock, no
   counter, no randomness).
2. **Round-trippable (normalized-slot roundtrip)** —
   ``decode_assigned_name(encode_assigned_name(ws, role, lane))`` recovers the
   *normalized* slot — i.e. the components after ``encode_assigned_name`` trims
   them and maps an empty ``lane`` to :data:`DEFAULT_LANE` — for *any* input
   strings (the components may themselves contain ``_`` or non-identifier
   bytes). The roundtrip is byte-for-byte only over that normalized slot, not
   over the raw pre-normalization input.
3. **Collision-free** — distinct slots always encode to distinct names (the
   encoding is injective), so two lanes/roles can never share a durable handle.

The output alphabet is deliberately conservative — ``[A-Za-z0-9_]`` only — the
safe intersection of "what herdr accepted in the PoC (``poc_claude``)" and "what
can never smuggle a shell metacharacter or an extra argv token". A generated name
is therefore also a valid transport target handle
(:func:`~mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport.valid_target`),
so it can be fed straight to the #13245 send/read primitives.

Encoding scheme (``mzb1``)
--------------------------
A name is ``mzb1_<f(workspace)>_<f(role)>_<f(lane)>`` where ``_`` is the sole
field delimiter and ``f`` escapes each field so that ``_`` (and every other
non-``[A-Za-z0-9]`` byte, and the escape character itself) can never appear raw
inside a field. Escaping is percent-encoding-style with a letter escape: a byte
that is an ASCII letter/digit other than the escape character passes through
literally; every other byte ``b`` becomes ``Z`` + two upper-hex digits. Because
``Z`` is not a hex digit, a ``Z<HH>`` run is unambiguous, and the escape
character self-escapes (``Z`` -> ``Z5A``). Splitting a name on ``_`` therefore
always yields exactly four parts, and each field decodes independently — that is
what makes the round-trip and the injectivity hold for arbitrary inputs.

Scope (staged seam — kept explicit so it does not drift)
--------------------------------------------------------
- **In scope:** the pure encode/decode naming convention, the pane/terminal-free
  :class:`HerdrAgentIdentity` mapping type, and the fail-closed name -> live
  re-bind procedure (:func:`rebind_by_name`) with its structured result.
- **Out of scope (later US's / gated):** conversation *session* resume after a
  restart (E10: sessions do not auto-revive; that needs herdr's official
  integration hook and is the #13249-gated extension), any live-herdr test, any
  wiring into the live handoff / cockpit actuator, and any installer.

Non-goals (unchanged, restated for this seam)
---------------------------------------------
- a herdr assigned name is a *transport locator handle*, not workflow authority:
  it never becomes owner approval, routing authority, or ticket-state truth (the
  durable work record stays Redmine, per the adapter-boundary design doc);
- no third-party / dynamic provider; herdr remains the only built-in terminal
  backend and it is default off (#13245).
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Encoding scheme constants (core-owned).
# ---------------------------------------------------------------------------
#: Scheme marker + version. A herdr assigned name minted by mozyo starts with
#: this literal segment so :func:`decode_assigned_name` can recognise (and refuse
#: to mis-parse) a name that was not minted by this scheme. Bump the trailing
#: digit if the field layout ever changes.
SCHEME_PREFIX: str = "mzb1"

#: The sole field delimiter. Field encoding guarantees this byte never appears
#: raw inside an encoded field, so ``name.split("_")`` is unambiguous.
_DELIMITER: str = "_"

#: The escape character introducing a ``Z<HH>`` byte escape. It is an ASCII
#: letter (so a bare name never needs a non-identifier character) and is *not* a
#: hex digit (so ``Z`` followed by two hex digits is unambiguous). It self-escapes
#: as ``Z5A``.
_ESCAPE: str = "Z"

#: The field-body characters that pass through unescaped: ASCII letters/digits
#: except the escape character. Everything else — including ``_`` and any
#: non-ASCII byte — is escaped, so an encoded field is drawn from
#: ``[A-Za-z0-9]`` only and contains no delimiter.
_SAFE_FIELD_CHARS: frozenset[str] = frozenset(
    ch
    for ch in (
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
    )
    if ch != _ESCAPE
)

_HEX_DIGITS: frozenset[str] = frozenset("0123456789ABCDEF")

#: A conservative cap on the whole assigned name. The PoC only proved a short
#: name (``poc_claude``); a bounded length keeps a pathological slot from minting
#: an unwieldy handle. Encoding a slot whose name would exceed this fails closed
#: rather than silently truncating (which would break injectivity).
NAME_MAX_LENGTH: int = 128

#: The exact number of ``_``-delimited parts a well-formed name has:
#: prefix + workspace + role + lane.
_EXPECTED_PARTS: int = 4

#: The full-name character guard: a decoded candidate must be non-empty and drawn
#: only from the scheme's output alphabet. Mirrors the conservative
#: ``[A-Za-z0-9_]`` posture and rejects anything that could smuggle a shell/argv
#: token before the structured decode even runs.
_NAME_CHAR_RE = re.compile(r"^[A-Za-z0-9_]+$")

#: Normalized stand-in for an unset ``lane_id`` — the same convention the route
#: identity ledger uses (an empty lane is the workspace-default lane).
DEFAULT_LANE: str = "default"


# ---------------------------------------------------------------------------
# Fail-closed decode reason vocabulary (core-owned, closed set).
# ---------------------------------------------------------------------------
#: The candidate is not a string / is empty.
REASON_EMPTY: str = "empty_name"
#: The candidate carries a character outside the ``[A-Za-z0-9_]`` output alphabet.
REASON_ILLEGAL_CHAR: str = "illegal_char"
#: The candidate does not start with the :data:`SCHEME_PREFIX` segment.
REASON_BAD_PREFIX: str = "bad_prefix"
#: The candidate does not split into exactly :data:`_EXPECTED_PARTS` parts.
REASON_BAD_SHAPE: str = "bad_shape"
#: A field carries a malformed ``Z<HH>`` escape (missing / non-hex / undecodable).
REASON_BAD_ESCAPE: str = "bad_escape"
#: A required decoded field (workspace / role) is empty.
REASON_EMPTY_REQUIRED: str = "empty_required_field"
#: The candidate is longer than :data:`NAME_MAX_LENGTH`.
REASON_TOO_LONG: str = "name_too_long"

DECODE_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_EMPTY,
        REASON_ILLEGAL_CHAR,
        REASON_BAD_PREFIX,
        REASON_BAD_SHAPE,
        REASON_BAD_ESCAPE,
        REASON_EMPTY_REQUIRED,
        REASON_TOO_LONG,
    }
)


# ---------------------------------------------------------------------------
# Re-bind (name -> live locator) outcome vocabulary (core-owned, closed set).
# ---------------------------------------------------------------------------
#: Exactly one live agent carries the assigned name -> re-bound (the transient
#: locator is recovered from the live snapshot).
REBIND_OK: str = "rebind_resolved"
#: No live agent carries the assigned name (e.g. not yet renamed, or gone).
REBIND_NOT_FOUND: str = "rebind_not_found"
#: More than one live agent carries the same assigned name — a herdr invariant
#: violation; fail closed rather than guess which one to address.
REBIND_AMBIGUOUS: str = "rebind_ambiguous"
#: The supplied name is not a well-formed scheme name (fails :func:`decode_assigned_name`).
REBIND_INVALID_NAME: str = "rebind_invalid_name"
#: Exactly one live agent matched the name, but its row carries no usable pane
#: locator (``pane`` / ``location`` absent or blank) — reporting success would
#: hand a downstream a blank target (fail-open); refuse and fail closed instead.
REBIND_MISSING_LOCATOR: str = "rebind_missing_locator"

REBIND_FAIL_STATUSES: frozenset[str] = frozenset(
    {REBIND_NOT_FOUND, REBIND_AMBIGUOUS, REBIND_INVALID_NAME, REBIND_MISSING_LOCATOR}
)

# ---------------------------------------------------------------------------
# Live ``agent list`` record keys. A restart re-bind consumes the read-only row
# shape herdr emits (PoC E10: ``agent list`` / ``pane list``). The durable
# assigned name rides on ``name``; the *transient* pane locator rides on ``pane``
# (alias ``location``) — recovered only to address the target now, never stored
# as identity.
# ---------------------------------------------------------------------------
AGENT_KEY_NAME: str = "name"
AGENT_KEY_LOCATOR: str = "pane"
AGENT_KEY_LOCATOR_ALIAS: str = "location"


class HerdrIdentityError(ValueError):
    """A herdr identity slot cannot be represented as a durable assigned name.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    adapter-boundary / route-identity domain errors
    (``TerminalTransportError`` / ``RouteIdentityError``). Raised by
    :func:`encode_assigned_name` / :class:`HerdrAgentIdentity` construction when a
    required component is empty or the resulting name would exceed
    :data:`NAME_MAX_LENGTH`. Parsing an *external* name never raises — it returns
    a structured :class:`HerdrNameDecode` instead.
    """


def _norm(value: object) -> str:
    """Trim a raw field to a comparable token (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def _norm_lane(value: object) -> str:
    """Normalize a lane id, mapping an empty lane to :data:`DEFAULT_LANE`."""
    lane = _norm(value)
    return lane or DEFAULT_LANE


# ---------------------------------------------------------------------------
# Pure field codec (the injective, round-trippable half of the scheme).
# ---------------------------------------------------------------------------
def encode_field(value: str) -> str:
    """Encode one identity field into the ``[A-Za-z0-9]`` field alphabet.

    An ASCII letter/digit other than the escape character passes through; every
    other byte (of the UTF-8 encoding) becomes ``Z`` + two upper-hex digits. The
    result therefore contains no delimiter and no escape ambiguity, and
    :func:`decode_field` inverts it exactly for any input string.
    """
    if not isinstance(value, str):
        raise HerdrIdentityError(
            f"identity field must be a string, got {type(value).__name__}"
        )
    out: list[str] = []
    for byte in value.encode("utf-8"):
        ch = chr(byte)
        if ch in _SAFE_FIELD_CHARS:
            out.append(ch)
        else:
            out.append(f"{_ESCAPE}{byte:02X}")
    return "".join(out)


def _decode_field(token: str) -> Optional[str]:
    """Invert :func:`encode_field`; return ``None`` on any malformed token.

    ``None`` (not an exception) is returned for a malformed escape or an
    out-of-alphabet character so the caller can map it to a fail-closed decode
    reason; a genuinely empty token decodes to ``""`` (an empty field).
    """
    result = bytearray()
    i = 0
    length = len(token)
    while i < length:
        ch = token[i]
        if ch == _ESCAPE:
            hexpart = token[i + 1 : i + 3]
            if len(hexpart) != 2 or any(c not in _HEX_DIGITS for c in hexpart):
                return None
            result.append(int(hexpart, 16))
            i += 3
        elif ch in _SAFE_FIELD_CHARS:
            result.append(ord(ch))
            i += 1
        else:
            # An out-of-alphabet byte inside a field body (should be unreachable
            # once the whole-name char guard has run, but stays fail-closed).
            return None
    try:
        return result.decode("utf-8")
    except UnicodeDecodeError:
        return None


# ---------------------------------------------------------------------------
# Assigned-name codec (the public naming convention).
# ---------------------------------------------------------------------------
def encode_assigned_name(workspace_id: str, role: str, lane_id: str = "") -> str:
    """Deterministically mint the herdr assigned name for an identity slot.

    Signature: ``encode_assigned_name(workspace_id, role, lane_id="") -> str``.

    The components are normalised (trimmed; an empty ``lane_id`` becomes
    :data:`DEFAULT_LANE`) exactly as the route-identity ledger normalises the same
    slot, then encoded into ``mzb1_<f(ws)>_<f(role)>_<f(lane)>``. Fails closed
    (:class:`HerdrIdentityError`) when a required component (``workspace_id`` /
    ``role``) is empty or when the resulting name would exceed
    :data:`NAME_MAX_LENGTH`.

    Example::

        >>> encode_assigned_name("giken-3800-mozyo-bridge", "claude", "lane_13247")
        'mzb1_gikenZ2D3800Z2DmozyoZ2Dbridge_claude_laneZ5F13247'
    """
    ws = _norm(workspace_id)
    ro = _norm(role)
    lane = _norm_lane(lane_id)
    missing = [
        name
        for name, val in (("workspace_id", ws), ("role", ro))
        if not val
    ]
    if missing:
        raise HerdrIdentityError(
            "herdr identity requires non-empty stable components "
            f"(missing: {', '.join(missing)}); a pane id / terminal id is never a "
            "durable herdr handle"
        )
    name = _DELIMITER.join(
        (SCHEME_PREFIX, encode_field(ws), encode_field(ro), encode_field(lane))
    )
    if len(name) > NAME_MAX_LENGTH:
        raise HerdrIdentityError(
            f"herdr assigned name for this slot is {len(name)} chars, exceeding the "
            f"conservative cap of {NAME_MAX_LENGTH}; refuse to truncate (would break "
            "round-trip)"
        )
    return name


@dataclass(frozen=True)
class HerdrNameDecode:
    """The structured, fail-closed result of :func:`decode_assigned_name`.

    ``ok`` is the sole authority on success; on success ``identity`` is the
    recovered :class:`HerdrAgentIdentity` and ``reason`` is ``None``. On failure
    ``identity`` is ``None`` and ``reason`` is exactly one of
    :data:`DECODE_FAILURE_REASONS`. Parsing never raises, so a caller branches on
    ``ok`` / ``reason`` instead of catching an exception.
    """

    ok: bool
    reason: Optional[str] = None
    identity: Optional["HerdrAgentIdentity"] = None
    detail: str = ""

    @classmethod
    def success(cls, identity: "HerdrAgentIdentity") -> "HerdrNameDecode":
        return cls(ok=True, reason=None, identity=identity)

    @classmethod
    def failure(cls, reason: str, detail: str = "") -> "HerdrNameDecode":
        if reason not in DECODE_FAILURE_REASONS:
            raise HerdrIdentityError(
                f"unknown decode failure reason {reason!r}; expected one of "
                f"{sorted(DECODE_FAILURE_REASONS)}"
            )
        return cls(ok=False, reason=reason, identity=None, detail=detail)


def decode_assigned_name(name: object) -> HerdrNameDecode:
    """Fail-closed inverse of :func:`encode_assigned_name`.

    Signature: ``decode_assigned_name(name) -> HerdrNameDecode``.

    Returns a :class:`HerdrNameDecode`; it never raises. A name that is not a
    string, is empty, carries an out-of-alphabet character, lacks the
    :data:`SCHEME_PREFIX`, has the wrong number of fields, carries a malformed
    escape, decodes to an empty required field, or exceeds
    :data:`NAME_MAX_LENGTH` fails closed with the matching reason.
    """
    if not isinstance(name, str) or not name:
        return HerdrNameDecode.failure(REASON_EMPTY, "name must be a non-empty string")
    if len(name) > NAME_MAX_LENGTH:
        return HerdrNameDecode.failure(
            REASON_TOO_LONG, f"name is {len(name)} chars, over cap {NAME_MAX_LENGTH}"
        )
    if not _NAME_CHAR_RE.match(name):
        return HerdrNameDecode.failure(
            REASON_ILLEGAL_CHAR, "name carries a character outside [A-Za-z0-9_]"
        )
    parts = name.split(_DELIMITER)
    if parts[0] != SCHEME_PREFIX:
        return HerdrNameDecode.failure(
            REASON_BAD_PREFIX,
            f"name does not start with the {SCHEME_PREFIX!r} scheme segment",
        )
    if len(parts) != _EXPECTED_PARTS:
        return HerdrNameDecode.failure(
            REASON_BAD_SHAPE,
            f"name has {len(parts)} '_'-delimited parts, expected {_EXPECTED_PARTS} "
            "(prefix + workspace + role + lane)",
        )
    _, enc_ws, enc_role, enc_lane = parts
    ws = _decode_field(enc_ws)
    role = _decode_field(enc_role)
    lane = _decode_field(enc_lane)
    if ws is None or role is None or lane is None:
        return HerdrNameDecode.failure(
            REASON_BAD_ESCAPE, "a field carries a malformed Z<HH> escape sequence"
        )
    if not ws or not role:
        return HerdrNameDecode.failure(
            REASON_EMPTY_REQUIRED,
            "decoded a required component (workspace / role) as empty",
        )
    try:
        identity = HerdrAgentIdentity(workspace_id=ws, role=role, lane_id=lane)
    except HerdrIdentityError as exc:  # defensive: keep the contract fail-closed
        return HerdrNameDecode.failure(REASON_EMPTY_REQUIRED, str(exc))
    return HerdrNameDecode.success(identity)


@dataclass(frozen=True)
class HerdrAgentIdentity:
    """The durable identity of one managed agent, keyed by its herdr assigned name.

    The stable components are ``(workspace_id, lane_id, role)`` — the same slot the
    route-identity ledger uses — and the durable handle is :attr:`assigned_name`,
    the restart-surviving herdr name (PoC E10). This type deliberately carries
    **no** ``pane_id`` / ``terminal_id`` field: those are per-process, disposable
    locators, so a caller structurally cannot persist one as identity. The live
    locator is recovered on demand by :func:`rebind_by_name` and is transient.

    Construction normalises the components (trim; empty lane -> :data:`DEFAULT_LANE`)
    and fails closed (:class:`HerdrIdentityError`) when a required component is
    empty or when the slot cannot mint a within-cap assigned name.
    """

    workspace_id: str
    role: str
    lane_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace_id", _norm(self.workspace_id))
        object.__setattr__(self, "role", _norm(self.role))
        object.__setattr__(self, "lane_id", _norm_lane(self.lane_id))
        # Validate by minting the name: this enforces both the non-empty required
        # components and the length cap in one place, and guarantees every
        # constructed identity has a usable durable handle.
        encode_assigned_name(self.workspace_id, self.role, self.lane_id)

    @property
    def identity_slot(self) -> tuple[str, str, str]:
        """The stable slot ``(workspace_id, lane_id, role)`` (ledger-compatible order)."""
        return (self.workspace_id, self.lane_id, self.role)

    @property
    def assigned_name(self) -> str:
        """The deterministic, restart-surviving herdr assigned name for this slot."""
        return encode_assigned_name(self.workspace_id, self.role, self.lane_id)

    @classmethod
    def from_assigned_name(cls, name: object) -> HerdrNameDecode:
        """Recover an identity from a herdr assigned name (fail-closed).

        Thin alias for :func:`decode_assigned_name` returning the same structured
        :class:`HerdrNameDecode` — parsing never raises.
        """
        return decode_assigned_name(name)

    def to_record(self) -> dict[str, str]:
        """Serialize for persistence: the durable name plus its decoded components.

        Only durable, restart-surviving fields are written — there is no pane /
        terminal locator to persist.
        """
        return {
            "assigned_name": self.assigned_name,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "role": self.role,
        }

    def public_pointer(self) -> str:
        """Public-safe one-line pointer (no session-local locator, ever)."""
        return (
            f"herdr_name={self.assigned_name} ws={self.workspace_id} "
            f"lane={self.lane_id} role={self.role}"
        )


@dataclass(frozen=True)
class HerdrRebindResult:
    """The typed result of re-binding an assigned name to a live agent (fail-closed).

    :attr:`status` is one of the module tokens. On :data:`REBIND_OK`,
    :attr:`locator` is the **transient** live pane locator (e.g. ``w1:p1``) to
    address the agent *now* — it is never durable identity and is recovered fresh
    on every re-bind. On any fail status the locator is empty and :attr:`detail`
    carries a public-safe explanation.
    """

    status: str
    assigned_name: str
    locator: str = ""
    identity: Optional[HerdrAgentIdentity] = None
    considered: int = 0
    detail: str = ""

    @property
    def is_rebound(self) -> bool:
        """True only for a clean single-agent re-bind."""
        return self.status == REBIND_OK

    @property
    def is_fail(self) -> bool:
        """True for any fail-closed status."""
        return self.status in REBIND_FAIL_STATUSES

    def public_pointer(self) -> str:
        """Public-safe one-line summary (no transient locator)."""
        return (
            f"herdr_name={self.assigned_name} status={self.status} "
            f"considered={self.considered}"
        )


def rebind_by_name(
    name: object, agents: Sequence[Mapping[str, object]]
) -> HerdrRebindResult:
    """Recover a live agent locator from a durable assigned name (PoC E10).

    Signature: ``rebind_by_name(name, agents) -> HerdrRebindResult``.

    This is the restart-recovery procedure the PoC prescribes: after a herdr
    ``server`` restart the ``pane_id`` / ``terminal_id`` are regenerated, but the
    assigned name persists, so the live target is re-discovered by **matching the
    name** in the ``agent list`` snapshot — never by trusting a cached locator.
    ``agents`` is the read-only row shape herdr's ``agent list`` emits; each row's
    name rides on :data:`AGENT_KEY_NAME` and its transient locator on
    :data:`AGENT_KEY_LOCATOR` (alias :data:`AGENT_KEY_LOCATOR_ALIAS`).

    Fail-closed outcomes:

    - the name is not a well-formed scheme name -> :data:`REBIND_INVALID_NAME`;
    - exactly one live agent matches the name and its row carries a usable pane
      locator -> :data:`REBIND_OK` (transient locator recovered);
    - exactly one match but its row carries no usable pane locator (``pane`` /
      ``location`` absent or blank) -> :data:`REBIND_MISSING_LOCATOR` (refuse to
      report success with a blank target);
    - zero matches -> :data:`REBIND_NOT_FOUND`;
    - more than one match -> :data:`REBIND_AMBIGUOUS` (a herdr-name uniqueness
      violation; refuse to guess).
    """
    decoded = decode_assigned_name(name)
    assigned_name = name if isinstance(name, str) else ""
    if not decoded.ok:
        return HerdrRebindResult(
            status=REBIND_INVALID_NAME,
            assigned_name=assigned_name,
            detail=f"name is not a valid herdr identity ({decoded.reason})",
        )
    rows = list(agents)
    matches = [
        agent for agent in rows if _norm(agent.get(AGENT_KEY_NAME)) == assigned_name
    ]
    considered = len(rows)
    if len(matches) == 1:
        locator = _agent_locator(matches[0])
        if not locator:
            return HerdrRebindResult(
                status=REBIND_MISSING_LOCATOR,
                assigned_name=assigned_name,
                identity=decoded.identity,
                considered=considered,
                detail=(
                    "name matched but the live row carries no usable pane locator; "
                    "refuse to report success"
                ),
            )
        return HerdrRebindResult(
            status=REBIND_OK,
            assigned_name=assigned_name,
            locator=locator,
            identity=decoded.identity,
            considered=considered,
            detail="live agent recovered by durable assigned name",
        )
    if len(matches) > 1:
        return HerdrRebindResult(
            status=REBIND_AMBIGUOUS,
            assigned_name=assigned_name,
            identity=decoded.identity,
            considered=considered,
            detail=(
                f"{len(matches)} live agents carry the same assigned name; herdr "
                "names must be unique, fail closed rather than guess"
            ),
        )
    return HerdrRebindResult(
        status=REBIND_NOT_FOUND,
        assigned_name=assigned_name,
        identity=decoded.identity,
        considered=considered,
        detail="no live agent carries this assigned name",
    )


def _agent_locator(agent: Mapping[str, object]) -> str:
    """Read the transient pane locator from an ``agent list`` row (fail-soft)."""
    locator = _norm(agent.get(AGENT_KEY_LOCATOR))
    if not locator:
        locator = _norm(agent.get(AGENT_KEY_LOCATOR_ALIAS))
    return locator


__all__ = (
    "AGENT_KEY_LOCATOR",
    "AGENT_KEY_LOCATOR_ALIAS",
    "AGENT_KEY_NAME",
    "DECODE_FAILURE_REASONS",
    "DEFAULT_LANE",
    "NAME_MAX_LENGTH",
    "REASON_BAD_ESCAPE",
    "REASON_BAD_PREFIX",
    "REASON_BAD_SHAPE",
    "REASON_EMPTY",
    "REASON_EMPTY_REQUIRED",
    "REASON_ILLEGAL_CHAR",
    "REASON_TOO_LONG",
    "REBIND_AMBIGUOUS",
    "REBIND_FAIL_STATUSES",
    "REBIND_INVALID_NAME",
    "REBIND_MISSING_LOCATOR",
    "REBIND_NOT_FOUND",
    "REBIND_OK",
    "SCHEME_PREFIX",
    "HerdrAgentIdentity",
    "HerdrIdentityError",
    "HerdrNameDecode",
    "HerdrRebindResult",
    "decode_assigned_name",
    "encode_assigned_name",
    "encode_field",
    "rebind_by_name",
)
