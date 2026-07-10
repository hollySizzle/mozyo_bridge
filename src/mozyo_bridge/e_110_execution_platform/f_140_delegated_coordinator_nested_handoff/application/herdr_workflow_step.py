"""herdr-native `workflow step` resolution adapter (Redmine #13489).

Bridges the impure runtime inputs a herdr session carries — launch-time sender env, the
repo anchor, the lane metadata store, and the live ``herdr agent list`` inventory — into the
pure herdr classifier + resolver (:mod:`...domain.workflow_step_herdr`). It is the herdr
counterpart of the tmux wiring in :func:`...application.cli_workflow.cmd_workflow_step`
(``current_pane`` + tmux inventory -> :func:`...domain.workflow_step.resolve_workflow_step`),
and it produces the SAME replayable :class:`~...domain.workflow_step.WorkflowStepOutcome`
envelope so ``workflow step`` reads the same under either backend.

Increment 1 (Redmine #13489 j#74685 design_boundary) is **resolution-only**: it resolves the
current lane's herdr-native identity + role and, for a worker / gateway lane, verifies the
lane's Redmine ``issue+journal`` anchor against **source-of-truth Redmine** and — for a
gateway — the same-lane worker liveness **cardinality** before naming a role-appropriate next
action. It fails closed on an unattested identity, an unclassifiable lane (default-lane pair /
unknown provider), an unverified / ambiguous / retired / missing anchor, or a missing /
duplicate / unaddressable worker. It performs no sublane lifecycle mutation and no delivery —
the policy-permitted one-step auto-execution and the destructive drain/retire boundary are
increment 2.

Mid-review corrections landed here (j#74748 … j#74787): F1 removes the registry
``project_name`` project-scope heuristic and defers to the pure classifier's default-lane
fail-closed; the same-lane worker liveness returns the 0 / 1 / 2+ cardinality (duplicate
identity is ambiguity, not a target); and **F3 verifies the anchor against live source-of-truth
Redmine** — the lane metadata (and the workflow runtime store) are only *caller-supplied
advisory projections*, so the lane's single non-retired record names the *candidate* issue
(cardinality-preserving) and the credential-gated :class:`LiveRedmineJournalSource` +
structured gate marker are the authority for the exact ``issue+journal`` anchor.
"""

from __future__ import annotations

