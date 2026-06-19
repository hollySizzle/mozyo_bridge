from __future__ import annotations

import json
import shlex
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import PurePosixPath
from typing import Any, Iterable, Literal, Optional, Sequence


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
MODE_QUEUE_ENTER = "queue-enter"
MODES: frozenset[str] = frozenset({MODE_STANDARD, MODE_PENDING, MODE_QUEUE_ENTER})

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


@dataclass(frozen=True)
class ExecutionRoot:
    """Receiver target execution root / workdir carried by a handoff.

    Redmine #12098: the pane cwd / cross-workspace repo root is not always the
    directory the receiver must operate from. In a cockpit workspace whose pane
    cwd is the workspace root (`IT導入_`-style anchor), the real work target can
    be a nested project several levels below (`.../rovoice/shinsei_llm`). When
    the durable anchor only stores relative save paths, the receiver cannot
    uniquely recover that nested execution root and ends up searching the wrong
    checkout. This value object carries the execution root explicitly so the
    notification body and durable delivery record can point the receiver at it
    without relying on pane scrollback, session/window name, or manual grep.

    Fields:

    - ``workdir`` — the absolute, resolved execution root (a runtime fact; the
      CLI runtime record may carry it per the issue's constraint).
    - ``repo_root`` — the cross-workspace repo / workspace root anchor the
      relative form was computed against (``None`` when no anchor was known).
    - ``relative`` — the repo-root-relative pointer (e.g. ``rovoice/shinsei_llm``)
      when ``workdir`` lives under ``repo_root``. This is the **portable**
      pointer: it carries no personal home prefix, so it is the form surfaced in
      the pane notification and preferred in pasteable records
      (``vibes/docs/rules/public-private-boundary.md``).
    """

    workdir: str
    repo_root: Optional[str] = None
    relative: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def is_nested(self) -> bool:
        """True when the execution root is a nested path below ``repo_root``.

        ``relative in (None, ".")`` means the execution root either could not be
        expressed relative to the anchor or equals the anchor itself; only a
        genuine sub-path is "nested" in the sense the issue cares about.
        """
        return bool(self.relative) and self.relative != "."

    def portable_pointer(self) -> Optional[str]:
        """Repo-relative pointer with no private home prefix, or ``None``.

        Returns ``None`` when no repo-relative form exists (the execution root
        is out-of-tree, or no repo anchor was known): the only remaining value
        would be the absolute ``workdir``, which must NOT leak into pane text or
        a pasteable Asana/Redmine record (Redmine #12098 review j#59662;
        ``vibes/docs/rules/public-private-boundary.md``). Callers fall back to a
        redaction phrase that points at the structured outcome's
        ``execution_root.workdir`` instead. The repo-root-itself case
        (``relative == "."``) is still portable and is rendered as such.
        """
        if self.relative is None:
            return None
        if self.relative == ".":
            return "`.` (the target repo root)"
        return f"`{self.relative}` (relative to the target repo root)"

    # Redaction phrase used wherever a portable pointer is unavailable: the
    # absolute path stays only in the structured runtime outcome, never in
    # pasteable text. Kept as one constant so the record and the notification
    # body redact identically.
    _OMITTED_ABS = (
        "outside the target repo — see `execution_root.workdir` in the "
        "structured delivery outcome (absolute path omitted to keep private "
        "paths out of pasteable records)"
    )

    def record_pointer(self) -> str:
        """Pointer for the durable, pasteable delivery record.

        Repo-relative (portable) form only — never the absolute ``workdir`` —
        so a record pasted into a Redmine journal carries no personal home /
        private project path.
        """
        pointer = self.portable_pointer()
        return pointer if pointer is not None else self._OMITTED_ABS

    def notification_clause(self) -> str:
        """Sentence appended to the pane notification body.

        Keeps the receiver contract intact: the execution root is a pointer the
        receiver still confirms from the durable anchor, not a new authority.
        Uses the portable (repo-relative) pointer; when none exists the absolute
        path is omitted (it remains in the structured outcome) rather than
        printed into pane text.
        """
        pointer = self.portable_pointer()
        if pointer is None:
            return (
                f"Target execution root: {self._OMITTED_ABS}. Confirm it from "
                "the durable anchor before operating, not from the pane location."
            )
        return (
            f"Target execution root: {pointer} — distinct from the pane cwd / "
            "workspace root; confirm it from the durable anchor before "
            "operating, not from the pane location."
        )


