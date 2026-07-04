"""Built-in herdr CLI terminal-transport adapter (Redmine #13245).

The core seam
(:mod:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport`)
defines the fail-closed :class:`TerminalTransportPort` and the default-off
backend selection. This module is the single concrete, built-in provider that
fills it: a subprocess wrapper over the **herdr CLI**. Core still owns the
send-safety contract, the result / reason vocabulary, and the target guard; this
module only performs the provider-owned mechanics, so the dependency points
provider -> core.

Why the CLI and not the socket protocol
---------------------------------------
The #13175 PoC (``vibes/docs/logics/herdr-poc-13175-experiment-log.md``) proved
the transport round-trip two ways: the herdr **CLI** (``pane send-text`` /
``pane send-keys`` / ``agent read``, experiments E8 / E11) and the raw Unix-domain
**socket JSON protocol** underneath it. This adapter deliberately targets the
**CLI**: it is the documented, stable surface, whereas the socket wire protocol
is an *internal, unpublished* herdr detail (E2 catalogued the remote/surface
inventory; the socket JSON shapes carry no compatibility promise). Binding to the
CLI keeps the adapter robust across herdr versions and avoids re-implementing an
undocumented protocol — the same "one built-in provider over a stable surface"
posture as the Redmine note transport (#12347) using the documented HTTP API.

Trusted-environment binary boundary
-----------------------------------
The herdr executable path comes **only** from the trusted environment
(:data:`HERDR_BINARY_ENV`), never a repo-local file. Running an arbitrary binary
is a code-execution vector, so — exactly like the delivery-write credentials
(#12347) — the executable a checkout can cause mozyo to spawn is pinned by the
daemon environment, not by ``.mozyo-bridge/config.yaml``. The repo-local config
only *selects* the herdr backend; it can never say *which* binary runs. When
herdr is selected but the binary is unset or unresolvable,
:func:`resolve_terminal_transport` fails closed
(``binary_unconfigured`` / ``binary_not_found``) with **no silent fallback** to
tmux.

Scope (staged seam)
-------------------
These are bare send / read primitives. The PoC learnings that belong to the
*send rail* — clearing a residual composer before injection (E8's stray ``qq``
prefix) and the Codex Enter-resend / check-then-wait turn-start rails (E9 /
E12–E14) — are **not** implemented here; they layer on top in the turn-start US
(#13248). No test in this US runs a live herdr binary: the port contract is
exercised through an in-memory fake, and this adapter is exercised through an
injected subprocess ``runner`` that verifies argv and simulates
success / not-found / non-zero-exit without spawning herdr.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    DEFAULT_PANE_READ_SOURCE,
    PANE_READ_SOURCES,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_INVALID_SOURCE,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    PaneReadResult,
    TerminalTransportConfig,
    TerminalTransportError,
    TransportResult,
    valid_target,
)

#: The trusted-environment variable naming the herdr executable (an absolute
#: path or a ``PATH``-resolvable name). Read at resolution time; a repo-local
#: file can never supply it.
HERDR_BINARY_ENV = "MOZYO_HERDR_BINARY"

#: How long a single herdr CLI invocation may block before it is treated as a
#: transport error. Kept short so an unresponsive herdr fails closed quickly.
COMMAND_TIMEOUT_SECONDS = 10

# The runner protocol: a callable with ``subprocess.run``'s shape. Injected so
# tests can verify argv and simulate outcomes without spawning a process.
Runner = Callable[..., "subprocess.CompletedProcess[str]"]


class HerdrCliTransport:
    """A :class:`TerminalTransportPort` implemented over the herdr CLI.

    Each primitive builds an explicit argv list (never a shell string) and runs
    it through the injected ``runner``. Every failure — a malformed target, a
    missing binary, a non-zero exit, a timeout, or an OS error — is turned into a
    structured failure result with a reason from the core vocabulary; a primitive
    never raises out of the transport and never returns a silent success.
    """

    backend = BACKEND_HERDR

    def __init__(
        self,
        binary: str,
        *,
        runner: Optional[Runner] = None,
        timeout: float = COMMAND_TIMEOUT_SECONDS,
    ):
        if not isinstance(binary, str) or not binary:
            raise TerminalTransportError(
                "herdr transport binary must be a non-empty string"
            )
        self._binary = binary
        self._runner: Runner = runner if runner is not None else subprocess.run
        self._timeout = timeout

    # -- primitives -----------------------------------------------------------

    def send_text(self, target: str, text: str) -> TransportResult:
        """Inject ``text`` into ``target``'s composer via ``pane send-text``.

        This is a bare primitive: it does not clear a residual composer or submit
        the text (that rail is #13248, PoC E8 / E14). ``text`` is passed as a
        single argv element, so it can never inject an extra token.
        """
        if not valid_target(target):
            return TransportResult.failure(
                REASON_INVALID_TARGET, f"invalid target handle: {target!r}"
            )
        if not isinstance(text, str):
            return TransportResult.failure(
                REASON_INVALID_TARGET, "send_text 'text' must be a string"
            )
        return self._run_send(["pane", "send-text", target, text])

    def send_keys(self, target: str, keys: str) -> TransportResult:
        """Send raw key token(s) (e.g. ``enter``) to ``target`` via ``pane send-keys``."""
        if not valid_target(target):
            return TransportResult.failure(
                REASON_INVALID_TARGET, f"invalid target handle: {target!r}"
            )
        if not isinstance(keys, str) or not keys:
            return TransportResult.failure(
                REASON_INVALID_TARGET, "send_keys 'keys' must be a non-empty string"
            )
        return self._run_send(["pane", "send-keys", target, keys])

    def read_pane(
        self,
        target: str,
        *,
        source: str = DEFAULT_PANE_READ_SOURCE,
        lines: Optional[int] = None,
    ) -> PaneReadResult:
        """Read rendered content of ``target`` via ``agent read`` (PoC E11).

        ``source`` must be one of the core-owned :data:`PANE_READ_SOURCES`;
        ``lines`` (when given) must be a positive int. On success the herdr JSON
        payload is parsed defensively for the rendered text and the ``truncated``
        flag; if the payload is not a recognised JSON object the raw stdout is
        returned as the content (the exact JSON schema is confirmed against a
        live binary in a later US, per the PoC caveat).
        """
        if not valid_target(target):
            return PaneReadResult.failure(
                REASON_INVALID_TARGET, f"invalid target handle: {target!r}"
            )
        # Check the type before the membership test: ``source not in
        # PANE_READ_SOURCES`` raises ``TypeError`` for an unhashable ``source``
        # (e.g. a list), which would escape the fail-closed contract. A non-str
        # source is always invalid, so reject it first.
        if not isinstance(source, str) or source not in PANE_READ_SOURCES:
            return PaneReadResult.failure(
                REASON_INVALID_SOURCE,
                f"unknown pane read source {source!r}; expected one of "
                f"{sorted(PANE_READ_SOURCES)}",
            )
        argv = ["agent", "read", target, "--source", source]
        if lines is not None:
            if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
                return PaneReadResult.failure(
                    REASON_INVALID_TARGET,
                    f"read_pane 'lines' must be a positive int, got {lines!r}",
                )
            argv += ["--lines", str(lines)]
        completed = self._invoke(argv)
        if isinstance(completed, TransportResult):
            # A fail-closed spawn/timeout outcome; re-shape to the read result.
            return PaneReadResult.failure(
                completed.reason or REASON_TRANSPORT_ERROR, completed.detail
            )
        if completed.returncode != 0:
            return PaneReadResult.failure(
                REASON_TRANSPORT_ERROR,
                _bounded_detail(completed.stderr) or f"herdr exit {completed.returncode}",
            )
        content, truncated = _parse_read_payload(completed.stdout)
        return PaneReadResult.success(content, truncated=truncated)

    # -- internals ------------------------------------------------------------

    def _run_send(self, tail: list) -> TransportResult:
        completed = self._invoke(tail)
        if isinstance(completed, TransportResult):
            return completed  # a fail-closed spawn/timeout outcome
        if completed.returncode != 0:
            return TransportResult.failure(
                REASON_TRANSPORT_ERROR,
                _bounded_detail(completed.stderr) or f"herdr exit {completed.returncode}",
            )
        return TransportResult.success()

    def _invoke(self, tail: list):
        """Run ``binary tail...``; return the CompletedProcess or a failure result.

        A missing binary maps to ``binary_not_found`` and any other spawn / OS /
        timeout failure to ``transport_error``. The failure is returned as a
        :class:`TransportResult` (the send helpers and ``read_pane`` re-shape it
        for their own return type), so no exception escapes a primitive.
        """
        argv = [self._binary, *tail]
        try:
            return self._runner(
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout,
            )
        except FileNotFoundError:
            return TransportResult.failure(
                REASON_BINARY_NOT_FOUND,
                f"herdr binary not found: {self._binary!r}",
            )
        except subprocess.TimeoutExpired:
            return TransportResult.failure(
                REASON_TRANSPORT_ERROR, "herdr command timed out"
            )
        except OSError as exc:
            return TransportResult.failure(
                REASON_TRANSPORT_ERROR, f"herdr command failed ({exc.__class__.__name__})"
            )


def _bounded_detail(text: object, *, limit: int = 200) -> str:
    """A short, single-line diagnostic from a subprocess stream (never a secret).

    A terminal transport handles no credentials, but stderr can still carry a
    local path; keep it bounded and single-line so a diagnostic never becomes a
    large or multi-line blob on a result.
    """
    if not isinstance(text, str):
        return ""
    collapsed = " ".join(text.split())
    if len(collapsed) > limit:
        return collapsed[:limit] + "…"
    return collapsed


def _parse_read_payload(stdout: object) -> tuple:
    """Extract ``(content, truncated)`` from a herdr ``agent read`` payload.

    Defensive by design (the live JSON schema is confirmed in a later US): a JSON
    object contributes its text field (the first present of a small candidate
    set) and a boolean ``truncated``; anything else is treated as raw text with
    ``truncated=False``.
    """
    if not isinstance(stdout, str):
        return "", False
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return stdout, False
    if not isinstance(payload, Mapping):
        return stdout, False
    content = None
    for key in ("content", "text", "visible", "data"):
        value = payload.get(key)
        if isinstance(value, str):
            content = value
            break
    if content is None:
        content = stdout
    truncated = bool(payload.get("truncated", False))
    return content, truncated


def resolve_terminal_transport(
    config: Optional[TerminalTransportConfig] = None,
    *,
    env: Optional[Mapping[str, str]] = None,
    runner: Optional[Runner] = None,
) -> Optional[HerdrCliTransport]:
    """Resolve the built-in terminal transport for ``config``, or ``None`` (off).

    Fail-closed selection semantics (no silent fallback to tmux):

    - the default / tmux backend returns ``None`` — herdr transport is off and
      the existing tmux path is untouched;
    - the herdr backend with no :data:`HERDR_BINARY_ENV` in the trusted
      environment raises :class:`TerminalTransportError` (``binary_unconfigured``);
    - the herdr backend with a configured but unresolvable binary (not an
      executable file and not on ``PATH``) raises
      :class:`TerminalTransportError` (``binary_not_found``);
    - the herdr backend with a resolvable binary returns a
      :class:`HerdrCliTransport` bound to the resolved path.
    """
    if config is None:
        config = TerminalTransportConfig.default()
    if not config.herdr_enabled:
        return None
    source_env = env if env is not None else os.environ
    raw = source_env.get(HERDR_BINARY_ENV)
    binary = raw.strip() if isinstance(raw, str) else ""
    if not binary:
        raise TerminalTransportError(
            f"terminal transport backend 'herdr' is selected but no herdr binary "
            f"is configured in the trusted environment ({HERDR_BINARY_ENV}); refusing "
            f"to fall back to tmux",
            reason=REASON_BINARY_UNCONFIGURED,
        )
    resolved = _resolve_binary(binary, source_env)
    if resolved is None:
        raise TerminalTransportError(
            f"herdr binary {binary!r} (from {HERDR_BINARY_ENV}) was not found as an "
            f"executable file or on the trusted environment PATH; refusing to fall "
            f"back to tmux",
            reason=REASON_BINARY_NOT_FOUND,
        )
    return HerdrCliTransport(resolved, runner=runner)


def _resolve_binary(binary: str, source_env: Mapping[str, str]) -> Optional[str]:
    """Resolve ``binary`` to an executable path, or ``None`` if unresolvable.

    A path-shaped value (containing a separator) must be an existing executable
    file; a bare name is resolved on the **trusted environment's** ``PATH`` (the
    same env the binary token itself came from), not the ambient process ``PATH``
    — so a supplied trusted env fully determines resolution and an entry present
    only on the ambient PATH is not silently picked up. When ``source_env`` is
    the ambient ``os.environ`` (the default), this is byte-for-byte the previous
    behaviour.
    """
    if os.sep in binary or (os.altsep and os.altsep in binary):
        if os.path.isfile(binary) and os.access(binary, os.X_OK):
            return binary
        return None
    return shutil.which(binary, path=source_env.get("PATH"))


__all__ = (
    "COMMAND_TIMEOUT_SECONDS",
    "HERDR_BINARY_ENV",
    "HerdrCliTransport",
    "resolve_terminal_transport",
)
