"""Core-facing terminal-transport port + backend selection (Redmine #13245).

This is the first concrete cut of the built-in **terminal runtime** adapter
boundary from ``vibes/docs/logics/plugin-ready-adapter-boundary.md`` (Redmine
#12001). The design doc scores this category a *medium* first-cut candidate:
the payoff is high but send safety is risk-heavy, so it was deferred until the
ticket (#12034) and presentation (#12156) seams had proven the "small pure
interface, one built-in provider, fail-closed" shape. #13245 lands that
interface for a second concrete terminal backend candidate — **herdr**
(evaluated in the #13175 PoC, ``vibes/docs/logics/herdr-poc-13175-experiment-log.md``).

Boundary, restated from the design doc so it stays enforced in code:

- **Core owns** the send-safety contract, the target-preflight vocabulary, the
  delivery-outcome shape, and the fail-closed behaviour. Those are the records
  and vocabularies in *this* module.
- **Providers own** the concrete send / capture / pane-listing mechanics. The
  built-in herdr provider that fills this port lives in
  ``mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport``;
  this module imports no provider, so the dependency only ever points
  provider -> core (exactly like ``ticket_adapter`` / ``presentation_adapter``).

The three primitives are the minimum a runtime adapter needs to *replace* the
current tmux ``send-keys`` + ``capture-pane`` heuristics (proven equivalent in
the PoC, experiments E8 / E11):

- :meth:`TerminalTransportPort.send_text` — inject text into a target's composer
  (herdr ``pane send-text``);
- :meth:`TerminalTransportPort.send_keys` — send raw key tokens, e.g. ``enter``
  (herdr ``pane send-keys``);
- :meth:`TerminalTransportPort.read_pane` — read rendered pane content (herdr
  ``agent read``, PoC E11).

Every primitive returns a **structured result** (:class:`TransportResult` /
:class:`PaneReadResult`) carrying an explicit ``ok`` flag and, on failure, a
reason drawn from the closed :data:`TRANSPORT_FAILURE_REASONS` vocabulary. A
primitive never returns a silent success and never falls back to another
backend — Implementation Guardrail #4 of the design doc ("provider failure must
be explicit: unavailable, unauthorized, ambiguous, unknown").

Scope (staged seam — kept explicit so it does not drift):

- **In scope:** the port, the result / reason vocabulary, the backend-selection
  config (default off), and the pure herdr CLI adapter + fail-closed resolver
  (sibling infrastructure module).
- **Out of scope (later US's):** turn-start / wait semantics (#13248 — the
  check-then-wait rail and the Codex Enter-resend rail from PoC E9 / E12–E14 are
  *not* built here; :meth:`send_text` is a bare primitive), ``agent_status``
  mapping (#13246), durable identity naming (#13247), any test that runs a live
  herdr binary, and any installer / distribution.

Non-goals (kept explicit so the seam does not become a plugin API):

- no third-party / arbitrary-code provider loading; herdr is the only built-in
  terminal-transport provider, and it is **default off**;
- no public ABI or long-term compatibility promise for these record shapes;
- no provider-defined workflow truth, owner approval, or routing authority —
  a terminal transport observes liveness and delivers sends; it never becomes
  durable identity (design doc "Terminal runtime adapter" boundary).
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

# --- backend vocabulary (core-owned) -----------------------------------------
# The terminal-transport backends this build recognises. ``tmux`` is the
# current, always-available runtime; ``herdr`` is the opt-in candidate. New
# backends are added here in core, never supplied by a provider. Selecting a
# backend outside this set fails closed.
BACKEND_TMUX = "tmux"
BACKEND_HERDR = "herdr"

TERMINAL_TRANSPORT_BACKENDS: frozenset[str] = frozenset({BACKEND_TMUX, BACKEND_HERDR})

#: The default backend. ``tmux`` means herdr transport is **off**: no herdr
#: adapter is constructed and the existing tmux path is untouched. A repo must
#: opt in explicitly (``terminal_transport.backend: herdr``) to select herdr.
DEFAULT_TERMINAL_BACKEND = BACKEND_TMUX

# --- pane read source vocabulary (core-owned) --------------------------------
# The pane-content sources a read may request, mirroring the herdr ``agent
# read --source`` values proven in PoC E11 (visible screen / recent scrollback /
# recent unwrapped). Core-owned so a provider cannot invent a source.
SOURCE_VISIBLE = "visible"
SOURCE_RECENT = "recent"
SOURCE_RECENT_UNWRAPPED = "recent-unwrapped"

PANE_READ_SOURCES: frozenset[str] = frozenset(
    {SOURCE_VISIBLE, SOURCE_RECENT, SOURCE_RECENT_UNWRAPPED}
)

DEFAULT_PANE_READ_SOURCE = SOURCE_VISIBLE

# --- fail-closed reason vocabulary (core-owned) ------------------------------
# Every unsuccessful primitive / resolution reports exactly one of these. They
# are the terminal-transport analogue of the delivery sink's
# ``PERSIST_FAILURE_REASONS``: an explicit, closed set so a caller can branch on
# a stable reason rather than parse a message.
REASON_BACKEND_DISABLED = "backend_disabled"
REASON_BINARY_UNCONFIGURED = "binary_unconfigured"
REASON_BINARY_NOT_FOUND = "binary_not_found"
REASON_INVALID_TARGET = "invalid_target"
REASON_INVALID_SOURCE = "invalid_source"
# The provider's command ran but returned a payload that could not be recognised
# as the expected read schema (non-JSON, a scalar JSON value, or an object with
# no recognised row container). Distinct from ``transport_error`` (a spawn / exit
# / OS failure): the process succeeded but its output is unusable, so a read that
# would otherwise report an *empty* success fails closed instead of pretending it
# observed "nothing".
REASON_INVALID_PAYLOAD = "invalid_payload"
REASON_TRANSPORT_ERROR = "transport_error"

TRANSPORT_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_BACKEND_DISABLED,
        REASON_BINARY_UNCONFIGURED,
        REASON_BINARY_NOT_FOUND,
        REASON_INVALID_TARGET,
        REASON_INVALID_SOURCE,
        REASON_INVALID_PAYLOAD,
        REASON_TRANSPORT_ERROR,
    }
)

#: The permitted shape of a target handle (a herdr ``window:pane`` locator or a
#: durable agent name, PoC E8 / E10). A leading alphanumeric then alphanumerics
#: / ``:`` / ``.`` / ``_`` / ``-`` — no spaces, empty value, shell
#: metacharacters, or flags, so a target can never smuggle an extra argv token
#: or an option into a subprocess call. Validated in core so *every* provider
#: gets the same fail-closed target guard.
_TARGET_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:._-]*$")


class TerminalTransportError(ValueError):
    """A terminal-transport config or selection violates the contract.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    adapter-boundary errors (``ProviderRegistryError`` / ``PresentationRecordError``).
    A :class:`TerminalTransportError` carries a ``reason`` drawn from
    :data:`TRANSPORT_FAILURE_REASONS` when it stands for a failed *selection*
    (e.g. herdr selected but no binary configured), so the caller can branch on
    the same closed vocabulary the result records use.
    """

    def __init__(self, message: str, *, reason: Optional[str] = None):
        super().__init__(message)
        if reason is not None and reason not in TRANSPORT_FAILURE_REASONS:
            raise TerminalTransportError(
                f"unknown transport failure reason {reason!r}; expected one of "
                f"{sorted(TRANSPORT_FAILURE_REASONS)}"
            )
        self.reason = reason


def valid_target(target: object) -> bool:
    """True iff ``target`` is a well-formed target handle (see :data:`_TARGET_TOKEN_RE`)."""
    return isinstance(target, str) and bool(_TARGET_TOKEN_RE.match(target))


@dataclass(frozen=True)
class TransportResult:
    """The structured outcome of a send primitive (``send_text`` / ``send_keys``).

    ``ok`` is the sole authority on success; on failure ``reason`` is one of
    :data:`TRANSPORT_FAILURE_REASONS` and ``detail`` is a short, credential-free
    diagnostic (a terminal transport handles no credentials, but ``detail`` is
    still kept bounded and free of any absolute path a subprocess might echo).
    ``ok=True`` requires ``reason is None``; ``ok=False`` requires a valid
    reason — construction fails closed on any other combination so a result can
    never be an ambiguous "succeeded with a reason".
    """

    ok: bool
    reason: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        _check_result_reason(self.ok, self.reason)

    @classmethod
    def success(cls, detail: str = "") -> "TransportResult":
        return cls(ok=True, reason=None, detail=detail)

    @classmethod
    def failure(cls, reason: str, detail: str = "") -> "TransportResult":
        return cls(ok=False, reason=reason, detail=detail)


@dataclass(frozen=True)
class PaneReadResult:
    """The structured outcome of :meth:`TerminalTransportPort.read_pane`.

    Adds the read payload to the :class:`TransportResult` shape: ``content`` is
    the rendered pane text on success (``None`` on failure) and ``truncated``
    mirrors herdr's truncation flag (PoC E11), so a caller can tell a clipped
    read from a complete one. The same ``ok`` / ``reason`` invariant applies.
    """

    ok: bool
    reason: Optional[str] = None
    detail: str = ""
    content: Optional[str] = None
    truncated: bool = False

    def __post_init__(self) -> None:
        _check_result_reason(self.ok, self.reason)

    @classmethod
    def success(cls, content: str, *, truncated: bool = False) -> "PaneReadResult":
        return cls(ok=True, reason=None, content=content, truncated=truncated)

    @classmethod
    def failure(cls, reason: str, detail: str = "") -> "PaneReadResult":
        return cls(ok=False, reason=reason, detail=detail, content=None)


def _check_result_reason(ok: object, reason: object) -> None:
    """Enforce the ok/reason invariant shared by both result records."""
    if not isinstance(ok, bool):
        raise TerminalTransportError(f"result 'ok' must be a bool, got {ok!r}")
    if ok:
        if reason is not None:
            raise TerminalTransportError(
                f"a successful transport result may not carry a failure reason, "
                f"got {reason!r}"
            )
        return
    if reason not in TRANSPORT_FAILURE_REASONS:
        raise TerminalTransportError(
            f"a failed transport result must carry a reason from "
            f"{sorted(TRANSPORT_FAILURE_REASONS)}, got {reason!r}"
        )


@runtime_checkable
class TerminalTransportPort(Protocol):
    """The built-in terminal-transport boundary — three fail-closed primitives.

    Implementations are *built-in* providers only — there is no dynamic loading
    and no public extension contract (see the module docstring and the
    adapter-boundary design doc). A provider takes a target handle and performs
    one send / read mechanic, returning a structured result. The protocol is
    deliberately narrow: it exposes the send / capture primitives a runtime
    adapter owns, and nothing that would let a transport define workflow truth,
    owner approval, routing, or durable identity.
    """

    backend: str

    def send_text(self, target: str, text: str) -> TransportResult:
        """Inject ``text`` into ``target``'s composer (no submit); fail closed."""
        ...

    def send_keys(self, target: str, keys: str) -> TransportResult:
        """Send raw key token(s) (e.g. ``enter``) to ``target``; fail closed."""
        ...

    def read_pane(
        self,
        target: str,
        *,
        source: str = DEFAULT_PANE_READ_SOURCE,
        lines: Optional[int] = None,
    ) -> PaneReadResult:
        """Read rendered content of ``target`` for ``source``; fail closed."""
        ...


