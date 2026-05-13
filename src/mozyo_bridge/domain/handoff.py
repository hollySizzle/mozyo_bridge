from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Literal, Optional


# Public set of intent labels accepted by the new primitive. `custom` requires
# an operator-supplied summary; the rest carry a deterministic default body
# that the receiver can parse without re-reading the pane.
KIND_LABELS: frozenset[str] = frozenset(
    {
        "implementation_request",
        "design_consultation",
        "review_request",
        "review_result",
        "implementation_done",
        "reply",
        "custom",
    }
)

SOURCE_ASANA = "asana"
SOURCE_REDMINE = "redmine"
SOURCES: frozenset[str] = frozenset({SOURCE_ASANA, SOURCE_REDMINE})

MODE_STANDARD = "standard"
MODE_PENDING = "pending"
MODES: frozenset[str] = frozenset({MODE_STANDARD, MODE_PENDING})

RECEIVERS: frozenset[str] = frozenset({"claude", "codex"})


class AnchorError(ValueError):
    """Anchor arguments did not satisfy the source's contract."""


@dataclass(frozen=True)
class AsanaAnchor:
    task_id: str
    comment_id: Optional[str] = None
    anchor_url: Optional[str] = None

    @property
    def source(self) -> str:
        return SOURCE_ASANA

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"source": self.source, "task_id": self.task_id}
        if self.comment_id:
            payload["comment_id"] = self.comment_id
        if self.anchor_url:
            payload["anchor_url"] = self.anchor_url
        return payload

    def marker_fields(self) -> list[tuple[str, str]]:
        fields = [("task", self.task_id)]
        if self.comment_id:
            fields.append(("comment", self.comment_id))
        elif self.anchor_url:
            fields.append(("anchor", self.anchor_url))
        return fields

    def human_pointer(self) -> str:
        url = f"https://app.asana.com/0/0/{self.task_id}"
        if self.comment_id:
            return f"Asana task {self.task_id} ({url}) comment {self.comment_id}"
        if self.anchor_url:
            return f"Asana task {self.task_id} ({url}) anchor {self.anchor_url}"
        return f"Asana task {self.task_id} ({url})"


@dataclass(frozen=True)
class RedmineAnchor:
    issue: str
    journal: str

    @property
    def source(self) -> str:
        return SOURCE_REDMINE

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "issue": self.issue, "journal": self.journal}

    def marker_fields(self) -> list[tuple[str, str]]:
        return [("issue", self.issue), ("journal", self.journal)]

    def human_pointer(self) -> str:
        return f"Redmine #{self.issue} journal #{self.journal}"


NormalizedAnchor = AsanaAnchor | RedmineAnchor


def normalize_anchor(
    source: str,
    *,
    task_id: Optional[str] = None,
    comment_id: Optional[str] = None,
    anchor_url: Optional[str] = None,
    issue: Optional[str] = None,
    journal: Optional[str] = None,
) -> NormalizedAnchor:
    """Validate and construct the normalized anchor for ``source``.

    Raises :class:`AnchorError` when the supplied fields do not satisfy the
    contract documented in the design record. Cross-source fields are
    explicitly rejected so a stray ``--journal`` does not silently survive an
    Asana handoff.
    """
    if source not in SOURCES:
        raise AnchorError(
            f"unknown handoff source: {source!r}; expected one of {sorted(SOURCES)}"
        )
    if source == SOURCE_ASANA:
        if issue or journal:
            raise AnchorError(
                "asana anchor must not carry --issue/--journal; those belong to source=redmine"
            )
        if not task_id:
            raise AnchorError("asana anchor requires --task-id")
        if bool(comment_id) == bool(anchor_url):
            raise AnchorError(
                "asana anchor requires exactly one of --comment-id or --anchor-url"
            )
        return AsanaAnchor(task_id=task_id, comment_id=comment_id, anchor_url=anchor_url)
    if task_id or comment_id or anchor_url:
        raise AnchorError(
            "redmine anchor must not carry --task-id/--comment-id/--anchor-url; those belong to source=asana"
        )
    if not issue or not journal:
        raise AnchorError("redmine anchor requires both --issue and --journal")
    return RedmineAnchor(issue=issue, journal=journal)


def build_marker(anchor: NormalizedAnchor, kind: str, receiver: str) -> str:
    """Build the deterministic landing marker that the wait gate inspects."""
    parts = [f"source={anchor.source}"]
    parts.extend(f"{key}={value}" for key, value in anchor.marker_fields())
    parts.append(f"kind={kind}")
    parts.append(f"to={receiver}")
    return "[mozyo:handoff:" + ":".join(parts) + "]"


