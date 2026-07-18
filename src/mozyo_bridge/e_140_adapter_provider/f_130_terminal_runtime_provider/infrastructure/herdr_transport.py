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
The herdr executable path comes **only** from the trusted environment, never a
repo-local file or the current working directory. Running an arbitrary binary is
a code-execution vector, so — exactly like the delivery-write credentials
(#12347) — the executable a checkout can cause mozyo to spawn is pinned by the
daemon environment, not by ``.mozyo-bridge/config.yaml``. The repo-local config
only *selects* the herdr backend; it can never say *which* binary runs.

The resolution order is the explicit :data:`HERDR_BINARY_ENV` value, then an
executable ``herdr`` on the trusted ``PATH`` (Redmine #13496 / #13500 — a normal
owner shell no longer has to export ``MOZYO_HERDR_BINARY`` on every run). If the
trusted ``PATH`` carries any empty or relative component (a shell would resolve it
against the cwd) the whole PATH is rejected — never silently skipped — so an
unsafe trusted environment can never resolve; only an all-absolute PATH is
searched. The executable bit is verified against the symlink-resolved realpath,
and more than one *distinct* real executable fails closed rather than guessing.
When nothing resolves, :func:`resolve_herdr_binary` (and thus
:func:`resolve_terminal_transport`) fails closed (``binary_unconfigured`` /
``binary_not_found`` / ``binary_unsafe_path`` / ``binary_ambiguous``) with **no
silent fallback** to tmux.

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
import re
import subprocess
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.pane_render_observation import (
    CURSOR_RELATION_ABSENT,
    CURSOR_RELATION_COMPOSER,
    CURSOR_RELATION_ELSEWHERE,
    CURSOR_RELATION_UNKNOWN,
    RENDER_REASON_AMBIGUOUS_RENDER,
    RENDER_REASON_ANSI_ABSENT,
    RENDER_REASON_ANSI_UNSUPPORTED,
    RENDER_REASON_EMPTY_COMPOSER,
    RENDER_REASON_INVALID_TARGET,
    RENDER_REASON_NO_COMPOSER,
    RENDER_REASON_OK,
    RENDER_REASON_UNREADABLE,
    STYLE_PROVENANCE_DIM,
    STYLE_PROVENANCE_MIXED,
    STYLE_PROVENANCE_NORMAL,
    PaneRenderObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (
    BACKEND_HERDR,
    BINARY_SOURCE_ENV,
    BINARY_SOURCE_PATH,
    DEFAULT_PANE_READ_SOURCE,
    PANE_READ_SOURCES,
    REASON_BINARY_AMBIGUOUS,
    REASON_BINARY_NOT_FOUND,
    REASON_BINARY_UNCONFIGURED,
    REASON_BINARY_UNSAFE_PATH,
    REASON_INVALID_SOURCE,
    REASON_INVALID_TARGET,
    REASON_TRANSPORT_ERROR,
    HerdrBinaryResolution,
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

#: The bare herdr executable name resolved on the trusted ``PATH`` when the
#: explicit :data:`HERDR_BINARY_ENV` value is absent (Redmine #13496 resolution
#: order step 2). The name is fixed — only the *trusted* ``PATH`` decides which
#: file it is, never a repo-local config.
HERDR_PATH_NAME = "herdr"

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

    @property
    def binary(self) -> str:
        """The resolved herdr executable path this transport is bound to.

        Read-only accessor so a caller that only needs the *resolved* binary
        (e.g. the backend-aware ``mozyo`` entrypoint's herdr UI ``exec``) can
        reuse :func:`resolve_terminal_transport`'s single fail-closed resolution
        (and its exact ``refusing to fall back to tmux`` wording) instead of
        re-implementing the ``MOZYO_HERDR_BINARY`` resolution.
        """
        return self._binary

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
        flag: the live schema nests both under ``result.read`` (confirmed against
        the live binary — Redmine #13322), with a top-level / raw-stdout fallback
        for any other shape (:func:`_parse_read_payload`).
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

    def read_pane_render(
        self,
        target: str,
        *,
        source: str = DEFAULT_PANE_READ_SOURCE,
        lines: Optional[int] = None,
    ) -> PaneRenderObservation:
        """Observe ``target``'s composer *style* via ``agent read --format ansi`` (#14065).

        A herdr-provider capability — deliberately NOT on the core
        :class:`TerminalTransportPort` (tmux has no composer-style surface). It is
        *capability-negotiated*: the adapter asks the supported binary for an ANSI
        render, and every way that can fail — an unsupported ``--format ansi`` /
        ``--ansi`` flag, an absent ANSI stream, a composer-less or empty pane, an
        unparseable render, a transport failure — resolves to a fail-closed
        :class:`PaneRenderObservation` (``readable=False`` + a specific closed
        reason), never a silent text fallback that fabricates a positive signal
        (#14065 Design Answer j#82160 scope item 4).

        The return value is fully redacted: only closed enums / bools leave this
        method. The raw ANSI is consumed *inside* :func:`_parse_render_payload` and
        never returned, logged, or surfaced (IR acceptance #3 / #4). The legacy
        :meth:`read_pane` text contract is untouched — this is an additive sibling.
        """
        if not valid_target(target):
            return PaneRenderObservation.failed(RENDER_REASON_INVALID_TARGET)
        if not isinstance(source, str) or source not in PANE_READ_SOURCES:
            return PaneRenderObservation.failed(RENDER_REASON_INVALID_TARGET)
        argv = ["agent", "read", target, "--source", source, "--format", "ansi", "--ansi"]
        if lines is not None:
            if isinstance(lines, bool) or not isinstance(lines, int) or lines <= 0:
                return PaneRenderObservation.failed(RENDER_REASON_INVALID_TARGET)
            argv += ["--lines", str(lines)]
        completed = self._invoke(argv)
        if isinstance(completed, TransportResult):
            # A fail-closed spawn / timeout / OS outcome: unreadable, preserve.
            return PaneRenderObservation.failed(RENDER_REASON_UNREADABLE)
        if completed.returncode != 0:
            # A non-zero exit whose stderr carries an unknown-flag signature means
            # the supported binary has no ANSI render surface (capability negotiation
            # failed); anything else is a generic unreadable transport failure. Both
            # are fail-closed / preserve — the distinction only sharpens the reason.
            reason = (
                RENDER_REASON_ANSI_UNSUPPORTED
                if _looks_like_unknown_flag(completed.stderr)
                else RENDER_REASON_UNREADABLE
            )
            return PaneRenderObservation.failed(reason)
        return _parse_render_payload(completed.stdout)

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

    The live herdr CLI nests the rendered text under ``result.read`` (E11 schema,
    confirmed against the live binary — Redmine #13322):
    ``{"result": {"read": {"text": "...", "truncated": false, ...}}}``. Extract from
    that nested object when present, else from the top-level mapping (an
    older/simpler shape), reading the first present of a small candidate text key
    set and a boolean ``truncated``; anything unrecognised is treated as raw text
    with ``truncated=False``.

    Getting this nesting right matters beyond diagnostics: the Enter-resend gate
    (:func:`~...domain.turn_start_rail.composer_retains_body`) whitespace-collapses
    the returned content and substring-matches the injected body against it. Before
    #13322 the nested schema fell through to the *raw JSON stdout*, whose composer
    line-wraps are JSON-escaped ``\\n`` sequences that ``str.split`` does not treat
    as whitespace — so a wrapped body never matched and the rail refused to resend
    (``enter_resends=0``). Returning the decoded ``result.read.text`` (real newlines)
    lets the collapse work and the resend gate fire.
    """
    if not isinstance(stdout, str):
        return "", False
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return stdout, False
    if not isinstance(payload, Mapping):
        return stdout, False
    # Prefer the live nested `result.read` object; fall back to the top-level
    # mapping so a flatter/older payload shape still parses.
    source: Mapping = payload
    result = payload.get("result")
    if isinstance(result, Mapping):
        read_obj = result.get("read")
        if isinstance(read_obj, Mapping):
            source = read_obj
    content = None
    for key in ("content", "text", "visible", "data"):
        value = source.get(key)
        if isinstance(value, str):
            content = value
            break
    if content is None:
        content = stdout
    truncated = bool(source.get("truncated", False))
    return content, truncated


# -- composer-render (ANSI style) parsing (Redmine #14065) --------------------
# Escape-sequence grammars. A full CSI is ESC '[' <parameter bytes> <intermediate
# bytes 0x20-0x2F> <final byte 0x40-0x7E>. The parameter class spans the private
# The ONLY escape a visible-buffer *style* snapshot legitimately needs: a standard
# SGR — ``ESC[`` <numeric / ``;`` parameters> ``m`` — which changes intensity / colour
# WITHOUT moving the cursor, erasing, scrolling, or otherwise mutating the screen.
# EVERY other escape is rejected as ambiguous, not consumed: a non-SGR CSI (cursor
# move ``ESC[H`` / erase ``ESC[2K`` / mode toggle), a private / extension CSI, an OSC
# or charset sequence, and any malformed / unterminated escape all either mutate the
# rendered buffer (whose effect this instrument does NOT emulate) or cannot be
# reconstructed. Consuming such a sequence and then trusting the reassembled text
# would launder a stale, erased, or displaced body into a positive style — exactly
# the residual the R2 review caught (Redmine #14065 review j#82171 finding 2: a
# ``CSI 2K`` erase after a dim prompt must NOT stay classified as ``dim``). So the
# scanner recognises SGR alone and fails the whole render closed (``ambiguous_render``)
# on the first escape that is not a standard SGR.
_SGR_RE = re.compile(r"\x1b\[[0-9;]*m")

# The composer prompt marker set + body, mirroring the e110 observer's
# ``_PROMPT_RE`` (``sublane_quarantine``). Duplicated as a one-line provider-local
# literal on purpose: the adapter (e140) must not import the consumer (e110), and
# the render parsing is a provider mechanic. Kept in sync by the adversarial
# regression fixtures, not by a cross-layer import.
_RENDER_PROMPT_RE = re.compile(r"^\s*[›❯>]\s*(?P<body>.*)$")
_PROMPT_MARKERS = "›❯>"

# The closed set of SGR intensities this instrument reads as "dim" (a ghost
# idle-placeholder candidate): SGR 2 (faint) and SGR 90 (bright-black / gray).
# Everything else is "normal"; an explicit reset (0) or normal-intensity (22) /
# default-or-standard foreground (39 / 30-37 / 38…) clears the respective dim
# axis. A provider that draws placeholders with some other gray (e.g. an 8-bit
# palette index) reads as ``normal`` here — the fail-SAFE direction, since phase 1
# only measures and phase 2 requires a STABLE positive separation before acting.


def _apply_sgr(params: str, faint: bool, dim_fg: bool) -> tuple[bool, bool]:
    """Fold one SGR parameter list into the (faint, dim_fg) intensity state."""
    codes = params.split(";") if params else [""]
    idx = 0
    while idx < len(codes):
        raw = codes[idx]
        code = raw if raw != "" else "0"
        if code == "0":
            faint = False
            dim_fg = False
        elif code == "2":
            faint = True
        elif code == "22":
            faint = False
        elif code == "90":
            dim_fg = True
        elif code == "39" or (code.isdigit() and 30 <= int(code) <= 37):
            dim_fg = False
        elif code == "38":
            # Extended foreground (38;5;N or 38;2;R;G;B): a concrete fg colour, so
            # it clears the bright-black gray-dim axis, and its own parameters are
            # skipped so they can never be misread as intensity codes.
            dim_fg = False
            if idx + 1 < len(codes) and codes[idx + 1] == "5":
                idx += 2
            elif idx + 1 < len(codes) and codes[idx + 1] == "2":
                idx += 4
        idx += 1
    return faint, dim_fg


def _consume_escape(ansi: str, i: int) -> tuple:
    """Consume the escape at ``i``: ``(end_index, kind, sgr_params)``.

    ``kind`` is ``"sgr"`` (a standard intensity/colour SGR, with ``sgr_params``) or
    ``"ambiguous"`` (anything else). Only a standard SGR is recognised; every other
    escape — a non-SGR CSI (cursor / erase / mode), a private / extension CSI, an OSC
    or charset sequence, or a malformed one — fails the whole render closed rather
    than being consumed and its surrounding text trusted (Redmine #14065 review
    j#82171 finding 2: a screen-mutating CSI must never launder a stale / erased body
    into a positive style).
    """
    match = _SGR_RE.match(ansi, i)
    if match:
        return match.end(), "sgr", match.group(0)[2:-1]
    return i + 1, "ambiguous", None


def _scan_ansi_intensities(ansi: str) -> tuple:
    """Reconstruct visible lines + an ambiguity flag from an ANSI stream.

    Returns ``([[(char, is_dim), …], …], ambiguous)``. Walks the stream tracking
    the SGR intensity state; every visible character is emitted with the intensity
    in force when it was drawn, and every recognised escape (SGR or other control)
    is consumed without emitting a character. The moment an escape cannot be
    resolved into a complete recognised sequence, ``ambiguous`` is ``True`` and the
    scan stops — the classifier fails the render closed rather than trusting a
    partially-parsed screen. The raw text never leaves this module.
    """
    lines: list = []
    current: list = []
    faint = False
    dim_fg = False
    i = 0
    n = len(ansi)
    while i < n:
        ch = ansi[i]
        if ch == "\x1b":
            end, kind, params = _consume_escape(ansi, i)
            if kind == "ambiguous":
                lines.append(current)
                return lines, True
            if kind == "sgr":
                faint, dim_fg = _apply_sgr(params, faint, dim_fg)
            i = end
            continue
        if ch == "\n":
            lines.append(current)
            current = []
            i += 1
            continue
        if ch == "\r":
            i += 1
            continue
        current.append((ch, faint or dim_fg))
        i += 1
    lines.append(current)
    return lines, False


def _prompt_body_cells(cells: list) -> list:
    """The composer body cells after the leading whitespace + prompt marker + gap."""
    i = 0
    n = len(cells)
    while i < n and cells[i][0].isspace():
        i += 1
    if i < n and cells[i][0] in _PROMPT_MARKERS:
        i += 1
    while i < n and cells[i][0].isspace():
        i += 1
    return cells[i:]


def _classify_composer_style(ansi: str) -> tuple:
    """Classify the last composer prompt body's style: ``(prompt_present, idx, prov, reason)``.

    ``reason`` is :data:`RENDER_REASON_OK` with ``prov`` in ``{dim, normal, mixed}``
    only when a composer prompt line with a non-empty body was found and classified;
    otherwise ``prov`` is ``None`` and ``reason`` is the fail-closed cause
    (``no_composer`` / ``empty_composer`` / ``ambiguous_render``).
    """
    lines, ambiguous = _scan_ansi_intensities(ansi)
    if ambiguous:
        return (False, -1, None, RENDER_REASON_AMBIGUOUS_RENDER)
    prompt_idx = -1
    prompt_cells: list = []
    for idx, cells in enumerate(lines):
        text = "".join(char for char, _dim in cells)
        if _RENDER_PROMPT_RE.match(text):
            prompt_idx = idx
            prompt_cells = cells
    if prompt_idx < 0:
        return (False, -1, None, RENDER_REASON_NO_COMPOSER)
    body = _prompt_body_cells(prompt_cells)
    non_space = [(char, dim) for char, dim in body if not char.isspace()]
    if not non_space:
        return (True, prompt_idx, None, RENDER_REASON_EMPTY_COMPOSER)
    intensities = {dim for _char, dim in non_space}
    if intensities == {True}:
        provenance = STYLE_PROVENANCE_DIM
    elif intensities == {False}:
        provenance = STYLE_PROVENANCE_NORMAL
    else:
        provenance = STYLE_PROVENANCE_MIXED
    return (True, prompt_idx, provenance, RENDER_REASON_OK)


def _cursor_relation(cursor: object, prompt_idx: int) -> str:
    """Derive the closed cursor relation from a payload cursor vs the composer line."""
    if cursor is None:
        return CURSOR_RELATION_UNKNOWN
    if cursor is False:
        return CURSOR_RELATION_ABSENT
    if not isinstance(cursor, Mapping):
        return CURSOR_RELATION_UNKNOWN
    row = cursor.get("row")
    if isinstance(row, bool) or not isinstance(row, int):
        return CURSOR_RELATION_UNKNOWN
    return (
        CURSOR_RELATION_COMPOSER if row == prompt_idx else CURSOR_RELATION_ELSEWHERE
    )


#: The rendered-string keys a read payload may carry, in preference order. Under
#: ``--format ansi`` / ``--ansi`` the supported binary is expected to embed SGR
#: escape sequences into the *same* rendered field (a dedicated ``ansi`` key is
#: tried first, then the plain-text keys ``_parse_read_payload`` already reads).
#: Capability negotiation is by *content*: the chosen field must actually contain
#: an ANSI CSI escape (``ESC[``); a payload with no escape in any field means the
#: ANSI capability was not exercised (``ansi_absent``), never a positive signal.
_RENDER_TEXT_KEYS = ("ansi", "content", "text", "visible", "data")


def _select_ansi_stream(source: Mapping) -> Optional[str]:
    """The first candidate field carrying an ANSI escape, or ``None`` (no ANSI)."""
    for key in _RENDER_TEXT_KEYS:
        value = source.get(key)
        if isinstance(value, str) and "\x1b[" in value:
            return value
    return None


def _parse_render_payload(stdout: object) -> PaneRenderObservation:
    """Extract a redacted :class:`PaneRenderObservation` from an ANSI ``agent read`` payload.

    The rendered text is read from the live nested ``result.read`` object (the E11
    schema shape) or the top-level mapping, choosing the first field
    (:data:`_RENDER_TEXT_KEYS`) that actually carries an ANSI CSI escape. A payload
    that parsed but carried no ANSI escape in any field is ``ansi_absent`` — the
    ``--format ansi`` capability was not exercised (the flag was ignored or the
    render is unstyled), so no style could be measured. The raw ANSI is classified
    in place and discarded; only the closed observation is returned, so no body /
    hash / length / excerpt / ANSI can escape.
    """
    if not isinstance(stdout, str):
        return PaneRenderObservation.failed(RENDER_REASON_ANSI_ABSENT)
    try:
        payload = json.loads(stdout)
    except (ValueError, TypeError):
        return PaneRenderObservation.failed(RENDER_REASON_ANSI_ABSENT)
    if not isinstance(payload, Mapping):
        return PaneRenderObservation.failed(RENDER_REASON_ANSI_ABSENT)
    source: Mapping = payload
    result = payload.get("result")
    if isinstance(result, Mapping):
        read_obj = result.get("read")
        if isinstance(read_obj, Mapping):
            source = read_obj
    ansi = _select_ansi_stream(source)
    if ansi is None:
        return PaneRenderObservation.failed(RENDER_REASON_ANSI_ABSENT)
    prompt_present, prompt_idx, provenance, reason = _classify_composer_style(ansi)
    if reason != RENDER_REASON_OK or provenance is None:
        return PaneRenderObservation.failed(reason, prompt_present=prompt_present)
    return PaneRenderObservation.classified(
        provenance, cursor_relation=_cursor_relation(source.get("cursor"), prompt_idx)
    )


def _looks_like_unknown_flag(stderr: object) -> bool:
    """Whether ``stderr`` reads as an unknown/unsupported-flag rejection (bounded).

    Conservative: it takes an explicit "unknown / unrecognised / unexpected /
    invalid / no such" signature AND a mention of a flag / option / argument (or of
    ``format`` / ``ansi`` themselves). Used only to *sharpen* a fail-closed reason
    (``ansi_unsupported`` vs ``unreadable``); both branches preserve, so a
    misread never changes safety.
    """
    if not isinstance(stderr, str):
        return False
    low = stderr.lower()
    signatures = (
        "unknown",
        "unrecognized",
        "unrecognised",
        "unexpected",
        "invalid",
        "no such",
    )
    mentions_flag = any(
        token in low
        for token in ("--format", "--ansi", "format", "ansi", "option", "argument", "flag")
    )
    return any(sig in low for sig in signatures) and mentions_flag


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
    - the herdr backend resolves the binary via :func:`resolve_herdr_binary`
      (explicit :data:`HERDR_BINARY_ENV` then trusted ``PATH`` ``herdr``); an
      unresolvable binary raises :class:`TerminalTransportError`
      (``binary_unconfigured`` / ``binary_not_found``);
    - the herdr backend with a resolvable binary returns a
      :class:`HerdrCliTransport` bound to the resolved absolute path.
    """
    if config is None:
        config = TerminalTransportConfig.default()
    if not config.herdr_enabled:
        return None
    source_env = env if env is not None else os.environ
    resolution = resolve_herdr_binary(source_env)
    return HerdrCliTransport(resolution.path, runner=runner)


def resolve_herdr_binary(source_env: Mapping[str, str]) -> HerdrBinaryResolution:
    """Resolve the herdr executable from the **trusted environment** (fail-closed).

    This is the single trusted-environment resolver every herdr surface shares
    (transport / discovery / state / turn-start / session-start), so the resolved
    binary, its provenance, and the fail-closed vocabulary can never drift between
    them (Redmine #13496). Resolution order:

    1. the explicit :data:`HERDR_BINARY_ENV` value — a path-shaped value must be an
       existing **absolute** executable file (a relative path-shaped value is
       cwd-dependent and refused); a bare name is resolved on the trusted ``PATH``;
    2. an executable :data:`HERDR_PATH_NAME` on the trusted ``PATH`` (Redmine
       #13500: a normal owner shell that already has ``herdr`` on its ``PATH`` no
       longer has to export ``MOZYO_HERDR_BINARY`` on every run).

    The repo-local config / cwd is **never** a source (#13502). If the trusted
    ``PATH`` carries *any* empty or relative component (a shell would resolve it
    against the cwd) the whole trusted PATH is rejected — the resolver does not
    silently skip it and search the rest (Redmine #13496 review j#74773). Only an
    all-absolute PATH is searched. The resolved path is made absolute before it is
    returned for injection into a launch agent, and the executable bit is verified
    against the symlink-resolved ``realpath`` so a dangling or non-executable
    symlink fails closed rather than resolving. Returns a
    :class:`HerdrBinaryResolution` carrying the absolute path, its realpath, and the
    source provenance. Raises :class:`TerminalTransportError` with **no** silent
    fallback to tmux:

    - ``binary_not_found`` — an explicit :data:`HERDR_BINARY_ENV` value that does
      not resolve to a verified executable;
    - ``binary_unconfigured`` — no explicit value AND no executable ``herdr`` on the
      trusted ``PATH`` (nothing to resolve from either trusted source);
    - ``binary_unsafe_path`` — the trusted PATH (or a path-shaped explicit value)
      carries an empty / relative, cwd-dependent component (review j#74773);
    - ``binary_ambiguous`` — more than one *distinct* real executable resolved from
      the trusted PATH (Redmine #13496 review F2: the resolver never guesses).
    """
    raw = source_env.get(HERDR_BINARY_ENV)
    binary = raw.strip() if isinstance(raw, str) else ""
    if binary:
        resolved = _resolve_binary_verbose(binary, source_env)
        if resolved is None:
            raise TerminalTransportError(
                f"herdr binary {binary!r} (from {HERDR_BINARY_ENV}) was not found as "
                f"an absolute executable file or on the trusted environment PATH; "
                f"refusing to fall back to tmux",
                reason=REASON_BINARY_NOT_FOUND,
            )
        path, realpath = resolved
        return HerdrBinaryResolution(
            path=path, realpath=realpath, source=BINARY_SOURCE_ENV
        )
    # Step 2 (Redmine #13496 / bug #13500): no explicit trusted value — fall back to
    # an executable ``herdr`` on the *trusted* PATH. The trusted env's ``PATH`` is the
    # authority (an entry present only on the ambient PATH is never picked up); an env
    # with no ``PATH`` key (or only empty / relative components) has no absolute search
    # dir, so ``env={}`` still fails closed as ``binary_unconfigured``.
    resolved = _search_trusted_path(HERDR_PATH_NAME, source_env)
    if resolved is None:
        raise TerminalTransportError(
            f"terminal transport backend 'herdr' is selected but no herdr binary is "
            f"configured in the trusted environment ({HERDR_BINARY_ENV}) and no "
            f"executable {HERDR_PATH_NAME!r} was found on the trusted PATH; refusing "
            f"to fall back to tmux",
            reason=REASON_BINARY_UNCONFIGURED,
        )
    path, realpath = resolved
    return HerdrBinaryResolution(
        path=path, realpath=realpath, source=BINARY_SOURCE_PATH
    )


def _verify_executable(candidate: str) -> Optional[tuple[str, str]]:
    """Return ``(abspath, realpath)`` iff ``candidate``'s real target is executable.

    The absolute path (:func:`os.path.abspath`, symlink-preserving) is what gets
    injected into a launch agent (#13496 — never a cwd-relative token); the
    ``realpath`` (symlinks resolved) is what the executable bit is verified
    against, so a dangling or non-executable symlink fails closed. ``None`` when
    the real target is not an existing regular file with the executable bit.
    """
    real = os.path.realpath(candidate)
    if os.path.isfile(real) and os.access(real, os.X_OK):
        return os.path.abspath(candidate), real
    return None


def _trusted_path_dirs(source_env: Mapping[str, str]) -> list[str]:
    """The trusted ``PATH``'s components, fail-closed on any unsafe one.

    A supplied env with no ``PATH`` key (or an empty ``PATH`` string) has no search
    directory and yields ``[]`` — a bare name simply does not resolve (it never
    falls back to the ambient process ``PATH``). Otherwise **every** component must
    be a non-empty absolute directory: if *any* component is empty or relative (a
    shell would resolve it against the cwd) the whole trusted PATH is rejected with
    ``binary_unsafe_path`` (Redmine #13496 review j#74773). The resolver does not
    silently drop the unsafe component and search the rest — that would quietly
    rewrite the caller's PATH semantics and let an unsafe trusted environment
    resolve. Absolute components are returned order-preserving and de-duplicated.
    """
    raw = source_env.get("PATH", "")
    if not isinstance(raw, str) or raw == "":
        return []
    components = raw.split(os.pathsep)
    unsafe = [comp for comp in components if comp == "" or not os.path.isabs(comp)]
    if unsafe:
        raise TerminalTransportError(
            f"trusted PATH contains {len(unsafe)} unsafe (empty or relative) "
            f"component(s) {unsafe!r} that a shell would resolve against the current "
            f"working directory; refusing to resolve herdr from an unsafe PATH (and "
            f"refusing to fall back to tmux)",
            reason=REASON_BINARY_UNSAFE_PATH,
        )
    dirs: list[str] = []
    for comp in components:
        if comp not in dirs:
            dirs.append(comp)
    return dirs


def _search_trusted_path(
    name: str, source_env: Mapping[str, str]
) -> Optional[tuple[str, str]]:
    """Find ``name`` across the trusted PATH's absolute components (fail-closed).

    Enumerates every ``PATH`` component (:func:`_trusted_path_dirs`, which raises
    ``binary_unsafe_path`` if any component is empty / relative), verifies each
    candidate against its realpath + executable bit, and de-duplicates by realpath.
    Returns ``(abspath, realpath)`` for a unique match, ``None`` when nothing
    matches, and raises ``binary_ambiguous`` when more than one **distinct** real
    executable is found (Redmine #13496 review F2) rather than silently taking the
    first. Duplicate PATH entries pointing at the SAME realpath (a symlink or a
    repeated dir) collapse to one and are not ambiguous.
    """
    matches: list[tuple[str, str]] = []  # (abspath, realpath), unique by realpath
    seen_real: set[str] = set()
    for directory in _trusted_path_dirs(source_env):
        verified = _verify_executable(os.path.join(directory, name))
        if verified is None:
            continue
        _abspath, real = verified
        if real in seen_real:
            continue
        seen_real.add(real)
        matches.append(verified)
    if not matches:
        return None
    if len(matches) > 1:
        raise TerminalTransportError(
            f"{len(matches)} distinct executable {name!r} binaries resolved from the "
            f"trusted PATH ({', '.join(real for _abs, real in matches)}); refusing to "
            f"guess which one to run",
            reason=REASON_BINARY_AMBIGUOUS,
        )
    return matches[0]


def _resolve_binary_verbose(
    binary: str, source_env: Mapping[str, str]
) -> Optional[tuple[str, str]]:
    """Resolve ``binary`` to ``(abspath, realpath)``, or ``None`` if unresolvable.

    A path-shaped value (containing a separator) must be an existing **absolute**
    executable file — a relative path-shaped value is cwd-dependent and fails
    closed with ``binary_unsafe_path`` (Redmine #13496 review j#74773). A bare name
    is resolved on the **trusted environment's** ``PATH``
    (:func:`_search_trusted_path`), not the ambient process ``PATH``, and may raise
    ``binary_unsafe_path`` (an unsafe PATH component) or ``binary_ambiguous`` (more
    than one distinct real executable on the trusted PATH).
    """
    if os.sep in binary or (os.altsep and os.altsep in binary):
        if not os.path.isabs(binary):
            raise TerminalTransportError(
                f"herdr binary {binary!r} (from {HERDR_BINARY_ENV}) is a relative "
                f"path that a shell would resolve against the current working "
                f"directory; the trusted herdr binary must be an absolute path; "
                f"refusing to fall back to tmux",
                reason=REASON_BINARY_UNSAFE_PATH,
            )
        return _verify_executable(binary)
    return _search_trusted_path(binary, source_env)


__all__ = (
    "COMMAND_TIMEOUT_SECONDS",
    "HERDR_BINARY_ENV",
    "HERDR_PATH_NAME",
    "HerdrCliTransport",
    "resolve_herdr_binary",
    "resolve_terminal_transport",
)