def build_execution_root(
    workdir_abs: str, *, repo_root_abs: Optional[str] = None
) -> ExecutionRoot:
    """Construct an :class:`ExecutionRoot` from resolved absolute paths.

    Pure: callers resolve ``workdir_abs`` / ``repo_root_abs`` against the
    filesystem first, this only derives the repo-relative portable pointer.
    The relative form is computed on NFC-normalized operands so a nested
    Japanese path (the #12098 reproduction had NFD/NFC-spelled directories)
    still resolves instead of falling through to an absolute-only pointer.
    Returns ``relative=None`` when ``workdir_abs`` does not live under
    ``repo_root_abs`` (an out-of-tree workdir is still carried, just without a
    portable relative pointer).
    """
    relative: Optional[str] = None
    if repo_root_abs:
        try:
            rel = PurePosixPath(
                unicodedata.normalize("NFC", workdir_abs)
            ).relative_to(PurePosixPath(unicodedata.normalize("NFC", repo_root_abs)))
        except ValueError:
            relative = None
        else:
            relative = str(rel)
    return ExecutionRoot(
        workdir=workdir_abs, repo_root=repo_root_abs or None, relative=relative
    )


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
    execution_root: Optional["ExecutionRoot"] = None,
) -> str:
    """Compose the pane text body that follows the landing marker.

    When ``execution_root`` is supplied (Redmine #12098), a trailing clause
    points the receiver at the target execution root / workdir so a nested
    project root is recoverable without pane scrollback. The clause does not
    replace the durable-anchor contract: the receiver still reads the anchor
    from the source-of-truth system before acting.
    """
    if kind not in KIND_LABELS:
        raise AnchorError(f"unknown handoff kind: {kind!r}; expected one of {sorted(KIND_LABELS)}")
    if kind == "custom" and not summary:
        raise AnchorError("--summary is required when --kind custom")
    intent = summary if summary else _default_body_for_kind(kind, receiver)
    pointer = anchor.human_pointer()
    body = (
        f"{intent}. {pointer} is the durable anchor; read it from the source-of-truth "
        "system before acting."
    )
    if execution_root is not None:
        body = f"{body} {execution_root.notification_clause()}"
    return body


Status = Literal["sent", "pending_input", "blocked"]
Reason = Literal[
    "ok",
    "target_unavailable",
    "target_not_agent",
    "marker_timeout",
    "invalid_anchor",
    "invalid_args",
    "queue_enter",
    "cross_session_claude",
    "target_repo_mismatch",
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
    # Redmine #12098: receiver target execution root / workdir, when it differs
    # from the pane cwd / repo root. `None` for the common same-root case.
    execution_root: Optional[dict[str, Any]] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, sort_keys=True)

    def to_last_input_projection(
        self,
        *,
        submitted_at: Optional[str] = None,
        input_kind: Optional[str] = None,
        prompt_turn_id: Optional[str] = None,
        input_id: Optional[str] = None,
    ) -> Optional["LastInputProjection"]:
        """Project this outcome into the inspector ``last_input`` shape.

        See :func:`project_last_input` for the full contract.
        """
        return project_last_input(
            self,
            submitted_at=submitted_at,
            input_kind=input_kind,
            prompt_turn_id=prompt_turn_id,
            input_id=input_id,
        )


AckStatus = Literal["submitted", "acknowledged", "unobserved"]