import argparse
import os
from typing import Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.relative_route import (
    ROLE_DELEGATED_COORDINATOR,
    ROLE_IMPLEMENTATION_WORKER,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step import (
    EXECUTION_BLOCKED,
    OWNER_OPERATOR,
    PRIMITIVE_NONE,
    STATE_LANE_UNRESOLVED,
    WorkflowAnchor,
    WorkflowStepOutcome,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_step_herdr import (
    ANCHOR_AMBIGUOUS,
    ANCHOR_MISSING,
    ANCHOR_RETIRED,
    ANCHOR_STORE_MISMATCH,
    ANCHOR_UNVERIFIED,
    ANCHOR_VERIFIED,
    REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
    WORKER_ABSENT,
    WORKER_AMBIGUOUS,
    WORKER_LIVE,
    WORKER_LOCATOR_MISSING,
    WORKER_UNAVAILABLE,
    classify_herdr_workflow_lane,
    resolve_herdr_workflow_step,
)


def _anchor_workspace_id(repo_root) -> Optional[str]:
    """The sender's own workspace segment for the anchor↔env gate (mirrors herdr_send_entry).

    Under the #13377 shared-project-workspace model a lane agent runs in a linked worktree
    whose segment resolves to the MAIN checkout's workspace identity; a standalone / main
    checkout resolves to its registry workspace_id. Legacy ``wt_<hash>`` lane attestation
    (pre-#13377 lanes still live during the transition) is accepted exactly when the env
    carries the worktree's deterministically re-derived token, never an arbitrary env value.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        herdr_workspace_segment,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        derive_lane_workspace_token,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        MOZYO_WORKSPACE_ID_ENV,
    )

    anchor_ws = herdr_workspace_segment(repo_root) or None
    env_ws = _norm(os.environ.get(MOZYO_WORKSPACE_ID_ENV))
    if env_ws and env_ws != (anchor_ws or ""):
        try:
            legacy_token = derive_lane_workspace_token(str(repo_root))
        except (OSError, ValueError):
            legacy_token = ""
        if legacy_token and env_ws == legacy_token:
            anchor_ws = legacy_token
    return anchor_ws


def _candidate_issue(repo_root, lane_id: str) -> tuple[str, str]:
    """The lane's candidate Redmine issue id, preserving record cardinality (F3b).

    The lane metadata store is **display metadata, never routing authority** — read here ONLY
    to name the *candidate* issue the source-of-truth Redmine read then verifies. Record
    cardinality is preserved (mid-review j#74785 F3b): the lane must have **exactly one**
    non-retired record. Returns ``(issue, "")`` on a single clean candidate, else
    ``("", fail_status)`` where ``fail_status`` is :data:`ANCHOR_AMBIGUOUS` (2+ records — a
    duplicate active or an active+retired stale coexistence), :data:`ANCHOR_RETIRED` (the sole
    record is a tombstone), or :data:`ANCHOR_MISSING` (no record / no issue / unreadable store).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        repo_scope_workspace_id,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        _norm_lane,
    )

    want_ws = _norm(repo_scope_workspace_id(repo_root))
    want_lane = _norm_lane(lane_id)
    if not want_ws:
        return "", ANCHOR_MISSING
    try:
        from mozyo_bridge.core.state.lane_metadata import load_lane_records

        records = load_lane_records()
    except Exception:  # noqa: BLE001 - an unreadable display store fails the anchor gate closed
        return "", ANCHOR_MISSING

    matching = [
        record
        for record in records.values()
        if _norm(getattr(record, "repo_workspace_id", "")) == want_ws
        and _norm_lane(getattr(record, "lane_id", "")) == want_lane
    ]
    if not matching:
        return "", ANCHOR_MISSING
    if len(matching) >= 2:
        # Duplicate active or active+retired stale coexistence: never collapse the drift.
        return "", ANCHOR_AMBIGUOUS
    record = matching[0]
    if getattr(record, "retired", False):
        return "", ANCHOR_RETIRED
    issue = _norm(getattr(record, "issue_id", ""))
    if not issue:
        return "", ANCHOR_MISSING
    return issue, ""


def _redmine_journal_source_for(args: argparse.Namespace):
    """The credential-gated live Redmine journal source (the source-of-truth read boundary).

    Reuses the existing :class:`LiveRedmineJournalSource` (Redmine #13289) — daemon-trusted
    credentials, redirect-refusing opener, injected transport, redacted errors — rather than
    reimplementing any credential / network layer (mid-review j#74784). Raises
    :class:`LiveRedmineJournalError` when credentials are unconfigured (the anchor gate then
    fails closed). ``args`` is accepted for a future ``--redmine-json`` snapshot override seam.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalSource,
    )

    return LiveRedmineJournalSource.from_environment()


def _verify_lane_gate_live(args: argparse.Namespace, issue: str) -> tuple[str, str]:
    """The verified ``(journal, gate)`` for ``issue`` from source-of-truth Redmine (F3a), or ``("","")``.

    Reads the candidate issue's journals live and extracts the **structured gate markers**
    (:func:`markers_from_source` — only gate-bearing kinds, from a machine ``[mozyo:…]`` token,
    never prose). Returns the latest gate marker's journal id + runtime gate **only** when its
    marker issue matches the candidate issue — so a forged / mismatched / non-gate / non-existent
    anchor never verifies. Any unconfigured-credential / transport / decode failure returns
    ``("", "")`` (the anchor gate fails closed; the underlying errors are already
    credential/URL-redacted).
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.live_redmine_journal_source import (
        LiveRedmineJournalError,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.redmine_journal_source import (
        markers_from_source,
    )

    issue = (issue or "").strip()
    if not issue:
        return "", ""
    try:
        source = _redmine_journal_source_for(args)
        markers = markers_from_source(source, issue)
    except LiveRedmineJournalError:
        return "", ""
    except Exception:  # noqa: BLE001 - any live-read failure fails the anchor gate closed
        return "", ""
    journal = ""
    gate = ""
    for marker in markers:
        if str(getattr(marker, "issue", "")).strip() != issue:
            continue  # issue mismatch: the gate marker is not this lane's issue
        candidate = str(getattr(marker, "journal", "")).strip()
        if candidate:
            journal = candidate  # latest gate marker (markers are note-ordered) wins
            gate = str(getattr(marker, "gate", "")).strip()
    return journal, gate


def _load_workflow_store(args: argparse.Namespace):
    """The persisted workflow runtime store (``--store-path`` or the home default), or ``None``.

    Read here ONLY as a caller-supplied advisory projection for the F3c cross-check — never the
    anchor authority. Returns ``None`` on any construction failure so an absent / unreadable
    store simply skips the cross-check (the live-authoritative path is unaffected).
    """
    try:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.cli_workflow_resume import (
            _store_from_args,
        )

        return _store_from_args(args)
    except Exception:  # noqa: BLE001 - a store construction failure just skips the cross-check
        return None


def _canonical_event_journal(event_id: str, issue: str) -> str:
    """The journal id of a canonical ``redmine:<issue>:<journal>`` / ``<issue>:<journal>`` anchor.

    Returns the journal **only** when the anchor's embedded issue equals ``issue`` (F3a canonical
    validation); a non-canonical / issue-mismatched / synthetic id yields "" so it never counts
    as this issue's gate anchor.
    """
    s = (event_id or "").strip()
    if s.startswith("redmine:"):
        s = s[len("redmine:"):]
    parts = s.split(":")
    if len(parts) != 2:
        return ""
    eid_issue, journal = parts[0].strip(), parts[1].strip()
    if not journal or eid_issue != (issue or "").strip():
        return ""
    return journal


def _store_lane_anchor(
    args: argparse.Namespace, workspace_id: str, lane_id: str
) -> "tuple[str, str, str] | None":
    """The advisory store's asserted ``(issue, journal, gate)`` for this lane, or ``None`` (F3c).

    Reads the caller-supplied workflow runtime store's ``(workspace_id, lane_id)`` route
    candidate + its canonical gate event as an **advisory cross-check** — never the authority.
    ``None`` means the store contributes no assertion for this lane (absent / unreadable / no
    route). A lane whose store routes name **two+ distinct issues** returns a sentinel issue
    (``"<ambiguous>"``) so the caller treats it as a mismatch. Empty ``journal`` / ``gate`` mean
    the store asserted a route issue but no canonical gate event.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        _norm,
        _norm_lane,
    )

    store = _load_workflow_store(args)
    if store is None or not store.path.exists():
        return None
    try:
        routes = store.read_route_identities()
        events = store.read_events()
    except Exception:  # noqa: BLE001 - an unreadable store contributes no cross-check assertion
        return None

    want_ws = _norm(workspace_id)
    want_lane = _norm_lane(lane_id)
    issues = {
        _norm(getattr(r, "issue", ""))
        for r in routes
        if _norm(getattr(r, "workspace_id", "")) == want_ws
        and _norm_lane(getattr(r, "lane_id", "")) == want_lane
        and _norm(getattr(r, "issue", ""))
    }
    if not issues:
        return None
    if len(issues) >= 2:
        return "<ambiguous>", "", ""
    issue = next(iter(issues))
    journal = ""
    gate = ""
    for event in events:
        if _norm(getattr(event, "issue", "")) != issue:
            continue
        candidate = _canonical_event_journal(getattr(event, "event_id", ""), issue)
        if candidate:
            journal = candidate
            gate = _norm(getattr(event, "gate", ""))
    return issue, journal, gate


def _store_contradicts(
    store_anchor: "tuple[str, str, str] | None", issue: str, journal: str, gate: str
) -> bool:
    """True when the advisory store asserts a *different* anchor for this lane (F3c).

    Only a **non-empty** store field that disagrees with the live-verified value is a
    contradiction — an absent store field (the store agreed on the issue but recorded no
    journal / gate) is not drift. ``None`` (no store assertion) never contradicts.
    """
    if store_anchor is None:
        return False
    s_issue, s_journal, s_gate = store_anchor
    if s_issue and s_issue != issue:
        return True
    if s_journal and s_journal != journal:
        return True
    if s_gate and gate and s_gate != gate:
        return True
    return False


def _resolve_lane_anchor(args: argparse.Namespace, workspace_id: str, repo_root, lane_id: str) -> tuple[str, str]:
    """Verify the lane's Redmine ``issue+journal`` anchor against source-of-truth Redmine (F3).

    Both the lane metadata and the workflow runtime store are **caller-supplied advisory
    projections**, not Redmine authority (mid-review j#74784): a caller can write either. So the
    anchor is verified against the **live source-of-truth Redmine** gate journal:

    1. the lane's single non-retired lane-metadata record names the *candidate* issue
       (:func:`_candidate_issue`, cardinality-preserving — duplicate / stale / missing fail
       closed);
    2. that issue's journals are read live via the credential-gated
       :class:`LiveRedmineJournalSource` and its **structured gate marker** is verified
       (:func:`_verify_lane_gate_live`): the exact gate journal id + gate, matching the candidate;
    3. the caller-supplied runtime store is cross-checked (F3c): if it asserts a *different*
       ``(issue, journal, gate)`` for this same lane, fail closed rather than trust the store.

    Returns (:data:`ANCHOR_VERIFIED`, ``redmine:issue=<id>:journal=<id>``) only when all hold;
    otherwise a fail-closed status (:data:`ANCHOR_AMBIGUOUS` / :data:`ANCHOR_RETIRED` /
    :data:`ANCHOR_MISSING` for the candidate, :data:`ANCHOR_UNVERIFIED` when the live Redmine read
    finds no matching gate marker, :data:`ANCHOR_STORE_MISMATCH` when the advisory store drifts).
    """
    issue, cand_status = _candidate_issue(repo_root, lane_id)
    if cand_status:
        return cand_status, ""
    journal, gate = _verify_lane_gate_live(args, issue)
    if not journal:
        return ANCHOR_UNVERIFIED, ""
    if _store_contradicts(_store_lane_anchor(args, workspace_id, lane_id), issue, journal, gate):
        return ANCHOR_STORE_MISMATCH, ""
    return ANCHOR_VERIFIED, WorkflowAnchor(issue=issue, journal=journal).pointer()


def _same_lane_worker_liveness(
    workspace_id: str, lane_id: str, *, env: Mapping[str, str]
) -> str:
    """The same-lane ``claude`` worker slot cardinality (mid-review j#74749 F2 / j#74750).

    Preserves 0 / 1 / 2+ and the usable-locator distinction from the live ``herdr agent list``
    inventory: :data:`WORKER_ABSENT` (0), :data:`WORKER_LIVE` (1 with a usable locator),
    :data:`WORKER_LOCATOR_MISSING` (1 without a locator), :data:`WORKER_AMBIGUOUS` (2+ =
    duplicate identity), :data:`WORKER_UNAVAILABLE` (the inventory could not be read). Pure over
    the decode of each row's mzb1 name — a duplicate is ambiguity, never a silently-picked target.
    """
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (
        list_herdr_agent_rows,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_lifecycle import (
        WORKER_ROLE,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
        HerdrSessionStartError,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (
        AGENT_KEY_NAME,
        _agent_locator,
        _norm_lane,
        decode_assigned_name,
    )

    try:
        rows = list_herdr_agent_rows(env)
    except HerdrSessionStartError:
        return WORKER_UNAVAILABLE
    want_lane = _norm_lane(lane_id)
    present = 0
    with_locator = 0
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        decode = decode_assigned_name(row.get(AGENT_KEY_NAME))
        if not decode.ok or decode.identity is None:
            continue
        identity = decode.identity
        if identity.workspace_id != workspace_id or identity.role != WORKER_ROLE:
            continue
        if _norm_lane(identity.lane_id) != want_lane:
            continue
        present += 1
        if _agent_locator(row):
            with_locator += 1
    if present == 0:
        return WORKER_ABSENT
    if present >= 2:
        return WORKER_AMBIGUOUS
    return WORKER_LIVE if with_locator == 1 else WORKER_LOCATOR_MISSING


def resolve_herdr_step_outcome(args: argparse.Namespace) -> WorkflowStepOutcome:
    """Resolve the herdr-native ``workflow step`` outcome for the current lane (Redmine #13489).

    Resolves the sender identity from launch env + the repo anchor (fail-closed on an
    unattested identity), classifies the workflow lane role, verifies the lane's Redmine issue
    anchor (worker / gateway), and — for a sublane gateway lane — reads the live inventory for
    its same-lane worker cardinality, then delegates to the pure resolver. Never mutates a lane
    or delivers anything (increment 1 is resolution-only).
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (
        resolve_sender_identity,
    )

    repo_root = repo_root_from_args(args)
    anchor_ws = _anchor_workspace_id(repo_root)
    sender_res = resolve_sender_identity(os.environ, anchor_workspace_id=anchor_ws)
    if not sender_res.ok or sender_res.identity is None:
        # An unattested herdr identity is not a workflow-step origin. Name the herdr-native
        # cause and the one sanctioned lane-dispatch route (mirrors herdr_send_entry).
        return WorkflowStepOutcome(
            state=STATE_LANE_UNRESOLVED,
            next_action=(
                "resolve the herdr lane identity before stepping: this shell carries no "
                "attested launch-time lane-sender identity (MOZYO_WORKSPACE_ID / "
                "MOZYO_AGENT_ROLE). Run workflow step from inside an attested herdr lane "
                "agent, or dispatch lanes through the coordinator (coordinator -> "
                "target-lane Codex gateway -> same-lane Claude worker). See "
                "vibes/docs/specs/herdr-native-identity.md."
            ),
            execution=EXECUTION_BLOCKED,
            reason=REASON_HERDR_SENDER_IDENTITY_UNRESOLVED,
            next_owner=OWNER_OPERATOR,
            primitive=PRIMITIVE_NONE,
            repo_root=str(repo_root),
            durable_anchor="none",
            detail=f"sender identity unresolved ({sender_res.reason}): {sender_res.detail}",
        )

    sender = sender_res.identity
    lane = classify_herdr_workflow_lane(
        provider=sender.role,
        lane_id=sender.lane_id,
        repo_root=str(repo_root),
    )

    # A worker / gateway lane is anchor-gated (j#74748 F3); default-lane / unknown provider
    # fails closed in the pure resolver without any store / inventory read.
    anchor_status: Optional[str] = None
    anchor_pointer = ""
    worker_liveness: Optional[str] = None
    if lane.caller_role in (ROLE_IMPLEMENTATION_WORKER, ROLE_DELEGATED_COORDINATOR):
        anchor_status, anchor_pointer = _resolve_lane_anchor(
            args, sender.workspace_id, repo_root, sender.lane_id
        )
    if lane.caller_role == ROLE_DELEGATED_COORDINATOR and anchor_status == ANCHOR_VERIFIED:
        # Only read the live inventory when the gateway lane actually reaches the worker gate.
        worker_liveness = _same_lane_worker_liveness(
            sender.workspace_id, sender.lane_id, env=os.environ
        )

    return resolve_herdr_workflow_step(
        lane,
        worker_liveness=worker_liveness,
        anchor_status=anchor_status,
        anchor_pointer=anchor_pointer,
    )


__all__ = ("resolve_herdr_step_outcome",)
