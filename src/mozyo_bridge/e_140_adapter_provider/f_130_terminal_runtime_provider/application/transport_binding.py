"""Runtime transport binding — the single tmux/herdr backend-selection seam (Redmine #13253).

The lower US's landed the built-in **terminal runtime** adapter parts behind a
default-off backend selection: the transport port + herdr CLI adapter (#13245),
the state snapshot (#13246), the durable-identity mapping (#13247), and the
check-then-wait turn-start rail (#13248). Each is a *staged* seam — constructed
and fake-tested, but **not** wired into the live handoff send path. This module
is that wiring: it resolves, from the repo-local ``terminal_transport`` selection,
a single :class:`TransportBinding` that the handoff rail installs in place of its
two tmux primitives (``run_tmux`` send-keys / ``capture_pane``).

The design (Redmine #13253 j#72349, candidate C) keeps the send *choreography*
untouched: ``orchestrate_handoff`` still calls the tmux-shaped
``run_tmux("send-keys", …)`` / ``capture_pane(target, lines)`` names it always
has, and the backend switch happens *behind* those names. This module supplies
the two callables those names resolve to:

- **tmux backend (default).** The binding's callables are the *exact same*
  ``run_tmux`` / ``capture_pane`` functions the rail already used (injected by the
  caller so this module never depends on the tmux infrastructure package). The
  handoff path is therefore **byte-for-byte unchanged** — the rail installs
  nothing, so the ``commands.run_tmux`` / ``commands.capture_pane`` monkeypatch
  seam (#12932) is untouched and every existing handoff test stays green.
- **herdr backend (opt-in).** The binding's callables are a *tmux-shaped shim*
  over the #13245 :class:`~...domain.terminal_transport.TerminalTransportPort`:
  the four tmux argv shapes the rail emits are mapped onto the port's
  ``send_text`` / ``send_keys`` / ``read_pane`` primitives. A tmux subcommand the
  shim does not recognise **fails closed** with an explicit error — it is never
  silently ignored or passed through, so the shim can never quietly drop a send.

Fail-closed selection (Redmine #13253 j#72318, no silent tmux fallback):

- a repo with **no** ``terminal_transport`` block (or a broken / unreadable
  config) is *not* a herdr selection — the caller resolves it to the default and
  gets the tmux binding, so an absent config never changes behaviour;
- once herdr is **selected**, any resolution failure (no binary configured, an
  unresolvable binary) raises :class:`~...domain.terminal_transport.TerminalTransportError`
  from the #13245 resolver — the binding is never quietly downgraded to tmux.

One-line cutover / roll-back (Redmine #13253 j#72318): selecting or reverting the
backend is a single ``terminal_transport.backend`` line in
``.mozyo-bridge/config.yaml`` plus a process restart — this resolver reads the
selection fresh per process and holds no state, so there is no data migration and
no persisted binding to clear.

Exhaustive audit of the tmux calls reachable under the binding (Redmine #13253 j#72361)
---------------------------------------------------------------------------------------
The decorator swaps the ``commands`` module's ``run_tmux`` / ``capture_pane`` for
this shim, so the shim must handle *every* tmux op ``orchestrate_handoff`` resolves
through those two names for the length of a send. That set was enumerated from the
send body (``commands.py`` strict/queue-enter rail), the target-activation tail
(``handoff_target_activation_command.py`` — activate + restore), and the
``wait_for_text`` loop (``session_bootstrap_command.py`` → ``commands.capture_pane``):

| tmux op reached under the binding                  | classification | herdr handling                                   |
| -------------------------------------------------- | -------------- | ------------------------------------------------ |
| ``send-keys -t T -l -- <text>``                    | map            | ``send_text(T, text)``                           |
| ``send-keys -t T Enter``                           | map            | ``send_keys(T, "enter")``                        |
| ``send-keys -t T C-u``                             | map            | ``send_keys(T, "C-u")``                          |
| ``capture-pane`` (via ``capture_pane(T, lines)``)  | map            | ``read_pane(T, source="visible", lines=lines)``  |
| ``select-pane -t T`` (activate + restore, #12597)  | no-op (valid target checked) | success, no port call — see below   |
| anything else                                      | fail-closed    | raise :class:`TransportBindingError`             |

``select-pane`` is mapped to a **target-validated no-op success**, not a herdr
call and not a tmux pass-through. Rationale (kept enforced in the docstring and the
design doc): pane *selection* is a tmux composer-landing concern (#12597 — activate
an admitted inactive split before typing, then optionally restore focus). herdr
lands text in a receiver's composer **without** needing the pane focused — every
PoC #13175 injection (experiments E8 / E12–E14) succeeded against a non-focused
pane — so there is nothing for herdr to do on a ``select-pane``. Passing the tmux
handle through to a tmux client would be wrong (it hands a herdr target to tmux),
so the shim absorbs it as a no-op. The target is still checked for well-formedness
(non-empty, no whitespace) so a malformed handle fails closed rather than being
silently absorbed — but *not* with the strict herdr-handle ``valid_target`` guard,
which is the subprocess-safety regex for ``window:pane`` / agent-name handles and
rejects the tmux pane ids (``%N``) the activation tail actually passes; a no-op runs
no subprocess, so that stricter guard is unwarranted here. ``run_tmux``'s return for
``select-pane`` is ignored by both call sites (``activate_target_pane`` /
``maybe_restore_previous_active``), but the shim still returns a success
``CompletedProcess`` for signature parity.

Scope (staged seam — kept explicit so it does not drift):

- **In scope:** the pure ``config -> TransportBinding`` resolver, the tmux
  passthrough binding, and the tmux-shaped herdr shim (the send-text / Enter / C-u /
  capture maps, the ``select-pane`` target-validated no-op, and fail-closed on any
  other subcommand or a failed primitive).
- **Out of scope (later US's):** switching a real workspace's config to herdr and
  the live cut-over smoke (#13254), any live herdr binary run, and wiring the
  richer event-based :class:`~...domain.turn_start_rail.HerdrTurnStartRail` (#13248)
  into the send — that rail integration was split out of #13253 into the follow-up
  **#13255** (Redmine #13253 j#72361). #13253 reuses the existing tmux-shaped
  send/capture choreography unchanged, so it binds only the transport primitives,
  not the turn-start orchestration.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BACKEND_TMUX,
    SOURCE_VISIBLE,
    TerminalTransportConfig,
    TerminalTransportError,
    TerminalTransportPort,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (
    Runner,
    resolve_terminal_transport,
)

#: A tmux-shaped ``run_tmux(*args, check=True) -> CompletedProcess`` callable.
RunTmux = Callable[..., "subprocess.CompletedProcess[str]"]
#: A tmux-shaped ``capture_pane(target, lines) -> str`` callable.
CapturePane = Callable[[str, int], str]


class TransportBindingError(TerminalTransportError):
    """A tmux operation cannot be mapped onto the selected transport backend.

    Subclasses :class:`TerminalTransportError` (itself a :class:`ValueError`) so
    the whole terminal-runtime seam shares one fail-closed error base. Raised by
    the herdr shim when it is handed a tmux subcommand it does not recognise, or
    when a mapped transport primitive reports a failure — never a silent no-op.
    """


@dataclass(frozen=True)
class TransportBinding:
    """The two tmux-shaped primitives the handoff rail runs, plus the backend name.

    ``run_tmux`` and ``capture_pane`` have the *exact* signatures the rail already
    calls (``run_tmux(*args, check=True)`` / ``capture_pane(target, lines)``), so
    the send choreography does not change when the backend does. ``backend`` names
    which runtime the callables drive (:data:`BACKEND_TMUX` / :data:`BACKEND_HERDR`),
    so the rail can install the herdr shim only when it differs from the default.
    """

    backend: str
    run_tmux: RunTmux
    capture_pane: CapturePane


class _HerdrTmuxShim:
    """A tmux-shaped adapter over a :class:`TerminalTransportPort` (herdr).

    Maps the tmux argv shapes the handoff rail reaches under the binding onto the
    port's primitives (or a no-op for pane selection); anything else fails closed.
    The mapping is intentionally an exact argv match (never a prefix / substring
    guess) so a new tmux call the rail might grow can never be silently mis-routed —
    an unrecognised shape raises :class:`TransportBindingError`. The reachable set
    was enumerated exhaustively in the module docstring's audit table.

    | tmux call (rail)                                   | herdr port call                                  |
    | -------------------------------------------------- | ------------------------------------------------ |
    | ``run_tmux("send-keys","-t",T,"-l","--",text)``    | ``send_text(T, text)`` (inject composer body)    |
    | ``run_tmux("send-keys","-t",T,"Enter")``           | ``send_keys(T, "enter")`` (submit the turn)      |
    | ``run_tmux("send-keys","-t",T,"C-u")``             | ``send_keys(T, "C-u")`` (composer rollback)      |
    | ``run_tmux("select-pane","-t",T)``                 | *no-op success* (target validated; see below)    |
    | ``capture_pane(T, lines)``                         | ``read_pane(T, source="visible", lines=lines)``  |

    ``select-pane`` (the #12597 activate-inactive-split / restore-focus tail) is a
    tmux composer-landing concern with no herdr equivalent — herdr injects into a
    receiver's composer without focusing its pane — so the shim absorbs it as a
    target-validated no-op rather than mapping it to the port or passing a herdr
    handle through to tmux. A mapped primitive that reports ``ok=False``, or a
    ``select-pane`` with a malformed target, raises :class:`TransportBindingError` —
    the herdr path never returns a silent success and never falls back to tmux.
    """

    #: The literal enter token the rail's ``send-keys … Enter`` maps to, matching
    #: the turn-start rail's ``DEFAULT_ENTER_KEYS`` so both drive the same herdr
    #: key surface.
    _ENTER_KEYS = "enter"
    #: The composer-rollback token (tmux ``C-u`` clears the line); passed through
    #: to herdr ``pane send-keys`` unchanged.
    _ROLLBACK_KEYS = "C-u"

    def __init__(self, port: TerminalTransportPort):
        self._port = port

    def run_tmux(self, *args: str, check: bool = True) -> "subprocess.CompletedProcess[str]":
        """Map a tmux ``send-keys`` / ``select-pane`` invocation; fail closed otherwise.

        Only the ``send-keys`` send shapes and the ``select-pane`` pane-selection
        shape the handoff rail reaches under the binding are recognised. The
        ``check`` flag is accepted for signature parity with the tmux client but is
        irrelevant here: a failed herdr primitive (or a malformed target) always
        raises regardless of ``check`` (a silent non-zero would defeat the
        fail-closed contract).
        """
        if len(args) >= 3 and args[0] == "send-keys" and args[1] == "-t":
            target = args[2]
            rest = tuple(args[3:])
            if len(rest) == 3 and rest[0] == "-l" and rest[1] == "--":
                self._require_ok(self._port.send_text(target, rest[2]), "send_text")
                return _ok_completed(args)
            if len(rest) == 1:
                if rest[0] == "Enter":
                    self._require_ok(
                        self._port.send_keys(target, self._ENTER_KEYS), "send_keys(enter)"
                    )
                    return _ok_completed(args)
                if rest[0] == self._ROLLBACK_KEYS:
                    self._require_ok(
                        self._port.send_keys(target, self._ROLLBACK_KEYS),
                        "send_keys(C-u)",
                    )
                    return _ok_completed(args)
        if len(args) == 3 and args[0] == "select-pane" and args[1] == "-t":
            # #12597 activate-inactive-split / restore-focus. Pane focus is a tmux
            # composer-landing concern; herdr lands without focusing the pane, so
            # this is a no-op — but the target is still checked for well-formedness
            # so a malformed handle fails closed rather than being silently absorbed.
            #
            # NB: this deliberately does NOT use the domain ``valid_target`` guard.
            # That guard is the subprocess-safety regex for *herdr* handles
            # (``window:pane`` / agent name) and rejects a leading ``%``, but the
            # activation tail's target is a tmux pane id (``%N``, from
            # ``target_info["id"]``). ``select-pane`` runs no subprocess here (it is
            # a no-op), so the strict argv-injection guard is unwarranted; a minimal
            # non-empty / no-whitespace check is the right fail-closed shape.
            target = args[2]
            if not _well_formed_pane_target(target):
                raise TransportBindingError(
                    f"herdr transport received a select-pane with a malformed target "
                    f"{target!r}; refusing to absorb it"
                )
            return _ok_completed(args)
        raise TransportBindingError(
            "herdr transport cannot map tmux invocation "
            f"{list(args)!r}; only the handoff send-keys shapes "
            "(literal text / Enter / C-u) and select-pane are supported — "
            "refusing to run it"
        )

    def capture_pane(self, target: str, lines: int) -> str:
        """Map ``capture_pane`` onto the herdr port's ``read_pane`` (visible source)."""
        result = self._port.read_pane(target, source=SOURCE_VISIBLE, lines=lines)
        if not result.ok:
            raise TransportBindingError(
                f"herdr read_pane failed (reason={result.reason}): {result.detail}"
            )
        return result.content or ""

    @staticmethod
    def _require_ok(result: object, primitive: str) -> None:
        ok = getattr(result, "ok", False)
        if not ok:
            reason = getattr(result, "reason", None)
            detail = getattr(result, "detail", "")
            raise TransportBindingError(
                f"herdr {primitive} failed (reason={reason}): {detail}"
            )


def _ok_completed(args: "tuple[str, ...]") -> "subprocess.CompletedProcess[str]":
    """A success ``CompletedProcess`` shaped like ``tmux_client.run_tmux``'s return.

    The handoff rail ignores ``run_tmux``'s return for its send-keys calls, but a
    tmux-shaped callable must still return a ``CompletedProcess`` so any caller
    reading ``returncode`` / ``stdout`` sees a well-formed success.
    """
    return subprocess.CompletedProcess(args=list(args), returncode=0, stdout="", stderr="")


def _well_formed_pane_target(target: object) -> bool:
    """A minimal fail-closed guard for a ``select-pane`` no-op target.

    ``select-pane`` is absorbed as a no-op (no subprocess, no port call), so the
    strict herdr-handle ``valid_target`` guard is unwarranted and would wrongly
    reject the tmux pane ids (``%N``) the activation tail actually passes. A target
    is well-formed here iff it is a non-empty string with no whitespace — enough to
    reject empty / garbage handles while accepting a tmux pane id or location.
    """
    return isinstance(target, str) and bool(target) and not any(
        c.isspace() for c in target
    )


def resolve_runtime_transport_binding(
    config: Optional[TerminalTransportConfig] = None,
    *,
    tmux_run_tmux: RunTmux,
    tmux_capture_pane: CapturePane,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
    port: Optional[TerminalTransportPort] = None,
) -> TransportBinding:
    """Resolve the :class:`TransportBinding` for a ``terminal_transport`` selection.

    ``config`` is the repo-local :class:`TerminalTransportConfig` (``None`` ⇒ the
    default / tmux). The tmux primitives are **injected** (``tmux_run_tmux`` /
    ``tmux_capture_pane``) so this module never imports the tmux infrastructure
    package: the caller — which already holds the rail's ``run_tmux`` /
    ``capture_pane`` — passes them in, and for the tmux backend they are returned
    *unchanged* (the binding's callables are identical objects, so the tmux path is
    byte-for-byte the current behaviour).

    Fail-closed selection (no silent tmux fallback once herdr is selected):

    - the default / tmux backend returns a passthrough binding over the injected
      tmux callables;
    - the herdr backend resolves the #13245 transport port (or uses an injected
      ``port`` for tests) and returns a tmux-shaped shim binding; an unconfigured
      or unresolvable herdr binary raises :class:`TerminalTransportError` from
      :func:`resolve_terminal_transport`.
    """
    if config is None:
        config = TerminalTransportConfig.default()
    if not config.herdr_enabled:
        return TransportBinding(
            backend=BACKEND_TMUX,
            run_tmux=tmux_run_tmux,
            capture_pane=tmux_capture_pane,
        )
    resolved_port: Optional[TerminalTransportPort]
    if port is not None:
        resolved_port = port
    else:
        # Fail-closed: raises TerminalTransportError when the herdr binary is
        # unconfigured / unresolvable — never a silent downgrade to tmux.
        resolved_port = resolve_terminal_transport(config, env=env, runner=runner)
    if resolved_port is None:
        raise TransportBindingError(
            "terminal transport backend 'herdr' is selected but no transport port "
            "could be resolved; refusing to fall back to tmux"
        )
    shim = _HerdrTmuxShim(resolved_port)
    return TransportBinding(
        backend=BACKEND_HERDR,
        run_tmux=shim.run_tmux,
        capture_pane=shim.capture_pane,
    )


__all__ = (
    "CapturePane",
    "RunTmux",
    "TransportBinding",
    "TransportBindingError",
    "resolve_runtime_transport_binding",
)
