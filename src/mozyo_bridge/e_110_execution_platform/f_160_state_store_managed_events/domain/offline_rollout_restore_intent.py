"""Sealed restore identities for one offline rollout action (#15227).

The public rollout plan deliberately carries no launch nonce.  A delegate fixes one
private nonce and its resulting startup-action identity for every canonical restore
group *before* the action record is created.  Execution can therefore distinguish an
unstarted group from this action's completed launch and from every partial/foreign
residual without minting a replacement identity during replay.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable, Mapping

from mozyo_bridge.core.state.startup_transaction_fence import (
    StartupUnit,
    startup_action_id,
)


RESTORE_INTENT_VERSION = 1
RESTORE_RECEIPT_VERSION = 1
TOP_RESTORE_PHASE = "top_restore_action_bootstrap"
REMAINING_RESTORE_PHASE = "remaining_workspace_restore"
RESTORE_PHASES = (TOP_RESTORE_PHASE, REMAINING_RESTORE_PHASE)

_INTENT_FIELDS = frozenset({"version", "groups"})
_GROUP_FIELDS = frozenset(
    {
        "phase",
        "workspace_id",
        "lane_id",
        "recovery_issue_id",
        "agents",
        "action_nonce",
        "expected_startup_action_id",
    }
)
_AGENT_FIELDS = frozenset(
    {"workspace_id", "lane_id", "provider", "assigned_name"}
)
_RECEIPT_FIELDS = frozenset(
    {"version", "phase", "groups", "cumulative_assigned_names"}
)
_RECEIPT_GROUP_FIELDS = frozenset(
    {
        "workspace_id",
        "lane_id",
        "recovery_issue_id",
        "assigned_names",
        "expected_startup_action_id",
    }
)
_NONCE = re.compile(r"[0-9a-f]{32}")
_STARTUP_ACTION = re.compile(r"startup-[0-9a-f]{64}")


class OfflineRolloutRestoreIntentError(ValueError):
    """The private restore authority is absent or malformed; effects must stop."""


def _token(value: object, reason: str = "restore_intent_invalid") -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OfflineRolloutRestoreIntentError(reason)
    return value


def _recovery_issue(value: object) -> str:
    if value == "":
        return ""
    token = _token(value)
    if not token.isascii() or not token.isdecimal() or str(int(token)) != token:
        raise OfflineRolloutRestoreIntentError("restore_intent_invalid")
    return token


@dataclass(frozen=True)
class OfflineRolloutRestoreAgent:
    workspace_id: str
    lane_id: str
    provider: str
    assigned_name: str

    def as_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "provider": self.provider,
            "assigned_name": self.assigned_name,
        }


@dataclass(frozen=True)
class OfflineRolloutRestoreGroup:
    phase: str
    workspace_id: str
    lane_id: str
    recovery_issue_id: str
    agents: tuple[OfflineRolloutRestoreAgent, ...]
    action_nonce: str = field(repr=False)
    expected_startup_action_id: str = field(repr=False)

    @property
    def assigned_names(self) -> tuple[str, ...]:
        return tuple(agent.assigned_name for agent in self.agents)

    @property
    def providers(self) -> tuple[str, ...]:
        return tuple(agent.provider for agent in self.agents)

    def as_payload(self) -> dict:
        return {
            "phase": self.phase,
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "recovery_issue_id": self.recovery_issue_id,
            "agents": [agent.as_payload() for agent in self.agents],
            "action_nonce": self.action_nonce,
            "expected_startup_action_id": self.expected_startup_action_id,
        }

    def receipt_payload(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "lane_id": self.lane_id,
            "recovery_issue_id": self.recovery_issue_id,
            "assigned_names": list(self.assigned_names),
            "expected_startup_action_id": self.expected_startup_action_id,
        }


@dataclass(frozen=True)
class OfflineRolloutRestoreIntent:
    groups: tuple[OfflineRolloutRestoreGroup, ...]
    version: int = RESTORE_INTENT_VERSION

    def as_payload(self) -> dict:
        return {
            "version": self.version,
            "groups": [group.as_payload() for group in self.groups],
        }

    def groups_for_phase(self, phase: str) -> tuple[OfflineRolloutRestoreGroup, ...]:
        return tuple(group for group in self.groups if group.phase == phase)

    def identities(self) -> Mapping[str, OfflineRolloutRestoreAgent]:
        return {
            agent.assigned_name: agent
            for group in self.groups
            for agent in group.agents
        }


def _plan_restore_agents(plan: Mapping[str, object]) -> tuple[tuple, ...]:
    """Return canonical ``(phase, recovery, identity...)`` rows from the public plan."""

    raw_agents = plan.get("agents")
    raw_recoveries = plan.get("legacy_recoveries")
    phases = plan.get("phase_order")
    if not isinstance(raw_agents, list) or not isinstance(raw_recoveries, list):
        raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
    if not isinstance(phases, list):
        raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")

    phase_names: dict[str, str] = {}
    for phase_name in RESTORE_PHASES:
        matches = [
            row
            for row in phases
            if isinstance(row, Mapping) and row.get("phase") == phase_name
        ]
        if len(matches) != 1 or not isinstance(matches[0].get("assigned_names"), list):
            raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
        for raw_name in matches[0]["assigned_names"]:
            name = _token(raw_name, "restore_intent_plan_invalid")
            if name in phase_names:
                raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
            phase_names[name] = phase_name

    identities: dict[str, tuple[str, str, str, str]] = {}
    recovery_by_name: dict[str, str] = {}
    for raw in raw_agents:
        if not isinstance(raw, Mapping):
            raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
        values = tuple(
            _token(raw.get(field), "restore_intent_plan_invalid")
            for field in ("workspace_id", "lane_id", "provider", "assigned_name")
        )
        if values[-1] in identities:
            raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
        identities[values[-1]] = values

    for recovery in raw_recoveries:
        if not isinstance(recovery, Mapping) or not isinstance(recovery.get("agents"), list):
            raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
        workspace = _token(recovery.get("workspace_id"), "restore_intent_plan_invalid")
        lane = _token(recovery.get("lane_id"), "restore_intent_plan_invalid")
        issue = _recovery_issue(recovery.get("issue_id"))
        for raw in recovery["agents"]:
            if not isinstance(raw, Mapping):
                raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
            provider = _token(raw.get("provider"), "restore_intent_plan_invalid")
            name = _token(raw.get("assigned_name"), "restore_intent_plan_invalid")
            values = (workspace, lane, provider, name)
            if name in identities and identities[name] != values:
                raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
            identities[name] = values
            if name in recovery_by_name and recovery_by_name[name] != issue:
                raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
            recovery_by_name[name] = issue

    if set(identities) != set(phase_names):
        raise OfflineRolloutRestoreIntentError("restore_intent_plan_invalid")
    return tuple(
        sorted(
            (
                phase_names[name],
                recovery_by_name.get(name, ""),
                *identity,
            )
            for name, identity in identities.items()
        )
    )


def _group_skeletons(plan: Mapping[str, object]) -> tuple[tuple, ...]:
    grouped: dict[tuple[str, str, str, str], list[OfflineRolloutRestoreAgent]] = {}
    for phase, recovery, workspace, lane, provider, name in _plan_restore_agents(plan):
        grouped.setdefault((phase, workspace, lane, recovery), []).append(
            OfflineRolloutRestoreAgent(workspace, lane, provider, name)
        )
    return tuple(
        (
            *key,
            tuple(sorted(agents, key=lambda row: (row.provider, row.assigned_name))),
        )
        for key, agents in sorted(
            grouped.items(),
            key=lambda item: (
                RESTORE_PHASES.index(item[0][0]),
                item[0][1],
                item[0][2],
                item[0][3],
            ),
        )
    )


def build_restore_intent(
    plan: Mapping[str, object], *, nonce_factory: Callable[[], str]
) -> OfflineRolloutRestoreIntent:
    """Mint every group identity before action creation; performs no store write."""

    groups = []
    for phase, workspace, lane, recovery, agents in _group_skeletons(plan):
        nonce = nonce_factory()
        if type(nonce) is not str or _NONCE.fullmatch(nonce) is None:
            raise OfflineRolloutRestoreIntentError("restore_intent_nonce_invalid")
        expected = startup_action_id(
            StartupUnit(workspace, lane, tuple(agent.provider for agent in agents)),
            nonce,
        )
        groups.append(
            OfflineRolloutRestoreGroup(
                phase=phase,
                workspace_id=workspace,
                lane_id=lane,
                recovery_issue_id=recovery,
                agents=agents,
                action_nonce=nonce,
                expected_startup_action_id=expected,
            )
        )
    intent = OfflineRolloutRestoreIntent(tuple(groups))
    # Route the producer through the same strict reader it must later survive.
    return decode_restore_intent(
        {"restore_intent": intent.as_payload()}, plan=plan
    )


def decode_restore_intent(
    private_bindings: object, *, plan: Mapping[str, object]
) -> OfflineRolloutRestoreIntent:
    if not isinstance(private_bindings, Mapping):
        raise OfflineRolloutRestoreIntentError("restore_intent_missing")
    raw = private_bindings.get("restore_intent")
    if raw is None:
        raise OfflineRolloutRestoreIntentError("restore_intent_missing")
    if not isinstance(raw, Mapping) or set(raw) != _INTENT_FIELDS:
        raise OfflineRolloutRestoreIntentError("restore_intent_shape_invalid")
    if (
        type(raw.get("version")) is not int
        or raw.get("version") != RESTORE_INTENT_VERSION
    ):
        raise OfflineRolloutRestoreIntentError("restore_intent_schema_unsupported")
    raw_groups = raw.get("groups")
    if not isinstance(raw_groups, list):
        raise OfflineRolloutRestoreIntentError("restore_intent_groups_invalid")

    skeletons = _group_skeletons(plan)
    if len(raw_groups) != len(skeletons):
        raise OfflineRolloutRestoreIntentError("restore_intent_plan_mismatch")
    groups = []
    nonces: set[str] = set()
    action_ids: set[str] = set()
    for raw_group, skeleton in zip(raw_groups, skeletons):
        if not isinstance(raw_group, Mapping) or set(raw_group) != _GROUP_FIELDS:
            raise OfflineRolloutRestoreIntentError("restore_intent_group_shape_invalid")
        phase = _token(raw_group.get("phase"))
        workspace = _token(raw_group.get("workspace_id"))
        lane = _token(raw_group.get("lane_id"))
        recovery = _recovery_issue(raw_group.get("recovery_issue_id"))
        raw_agents = raw_group.get("agents")
        nonce = raw_group.get("action_nonce")
        observed_action = raw_group.get("expected_startup_action_id")
        if not isinstance(raw_agents, list):
            raise OfflineRolloutRestoreIntentError("restore_intent_agents_invalid")
        agents = []
        for raw_agent in raw_agents:
            if not isinstance(raw_agent, Mapping) or set(raw_agent) != _AGENT_FIELDS:
                raise OfflineRolloutRestoreIntentError("restore_intent_agent_shape_invalid")
            agents.append(
                OfflineRolloutRestoreAgent(
                    *(
                        _token(raw_agent.get(field))
                        for field in ("workspace_id", "lane_id", "provider", "assigned_name")
                    )
                )
            )
        expected_skeleton = (phase, workspace, lane, recovery, tuple(agents))
        if expected_skeleton != skeleton:
            raise OfflineRolloutRestoreIntentError("restore_intent_plan_mismatch")
        if type(nonce) is not str or _NONCE.fullmatch(nonce) is None or nonce in nonces:
            raise OfflineRolloutRestoreIntentError("restore_intent_nonce_invalid")
        if (
            type(observed_action) is not str
            or _STARTUP_ACTION.fullmatch(observed_action) is None
        ):
            raise OfflineRolloutRestoreIntentError("restore_intent_action_invalid")
        expected_action = startup_action_id(
            StartupUnit(workspace, lane, tuple(agent.provider for agent in agents)),
            nonce,
        )
        if observed_action != expected_action or observed_action in action_ids:
            raise OfflineRolloutRestoreIntentError("restore_intent_action_mismatch")
        nonces.add(nonce)
        action_ids.add(observed_action)
        groups.append(
            OfflineRolloutRestoreGroup(
                phase, workspace, lane, recovery, tuple(agents), nonce, observed_action
            )
        )
    return OfflineRolloutRestoreIntent(tuple(groups))


def restore_phase_receipt(
    intent: OfflineRolloutRestoreIntent, phase: str
) -> dict:
    if phase not in RESTORE_PHASES:
        raise OfflineRolloutRestoreIntentError("restore_receipt_phase_invalid")
    groups = intent.groups_for_phase(phase)
    completed_phases = RESTORE_PHASES[: RESTORE_PHASES.index(phase) + 1]
    cumulative = sorted(
        agent.assigned_name
        for group in intent.groups
        if group.phase in completed_phases
        for agent in group.agents
    )
    return {
        "version": RESTORE_RECEIPT_VERSION,
        "phase": phase,
        "groups": [group.receipt_payload() for group in groups],
        "cumulative_assigned_names": cumulative,
    }


def validate_restore_phase_receipt(
    raw: object, *, intent: OfflineRolloutRestoreIntent, phase: str
) -> Mapping[str, object]:
    if not isinstance(raw, Mapping) or set(raw) != _RECEIPT_FIELDS:
        raise OfflineRolloutRestoreIntentError("restore_receipt_shape_invalid")
    if (
        type(raw.get("version")) is not int
        or raw.get("version") != RESTORE_RECEIPT_VERSION
    ):
        raise OfflineRolloutRestoreIntentError("restore_receipt_schema_unsupported")
    if raw.get("phase") != phase:
        raise OfflineRolloutRestoreIntentError("restore_receipt_phase_invalid")
    groups = raw.get("groups")
    if not isinstance(groups, list) or any(
        not isinstance(group, Mapping) or set(group) != _RECEIPT_GROUP_FIELDS
        for group in groups
    ):
        raise OfflineRolloutRestoreIntentError("restore_receipt_groups_invalid")
    expected = restore_phase_receipt(intent, phase)
    if dict(raw) != expected:
        raise OfflineRolloutRestoreIntentError("restore_receipt_mismatch")
    return raw


def validate_completed_restore_receipts(
    action: Mapping[str, object], *, intent: OfflineRolloutRestoreIntent
) -> None:
    completed = set(action.get("completed_phases", ()))
    receipts = action.get("phase_receipts")
    if not isinstance(receipts, Mapping):
        raise OfflineRolloutRestoreIntentError("restore_receipt_shape_invalid")
    for phase in RESTORE_PHASES:
        if phase in completed:
            validate_restore_phase_receipt(
                receipts.get(phase), intent=intent, phase=phase
            )


__all__ = (
    "OfflineRolloutRestoreAgent",
    "OfflineRolloutRestoreGroup",
    "OfflineRolloutRestoreIntent",
    "OfflineRolloutRestoreIntentError",
    "REMAINING_RESTORE_PHASE",
    "RESTORE_INTENT_VERSION",
    "RESTORE_PHASES",
    "RESTORE_RECEIPT_VERSION",
    "TOP_RESTORE_PHASE",
    "build_restore_intent",
    "decode_restore_intent",
    "restore_phase_receipt",
    "validate_completed_restore_receipts",
    "validate_restore_phase_receipt",
)
