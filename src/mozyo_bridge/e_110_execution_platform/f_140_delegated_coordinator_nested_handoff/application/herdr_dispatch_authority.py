"""Live dispatch-authority resolution adapter (Redmine #13489 increment 2).

Bridges the two impure action-time reads the design contract requires into the pure decider
(:func:`...domain.dispatch_authority.decide_dispatch_authority`):

1. the **source-of-truth Redmine** dispatch authorization: the candidate issue's journals are
   read live via the credential-gated :class:`LiveRedmineJournalSource` (reused from #13289 —
   daemon-trusted credentials, redirect-refusing, redacted errors), the dedicated
   ``[mozyo:dispatch-authorization:...]`` markers are parsed, the latest one correlated to this
   exact lane + issue is selected, and a later durable gate (implementation_done / review /
   close / blocked) supersede is detected from the same entries' structured gate markers;
2. the **credential-trusted runtime** observation: the authorization's *exact*
   ``target_assigned_name`` is resolved against the live ``herdr agent list`` inventory and its
   cardinality + runtime state are folded into one :data:`...dispatch_authority.TARGET_*` token
   (a drifted / renamed target -> :data:`TARGET_ABSENT`, a duplicate -> :data:`TARGET_AMBIGUOUS`).

Every read failure (unconfigured credentials, transport error, unreadable inventory) fails
**closed** — the decision is zero-send. The Redmine source and the inventory reader are
injectable so the required regressions drive the whole authority hermetically.
"""

from __future__ import annotations

