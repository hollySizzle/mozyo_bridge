"""herdr-native sender identity + target resolution (Redmine #13261).

Pure, core-owned resolution for a **pure herdr session** (no tmux server / ``TMUX``
unset / isolated socket). It replaces the tmux-pane projection the #13253 wiring
used to derive a handoff target's durable herdr name
(``project_preflight_target(pane_info(target))`` — tmux pane user-options) with two
herdr-native inputs:

- **launch-time sender env** (``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` /
  ``MOZYO_LANE_ID``) — the sender agent's own workspace / provider / lane, injected
  into its process environment by the session-start helper (#13261 WU2). It is the
  basis for the coordinator pseudo-target's workspace scope and the lane context
  **only**; it is never target authority (auditor answer j#72519);
- **live herdr inventory** (``agent list`` rows) + the #13247 assigned-name decode —
  the sole target authority. A ``claude`` / ``codex`` / ``coordinator`` receiver is
  resolved by decoding every live agent's assigned name and matching the sender's
  workspace + the receiver's provider role.

Boundary (spec ``vibes/docs/specs/herdr-native-identity.md``): this module is
**core** — identity contract, target-role vocabulary, the fail-closed reason set,
the pure ``resolve_herdr_target`` projection, and the discovery **Port** protocol.
The provider (``infrastructure/herdr_discovery.py``) owns only the ``agent list``
subprocess mechanics. The :class:`~...domain.terminal_transport.TerminalTransportPort`
is **not** widened — discovery is a separate listing protocol in the same bounded
context. herdr is not registered in the provider registry; the
``terminal_transport.backend: herdr`` flag remains the sole selector.

Everything here is pure: value objects + total functions over plain strings / row
mappings. It opens no subprocess, reads no env of its own, scans no tmux — the
caller supplies the env mapping, the anchor workspace id, and the ``agent list``
rows.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional, Protocol, runtime_checkable

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
    AGENT_KEY_NAME,
    DEFAULT_LANE,
    _agent_locator,
    _norm,
    decode_assigned_name,
    HerdrAgentIdentity,
)

# ---------------------------------------------------------------------------
# Launch-time sender-identity environment variables (trusted, launch-injected).
# Named alongside the terminal-runtime domain's other MOZYO_* env boundary
# (``MOZYO_HERDR_BINARY`` lives in the sibling infrastructure resolver). These
# carry no secret — they are the sender agent's own public identity slot.
# ---------------------------------------------------------------------------
MOZYO_WORKSPACE_ID_ENV: str = "MOZYO_WORKSPACE_ID"
MOZYO_AGENT_ROLE_ENV: str = "MOZYO_AGENT_ROLE"
MOZYO_LANE_ID_ENV: str = "MOZYO_LANE_ID"

# ---------------------------------------------------------------------------
# Provider / receiver vocabulary. The mzb1 "role" field is a runtime *provider*
# token (agent kind), not a workflow role — ``claude`` / ``codex`` (same vocab as
# the tmux role resolver's ``agent_kind``). ``coordinator`` is the pseudo-target
# resolved through the role->provider binding (#13174 / #12673).
# ---------------------------------------------------------------------------
PROVIDER_CLAUDE: str = "claude"
PROVIDER_CODEX: str = "codex"
RECEIVER_COORDINATOR: str = "coordinator"

#: The provider tokens a launched agent (and thus a mzb1 identity slot) may carry.
AGENT_PROVIDERS: frozenset[str] = frozenset({PROVIDER_CLAUDE, PROVIDER_CODEX})

# ---------------------------------------------------------------------------
# Fail-closed reason vocabulary (core-owned, closed set).
# ---------------------------------------------------------------------------
# -- sender identity --
REASON_MISSING_SENDER_ENV: str = "missing_sender_env"
REASON_INVALID_SENDER_ROLE: str = "invalid_sender_role"
REASON_MISSING_ANCHOR: str = "missing_anchor"
REASON_ENV_ANCHOR_WORKSPACE_MISMATCH: str = "env_anchor_workspace_mismatch"
# -- target resolution --
REASON_UNKNOWN_RECEIVER: str = "unknown_receiver"
REASON_COORDINATOR_BINDING_UNRESOLVED: str = "coordinator_binding_unresolved"
REASON_NO_MATCH: str = "no_match"
REASON_MULTIPLE_MATCHES: str = "multiple_matches"
REASON_MISSING_LOCATOR: str = "missing_locator"

SENDER_IDENTITY_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_MISSING_SENDER_ENV,
        REASON_INVALID_SENDER_ROLE,
        REASON_MISSING_ANCHOR,
        REASON_ENV_ANCHOR_WORKSPACE_MISMATCH,
    }
)

TARGET_RESOLUTION_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_UNKNOWN_RECEIVER,
        REASON_COORDINATOR_BINDING_UNRESOLVED,
        REASON_NO_MATCH,
        REASON_MULTIPLE_MATCHES,
        REASON_MISSING_LOCATOR,
    }
)


class HerdrTargetResolutionError(ValueError):
    """A herdr-native resolution result was constructed with an illegal reason.

    Inherits :class:`ValueError` for the fail-closed semantics shared by the
    sibling terminal-runtime domain errors.
    """


# ---------------------------------------------------------------------------
# Sender identity.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SenderIdentity:
    """The launch-injected identity of the agent performing a handoff (fail-closed).

    ``workspace_id`` / ``role`` are required non-empty; ``lane_id`` normalises an
    empty value to :data:`~...herdr_identity.DEFAULT_LANE`. ``role`` is the sender's
    provider token (``claude`` / ``codex``). This is the sender's *own* identity —
    it scopes the target search to the sender's workspace and supplies the lane
    context, but is never the target's authority.
    """

    workspace_id: str
    role: str
    lane_id: str = DEFAULT_LANE


@dataclass(frozen=True)
class SenderIdentityResolution:
    """The fail-closed result of :func:`resolve_sender_identity`.

    ``ok`` is the sole success authority; on success ``identity`` is the
    :class:`SenderIdentity` and ``reason`` is ``None``. On failure ``identity`` is
    ``None`` and ``reason`` is one of :data:`SENDER_IDENTITY_FAILURE_REASONS`.
    """

    ok: bool
    reason: Optional[str] = None
    identity: Optional[SenderIdentity] = None
    detail: str = ""

    @classmethod
    def success(cls, identity: SenderIdentity) -> "SenderIdentityResolution":
        return cls(ok=True, reason=None, identity=identity)

    @classmethod
    def failure(cls, reason: str, detail: str = "") -> "SenderIdentityResolution":
        if reason not in SENDER_IDENTITY_FAILURE_REASONS:
            raise HerdrTargetResolutionError(
                f"unknown sender-identity failure reason {reason!r}; expected one of "
                f"{sorted(SENDER_IDENTITY_FAILURE_REASONS)}"
            )
        return cls(ok=False, reason=reason, identity=None, detail=detail)


def resolve_sender_identity(
    env: Mapping[str, str], *, anchor_workspace_id: Optional[str]
) -> SenderIdentityResolution:
    """Resolve the sender's identity from launch env + the repo anchor (fail-closed).

    ``env`` is the sender process environment (the caller passes ``os.environ`` or a
    subset); ``anchor_workspace_id`` is the workspace id from the repo anchor
    (``read_anchor`` — ``None`` when unreadable). Fail-closed cases:

    - ``MOZYO_WORKSPACE_ID`` / ``MOZYO_AGENT_ROLE`` missing / empty ->
      :data:`REASON_MISSING_SENDER_ENV`;
    - ``MOZYO_AGENT_ROLE`` not ``claude`` / ``codex`` -> :data:`REASON_INVALID_SENDER_ROLE`;
    - no anchor workspace id -> :data:`REASON_MISSING_ANCHOR`;
    - env workspace id != anchor workspace id ->
      :data:`REASON_ENV_ANCHOR_WORKSPACE_MISMATCH` (a checkout must not mint a name
      for another workspace via a leaked env var).
    """
    workspace_id = _norm(env.get(MOZYO_WORKSPACE_ID_ENV))
    role = _norm(env.get(MOZYO_AGENT_ROLE_ENV))
    lane_id = _norm(env.get(MOZYO_LANE_ID_ENV)) or DEFAULT_LANE
    if not workspace_id or not role:
        return SenderIdentityResolution.failure(
            REASON_MISSING_SENDER_ENV,
            f"{MOZYO_WORKSPACE_ID_ENV} and {MOZYO_AGENT_ROLE_ENV} must both be set",
        )
    if role not in AGENT_PROVIDERS:
        return SenderIdentityResolution.failure(
            REASON_INVALID_SENDER_ROLE,
            f"sender role {role!r} is not a known provider ({sorted(AGENT_PROVIDERS)})",
        )
    anchor_ws = _norm(anchor_workspace_id)
    if not anchor_ws:
        return SenderIdentityResolution.failure(
            REASON_MISSING_ANCHOR,
            "repo anchor has no workspace_id; run `mozyo-bridge workspace register`",
        )
    if workspace_id != anchor_ws:
        return SenderIdentityResolution.failure(
            REASON_ENV_ANCHOR_WORKSPACE_MISMATCH,
            f"sender env workspace {workspace_id!r} != anchor workspace {anchor_ws!r}",
        )
    return SenderIdentityResolution.success(
        SenderIdentity(workspace_id=workspace_id, role=role, lane_id=lane_id)
    )


# ---------------------------------------------------------------------------
# Receiver -> target provider role.
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TargetRoleResolution:
    """The resolved provider role for a receiver label (fail-closed)."""

    ok: bool
    role: str = ""
    reason: Optional[str] = None
    detail: str = ""


def resolve_target_role(
    receiver: object, *, coordinator_provider: Optional[str]
) -> TargetRoleResolution:
    """Map a handoff ``receiver`` label to the target's provider role (fail-closed).

    - ``coordinator`` -> ``coordinator_provider`` (from the role->provider binding,
      default ``codex``); an empty / ``None`` binding fails closed with
      :data:`REASON_COORDINATOR_BINDING_UNRESOLVED`;
    - ``claude`` / ``codex`` -> itself;
    - anything else -> :data:`REASON_UNKNOWN_RECEIVER`.
    """
    label = _norm(receiver)
    if label == RECEIVER_COORDINATOR:
        provider = _norm(coordinator_provider)
        if not provider:
            return TargetRoleResolution(
                ok=False,
                reason=REASON_COORDINATOR_BINDING_UNRESOLVED,
                detail="coordinator role is not bound to any runtime provider",
            )
        return TargetRoleResolution(ok=True, role=provider)
    if label in AGENT_PROVIDERS:
        return TargetRoleResolution(ok=True, role=label)
    return TargetRoleResolution(
        ok=False,
        reason=REASON_UNKNOWN_RECEIVER,
        detail=f"receiver {label!r} is not claude / codex / coordinator",
    )


# ---------------------------------------------------------------------------
# Discovery port (core-owned Protocol; provider fills it).
# ---------------------------------------------------------------------------
@runtime_checkable
class HerdrAgentDiscoveryPort(Protocol):
    """The listing half of the terminal-runtime boundary (separate from send-safety).

    A single fail-closed primitive that returns the live ``agent list`` rows — each
    a read-only mapping carrying the durable ``name`` and a transient ``pane_id``
    locator. The provider owns the subprocess mechanics; core owns what the rows
    *mean* (:func:`resolve_herdr_target`). Kept deliberately separate from
    :class:`~...terminal_transport.TerminalTransportPort` so the send-safety port is
    never widened with discovery concerns (auditor answer j#72519).
    """

    def list_agent_rows(self) -> Sequence[Mapping[str, object]]:
        """Return the live herdr ``agent list`` rows; fail closed (raise) on error."""
        ...


# ---------------------------------------------------------------------------
# Target resolution (pure projection over agent-list rows).
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class HerdrTargetResolution:
    """The fail-closed result of resolving a receiver label to a live herdr agent.

    On success ``assigned_name`` is the target agent's durable herdr name, ``locator``
    is its **transient** live pane locator (recovered fresh, never persisted), and
    ``identity`` is the decoded :class:`~...herdr_identity.HerdrAgentIdentity`. On
    failure ``reason`` is one of :data:`TARGET_RESOLUTION_FAILURE_REASONS` and the
    name / locator are empty — a send never lands on a guessed target.
    """

    ok: bool
    reason: Optional[str] = None
    assigned_name: str = ""
    locator: str = ""
    identity: Optional[HerdrAgentIdentity] = None
    considered: int = 0
    detail: str = ""

    @classmethod
    def success(
        cls,
        *,
        assigned_name: str,
        locator: str,
        identity: HerdrAgentIdentity,
        considered: int,
    ) -> "HerdrTargetResolution":
        return cls(
            ok=True,
            assigned_name=assigned_name,
            locator=locator,
            identity=identity,
            considered=considered,
            detail="live herdr agent resolved by workspace + provider role",
        )

    @classmethod
    def failure(
        cls, reason: str, detail: str = "", *, considered: int = 0
    ) -> "HerdrTargetResolution":
        if reason not in TARGET_RESOLUTION_FAILURE_REASONS:
            raise HerdrTargetResolutionError(
                f"unknown target-resolution failure reason {reason!r}; expected one of "
                f"{sorted(TARGET_RESOLUTION_FAILURE_REASONS)}"
            )
        return cls(ok=False, reason=reason, considered=considered, detail=detail)

    @property
    def is_fail(self) -> bool:
        return not self.ok


def resolve_herdr_target(
    receiver: object,
    sender: SenderIdentity,
    rows: Sequence[Mapping[str, object]],
    *,
    coordinator_provider: Optional[str],
) -> HerdrTargetResolution:
    """Resolve a receiver label to a live herdr agent, scoped to the sender's workspace.

    The sole target authority is the live inventory + assigned-name decode (never the
    sender env, which only supplies the workspace scope + coordinator binding). The
    match key is **(workspace_id, provider role)**: for a pure herdr session there is
    one ``claude`` and one ``codex`` per workspace, so lane is intentionally not part
    of the match (multi-lane cross routing is a later US, see the spec).

    Steps: resolve the receiver's target provider role; decode every row's assigned
    name (skipping rows whose name is not a mzb1 scheme name — a foreign / unmanaged
    herdr agent); keep the rows whose decoded workspace equals the sender's and whose
    decoded role equals the target role. Fail-closed:

    - zero candidates -> :data:`REASON_NO_MATCH` (covers both "no agent with that role"
      and "the only agents are in another workspace"; the distinction rides in
      ``detail``);
    - more than one candidate, or a duplicated assigned name ->
      :data:`REASON_MULTIPLE_MATCHES` (a herdr-name uniqueness violation; refuse to
      guess);
    - one candidate whose row carries no usable pane locator ->
      :data:`REASON_MISSING_LOCATOR` (refuse to send to a blank target).
    """
    role_res = resolve_target_role(receiver, coordinator_provider=coordinator_provider)
    if not role_res.ok:
        assert role_res.reason is not None
        return HerdrTargetResolution.failure(role_res.reason, role_res.detail)
    target_role = role_res.role

    row_list = list(rows)
    considered = len(row_list)
    candidates: list[tuple[str, Mapping[str, object], HerdrAgentIdentity]] = []
    saw_target_role_other_workspace = False
    for row in row_list:
        if not isinstance(row, Mapping):
            continue
        name = _norm(row.get(AGENT_KEY_NAME))
        decoded = decode_assigned_name(name)
        if not decoded.ok or decoded.identity is None:
            continue  # foreign / unmanaged herdr agent — not a mozyo mzb1 name
        identity = decoded.identity
        if identity.role != target_role:
            continue
        if identity.workspace_id != sender.workspace_id:
            saw_target_role_other_workspace = True
            continue
        candidates.append((name, row, identity))

    if not candidates:
        detail = (
            f"no live {target_role!r} agent in workspace {sender.workspace_id!r}"
        )
        if saw_target_role_other_workspace:
            detail += " (a matching-role agent exists in another workspace)"
        return HerdrTargetResolution.failure(
            REASON_NO_MATCH, detail, considered=considered
        )
    distinct_names = {name for name, _, _ in candidates}
    if len(candidates) > 1 or len(distinct_names) > 1:
        return HerdrTargetResolution.failure(
            REASON_MULTIPLE_MATCHES,
            f"{len(candidates)} live {target_role!r} agents matched in workspace "
            f"{sender.workspace_id!r}; refuse to guess",
            considered=considered,
        )
    name, row, identity = candidates[0]
    locator = _agent_locator(row)
    if not locator:
        return HerdrTargetResolution.failure(
            REASON_MISSING_LOCATOR,
            f"matched {name!r} but its live row carries no usable pane locator",
            considered=considered,
        )
    return HerdrTargetResolution.success(
        assigned_name=name,
        locator=locator,
        identity=identity,
        considered=considered,
    )


__all__ = (
    "AGENT_PROVIDERS",
    "MOZYO_AGENT_ROLE_ENV",
    "MOZYO_LANE_ID_ENV",
    "MOZYO_WORKSPACE_ID_ENV",
    "PROVIDER_CLAUDE",
    "PROVIDER_CODEX",
    "REASON_COORDINATOR_BINDING_UNRESOLVED",
    "REASON_ENV_ANCHOR_WORKSPACE_MISMATCH",
    "REASON_INVALID_SENDER_ROLE",
    "REASON_MISSING_ANCHOR",
    "REASON_MISSING_LOCATOR",
    "REASON_MISSING_SENDER_ENV",
    "REASON_MULTIPLE_MATCHES",
    "REASON_NO_MATCH",
    "REASON_UNKNOWN_RECEIVER",
    "RECEIVER_COORDINATOR",
    "SENDER_IDENTITY_FAILURE_REASONS",
    "TARGET_RESOLUTION_FAILURE_REASONS",
    "HerdrAgentDiscoveryPort",
    "HerdrTargetResolution",
    "HerdrTargetResolutionError",
    "SenderIdentity",
    "SenderIdentityResolution",
    "TargetRoleResolution",
    "resolve_herdr_target",
    "resolve_sender_identity",
    "resolve_target_role",
)
