"""Fail-closed preflight for Redmine Version metadata operations (Redmine #12651,
parent US #12643).

The team uses Redmine Versions as planning / execution / acceptance-window
buckets, **not** as the package release number (#12643). The MCP surface
available today (``list_versions`` / ``create_version`` / ``assign_to_version``)
exposes no rename / close / lock / delete, and this repo's Redmine adapter is
read-only-by-design (see ``f_120_redmine_adapter/__init__``). This module does
**not** add a destructive REST call. It is the *decision* half of a safe
wrapper: given a Version's already-read state and a requested operation, it
returns an allow/blocked :class:`VersionOperationDecision` with fail-closed
preflight + an explicit confirmation requirement, plus the concrete REST /
operator-UI step an out-of-band executor (operator UI now, or a future
double-opt-in live adapter behind :class:`RedmineVersionWrites`) must perform.

Fail-closed posture: an unknown operation, a missing/mismatched confirmation
token, a non-empty delete target, a rename to a package-numbered name, or a
close/lock that would orphan open issues all yield ``allowed=False`` with no
``rest_step`` — the executable step is emitted only when every guard passes.
Nothing here executes, mutates, or touches the network.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Protocol, runtime_checkable

#: The closed vocabulary of Version metadata operations this preflight governs.
#: Anything outside this set fails closed (``unknown_operation``).
VERSION_OPERATIONS: frozenset[str] = frozenset({"rename", "close", "lock", "delete"})

#: Version lifecycle states Redmine exposes. ``open`` accepts new issues; a
#: ``locked`` version keeps its issues but takes no new ones; ``closed`` is
#: retired. Unknown states are treated conservatively by the per-op guards.
VERSION_STATUSES: frozenset[str] = frozenset({"open", "locked", "closed"})

# A package release number prefix, e.g. ``v0.10.22`` / ``0.9.0`` / ``v1.2``.
# Redmine Version *names* must not re-encode this (#12643); a rename to such a
# name fails closed so release numbering stays a release-gate decision.
_PACKAGE_NUMBER_RE = re.compile(r"^v?\d+\.\d+(?:\.\d+)*", re.IGNORECASE)


class VersionOperationError(ValueError):
    """Raised when a Version operation request cannot be parsed/validated."""


def classify_version_name(name: str) -> str:
    """Return ``"package_numbered"`` if ``name`` opens with a release number,
    else ``"planning_bucket"``. Pure string policy; see ``_PACKAGE_NUMBER_RE``."""
    if _PACKAGE_NUMBER_RE.match((name or "").strip()):
        return "package_numbered"
    return "planning_bucket"


def confirmation_token_for(operation: str, version_id: str) -> str:
    """The exact confirmation token a caller must echo to authorize ``operation``
    on ``version_id``. Operation-and-target specific so a token cannot be reused
    across operations or versions."""
    return f"{operation}:{version_id}"


@dataclass(frozen=True)
class VersionState:
    """An already-read Redmine Version, as the preflight needs to see it.

    Mirrors the fields of a ``list_versions`` / ``GET /versions/<id>.json`` entry
    that matter for a safe operation decision. Counts are authoritative inputs;
    the preflight never re-fetches them.
    """

    version_id: str
    name: str
    status: str
    issues_count: int
    open_issues_count: int
    closed_issues_count: int
    # Whether the issue counts above are an authoritative reading or absent
    # placeholders. Defaults to ``False`` (fail-closed): an operation whose
    # safety depends on the counts (delete / close / lock) is blocked until the
    # counts are known, so a missing/defaulted ``0`` can never be mistaken for a
    # genuinely empty Version. The id-only / inline construction paths must set
    # this explicitly; only a fully-populated reading sets it ``True``.
    counts_known: bool = False

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> "VersionState":
        """Parse one ``list_versions`` entry. Fail-closed on a missing id, and
        ``counts_known`` only when all three count fields are present **and parse
        as integers** — a present-but-unparseable count (e.g. a malformed
        snapshot) must not be trusted as a genuine ``0``."""
        version_id = str(payload.get("id", "")).strip()
        if not version_id:
            raise VersionOperationError("version entry has no id")
        count_keys = ("issues_count", "open_issues_count", "closed_issues_count")
        parsed = [_coerce_count(payload[key]) if key in payload else None for key in count_keys]
        counts_known = all(value is not None for value in parsed)
        return cls(
            version_id=version_id,
            name=str(payload.get("name", "") or ""),
            status=str(payload.get("status", "") or "").strip().lower(),
            issues_count=parsed[0] or 0,
            open_issues_count=parsed[1] or 0,
            closed_issues_count=parsed[2] or 0,
            counts_known=counts_known,
        )


@dataclass(frozen=True)
class VersionOperationRequest:
    """A requested Version operation plus the operator-supplied authorization."""

    operation: str
    state: VersionState
    new_name: str | None = None
    confirmation: str | None = None
    allow_open_issues: bool = False
    historical_protected: bool = False


@dataclass(frozen=True)
class VersionOperationDecision:
    """The preflight verdict. ``rest_step`` / ``operator_ui_step`` are populated
    only when ``allowed`` is true; otherwise the executable step is withheld."""

    operation: str
    version_id: str
    allowed: bool
    blocked_reasons: tuple[str, ...]
    warnings: tuple[str, ...]
    required_confirmation: str
    confirmation_satisfied: bool
    rest_step: str | None
    operator_ui_step: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "operation": self.operation,
            "version_id": self.version_id,
            "allowed": self.allowed,
            "blocked_reasons": list(self.blocked_reasons),
            "warnings": list(self.warnings),
            "required_confirmation": self.required_confirmation,
            "confirmation_satisfied": self.confirmation_satisfied,
            "rest_step": self.rest_step,
            "operator_ui_step": self.operator_ui_step,
        }


@runtime_checkable
class RedmineVersionWrites(Protocol):
    """The write port a future live executor implements to actually perform an
    *already-approved* :class:`VersionOperationDecision`.

    Declared here so the safe seam is explicit, but intentionally left without a
    live implementation in this slice: the repo's Redmine adapter is
    read-only-by-design and no Version-write REST credential is wired. A live
    adapter must follow the double-opt-in, fail-closed precedent of
    ``redmine_note_transport`` (CLI opt-in **and** env gate) and must refuse any
    decision whose ``allowed`` is false.
    """

    def rename(self, version_id: str, new_name: str) -> None: ...

    def close(self, version_id: str) -> None: ...

    def lock(self, version_id: str) -> None: ...

    def delete(self, version_id: str) -> None: ...


def decide_version_operation(
    request: VersionOperationRequest,
) -> VersionOperationDecision:
    """Pure, fail-closed preflight for one Version operation.

    Returns ``allowed=True`` with a concrete ``rest_step`` / ``operator_ui_step``
    only when the operation vocabulary is known, the confirmation token matches,
    and every per-operation guard passes. Otherwise returns ``allowed=False``
    with the accumulated ``blocked_reasons`` and no executable step.
    """
    op = request.operation
    state = request.state
    required = confirmation_token_for(op, state.version_id)
    satisfied = request.confirmation == required
    reasons: list[str] = []
    warnings: list[str] = []

    # 1. Closed operation vocabulary — fail closed on anything unknown before any
    #    other guard runs (no executable step is ever derived for it).
    if op not in VERSION_OPERATIONS:
        reasons.append(f"unknown_operation:{op}")
        return _blocked(op, state.version_id, reasons, warnings, required, satisfied)

    status = state.status.strip().lower()

    # 2. Every operation — including the non-destructive rename — requires the
    #    explicit, operation-and-target-specific confirmation token.
    if not satisfied:
        reasons.append("confirmation_required")

    # 3. Per-operation preflight.
    if op == "rename":
        new_name = (request.new_name or "").strip()
        if not new_name:
            reasons.append("new_name_required")
        else:
            if new_name == state.name.strip():
                reasons.append("new_name_unchanged")
            if classify_version_name(new_name) == "package_numbered":
                # #12643: Redmine Version names are planning buckets, never the
                # package release number.
                reasons.append("new_name_package_numbered")
    elif op in ("close", "lock"):
        if status not in VERSION_STATUSES:
            reasons.append(f"unknown_status:{status or 'missing'}")
        if op == "close" and status == "closed":
            reasons.append("already_closed")
        if op == "lock" and status == "locked":
            reasons.append("already_locked")
        if not state.counts_known:
            # The open-issue guard below trusts open_issues_count; an absent /
            # defaulted reading must not be read as "no open issues".
            reasons.append("counts_required")
        elif state.open_issues_count > 0:
            if request.allow_open_issues:
                warnings.append("open_issues_present")
            else:
                # Closing/locking a bucket that still holds open issues strands
                # them; require an explicit override.
                reasons.append("open_issues_present")
    elif op == "delete":
        if request.historical_protected:
            # An old numbered Version kept as a historical record is close/lock
            # territory, never delete.
            reasons.append("historical_protected")
        if not state.counts_known:
            # Delete is irreversible: never derive "empty" from a missing /
            # defaulted count. Require an authoritative reading first.
            reasons.append("counts_required")
        elif (
            state.issues_count > 0
            or state.open_issues_count > 0
            or state.closed_issues_count > 0
        ):
            # Allow delete solely for a truly empty Version. Check all three
            # counts so an inconsistent snapshot (e.g. issues_count==0 while
            # open_issues_count>0) still blocks.
            reasons.append("version_not_empty")

    allowed = not reasons
    rest_step = _rest_step(op, state, request) if allowed else None
    ui_step = _operator_ui_step(op, state, request) if allowed else None
    return VersionOperationDecision(
        operation=op,
        version_id=state.version_id,
        allowed=allowed,
        blocked_reasons=tuple(reasons),
        warnings=tuple(warnings),
        required_confirmation=required,
        confirmation_satisfied=satisfied,
        rest_step=rest_step,
        operator_ui_step=ui_step,
    )


def _blocked(
    op: str,
    version_id: str,
    reasons: list[str],
    warnings: list[str],
    required: str,
    satisfied: bool,
) -> VersionOperationDecision:
    return VersionOperationDecision(
        operation=op,
        version_id=version_id,
        allowed=False,
        blocked_reasons=tuple(reasons),
        warnings=tuple(warnings),
        required_confirmation=required,
        confirmation_satisfied=satisfied,
        rest_step=None,
        operator_ui_step=None,
    )


def _rest_step(
    op: str, state: VersionState, request: VersionOperationRequest
) -> str:
    vid = state.version_id
    if op == "rename":
        new_name = (request.new_name or "").strip()
        return f'PUT /versions/{vid}.json  {{"version": {{"name": "{new_name}"}}}}'
    if op == "close":
        return f'PUT /versions/{vid}.json  {{"version": {{"status": "closed"}}}}'
    if op == "lock":
        return f'PUT /versions/{vid}.json  {{"version": {{"status": "locked"}}}}'
    # delete
    return f"DELETE /versions/{vid}.json"


def _operator_ui_step(
    op: str, state: VersionState, request: VersionOperationRequest
) -> str:
    vid = state.version_id
    base = f"Redmine UI: Settings > Versions > version #{vid}"
    if op == "rename":
        new_name = (request.new_name or "").strip()
        return f'{base} > Edit > set Name = "{new_name}" > Save'
    if op == "close":
        return f"{base} > Edit > set Status = closed > Save"
    if op == "lock":
        return f"{base} > Edit > set Status = locked > Save"
    return f"{base} > Delete (confirm; only valid for an empty version)"


def _coerce_count(value: object) -> int | None:
    """Parse an issue-count field to an int, or ``None`` when it is missing or
    not an integer. ``None`` keeps ``counts_known`` false so a malformed count is
    never trusted as a genuine ``0`` by a destructive preflight."""
    if isinstance(value, bool):  # bool is an int subclass; reject it explicitly
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None
