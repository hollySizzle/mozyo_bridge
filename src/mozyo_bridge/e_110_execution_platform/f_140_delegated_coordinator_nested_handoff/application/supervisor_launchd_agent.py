"""Owned launchd identity + the pure plist text layer for the callback supervisor (Redmine #15192).

Split out of :mod:`...application.supervisor_launchd` so neither side exceeds the module-health
line budget, mirroring the split the Linux adapter already carries
(:mod:`...application.supervisor_systemd_unit`, review j#102069 F7). The division is the same one:
everything here is **pure** — owned identity, path resolution, argv resolution, plist rendering and
read-back, the plist-ownership classification every destructive verb shares, the launchctl argv
builders, and the fixed vocabularies those produce. Nothing in this module runs a process, touches a
credential, or mutates the host; the lifecycle verbs that do live in the sibling modules.

``launchctl`` appears here only as *argv construction*: :func:`launchctl` hands the command to an
injected runner and starts nothing itself. Keeping it at this level is what lets the lifecycle verbs
and the retired-drain migration compose the same command without importing each other.

Every name is re-exported from ``supervisor_launchd``, so that module remains the single import for
the whole macOS adapter and no caller or test had to change.
"""

from __future__ import annotations

import dataclasses
import os
import plistlib
import shutil
from pathlib import Path
from typing import Callable, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workspace_supervisor import (  # noqa: E501
    DEFAULT_OS_TICK_INTERVAL_SECONDS,
    DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL,
    DEFAULT_SUPERVISOR_SERVICE_LABEL,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

# ---------------------------------------------------------------------------
# Owned identity (a reverse-DNS label + owned plist/log paths; not operator-private).
#
# Two DISTINCT roots must never be conflated (review j#79092 R2-F1):
#   - the **OS user home** (``Path.home()``) owns the plist + log under ``~/Library`` — this is
#     where launchd looks for LaunchAgents, independent of any mozyo config;
#   - the **mozyo home** (``mozyo_bridge_home()``: ``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``)
#     owns the registry / store / credential root the supervisor reads at run time.
# ---------------------------------------------------------------------------

SUPERVISOR_LAUNCHD_LABEL = DEFAULT_SUPERVISOR_SERVICE_LABEL
PLIST_RELATIVE = Path("Library/LaunchAgents") / f"{SUPERVISOR_LAUNCHD_LABEL}.plist"
LOG_RELATIVE = Path("Library/Logs/mozyo-bridge/callback-supervisor.log")

#: The executable name resolved from PATH at install time (never a shell string).
SUPERVISOR_EXECUTABLE_NAME = "mozyo-bridge"
#: The structured argv tail the scheduled agent runs each tick (one bounded sweep, then exit). The
#: resolved mozyo home is pinned onto this as ``--home <root>`` at install time (see
#: :func:`resolve_supervisor_command`) so the launchd daemon reads the *same* credential / registry
#: root the install preflight validated — launchd carries no ``MOZYO_BRIDGE_HOME`` (j#79092 R2-F1).
SUPERVISOR_ARGV_TAIL = ("workflow", "supervisor", "--run-once")
#: The structured flag that pins the mozyo home root onto the daemon argv (non-secret; a config
#: directory, resolved by the supervisor CLI's ``--home``).
SUPERVISOR_HOME_FLAG = "--home"

# ---------------------------------------------------------------------------
# Owned agent (Redmine #15192): exactly ONE. The retired ``--drain-only`` agent's identity is kept
# below purely so an upgrade can RECOGNIZE and remove what a pre-#15192 install left behind — it is a
# migration target, never something a verb installs.
# ---------------------------------------------------------------------------

#: The retired local-drain agent's owned identity (#14150, retired by #15192). Kept so
#: :func:`classify_legacy_drain` can tell "our old registration" from "a stranger's plist that
#: happens to sit at this path" — the removal fence needs both the path and the label.
SUPERVISOR_DRAIN_LAUNCHD_LABEL = DEFAULT_SUPERVISOR_DRAIN_SERVICE_LABEL
DRAIN_PLIST_RELATIVE = Path("Library/LaunchAgents") / f"{SUPERVISOR_DRAIN_LAUNCHD_LABEL}.plist"
DRAIN_LOG_RELATIVE = Path("Library/Logs/mozyo-bridge/callback-supervisor-drain.log")
SUPERVISOR_DRAIN_ARGV_TAIL = ("workflow", "supervisor", "--drain-only")


@dataclasses.dataclass(frozen=True)
class SupervisorAgent:
    """One owned launchd agent's identity (label + plist/log paths + the bounded argv tail it runs)."""

    label: str
    argv_tail: tuple[str, ...]
    plist_relative: Path
    log_relative: Path
    default_interval_seconds: int


#: The single owned agent: one bounded ``workflow supervisor --run-once`` per tick, at the shared
#: portable OS cadence both host adapters register at (#15192).
SUPERVISOR_AGENT = SupervisorAgent(
    label=SUPERVISOR_LAUNCHD_LABEL,
    argv_tail=SUPERVISOR_ARGV_TAIL,
    plist_relative=PLIST_RELATIVE,
    log_relative=LOG_RELATIVE,
    default_interval_seconds=DEFAULT_OS_TICK_INTERVAL_SECONDS,
)
#: The owned agents an install/uninstall/status sweep manages. Exactly one since #15192; the tuple
#: shape is kept because the CLI renders an ``agents`` roster on every backend.
SUPERVISOR_AGENTS = (SUPERVISOR_AGENT,)

#: The retired drain agent, as a migration target only. Deliberately NOT in
#: :data:`SUPERVISOR_AGENTS`: no verb installs, restarts, or reports it as owned — ``install`` and
#: ``uninstall`` only *remove* it.
LEGACY_DRAIN_AGENT = SupervisorAgent(
    label=SUPERVISOR_DRAIN_LAUNCHD_LABEL,
    argv_tail=SUPERVISOR_DRAIN_ARGV_TAIL,
    plist_relative=DRAIN_PLIST_RELATIVE,
    log_relative=DRAIN_LOG_RELATIVE,
    default_interval_seconds=DEFAULT_OS_TICK_INTERVAL_SECONDS,
)

#: ``home_pin`` extraction status vocabulary (see :func:`_extract_pinned_home`).
HOME_PIN_OK = "ok"
HOME_PIN_MISSING = "missing"
HOME_PIN_DUPLICATE = "duplicate"
HOME_PIN_MALFORMED = "malformed"
#: The pin value is present but not an absolute, lexically-canonical path (relative / ``~`` / has
#: ``..`` etc.) — a launchd daemon resolves it from a different cwd than the installer (j#79136 R4-F1).
HOME_PIN_NOT_ABSOLUTE = "not_absolute"
HOME_PIN_NO_ARGV = "no_argv"
#: The owned plist file exists but could not be parsed / is not a mapping (distinct from absence,
#: which is ``not_installed``) — j#79136 R4-F3.
HOME_PIN_UNREADABLE = "unreadable_plist"
HOME_PIN_NOT_INSTALLED = "not_installed"


# ---------------------------------------------------------------------------
# Path + command + plist rendering (pure; no host mutation, no secrets).
# ---------------------------------------------------------------------------


def plist_path(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> Path:
    """The owned plist path under the **OS user home** (``~/Library/LaunchAgents``)."""
    return (os_home or Path.home()) / agent.plist_relative


def log_path(os_home: Optional[Path] = None, *, agent: SupervisorAgent = SUPERVISOR_AGENT) -> Path:
    """The owned log path under the **OS user home** (``~/Library/Logs``)."""
    return (os_home or Path.home()) / agent.log_relative


def resolve_mozyo_home(mozyo_home: Optional[Path] = None) -> Path:
    """Resolve the exact **mozyo home** root (credential / registry / store) as an absolute path.

    ``mozyo_home`` (the supervisor CLI's ``--home``) wins; otherwise the package's home contract
    (:func:`mozyo_bridge_home`: ``MOZYO_BRIDGE_HOME`` or ``~/.mozyo_bridge``). An explicit value is
    ``expanduser().resolve()``-normalized to an **absolute canonical root** — a relative / ``~``
    input must never be pinned onto the daemon argv, since a LaunchAgent's working directory is not
    the installer shell's, so a relative pin would re-diverge the credential / registry root
    (j#79125 R3-F2). ``mozyo_bridge_home()`` already returns an absolute resolved path.
    """
    if mozyo_home is not None:
        return Path(mozyo_home).expanduser().resolve()
    return mozyo_bridge_home()


def resolve_supervisor_command(
    *,
    mozyo_home: Optional[Path] = None,
    which: Callable[[str], Optional[str]] = shutil.which,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> Optional[list[str]]:
    """The exact argv the agent runs, or ``None`` when the executable is not on PATH.

    The executable is PATH-resolved at install time (so the plist survives shell-env differences)
    and normalized to an **absolute canonical path** (``os.path.abspath``): a relative PATH entry
    makes ``shutil.which`` return a relative path, which a LaunchAgent would resolve from its own
    working directory rather than the installer's — the same cwd divergence closed for the ``--home``
    pin (j#79149 R5-F1). The **resolved mozyo home** is likewise pinned as ``--home <root>`` so the
    daemon reads the credential / registry root the preflight validated (j#79092 R2-F1). A missing
    executable is a fail-closed condition the caller turns into a zero-mutation refusal (install the
    package first) — never a shell string and never a guessed path.
    """
    executable = which(SUPERVISOR_EXECUTABLE_NAME)
    if not executable:
        return None
    return [
        os.path.abspath(executable),
        *agent.argv_tail,
        SUPERVISOR_HOME_FLAG,
        str(resolve_mozyo_home(mozyo_home)),
    ]


def render_plist(
    command: Sequence[str],
    *,
    interval_seconds: int,
    os_home: Optional[Path] = None,
    agent: SupervisorAgent = SUPERVISOR_AGENT,
) -> bytes:
    """Render the LaunchAgent plist for the one-shot scheduled supervisor sweep.

    Structurally minimal and secret-free:

    - **No** ``EnvironmentVariables`` key exists in the output, so no secret can be serialized in.
    - **No** ``KeepAlive`` key: the command is a bounded ``--run-once`` sweep that exits;
      ``RunAtLoad`` runs it once at load and ``StartInterval`` re-runs it every ``interval_seconds``.
      KeepAlive would be a tight restart loop for a one-shot command, so it is absent by design.
    - ``ProgramArguments`` is the exact structured argv (PATH-resolved executable + fixed tail +
      the pinned ``--home <mozyo root>``). The log lives under the OS user home (``os_home``).
    """
    payload = {
        "Label": agent.label,
        "ProgramArguments": list(command),
        "RunAtLoad": True,
        "StartInterval": max(1, int(interval_seconds)),
        "StandardOutPath": str(log_path(os_home, agent=agent)),
        "StandardErrorPath": str(log_path(os_home, agent=agent)),
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload)




def read_installed_plist(target: Path) -> Optional[dict]:
    """Best-effort parse of the installed plist; ``None`` if unreadable/malformed (never raises)."""
    try:
        raw = target.read_bytes()
        parsed = plistlib.loads(raw)
    except (OSError, ValueError, plistlib.InvalidFileException):
        return None
    return parsed if isinstance(parsed, dict) else None


#: The manager binary. Named here, with the argv builders, so the retired-drain migration and the
#: lifecycle verbs compose the same command without importing each other.
LAUNCHCTL = "launchctl"


def gui_domain() -> str:
    """The per-user launchd domain a LaunchAgent lives in."""
    return f"gui/{os.getuid()}"


def service_target(agent: SupervisorAgent = SUPERVISOR_AGENT) -> str:
    """``<domain>/<label>`` — how launchctl names one service. The label is the identity."""
    return f"{gui_domain()}/{agent.label}"


def launchctl(runner, args: Sequence[str]):
    """Build the launchctl argv and hand it to the injected ``runner``. Runs no process itself."""
    return runner([LAUNCHCTL, *args])


#: Identity of whatever currently occupies an agent's plist path. Every destructive verb classifies
#: before it writes or unlinks, and only :data:`PLIST_OWNED` may be mutated.
PLIST_ABSENT = "absent"  # nothing there: a clean host, or one already torn down
PLIST_OWNED = "owned"  # parses, and ``Label`` is exactly this agent's label
PLIST_FOREIGN = "foreign"  # parses, but the ``Label`` belongs to someone else
PLIST_UNREADABLE = "unreadable"  # present but unparseable / non-mapping / no ``Label`` string


def classify_plist(target: Path, *, label: str) -> str:
    """Classify who owns the file at ``target`` — the single identity test every verb shares.

    Returns one of :data:`PLIST_ABSENT` / :data:`PLIST_OWNED` / :data:`PLIST_FOREIGN` /
    :data:`PLIST_UNREADABLE`. Identity is read from the plist's own ``Label``, never inferred from
    the filename: a path is a *location*, and a location says nothing about who wrote what is there.

    This was previously implemented only for the retired drain agent, so the adapter could refuse to
    delete a stranger's retired plist while its *current* agent's install overwrote and its uninstall
    deleted whatever happened to occupy the path (review j#102496 r12f2). One classifier, applied by
    every verb, is what makes "we mutate exactly our own artifacts" a property of the code rather
    than a claim in a docstring.

    ``UNREADABLE`` is deliberately distinct from ``FOREIGN``: "this is someone else's" and "I cannot
    tell whose this is" are different facts, and neither one authorizes a mutation.
    """
    if not target.exists():
        return PLIST_ABSENT
    parsed = read_installed_plist(target)
    if parsed is None:
        return PLIST_UNREADABLE
    found = parsed.get("Label")
    if not isinstance(found, str) or not found:
        return PLIST_UNREADABLE
    return PLIST_OWNED if found == label else PLIST_FOREIGN


def extract_pinned_home(installed_argv: object) -> tuple[Optional[str], str]:
    """Extract the ``--home`` pin from an installed plist's ``ProgramArguments`` (strict).

    Returns ``(pinned_home, status)``. The installed plist — not the caller's current shell — is the
    authority on the daemon's mozyo home, so restart / status read the pin from here (j#79125 R3-F1).
    A missing / duplicated / value-less pin is *not* trusted (the daemon-effective root is unknowable),
    and a pin that is not an **absolute, lexically-canonical** path (relative / ``~`` / containing
    ``..``) is rejected too: a LaunchAgent resolves such a pin from a different working directory than
    the installer, re-opening the R3-F2 divergence in the installed service (j#79136 R4-F1). Every
    non-``ok`` case is surfaced (fail-closed for restart, unhealthy for status), never guessed.
    """
    if not isinstance(installed_argv, list):
        return None, HOME_PIN_NO_ARGV
    indices = [i for i, arg in enumerate(installed_argv) if arg == SUPERVISOR_HOME_FLAG]
    if not indices:
        return None, HOME_PIN_MISSING
    if len(indices) > 1:
        return None, HOME_PIN_DUPLICATE
    value_index = indices[0] + 1
    if value_index >= len(installed_argv):
        return None, HOME_PIN_MALFORMED
    value = installed_argv[value_index]
    if not isinstance(value, str) or not value.strip() or value.startswith("--"):
        return None, HOME_PIN_MALFORMED
    # An install always pins ``str(resolve_mozyo_home(...))`` — absolute + canonical. Anything else
    # (relative, ``~``, ``/a/../b``) would be resolved from launchd's cwd, not the installer's.
    if not os.path.isabs(value) or value != os.path.normpath(value):
        return None, HOME_PIN_NOT_ABSOLUTE
    return value, HOME_PIN_OK




# ---------------------------------------------------------------------------
# launchctl message text (pure): what the manager's wording SAYS, kept apart from running it.
#
# These parse an error string and nothing else — no subprocess, no host, no credential. They live
# here because the question "does this sentence report OUR service as missing?" is a text question,
# and because the lifecycle module has a line budget (review j#102069 F7 established the split).
# ---------------------------------------------------------------------------

#: ``launchctl print`` exit codes seen for an unknown label. **Necessary, never sufficient** (review
#: j#102200 finding r3f1): launchctl's man page documents only "0 on success, non-zero on failure"
#: and does not make 113 a stable not-found contract, so this corroborates label-bound evidence and
#: cannot authorize a removal by itself.
LAUNCHCTL_NOT_FOUND_CODES = (113,)
#: Lowercased fragments of launchctl's "unknown service" message. Also necessary-but-not-sufficient:
#: the man page states ``print`` output is not an API and may change.
LAUNCHCTL_NOT_FOUND_PHRASES = ("could not find service", "no such process", "not find service")
#: Lowercased fragments meaning "this read failed for a reason OTHER than absence". Their presence
#: disqualifies a not-found reading outright — a permission error names why we could not look, which
#: is the opposite of evidence that there is nothing to look at.
LAUNCHCTL_UNREADABLE_PHRASES = (
    "not permitted",
    "permission denied",
    "not privileged",
    "denied",
    "unauthori",  # unauthorised / unauthorized
    "could not connect",
    "connection invalid",
)



def quoted_spans(message: str) -> Optional[list[tuple[int, int]]]:
    """Every complete quoted span as ``(open_index, close_index)``, or ``None`` if undecidable.

    Positions, not just contents (review j#102398 finding r9f1). The earlier parser recovered the
    span *strings* and then searched for a quote character separately, which meant it could not tell
    an opening quote from a closing one — ``diagnostic "could not find service"<owned>"x"`` bound our
    label off a quote that was *closing* someone else's span.

    Same refusal rules as :func:`quoted_names`: any backslash, an unclosed span, or two adjacent
    spans mean the quoting grammar is not the one this scanner reads, and it declines to answer.
    """
    if "\\" in message:
        return None
    opens = [i for i, ch in enumerate(message) if ch == '"']
    if len(opens) % 2:
        return None
    spans = list(zip(opens[0::2], opens[1::2]))
    for previous, following in zip(spans, spans[1:]):
        if previous[1] + 1 == following[0]:  # `""` — the signature of a doubling escape
            return None
    return spans


#: Whitespace that can separate words *within* one line. Deliberately excludes every line break:
#: `\n`, `\r`, and the vertical/form feeds `str.isspace()` also accepts.
_INTRA_LINE_SPACE = frozenset(" \t")


def not_found_operand(message: str) -> Optional[str]:
    """The service a recognized "no such service" clause is ABOUT, or ``None`` when undecidable.

    One position-aware pass, because the binding *is* a position claim (review j#102398 r9f1). The
    previous version searched for the phrase and for a quote independently and called the pair a
    clause; three different messages satisfied it while saying something else entirely:

    - ``Could not find service com.example.other; suggestion "<owned>"`` — the clause's real operand
      is unquoted, and ours is a later, unrelated span;
    - ``diagnostic "could not find service"<owned>"x" "<owned>"`` — the phrase sits *inside* a quoted
      span, so the "next quote" was a closing delimiter;
    - ``no such processnot find service "<owned>"`` — two abutting phrases merged into one clause.

    Each authorized unlinking the owned registration. So the rules are now positional and all four
    must hold:

    1. the phrase occurs **outside** every quoted span (a phrase inside a span is data, not wording);
    2. exactly **one** clause survives merging, and merging joins only genuinely *overlapping* hits —
       abutting hits are separate clauses, which is ambiguity, not one clause;
    3. the operand span **starts immediately after** the clause, separated by nothing but spaces or
       tabs — never a line break;
    4. the operand is a complete span this scanner itself delimited.

    Offsets are computed on the original ``message``. Case-folding for the phrase comparison is done
    per-slice, never by lowercasing the whole string and indexing the result: ``len("İ") == 1`` while
    ``len("İ".lower()) == 2``, so a folded copy is not positionally aligned with the original.
    """
    spans = quoted_spans(message)
    if spans is None:
        return None
    clauses = _not_found_clauses(message, spans)
    if len(clauses) != 1:
        return None
    _, clause_end = clauses[0]
    # Only INTRA-LINE whitespace may separate the clause from its operand. A newline is not a wide
    # space: it ends a line, and one stream is not one record. Treating `\n` as ordinary spacing
    # bound a phrase on one line to a label on another (review j#102438 finding r11f2) — the same
    # false adjacency that cross-stream concatenation produced, one level in. Any other prose
    # between them means the sentence said something this parser is not entitled to guess at.
    cursor = clause_end
    while cursor < len(message) and message[cursor] in _INTRA_LINE_SPACE:
        cursor += 1
    for open_index, close_index in spans:
        if open_index == cursor:
            return message[open_index + 1:close_index]
    return None


def has_not_found_clause(message: str) -> bool:
    """Whether ``message`` carries recognized not-found wording outside every quoted span (pure).

    Used to tell "this stream said nothing about absence" (normal) from "this stream tried to say
    something and could not be resolved" (ambiguity). Only the first may be passed over silently.
    """
    spans = quoted_spans(message)
    if spans is None:
        # The quoting could not be read, so whether a clause is *well-formed* is unknowable — but
        # the question this answers is whether the stream TRIED to say something about absence, and
        # recognized wording is that attempt. Returning False here made an unparseable stream
        # indistinguishable from one that said nothing, which let the other stream's positive
        # reading carry a deletion (review j#102438 finding r11f1).
        wording = message.lower()
        return any(phrase in wording for phrase in LAUNCHCTL_NOT_FOUND_PHRASES)
    return bool(_not_found_clauses(message, spans))


def _not_found_clauses(
    message: str, spans: list[tuple[int, int]]
) -> list[tuple[int, int]]:
    """Merged ``(start, end)`` ranges of recognized not-found wording OUTSIDE any quoted span.

    Merging joins hits that genuinely overlap — the phrases are deliberately nested, so
    ``"could not find service"`` and ``"not find service"`` describe one clause. Hits that merely
    touch are NOT merged: ``no such process`` immediately followed by ``not find service`` is two
    clauses, and treating them as one let an ambiguous message read as a single confident statement.
    """
    inside = [range(open_index, close_index + 1) for open_index, close_index in spans]

    def in_span(index: int) -> bool:
        return any(index in window for window in inside)

    hits: list[tuple[int, int]] = []
    for phrase in LAUNCHCTL_NOT_FOUND_PHRASES:
        needle = phrase.lower()
        start = 0
        while True:
            index = _find_folded(message, needle, start)
            if index < 0:
                break
            if not in_span(index):
                hits.append((index, index + len(phrase)))
            start = index + 1
    merged: list[tuple[int, int]] = []
    for begin, finish in sorted(hits):
        if merged and begin < merged[-1][1]:  # strictly overlapping, not merely abutting
            merged[-1] = (merged[-1][0], max(merged[-1][1], finish))
        else:
            merged.append((begin, finish))
    return merged


def _find_folded(message: str, needle_lower: str, start: int) -> int:
    """Index of ``needle_lower`` in ``message`` compared case-insensitively, in ORIGINAL offsets.

    Each candidate slice is folded on its own so the returned index always refers to ``message``.
    Lowercasing the whole string first and searching that would return offsets into a *different*
    string whenever folding changes length (``"İ".lower()`` is two code points), and those offsets
    were then used to slice the original.
    """
    width = len(needle_lower)
    for index in range(start, len(message) - width + 1):
        if message[index:index + width].lower() == needle_lower:
            return index
    return -1


def quoted_names(message: str) -> Optional[list[str]]:
    """Every complete double-quoted span in ``message``, or ``None`` when the quoting is undecidable.

    launchctl's error wording is explicitly not an API, so the quoting *grammar* it uses is unknown:
    we have never seen how it renders a label that itself contains a quote. This scanner therefore
    recognizes exactly one grammar — spans delimited by plain ``"`` with nothing escaped — and
    refuses to answer whenever the text shows a sign that some OTHER grammar is in play:

    - **any backslash**, which would mean an escape convention this scanner cannot read;
    - an **odd number of quotes**, i.e. a span that never closes;
    - **two adjacent spans** (an empty run between them), the signature of ``""``-style escaping.

    ``None`` is not "no match": it means the message cannot be parsed, which :func:`_says_not_found`
    turns into :data:`PROBE_UNREADABLE`, never into a confirmed absence.
    """
    if "\\" in message:
        return None
    parts = message.split('"')
    if len(parts) % 2 == 0:  # one quote per span boundary, so a balanced message splits into odd
        return None
    if any(parts[i] == "" for i in range(2, len(parts) - 1, 2)):
        return None
    return parts[1::2]


def names_exactly(message: str, token: str) -> bool:
    """Whether ``message`` names ``token`` as launchd's own **quoted** service name.

    Only one form is accepted: the token as a complete quoted span, compared in full. That is how
    launchctl renders the name it could not find, and a delimited span is what makes the boundary
    *observed* rather than *assumed*.

    The comparison is over the **exact decoded string** — the ``str`` the runner hands back from
    ``subprocess.run(..., text=True)``, character for character. (Earlier revisions of this docstring
    said "bytes"; nothing in this path ever sees bytes, and describing a check in terms it does not
    use is its own defect — review j#102378 finding r7f1.) In particular the message is never
    case-folded before it gets here. Comparing folded strings made
    ``"ORG.MOZYO-BRIDGE.CALLBACK-SUPERVISOR.DRAIN"`` — a different string, and therefore a label this
    adapter never installed — satisfy the check for our own label and authorize unlinking our plist
    (review j#102327 finding r6f1). Apple documents ``Label`` only as a string that uniquely
    identifies a job; that it may be compared case-insensitively is nowhere stated, so it is an
    assumption, and an assumption is not something to hang a destructive migration on.

    The check must also *parse*, not merely find (review j#102378 finding r7f1). ``f'"{token}"' in
    message`` was a substring test wearing a parser's clothes: for a different label rendered as
    ``"prefix\\"<owned>"``, the opening quote of the "match" is the escaped quote that belongs to
    THAT label's data and the closing one is the outer delimiter, so the two quotes bounding the hit
    were never the two ends of one span — and the hit authorized unlinking our registration. Spans
    now come from :func:`quoted_names`, which refuses to answer at all when the quoting cannot be
    read unambiguously.

    Two earlier boundary rules failed the same way from further out. A character allowlist —
    alphanumerics plus ``.-_``, anything else a delimiter — was invented here, and Apple constrains
    ``Label``'s characters nowhere, so ``<owned>@helper`` / ``<owned>:helper`` / ``<owned>+helper`` /
    ``<owned>/helper`` all satisfied it (review j#102309 finding r5f1). Before that, a bare substring
    test made our label a prefix of every longer one (review j#102235 finding r4f1).

    Everything unrecognized — an unquoted mention, an unreadable quoting, a different label — yields
    no binding and therefore :data:`PROBE_UNREADABLE`. That is deliberately an over-refusal: until
    the real launchctl wording is captured on a live host (#15194), refusing is the only honest
    answer, and refusing costs a retry while a wrong match costs someone else's running service.
    """
    if not token or '"' in token or "\\" in token:
        return False
    names = quoted_names(message)
    return names is not None and token in names


__all__ = (
    "SUPERVISOR_LAUNCHD_LABEL",
    "PLIST_RELATIVE",
    "LOG_RELATIVE",
    "SUPERVISOR_EXECUTABLE_NAME",
    "SUPERVISOR_ARGV_TAIL",
    "SUPERVISOR_HOME_FLAG",
    "SUPERVISOR_DRAIN_LAUNCHD_LABEL",
    "DRAIN_PLIST_RELATIVE",
    "DRAIN_LOG_RELATIVE",
    "SUPERVISOR_DRAIN_ARGV_TAIL",
    "SupervisorAgent",
    "SUPERVISOR_AGENT",
    "SUPERVISOR_AGENTS",
    "LEGACY_DRAIN_AGENT",
    "HOME_PIN_OK",
    "HOME_PIN_MISSING",
    "HOME_PIN_DUPLICATE",
    "HOME_PIN_MALFORMED",
    "HOME_PIN_NOT_ABSOLUTE",
    "HOME_PIN_NO_ARGV",
    "HOME_PIN_UNREADABLE",
    "HOME_PIN_NOT_INSTALLED",
    "plist_path",
    "log_path",
    "resolve_mozyo_home",
    "resolve_supervisor_command",
    "render_plist",
    "extract_pinned_home",
    "read_installed_plist",
    "PLIST_ABSENT",
    "PLIST_OWNED",
    "PLIST_FOREIGN",
    "PLIST_UNREADABLE",
    "classify_plist",
    "LAUNCHCTL",
    "gui_domain",
    "service_target",
    "launchctl",
    "LAUNCHCTL_NOT_FOUND_CODES",
    "not_found_operand",
    "has_not_found_clause",
    "quoted_spans",
    "quoted_names",
    "names_exactly",
)