def _default_body_for_kind(kind: str, receiver: str) -> str:
    if kind == "implementation_request":
        return f"implementation request ready for {receiver}"
    if kind == "design_consultation":
        return f"design consultation ready for {receiver}"
    if kind == "review_request":
        return f"review request ready for {receiver}"
    if kind == "review_result":
        return f"review result ready for {receiver}"
    if kind == "implementation_done":
        return f"implementation done; review handoff ready for {receiver}"
    if kind == "reply":
        return f"reply ready for {receiver}"
    return f"handoff ready for {receiver}"


def build_notification_body(
    anchor: NormalizedAnchor,
    kind: str,
    summary: Optional[str],
    receiver: str,
) -> str:
    """Compose the pane text body that follows the landing marker."""
    if kind not in KIND_LABELS:
        raise AnchorError(f"unknown handoff kind: {kind!r}; expected one of {sorted(KIND_LABELS)}")
    if kind == "custom" and not summary:
        raise AnchorError("--summary is required when --kind custom")
    intent = summary if summary else _default_body_for_kind(kind, receiver)
    pointer = anchor.human_pointer()
    return (
        f"{intent}. {pointer} is the durable anchor; read it from the source-of-truth "
        "system before acting."
    )


Status = Literal["sent", "pending_input", "blocked"]
Reason = Literal[
    "ok",
    "target_unavailable",
    "target_not_agent",
    "marker_timeout",
    "invalid_anchor",
    "invalid_args",
]
NextActionOwner = Literal["receiver", "sender", "operator"]


@dataclass(frozen=True)
class DeliveryOutcome:
    """Structured result emitted by the new handoff primitive.

    Task 1214760547941073 will turn this into a durable Asana / Redmine
    delivery record; the primitive itself must not perform that ticket-system
    persistence.
    """

    status: Status
    reason: Reason
    receiver: str
    target: Optional[str]
    source: Optional[str]
    anchor: Optional[dict[str, Any]]
    mode: Optional[str]
    kind: Optional[str]
    next_action_owner: NextActionOwner
    next_action: str
    notification_marker: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)


def next_action_for(status: Status, reason: Reason, receiver: str) -> tuple[NextActionOwner, str]:
    """Return the canonical owner/action phrase for an outcome."""
    if status == "sent":
        return "receiver", f"read the durable anchor and act from that record as {receiver}"
    if status == "pending_input":
        return (
            "operator",
            "inspect the pending prompt at the target pane and decide whether to submit",
        )
    if reason == "marker_timeout":
        return (
            "sender",
            "record un-notified or pending-operator-action in the durable record with the attempted command",
        )
    if reason == "target_unavailable":
        return (
            "sender",
            f"ensure the {receiver} window exists (run `mozyo` or `mozyo-bridge init {receiver}`) and retry",
        )
    if reason == "target_not_agent":
        return (
            "sender",
            f"verify the {receiver} pane is running the agent process, or pass --force for an explicit operator-approved send",
        )
    if reason == "invalid_anchor":
        return "sender", "supply a valid durable anchor for the chosen source"
    if reason == "invalid_args":
        return "sender", "supply the required arguments for handoff send/reply"
    return "sender", "inspect handoff failure and decide the next step"


def make_outcome(
    *,
    status: Status,
    reason: Reason,
    receiver: str,
    target: Optional[str],
    anchor: Optional[NormalizedAnchor],
    mode: Optional[str],
    kind: Optional[str],
    notification_marker: Optional[str],
    source: Optional[str] = None,
) -> DeliveryOutcome:
    # `source` is part of the structured outcome contract and must survive
    # anchor-normalization failure paths. When the anchor was successfully
    # built, prefer its source (cheaper than asking callers to pass it
    # redundantly); otherwise fall back to the explicit `source` argument so
    # `invalid_anchor` / `invalid_args` outcomes still carry the chosen
    # source system.
    resolved_source = anchor.source if anchor else source
    owner, action = next_action_for(status, reason, receiver)
    return DeliveryOutcome(
        status=status,
        reason=reason,
        receiver=receiver,
        target=target,
        source=resolved_source,
        anchor=anchor.to_dict() if anchor else None,
        mode=mode,
        kind=kind,
        next_action_owner=owner,
        next_action=action,
        notification_marker=notification_marker,
    )


__all__: Iterable[str] = (
    "AnchorError",
    "AsanaAnchor",
    "DeliveryOutcome",
    "KIND_LABELS",
    "MODES",
    "MODE_PENDING",
    "MODE_STANDARD",
    "NormalizedAnchor",
    "RECEIVERS",
    "RedmineAnchor",
    "SOURCES",
    "SOURCE_ASANA",
    "SOURCE_REDMINE",
    "build_marker",
    "build_notification_body",
    "make_outcome",
    "next_action_for",
    "normalize_anchor",
)
