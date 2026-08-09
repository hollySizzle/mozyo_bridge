"""Exact Unit identity for the read-only Unit-state tool (pure, Redmine #15162).

``unit-target-model.md`` defines a Unit as

    Unit = workspace + lane + project/governance context + role set

and gives ``UnitRecord`` its portable key, ``unit:<host>:<workspace_id>:<lane_id>``.
This module is that definition in code, plus the fail-closed resolution a tool call
must pass before any state is read.

Why resolution is a separate, typed step rather than "look up whatever was sent":
the caller of this tool is an LLM. A selector that half-matches must not quietly
resolve to *a* Unit — reading the wrong lane's state is worse than returning
nothing, because the answer looks authoritative. So every failure mode is a named
refusal:

``missing``
    A required identity component (workspace / lane / project-governance context)
    was absent or blank. The Unit was never named.
``unknown``
    The required triple names no Unit in the index.
``ambiguous``
    The selector still matches more than one Unit after every supplied component
    was applied. Narrowing is the caller's job; guessing is not ours.
``mismatch``
    The required triple matches, but a supplied optional component (host, repo
    label, ticket system) contradicts every candidate — the caller is describing a
    Unit that does not exist as described.
``foreign``
    The selector resolves to a Unit outside the scope this server is authorized to
    read. A neighbouring workspace's lane on the same host is not this server's to
    report on (the same scoping ``workflow glance`` fixed for its roster).

The distinction between ``unknown`` and ``mismatch`` matters to the caller: the
first says "no such Unit", the second says "that Unit exists but not with those
attributes", and only the second is fixed by dropping a narrowing field.

Pure: no registry read, no filesystem, no tmux. The index is supplied by the
application layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Optional, Sequence

#: The lane id a Unit carries when it is the workspace's main lane. Mirrors the
#: read model's ``DEFAULT_LANE`` (pinned against it by test, rather than imported,
#: so the cockpit read model stays free of a dependency on this Feature).
DEFAULT_LANE = "default"

#: The host id assumed by ``UnitRecord`` when a record carries none.
DEFAULT_HOST = "local"

# --- refusal vocabulary (closed) ------------------------------------------- #

SELECTOR_MISSING = "missing"
SELECTOR_UNKNOWN = "unknown"
SELECTOR_AMBIGUOUS = "ambiguous"
SELECTOR_MISMATCH = "mismatch"
SELECTOR_FOREIGN = "foreign"

SELECTOR_REFUSALS = frozenset(
    {
        SELECTOR_MISSING,
        SELECTOR_UNKNOWN,
        SELECTOR_AMBIGUOUS,
        SELECTOR_MISMATCH,
        SELECTOR_FOREIGN,
    }
)

#: The identity components a selector MUST carry. ``project_id`` is the
#: project/governance context the Unit definition names — without it a
#: ``workspace + lane`` pair does not identify a Unit under a governance model
#: where one workspace can host lanes governed by different projects.
REQUIRED_SELECTOR_FIELDS = ("workspace_id", "lane_id", "project_id")

#: Components that only ever *narrow* a match. Supplying one cannot widen the
#: candidate set, and contradicting one is a ``mismatch``, never a silent drop.
NARROWING_SELECTOR_FIELDS = ("host_id", "repo_label", "ticket_system")


class UnitSelectorError(ValueError):
    """A selector could not be resolved to exactly one authorized Unit.

    Carries the closed ``reason`` token so the tool layer reports a typed refusal
    rather than a prose string the caller would have to parse.
    """

    def __init__(
        self,
        reason: str,
        message: str,
        *,
        candidates: Sequence[str] = (),
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.message = message
        #: Unit ids that matched, when reporting them helps the caller narrow.
        #: Deliberately only unit ids — never pane ids, paths, or credentials.
        self.candidates = tuple(candidates)

    def as_payload(self) -> dict:
        return {
            "error": "unit_selector",
            "reason": self.reason,
            "message": self.message,
            "candidates": list(self.candidates),
        }


@dataclass(frozen=True)
class UnitRecord:
    """One Unit, in the ``unit-target-model.md`` ``UnitRecord`` shape.

    Identity is ``host_id`` + ``workspace_id`` + ``lane_id`` + the
    project/governance context. ``repo_label`` / ``branch`` are join facts.
    ``roles`` is the Unit's role set — present because the Unit definition names
    it, and because a caller asking "which roles exist on this Unit" must not have
    to reach a Target (a live pane) to find out.

    Deliberately carries **no** Target: no pane id, no host path, no session name.
    A Unit is not a delivery endpoint, and this Feature's tools are read-only, so
    exposing a routable endpoint here would hand a side effect's address to a
    surface that has no authority to use it.
    """

    workspace_id: str
    lane_id: str = DEFAULT_LANE
    host_id: str = DEFAULT_HOST
    project_id: str = ""
    repo_label: Optional[str] = None
    branch: Optional[str] = None
    ticket_system: Optional[str] = None
    roles: tuple[str, ...] = ()

    def unit_id(self) -> str:
        """The portable Unit key.

        Byte-identical to the cockpit read model's ``UnitRow.unit_id()`` for a
        tmux-backed Unit, so the same Unit has one id across surfaces.
        """
        return f"unit:{self.host_id}:{self.workspace_id}:{self.lane_id}"

    def as_payload(self) -> dict:
        return {
            "unit_id": self.unit_id(),
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "host_id": self.host_id,
            "project_id": self.project_id,
            "repo_label": self.repo_label,
            "branch": self.branch,
            "roles": list(self.roles),
            "governance": {"ticket_system": self.ticket_system},
        }


@dataclass(frozen=True)
class UnitSelector:
    """The caller-supplied Unit identity."""

    workspace_id: str
    lane_id: str
    project_id: str
    host_id: Optional[str] = None
    repo_label: Optional[str] = None
    ticket_system: Optional[str] = None

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "project_id": self.project_id,
            "host_id": self.host_id,
            "repo_label": self.repo_label,
            "ticket_system": self.ticket_system,
        }


def _clean(value: object) -> str:
    """A trimmed string, or ``""`` for anything that is not usable text."""
    if value is None or isinstance(value, bool):
        return ""
    if not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def parse_unit_selector(arguments: Mapping[str, object]) -> UnitSelector:
    """Build a :class:`UnitSelector` from tool arguments, fail-closed.

    Raises :class:`UnitSelectorError` with ``reason=missing`` naming **every**
    absent required component at once, so a caller fixes the call in one round
    rather than discovering the fields one refusal at a time.
    """
    raw = arguments.get("unit")
    if not isinstance(raw, Mapping):
        raise UnitSelectorError(
            SELECTOR_MISSING,
            'the "unit" argument must be an object naming the Unit '
            f"({', '.join(REQUIRED_SELECTOR_FIELDS)})",
        )
    values = {name: _clean(raw.get(name)) for name in REQUIRED_SELECTOR_FIELDS}
    absent = [name for name, value in values.items() if not value]
    if absent:
        raise UnitSelectorError(
            SELECTOR_MISSING,
            "the Unit is not fully identified; missing: " + ", ".join(absent),
        )
    narrowing = {name: _clean(raw.get(name)) or None for name in NARROWING_SELECTOR_FIELDS}
    return UnitSelector(
        workspace_id=values["workspace_id"],
        lane_id=values["lane_id"],
        project_id=values["project_id"],
        host_id=narrowing["host_id"],
        repo_label=narrowing["repo_label"],
        ticket_system=narrowing["ticket_system"],
    )


def _matches_required(record: UnitRecord, selector: UnitSelector) -> bool:
    return (
        record.workspace_id == selector.workspace_id
        and record.lane_id == selector.lane_id
        and record.project_id == selector.project_id
    )


def _matches_narrowing(record: UnitRecord, selector: UnitSelector) -> bool:
    for supplied, observed in (
        (selector.host_id, record.host_id),
        (selector.repo_label, record.repo_label),
        (selector.ticket_system, record.ticket_system),
    ):
        if supplied is not None and supplied != observed:
            return False
    return True


def resolve_unit(
    selector: UnitSelector,
    index: Iterable[UnitRecord],
    *,
    authorized_workspace_ids: Optional[Iterable[str]] = None,
) -> UnitRecord:
    """Resolve ``selector`` to exactly one authorized :class:`UnitRecord`.

    ``authorized_workspace_ids`` is the scope this server may report on. ``None``
    means "no scope restriction was resolved" — which is **not** treated as
    "everything is allowed": the caller supplies the resolved scope, and an
    unresolvable scope is the application layer's decision to refuse, not this
    function's to wave through. Passing an empty iterable refuses everything.

    The authorization check runs on the *resolved* record rather than the
    selector, so a Unit cannot be reached by naming a workspace id that differs in
    spelling from the record's own.
    """
    candidates = [record for record in index if _matches_required(record, selector)]
    if not candidates:
        raise UnitSelectorError(
            SELECTOR_UNKNOWN,
            f"no Unit matches workspace {selector.workspace_id!r} / "
            f"lane {selector.lane_id!r} / project {selector.project_id!r}",
        )
    narrowed = [record for record in candidates if _matches_narrowing(record, selector)]
    if not narrowed:
        raise UnitSelectorError(
            SELECTOR_MISMATCH,
            "the Unit exists but not with the supplied "
            f"{'/'.join(NARROWING_SELECTOR_FIELDS)} values",
            candidates=[record.unit_id() for record in candidates],
        )
    if len(narrowed) > 1:
        raise UnitSelectorError(
            SELECTOR_AMBIGUOUS,
            f"{len(narrowed)} Units match; narrow the selector with "
            f"{' / '.join(NARROWING_SELECTOR_FIELDS)}",
            candidates=[record.unit_id() for record in narrowed],
        )
    resolved = narrowed[0]
    allowed = None if authorized_workspace_ids is None else set(authorized_workspace_ids)
    if allowed is None or resolved.workspace_id not in allowed:
        raise UnitSelectorError(
            SELECTOR_FOREIGN,
            f"Unit {resolved.unit_id()} is outside this server's authorized scope",
        )
    return resolved


__all__ = (
    "DEFAULT_HOST",
    "DEFAULT_LANE",
    "NARROWING_SELECTOR_FIELDS",
    "REQUIRED_SELECTOR_FIELDS",
    "SELECTOR_AMBIGUOUS",
    "SELECTOR_FOREIGN",
    "SELECTOR_MISMATCH",
    "SELECTOR_MISSING",
    "SELECTOR_REFUSALS",
    "SELECTOR_UNKNOWN",
    "UnitRecord",
    "UnitSelector",
    "UnitSelectorError",
    "parse_unit_selector",
    "resolve_unit",
)