@dataclass(frozen=True)
class LastInputProjection:
    """Inspector-compatible projection of a :class:`DeliveryOutcome`.

    Mirrors the ``last_input`` block defined by
    ``mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md``.
    The tmux compatibility path can only populate the delivery-ACK-derived
    timestamps and ack status; PTY-first paths may fill ``acknowledged_at``
    and elevate ``ack_status`` later. This dataclass does not carry any
    runtime/process state — ACK terminal states (``blocked + *``) deliberately
    yield no projection.
    """

    submitted_at: Optional[str]
    acknowledged_at: Optional[str]
    ack_status: AckStatus
    input_kind: Optional[str]
    prompt_turn_id: Optional[str]
    input_id: Optional[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_last_input(
    outcome: "DeliveryOutcome",
    *,
    submitted_at: Optional[str] = None,
    input_kind: Optional[str] = None,
    prompt_turn_id: Optional[str] = None,
    input_id: Optional[str] = None,
) -> Optional[LastInputProjection]:
    """Project a :class:`DeliveryOutcome` into the inspector ``last_input`` block.

    The mapping is the one approved by
    ``mozyo_bridge_pty/vibes/docs/specs/receiver-state-inspector-contract.md``
    and the upstream ACK contract:

    - ``sent`` / ``ok`` -> projection with ``ack_status="submitted"`` and the
      caller-supplied ``submitted_at``. The tmux path never claims
      ``acknowledged`` here; ``acknowledged_at`` stays ``None`` and is only
      populated by PTY-side callers when ``runtime.input.ack`` arrives.
    - ``pending_input`` / ``ok`` -> projection with ``submitted_at=None`` and
      ``ack_status="unobserved"``. The input is staged at the prompt but the
      receiver runtime has not received it as a turn, so the ACK timestamp
      does not exist yet.
    - any ``blocked`` reason (``marker_timeout``, ``target_unavailable``,
      ``target_not_agent``, ``invalid_anchor``, ``invalid_args``) -> ``None``.
      ACK terminal states are not receiver-runtime facts; the helper refuses
      to project them so callers cannot accidentally translate
      ``marker_timeout`` into ``process.exited`` or push ``invalid_args`` into
      ``runtime_phase`` (both explicitly prohibited by the inspector contract).
    - ``delivery_failed`` is reserved by the ACK contract but is not currently
      emitted by ``make_outcome``; it falls through to ``None`` for the same
      reason.
    """
    if outcome.status == "sent" and outcome.reason in ("ok", "queue_enter"):
        # `queue_enter` is a wording-layer differentiator on the sender side
        # (the marker was not pre-observed before Enter under the relaxed
        # `queue-enter` rail). The inspector projection still resolves to
        # `submitted` with `submitted_at` populated because the upstream
        # receiver-state-inspector contract derives `ack_status` from
        # `submitted_at`/`acknowledged_at` and the same spec's Capability
        # Matrix says tmux compat populates `submitted_at` for `submitted`
        # arrivals. Returning `unobserved` here would be structurally
        # impossible (cannot have `submitted_at != None` with
        # `ack_status="unobserved"`) and would collapse queue-enter into
        # `pending_input/ok`. The wording differentiation lives in
        # `DeliveryOutcome.reason` and the durable record narrative only.
        return LastInputProjection(
            submitted_at=submitted_at,
            acknowledged_at=None,
            ack_status="submitted",
            input_kind=input_kind,
            prompt_turn_id=prompt_turn_id,
            input_id=input_id,
        )
    if outcome.status == "pending_input" and outcome.reason == "ok":
        return LastInputProjection(
            submitted_at=None,
            acknowledged_at=None,
            ack_status="unobserved",
            input_kind=input_kind,
            prompt_turn_id=prompt_turn_id,
            input_id=input_id,
        )
    return None


NO_SUBMIT_RETRY_BUDGET = 3
"""Per preset contract, the `mozyo-bridge message --no-submit` retry budget cap.

This is the *only* place the budget cap is hard-coded. CLI guidance lines and
``next_action_for`` derive their N/3 framing from this constant so the budget
stays in lockstep across the structured outcome, the durable record, the CLI
stderr trailer, and any preset wording that references the same N.
"""


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
        # The previous wording attributed the next action to "record
        # un-notified ... immediately", which let agents skip the retry budget
        # entirely after a single transient gate failure (Asana task
        # 1214779823377861, Asana task 1214774670696760 comment 1214778979254677
        # for the worked example). The contract is: refresh the read marker,
        # retry under `--no-submit` up to NO_SUBMIT_RETRY_BUDGET attempts, and
        # only escalate to `un-notified` after that budget is exhausted AND the
        # last gate error lacks a literal next-action verb. "un-notified" is
        # preserved as the terminal escalation label so existing audit tooling
        # and the preset's "Notification fails" branch continue to grep the
        # same vocabulary.
        return (
            "sender",
            (
                f"refresh the read marker via `mozyo-bridge read {receiver}` then "
                f"retry with `mozyo-bridge message {receiver} \"<resubmit text>\" "
                f"--no-submit --attempt <N>` (up to {NO_SUBMIT_RETRY_BUDGET} "
                "attempts per preset contract). Only after that budget is "
                "exhausted AND the last gate error lacks a next-action verb "
                "(`read target again`, `retry`, `refresh`), record `un-notified` "
                "in the durable record with every attempted command and observed "
                "error verbatim."
            ),
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
    if reason == "cross_session_claude":
        return (
            "sender",
            (
                "route the handoff through the target session's Codex window: "
                "re-run with `--to codex --target <target_session>:codex "
                "--target-repo <target_workspace_root>` and ask that Codex to "
                "perform the local Claude handoff. With an explicit --target "
                "and a passing --target-repo identity gate, that gateway send "
                "is admitted on the default `queue-enter` rail (Redmine "
                "#11301); `--mode standard` (or `--mode pending`) remains an "
                "available fallback, e.g. when you cannot assert --target-repo. "
                "Naming a cross-session Claude pane directly is rejected by the "
                "Cross-Workspace Handoff gate."
            ),
        )
    if reason == "target_repo_mismatch":
        return (
            "sender",
            (
                "verify `--target-repo` matches the receiver pane's inferred "
                "repo root, or drop the flag to skip the repo gate. Pass a "
                "target whose cwd lives under the expected repo."
            ),
        )
    return "sender", "inspect handoff failure and decide the next step"


AUTO_TARGET_REPO = "auto"
"""Sentinel `--target-repo` value that resolves identity from the target pane.

Redmine #11778: `--target-repo auto` derives the cross-workspace identity gate
from an explicitly-named `%pane`'s own cwd instead of forcing the operator to
hand-run `tmux display-message -p -t %pane '#{pane_current_path}'`.
"""


def is_explicit_pane_target(target_arg: "Optional[str]") -> bool:
    """True only for an explicit tmux pane-id target (``%n``).

    Used to gate `--target-repo auto` (Redmine #11778): auto identity
    resolution is admitted ONLY when the operator named an exact pane, never
    for a receiver label, a ``session:window`` location, or implicit
    receiver-window discovery. Pure/string-only.
    """
    return bool(target_arg) and target_arg.startswith("%")


def build_inactive_pane_fallback_command(
    *,
    receiver: str,
    kind: Optional[str],
    target: Optional[str],
    anchor: Optional[NormalizedAnchor],
) -> Optional[str]:
    """Build the copy-pasteable strict-rail recovery command for a queue-enter
    inactive-split rejection (Redmine #12162).

    When a `--mode queue-enter` send (including a `notify-*` wrapper that
    defaults to queue-enter) resolves an explicit receiver pane but that pane is
    not the active split, the Step 11 admission gate blocks with
    ``blocked / invalid_args`` and types nothing. Pre-#12162 the operator only
    got prose telling them strict ``--mode standard`` would work; they still had
    to reassemble the whole command by hand, so the dogfooding handoff stalled on
    a human re-dispatch decision (Redmine #12137 j#60072/#60073).

    This returns the exact ``mozyo-bridge handoff send … --target %pane
    --target-repo auto --mode standard`` they can run verbatim: same receiver,
    source, kind, and durable anchor, pinned to the already-resolved ``%pane``
    with a fail-closed ``--target-repo auto`` identity gate. The strict rail
    observes the landing marker instead of requiring the active split, so it can
    deliver to the inactive same-identity pane **without** weakening the
    queue-enter guard (which stays the default rail).

    Returns ``None`` when a safe explicit recovery cannot be formed (no resolved
    explicit ``%pane``, unknown receiver, or no durable anchor); callers then
    fall back to the descriptive hint. The command carries only pane ids, anchor
    ids, and the ``auto`` sentinel — never an absolute path — so it is safe to
    paste into a durable record
    (``vibes/docs/rules/public-private-boundary.md``).

    Every token is rendered through ``shlex.quote`` so the command is genuinely
    copy-pasteable (Redmine #12162 review j#60107): an Asana ``--anchor-url``
    permalink commonly carries shell metacharacters (``?`` / ``&`` query params)
    that would otherwise background or re-parse the command in a normal shell.
    Tokens without metacharacters (the Redmine ``--issue`` / ``--journal`` path,
    flag names, the ``mozyo-bridge`` prefix) are left unchanged by ``shlex.quote``.
    """
    if not is_explicit_pane_target(target) or anchor is None or receiver not in RECEIVERS:
        return None
    parts = [
        "mozyo-bridge",
        "handoff",
        "send",
        "--to",
        receiver,
        "--source",
        anchor.source,
        "--kind",
        kind or "<kind>",
    ]
    if isinstance(anchor, RedmineAnchor):
        parts += ["--issue", anchor.issue, "--journal", anchor.journal]
    else:  # AsanaAnchor
        parts += ["--task-id", anchor.task_id]
        if anchor.comment_id:
            parts += ["--comment-id", anchor.comment_id]
        elif anchor.anchor_url:
            parts += ["--anchor-url", anchor.anchor_url]
    parts += [
        "--target",
        str(target),
        "--target-repo",
        AUTO_TARGET_REPO,
        "--mode",
        MODE_STANDARD,
    ]
    return " ".join(shlex.quote(part) for part in parts)


def _gateway_candidate_lines(candidates: "Iterable[Any]") -> list[str]:
    """Format Codex gateway candidate panes as ``- pane window cwd repo_root``.

    Each candidate is a mapping (e.g. ``AgentRecord.to_dict()``) carrying
    ``pane_id`` / ``window_name`` / ``cwd`` / ``repo_root``. Pure/string-only.
    """
    lines: list[str] = []
    for cand in candidates:
        get = cand.get
        lines.append(
            f"  - {get('pane_id') or '<?>'}  "
            f"window={get('window_name') or '<?>'}  "
            f"cwd={get('cwd') or '<?>'}  "
            f"repo_root={get('repo_root') or '<unresolved>'}"
        )
    return lines


def cross_session_gateway_hint(
    target_session: str, candidates: "Sequence[Any]"
) -> str:
    """Operator hint appended to a blocked ``cross_session_claude`` outcome.

    Diagnostics only (Redmine #11776): names the safe Codex gateway pane(s) in
    ``target_session`` and a copyable explicit-pane command shape, so the
    operator does not have to hand-discover the Codex pane and its repo root.
    Does not change the safety boundary — cross-session ``--to claude`` stays
    blocked; this only points at the gateway route. Pure/string-only.
    """
    if candidates:
        first = candidates[0]
        pane = first.get("pane_id") or "<codex_pane>"
        repo = first.get("repo_root") or "<target_workspace_root>"
        return "\n".join(
            [
                f"Gateway route: target session '{target_session}' has these "
                "Codex gateway candidate pane(s):",
                *_gateway_candidate_lines(candidates),
                "Re-send through that Codex window using an explicit pane id and "
                "its repo_root (keep your --source/--anchor/--kind/--summary):",
                f"  mozyo-bridge handoff send --to codex --target {pane} "
                f"--target-repo {repo} ...",
            ]
        )
    return (
        f"Gateway route: no Codex-classified pane found in target session "
        f"'{target_session}'. The cross-session gateway needs a pane whose tmux "
        "window is named 'codex' (agent_kind=codex). Start that workspace's "
        "Codex window with `mozyo` there, or run `mozyo-bridge agents list` to "
        "inspect classification, then re-send with `--to codex --target "
        "<codex_pane> --target-repo <target_workspace_root>`."
    )


def target_unavailable_codex_diagnostic(
    target_session: str, requested_window: str, candidates: "Sequence[Any]"
) -> str:
    """Diagnose a ``<session>:<window>`` ``target_unavailable`` (Redmine #11776).

    Distinguishes exact tmux window-name resolution from inventory agent_kind
    classification: a pane can be ``agent_kind=codex`` yet not resolve as
    ``:codex`` when its tmux window carries a different name. ``candidates`` are
    the ``agent_kind=codex`` panes in ``target_session``. Pure/string-only.
    """
    head = (
        f"diagnostic: '{target_session}:{requested_window}' did not resolve to a "
        f"live tmux window named '{requested_window}'. A tmux location target "
        "matches the window *name* exactly, which is independent of inventory "
        "agent_kind classification."
    )
    if candidates:
        return "\n".join(
            [
                head,
                f"Inventory classifies these pane(s) as Codex in "
                f"'{target_session}':",
                *_gateway_candidate_lines(candidates),
                "Target one by explicit pane id (with --target-repo <repo_root>) "
                "instead of the ':codex' window form.",
            ]
        )
    return (
        head
        + f" No pane in '{target_session}' is classified agent_kind=codex either; "
        "run `mozyo` there to start its Codex window, or `mozyo-bridge agents "
        "list` to check classification."
    )


RECORD_FORMAT_TEXT = "text"
RECORD_FORMAT_JSON = "json"
RECORD_FORMAT_BOTH = "both"
RECORD_FORMATS: frozenset[str] = frozenset(
    {RECORD_FORMAT_TEXT, RECORD_FORMAT_JSON, RECORD_FORMAT_BOTH}
)


def _header_label(status: Status, reason: Reason, mode: Optional[str] = None) -> str:
    if status == "sent":
        if reason == "queue_enter":
            return "sent (queue-enter, marker unobserved)"
        if mode == MODE_QUEUE_ENTER:
            return "sent (queue-enter, marker observed)"
        return "sent"
    if status == "pending_input":
        return "pending input"
    return f"not delivered ({reason})"


def _outcome_narrative(status: Status, reason: Reason, mode: Optional[str] = None) -> str:
    if status == "sent":
        if reason == "queue_enter":
            return (
                "Landing marker was not observed in the target pane before "
                "timeout, but Enter was issued under the queue-enter rail "
                "because the target is a registered agent pane. No rollback "
                "was triggered."
            )
        if mode == MODE_QUEUE_ENTER:
            return (
                "Landing marker observed in the target pane; Enter was "
                "pressed under the queue-enter rail. No rollback was "
                "triggered."
            )
        return (
            "Landing marker observed in the target pane; Enter was pressed. "
            "No rollback was triggered."
        )
    if status == "pending_input":
        return (
            "Notification body was typed but Enter was intentionally not pressed; "
            "input is left pending at the target prompt."
        )
    if reason == "marker_timeout":
        return (
            "Landing marker was not observed in the target pane before timeout; "
            "a C-u rollback was issued and Enter was not pressed. The sender "
            "cannot verify from tmux capture that the receiver composer was "
            "cleared."
        )
    if reason == "target_unavailable":
        return (
            "Receiver pane could not be resolved; no notification was typed."
        )
    if reason == "target_not_agent":
        return (
            "Target pane is not running an agent process and --force was not given; "
            "no notification was typed."
        )
    if reason == "invalid_anchor":
        return (
            "Anchor arguments did not satisfy the source's contract; "
            "handoff aborted before resolving the receiver pane. No notification was typed."
        )
    if reason == "invalid_args":
        return (
            "Required arguments for handoff send/reply were missing or invalid; "
            "handoff aborted before resolving the receiver pane. No notification was typed."
        )
    if reason == "cross_session_claude":
        return (
            "Cross-Workspace Handoff gate: sender and target live in different "
            "tmux sessions, and `--to claude` was used. Route through the "
            "target session's Codex window with `--to codex --target "
            "<target_session>:codex --target-repo <target_workspace_root>`; "
            "with an explicit --target and a passing --target-repo identity "
            "gate, that gateway send is admitted on the default `queue-enter` "
            "rail (Redmine #11301). `--mode standard` (or `--mode pending`) "
            "remains an available fallback. No notification was typed."
        )
    if reason == "target_repo_mismatch":
        return (
            "Target pane's inferred repo root does not match `--target-repo`; "
            "handoff aborted before typing. No notification was typed."
        )
    return "Handoff did not deliver; see structured outcome for details."


def _receiver_contract_line(status: Status, reason: Reason, receiver: str) -> Optional[str]:
    if status == "sent":
        return (
            f"Receiver-side contract: {receiver} must read the durable anchor "
            "before acting; the pane notification is only the pointer."
        )
    if reason == "marker_timeout":
        return (
            f"Receiver-side contract: {receiver} must read the durable anchor "
            "manually if action is still required; nothing was submitted at the pane."
        )
    return None


def _anchor_pointer_or_dash(anchor_payload: Optional[dict[str, Any]]) -> str:
    if not anchor_payload:
        return "—"
    source = anchor_payload.get("source")
    if source == SOURCE_ASANA:
        task = anchor_payload.get("task_id")
        comment = anchor_payload.get("comment_id")
        anchor_url = anchor_payload.get("anchor_url")
        url = f"https://app.asana.com/0/0/{task}"
        if comment:
            return f"Asana task {task} ({url}) comment {comment}"
        if anchor_url:
            return f"Asana task {task} ({url}) anchor {anchor_url}"
        return f"Asana task {task} ({url})"
    if source == SOURCE_REDMINE:
        return (
            f"Redmine #{anchor_payload.get('issue')} "
            f"journal #{anchor_payload.get('journal')}"
        )
    return "—"


def _execution_root_pointer_or_dash(execution_root: Optional[dict[str, Any]]) -> str:
    if not execution_root:
        return "—"
    return ExecutionRoot(
        workdir=execution_root.get("workdir") or "",
        repo_root=execution_root.get("repo_root"),
        relative=execution_root.get("relative"),
    ).record_pointer()


def build_delivery_record(
    outcome: "DeliveryOutcome",
    *,
    command: Optional[str] = None,
    recovery_command: Optional[str] = None,
    duplicate_lane_panes: Optional[Sequence[str]] = None,
) -> str:
    """Render a durable delivery-record text from a structured outcome.

    The returned markdown block is meant to be pasted verbatim into the
    source-of-truth ticket-system (Asana task comment, Redmine journal) so the
    sender does not have to invent phrasing or re-read the pane to describe
    what happened. The CLI is responsible for emitting this alongside the
    structured outcome; this module does not perform any ticket-system API
    write.

    The structured outcome carries everything the record needs after the
    source-preservation fix from the previous task, so this function is pure
    and deterministic over the outcome dataclass.

    ``duplicate_lane_panes`` (Redmine #12229) is an optional list of
    already-redacted identity rows for OTHER live same-lane panes that resolve
    to the same receiver role. The caller computes them from a live snapshot
    (``pane_resolver.same_lane_receiver_duplicates`` →
    ``duplicate_pane_record_row``); when present they render a diagnostic
    advisory so the receiver pane and any stale-input duplicate stay both
    visible and the receiver/actor record cannot silently diverge. Like
    ``recovery_command`` it does not affect the ``json`` outcome shape.
    """
    header = f"Delivery result — {_header_label(outcome.status, outcome.reason, outcome.mode)}"
    lines = [
        header,
        "",
        f"- Receiver: `{outcome.receiver}`",
        f"- Source: `{outcome.source or '—'}`",
        f"- Kind: `{outcome.kind or '—'}`",
        f"- Mode: `{outcome.mode or '—'}`",
        f"- Target pane: `{outcome.target or '—'}`",
        f"- Notification marker: `{outcome.notification_marker or '—'}`",
        f"- Durable anchor: {_anchor_pointer_or_dash(outcome.anchor)}",
        f"- Target execution root: {_execution_root_pointer_or_dash(outcome.execution_root)}",
        f"- Status: `{outcome.status}` (reason: `{outcome.reason}`)",
        f"- Outcome: {_outcome_narrative(outcome.status, outcome.reason, outcome.mode)}",
        f"- Next action owner: `{outcome.next_action_owner}` — {outcome.next_action}",
    ]
    if command:
        lines.append(f"- Command: `{command}`")
    if recovery_command:
        # Redmine #12162: a concrete, copy-pasteable recovery command supplied
        # by the caller for failure paths whose `(status, reason)` is too
        # generic to special-case here (the queue-enter inactive-split block
        # emits `blocked / invalid_args`, a reason shared with several gates).
        # Keeping the command out of the shared-reason branches below means
        # only the inactive-split rejection — which actually resolved a pane
        # and anchor — surfaces it. Carries only pane/anchor ids and the `auto`
        # sentinel, never an absolute path, so it is durable-record safe.
        lines.append(
            "- Fallback recovery: run "
            f"`{recovery_command}` — the strict `--mode standard` rail observes "
            "the landing marker instead of requiring the active split, so it "
            "reaches the resolved inactive same-identity pane without weakening "
            "the queue-enter guard."
        )
    if duplicate_lane_panes:
        # Redmine #12229: duplicate same-lane receiver panes were live at send
        # time (a cockpit gateway repair can leave two same-lane Claude panes,
        # #12226 j#61213). Name them so the durable record keeps the receiver
        # pane and any stale-input duplicate both visible: the receiver/actor
        # record must not silently diverge (delivery record named `%14` while
        # Implementation Done named `%16`, #12226 j#61224 vs j#61228). This is a
        # diagnostic surface, not a block — the receiver of THIS send is the
        # `Target pane` above; the actor recorded downstream must match it.
        rows = "; ".join(duplicate_lane_panes)
        lines.append(
            "- Duplicate same-lane pane(s): "
            f"{rows}. These are NOT the receiver of this send (the receiver is "
            f"`{outcome.target or '—'}`). A prior failed `--mode standard` send "
            "can leave residual prompt text in a duplicate — a `C-u` rollback is "
            "issued but composer clearing is not verifiable from tmux capture — "
            "so read each duplicate before reusing it, and record the "
            "implementation actor as the target pane above (not a duplicate) so "
            "the receiver and actor records do not diverge."
        )
    if outcome.status == "sent" and outcome.reason == "queue_enter":
        # Operator-facing escalation hint required by the contract's Durable
        # Wording Requirements. This note does NOT override `next_action`;
        # the receiver-owned primary contract still stands.
        lines.append(
            "- Operator note: Marker was not observed before Enter; if the "
            f"receiver does not pick up the prompt, fall back to "
            "`mozyo-bridge handoff send --mode standard` and re-attempt with "
            "the recovered read marker."
        )
    if outcome.status == "blocked" and outcome.reason == "marker_timeout":
        # Sender-facing fallback hint required to prevent the "transient
        # marker_timeout → immediately record un-notified" shortcut described
        # in Asana task 1214779823377861. Mirrors and is constrained by the
        # `next_action` line; the `un-notified` terminal label is still in
        # `next_action`, but this hint surfaces the ordered retry path
        # explicitly in the durable record so an auditor and any future agent
        # can replay why escalation happened (or did not).
        receiver_label = outcome.receiver or "<receiver>"
        lines.append(
            f"- Fallback path: refresh the read marker via `mozyo-bridge read "
            f"{receiver_label}` then retry with `mozyo-bridge message "
            f"{receiver_label} \"<resubmit text>\" --no-submit --attempt <N>` "
            f"(up to {NO_SUBMIT_RETRY_BUDGET} attempts per preset contract; "
            "track attempts with `--attempt N`). Only after the budget is "
            "exhausted AND the last gate error lacks a literal next-action "
            "verb (`read target again`, `retry`, `refresh`) should the "
            "preset's `Notification fails` branch fire."
        )
    contract = _receiver_contract_line(outcome.status, outcome.reason, outcome.receiver)
    if contract:
        lines.append("")
        lines.append(contract)
    return "\n".join(lines)


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
    execution_root: Optional[ExecutionRoot] = None,
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
        execution_root=execution_root.to_dict() if execution_root else None,
    )


__all__: Iterable[str] = (
    "AckStatus",
    "AnchorError",
    "AsanaAnchor",
    "DeliveryOutcome",
    "ExecutionRoot",
    "KIND_LABELS",
    "LastInputProjection",
    "MODES",
    "MODE_PENDING",
    "MODE_QUEUE_ENTER",
    "MODE_STANDARD",
    "NO_SUBMIT_RETRY_BUDGET",
    "NormalizedAnchor",
    "RECEIVERS",
    "RECORD_FORMATS",
    "RECORD_FORMAT_BOTH",
    "RECORD_FORMAT_JSON",
    "RECORD_FORMAT_TEXT",
    "RedmineAnchor",
    "SOURCES",
    "SOURCE_ASANA",
    "SOURCE_REDMINE",
    "AUTO_TARGET_REPO",
    "build_delivery_record",
    "build_execution_root",
    "build_inactive_pane_fallback_command",
    "build_marker",
    "build_notification_body",
    "cross_session_gateway_hint",
    "is_explicit_pane_target",
    "make_outcome",
    "next_action_for",
    "normalize_anchor",
    "project_last_input",
    "target_unavailable_codex_diagnostic",
)