# --- backend-selection config (default off) ----------------------------------

#: The closed set of recognised keys inside the ``terminal_transport`` sub-record
#: of ``.mozyo-bridge/config.yaml``. ``backend`` selects the runtime; the binary
#: is deliberately **not** a config key (see :class:`TerminalTransportConfig`).
TERMINAL_TRANSPORT_KEYS: frozenset[str] = frozenset({"version", "backend"})


@dataclass(frozen=True)
class TerminalTransportConfig:
    """Projection-free selection of the terminal-transport backend (default off).

    The *only* thing this config expresses is which recognised backend to use.
    The default (``tmux``) means herdr transport is off and the existing tmux
    path is untouched, so a repo with no ``terminal_transport`` block behaves
    exactly as before.

    **The herdr binary is intentionally not a config field.** Selecting herdr is
    a declarative, repo-local decision; but the *path of the executable mozyo
    would run* must come from the **trusted environment** (``MOZYO_HERDR_BINARY``),
    never a repo-local file — a hostile or mistaken checkout must not be able to
    point the runtime at an arbitrary binary. This mirrors the delivery-record
    transport (#12347), where ``--persist-delivery`` selects the seam in the repo
    but the live-write opt-in + credentials live only in the trusted environment.
    The binary is resolved by the sibling infrastructure resolver, which fails
    closed (``binary_unconfigured`` / ``binary_not_found``) with no silent
    fallback to tmux.

    Construction validates the backend against the core-owned vocabulary;
    an unknown backend fails closed with :class:`TerminalTransportError`.
    """

    backend: str = DEFAULT_TERMINAL_BACKEND

    def __post_init__(self) -> None:
        if (
            not isinstance(self.backend, str)
            or self.backend not in TERMINAL_TRANSPORT_BACKENDS
        ):
            raise TerminalTransportError(
                f"terminal transport backend {self.backend!r} is not a recognised "
                f"backend; allowed: {sorted(TERMINAL_TRANSPORT_BACKENDS)}"
            )

    @classmethod
    def default(cls) -> "TerminalTransportConfig":
        """The behaviour-preserving default: tmux backend (herdr off)."""
        return cls()

    @property
    def herdr_enabled(self) -> bool:
        """True iff the herdr backend is explicitly selected."""
        return self.backend == BACKEND_HERDR

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "TerminalTransportConfig":
        """Normalise a ``terminal_transport`` sub-record into a typed selection.

        ``None`` or an empty mapping yields the default (tmux / off). A
        non-mapping record, an unknown key, an unsupported version, or a
        non-string / unrecognised backend fails closed with
        :class:`TerminalTransportError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise TerminalTransportError(
                "terminal transport config record must be a mapping (a YAML "
                f"table), got {type(record).__name__}"
            )
        for key in record:
            if not isinstance(key, str) or not key:
                raise TerminalTransportError(
                    f"terminal transport config keys must be non-empty strings; "
                    f"got {key!r}"
                )
            if key not in TERMINAL_TRANSPORT_KEYS:
                raise TerminalTransportError(
                    f"terminal transport config has unknown key {key!r}; allowed "
                    f"keys: {sorted(TERMINAL_TRANSPORT_KEYS)}"
                )
        version = record.get("version", 1)
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise TerminalTransportError(
                f"unsupported terminal transport config version {version!r}; this "
                f"build understands version 1"
            )
        backend = record.get("backend", DEFAULT_TERMINAL_BACKEND)
        if not isinstance(backend, str):
            raise TerminalTransportError(
                f"terminal transport config 'backend' must be a string naming a "
                f"recognised backend, got {type(backend).__name__}"
            )
        return cls(backend=backend)


__all__ = (
    "BACKEND_HERDR",
    "BACKEND_TMUX",
    "DEFAULT_PANE_READ_SOURCE",
    "DEFAULT_TERMINAL_BACKEND",
    "PANE_READ_SOURCES",
    "REASON_BACKEND_DISABLED",
    "REASON_BINARY_NOT_FOUND",
    "REASON_BINARY_UNCONFIGURED",
    "REASON_INVALID_PAYLOAD",
    "REASON_INVALID_SOURCE",
    "REASON_INVALID_TARGET",
    "REASON_TRANSPORT_ERROR",
    "SOURCE_RECENT",
    "SOURCE_RECENT_UNWRAPPED",
    "SOURCE_VISIBLE",
    "TERMINAL_TRANSPORT_BACKENDS",
    "TERMINAL_TRANSPORT_KEYS",
    "TRANSPORT_FAILURE_REASONS",
    "PaneReadResult",
    "TerminalTransportConfig",
    "TerminalTransportError",
    "TerminalTransportPort",
    "TransportResult",
    "valid_target",
)