import argparse
from dataclasses import replace
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authority import (
    MONITOR,
    REASON_REDMINE_UNAVAILABLE,
    TARGET_ABSENT,
    TARGET_AMBIGUOUS,
    TARGET_AWAITING_INPUT,
    TARGET_BLOCKED,
    TARGET_BUSY,
    TARGET_TURN_ENDED,
    TARGET_UNAVAILABLE,
    TARGET_UNKNOWN,
    DispatchDecision,
    decide_dispatch_authority,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.dispatch_authorization import (
    AMBIGUITY_DUPLICATE_AT_JOURNAL,
    DispatchAuthorization,
    parse_dispatch_authorizations,
)

# Runtime receiver-state token -> resolved-single-target token. The e_140 ``agent_state`` tokens
# are mirrored as literals to avoid importing the terminal-runtime adapter's vocabulary here.
_RUNTIME_TO_TARGET = {
    "awaiting_input": TARGET_AWAITING_INPUT,
    "busy": TARGET_BUSY,
    "blocked": TARGET_BLOCKED,
    "turn_ended": TARGET_TURN_ENDED,
    "unknown": TARGET_UNKNOWN,
}

# Injection seam types.
JournalSourceFactory = Callable[[argparse.Namespace], object]
AgentRowsReader = Callable[[Mapping[str, str]], Sequence[Mapping[str, object]]]


def _default_journal_source(args: argparse.Namespace):
    """The credential-gated live Redmine journal source (raises on unconfigured credentials)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalSource,
    )

    return LiveRedmineJournalSource.from_environment()


def _default_agent_rows(env: Mapping[str, str]) -> Sequence[Mapping[str, object]]:
    """The live ``herdr agent list`` rows (raises ``HerdrSessionStartError`` on failure)."""
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        list_herdr_agent_rows,
    )

    return list_herdr_agent_rows(env)


def _select_authorization(
    entries, *, workspace_id: str, lane_id: str, issue: str
) -> Optional[DispatchAuthorization]:
    """The latest dispatch authorization correlated to this exact lane + issue, or ``None``.

    Selection is lane/issue-correlated but validity-agnostic: a malformed authorization *aimed
    at this lane* is still selected so the decider surfaces it as invalid (fail closed), whereas
    no lane-matched authorization at all is ``None`` (monitor — not authorized yet). Note order
    means the last matching marker wins (a re-authorization supersedes an earlier one).

    "Last wins" is a rule about SUPERSESSION, and supersession happens BETWEEN journals — a
    later coordinator record replaces an earlier one. Two authorizations at ONE journal are not
    a supersession; they are one record naming two different rounds, and picking the later of
    them by note order is guessing (Redmine #14539 review j#92227 finding 2). The downstream
    correlator has always said so — ``correlate_dispatch_disposition`` refuses a row whose
    journal carries 2+ valid AUTHORIZE, with "Cardinality is the gate input here — 0, 1 and 2+
    are three different answers and only 1 can correlate" (review j#80644 R6-F2). That rule
    existed only on the discharge side, so the identical durable record read as ``ambiguous``
    when the retire asked and ``authorize`` when the sender asked. Selection therefore groups by
    journal, keeps last-journal-wins, and marks the winner ambiguous when its OWN journal names
    more than one — zero send, surfaced as blocked rather than monitored.
    """
    by_journal: dict[str, list[DispatchAuthorization]] = {}
    for auth in parse_dispatch_authorizations(entries):
        if auth.matches_lane(workspace_id=workspace_id, lane_id=lane_id, issue=issue):
            by_journal.setdefault(auth.journal, []).append(auth)
    if not by_journal:
        return None
    # Insertion order is note order, so the last key is the newest journal naming this lane.
    at_journal = by_journal[list(by_journal)[-1]]
    if len(at_journal) > 1:
        return replace(at_journal[-1], ambiguity=AMBIGUITY_DUPLICATE_AT_JOURNAL)
    return at_journal[-1]


def _is_superseded(
    entries, *, workspace_id: str, lane_id: str, issue: str, authorization_journal: str
) -> bool:
    """True when the authorization is not the latest durable intent for this lane (fail-safe).

    The design requires monitor when the latest durable state has advanced to
    implementation_done / review / close / blocked. Redmine gate journals are Markdown prose
    that carry **no** machine marker (mid-review j#75047 F3), so scanning only for structured
    gate markers would let a real Implementation Done / Review Request silently fail to
    supersede a standing authorization.

    So the rule is fail-safe over the *latest durable state*: any journal recorded **after** the
    selected authorization supersedes it — UNLESS that later journal is itself a valid
    dispatch-authorization for this same lane + issue (a re-authorization, which
    :func:`_select_authorization` already picks as the newest). An unclassifiable prose gate
    journal (Implementation Done, Review Request, close, blocked, or any later note) therefore
    drives monitor rather than being safely ignored. Pure over the read entries.
    """
    try:
        auth_j = int((authorization_journal or "").strip())
    except (TypeError, ValueError):
        return False
    issue = (issue or "").strip()
    for entry in entries:
        if str(getattr(entry, "issue_id", "")).strip() != issue:
            continue
        try:
            entry_j = int(str(getattr(entry, "journal_id", "")).strip())
        except (TypeError, ValueError):
            continue
        if entry_j <= auth_j:
            continue
        # A later journal exists. It only fails to supersede when it is itself a valid
        # re-authorization for this exact lane + issue (a fresh authority, not a state advance).
        reauths = [
            a
            for a in parse_dispatch_authorizations([entry])
            if a.valid and a.matches_lane(workspace_id=workspace_id, lane_id=lane_id, issue=issue)
        ]
        if reauths:
            continue
        return True
    return False


def _target_runtime(
    target_assigned_name: str,
    *,
    env: Mapping[str, str],
    agent_rows: AgentRowsReader,
) -> str:
    """Fold the authorized target's live cardinality + runtime state into one target token.

    Resolves the **exact** ``target_assigned_name`` against the live inventory: 0 rows ->
    :data:`TARGET_ABSENT` (covers identity drift — the named target simply is not live), 2+ ->
    :data:`TARGET_AMBIGUOUS`, exactly 1 -> its runtime receiver-state mapped via
    :data:`_RUNTIME_TO_TARGET` (unknown / unmapped -> :data:`TARGET_UNKNOWN`). An unreadable
    inventory -> :data:`TARGET_UNAVAILABLE`. The runtime state is read from the row itself, never
    from caller-supplied lane metadata or pane text.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (
        agent_row_runtime_state,
    )

    want = (target_assigned_name or "").strip()
    if not want:
        return TARGET_ABSENT
    try:
        rows = agent_rows(env)
    except HerdrSessionStartError:
        return TARGET_UNAVAILABLE
    except Exception:  # noqa: BLE001 - any inventory-read failure fails the runtime gate closed
        return TARGET_UNAVAILABLE
    matches = [
        row
        for row in rows
        if isinstance(row, Mapping) and str(row.get(AGENT_KEY_NAME, "")).strip() == want
    ]
    if not matches:
        return TARGET_ABSENT
    if len(matches) >= 2:
        return TARGET_AMBIGUOUS
    runtime = agent_row_runtime_state(matches[0])
    return _RUNTIME_TO_TARGET.get(runtime, TARGET_UNKNOWN)


def resolve_dispatch_decision(
    args: argparse.Namespace,
    *,
    workspace_id: str,
    lane_id: str,
    issue: str,
    env: Mapping[str, str],
    journal_source_factory: JournalSourceFactory = _default_journal_source,
    agent_rows: AgentRowsReader = _default_agent_rows,
) -> DispatchDecision:
    """Resolve the action-time dispatch decision from source-of-truth Redmine + live runtime.

    Reads the candidate issue's journals live (fail-closed on any credential / transport
    failure), selects the lane/issue-correlated authorization, detects supersede, resolves the
    exact target's runtime, and delegates to :func:`decide_dispatch_authority`. Only a valid,
    non-superseded authorization whose exact target is a single ``awaiting_input`` slot decides
    :data:`AUTHORIZE`; everything else is zero send.
    """
    issue = (issue or "").strip()
    try:
        source = journal_source_factory(args)
        entries = list(source.read_entries(issue))
    except Exception:  # noqa: BLE001 - unconfigured credentials / transport failure -> zero send
        # A dispatch authority read failure (the normal state for a lane not running
        # auto-dispatch: no credentials) degrades to MONITOR, not a hard block — the gateway
        # keeps its resolution-only monitor no-op (increment-1 behavior) and simply does not
        # auto-dispatch this turn. Still zero send (the required "Redmine unavailable" regression).
        return DispatchDecision(
            MONITOR,
            REASON_REDMINE_UNAVAILABLE,
            "the source-of-truth Redmine journals could not be read to verify a dispatch "
            "authorization (credential / transport failure); monitor rather than dispatch",
        )

    authorization = _select_authorization(
        entries, workspace_id=workspace_id, lane_id=lane_id, issue=issue
    )
    if authorization is None or not authorization.valid:
        # No lane-matched authorization (monitor) or a malformed one (blocked) — the decider
        # distinguishes; runtime is irrelevant to either, so it is not read.
        return decide_dispatch_authority(
            authorization=authorization, superseded=False, target_runtime=TARGET_ABSENT
        )

    superseded = _is_superseded(
        entries,
        workspace_id=workspace_id,
        lane_id=lane_id,
        issue=issue,
        authorization_journal=authorization.journal,
    )
    if superseded:
        return decide_dispatch_authority(
            authorization=authorization, superseded=True, target_runtime=TARGET_ABSENT
        )

    runtime = _target_runtime(
        authorization.target_assigned_name, env=env, agent_rows=agent_rows
    )
    return decide_dispatch_authority(
        authorization=authorization, superseded=False, target_runtime=runtime
    )


__all__ = ("resolve_dispatch_decision",)
