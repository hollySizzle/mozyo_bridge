"""agents command family — OOP-first boundary, attention-projection tranche (Redmine #12749 / #12638).

Second OOP-first conversion tranche for the ``application/commands.py``
decomposition (after the tmux-config family). It carries the ``agents
attention-project`` command into the policy's object boundaries
(``vibes/docs/logics/object-oriented-architecture-policy.md``), behavior-preserving:

- **Port/Adapter**: the tmux pane-option *write* side effect goes through the
  injected :class:`~mozyo_bridge.application.tmux_option_port.TmuxOptionWriterPort`
  instead of the old naked ``run_tmux(*argv, check=False)`` loop.
- **Use case**: :class:`ProjectAttentionUseCase` owns the
  derive-attention → build-plan → best-effort-apply state transition over the
  discovered candidates, returning typed entries; it has no presentation or
  ``argparse`` dependency and is unit-tested with a fake writer port.
- **Value object**: :class:`AttentionProjectionEntry` (frozen) replaces the ad-hoc
  ``(candidate, record, plan, applied_ok)`` tuple the procedural handler threaded
  between its apply loop and its two render branches.
- **Thin command handler**: :func:`cmd_agents_attention_project` resolves the CLI
  flags, runs discovery, drives the use case with a live writer, and renders text
  / JSON. It holds no external boundary directly beyond the tmux availability
  guard.

Subsequent tranches added to this module: the read-discovery boundary
(:class:`ResolveAgentTargetsUseCase` over the
:class:`~mozyo_bridge.application.agent_discovery_port.AgentDiscoveryPort`), the
``agents list`` / ``agents targets`` render handlers (:func:`cmd_agents_list` /
:func:`cmd_agents_targets`), and the legacy ``list`` pane dump (:func:`cmd_list`),
all moved here behavior-preserving.

Compatibility: ``commands.py`` re-exports :func:`cmd_agents_attention_project`,
:func:`cmd_agents_list`, :func:`cmd_agents_targets`, :func:`cmd_list` and
:func:`_attention_for_candidate` so the ``mozyo_bridge.application.commands.*``
identities (cli / cli_agents / cli_core parser registrars; the
``commands._attention_for_candidate`` import a discovery test relies on) are
unchanged. The thin ``commands._agents_target_candidates`` wrapper stays in
``commands.py`` as the shared discovery seam the delegated-coordinator /
project-gateway callers and their tests import; this module's handlers reach it
through :func:`_discover_candidates` (a call-time import) so those patch points are
preserved. The handlers call the tmux availability guard ``require_tmux`` (and
``pane_lines`` for :func:`cmd_list`) bound in this module, so their tests patch
``commands_agents.require_tmux`` / ``commands_agents.pane_lines``. Relocating the
residual ``commands``-owned leaf reads (``resolve_canonical_session`` /
``_probe_checkout_facts``) out of ``commands.py`` stays carried to #12638 / #12785.
"""

from __future__ import annotations

import argparse
import json as _json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional, Sequence

from mozyo_bridge.application.agent_discovery_port import (
    AgentDiscoveryPort,
    LiveAgentDiscovery,
)
from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.application.attention_projection import build_attention_option_plan
from mozyo_bridge.application.tmux_option_port import (
    LiveTmuxOptionWriter,
    TmuxOptionWriterPort,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
    build_target_candidates,
    discover_agents,
    filter_agents,
    fold_agents_by_pane,
)
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
    pane_lines,
    require_tmux,
)
from mozyo_bridge.shared.errors import die
from mozyo_bridge.workspace_registry import resolve_canonical_session


class ResolveAgentTargetsUseCase:
    """Resolve discovered agent panes into classified ``TargetRecord`` candidates.

    The shared read pipeline behind ``agents targets`` and the attention
    projection (#11811 / #11907): ``discover`` → ``fold_agents_by_pane`` (with
    registry-resolved canonical session) → ``filter`` → ``build_target_candidates``
    with workspace / branch / project-scope resolvers. The four external reads are
    taken through the injected :class:`AgentDiscoveryPort`, so this orchestration
    is decoupled from live tmux / registry / git / project-discovery and is
    unit-testable with a fake port. Pure classification stays in the domain.

    Behavior-preserving relative to the former ``_agents_target_candidates``: the
    canonical-session lookup is cached and shared between the fold's
    ``resolve_canonical`` and the workspace resolver; the branch probe is cached
    per repo root; project-scope resolution is fail-soft (the adapter degrades to
    ``None``). ``commands._agents_target_candidates`` is now a thin wrapper around
    this use case with a :class:`LiveAgentDiscovery`.
    """

    def __init__(self, discovery: AgentDiscoveryPort) -> None:
        self._discovery = discovery

    def resolve(self, *, agent_filter, session_filter, snapshot=None) -> list:
        # Validate against the injected snapshot's vocabulary (Redmine #13569 R2-F1), so a
        # synthetic provider the composition injected is accepted here; `None` uses built-in.
        from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E501
            agent_kinds,
        )

        known = agent_kinds(snapshot)
        if agent_filter is not None and agent_filter not in known:
            die(f"--agent must be one of {sorted(known)}; got {agent_filter!r}")

        canonical_cache: dict[str, object] = {}

        def _canonical(repo_root: str):
            if repo_root not in canonical_cache:
                canonical_cache[repo_root] = self._discovery.canonical_session(repo_root)
            return canonical_cache[repo_root]

        records = filter_agents(
            fold_agents_by_pane(
                self._discovery.discover(),
                resolve_canonical=lambda root: _canonical(root).name,
            ),
            session=session_filter,
            agent_kind=agent_filter,
        )

        def resolve_workspace(repo_root: str):
            canon = _canonical(repo_root)
            return (getattr(canon, "workspace_id", None), getattr(canon, "name", None))

        branch_cache: dict[str, str | None] = {}

        def resolve_branch(repo_root: str):
            if repo_root not in branch_cache:
                branch_cache[repo_root] = self._discovery.checkout_facts(repo_root).get(
                    "branch"
                )
            return branch_cache[repo_root]

        def resolve_project(repo_root: str | None, cwd: str):
            scope = self._discovery.project_scope(cwd, repo_root)
            if scope is None:
                return None
            return (scope.scope, scope.path, scope.label)

        return build_target_candidates(
            records,
            resolve_workspace=resolve_workspace,
            resolve_branch=resolve_branch,
            resolve_project=resolve_project,
        )


# Reason code for the conservative pre-wiring attention projection (#11952):
def _attention_for_candidate(candidate, observed_at: str):
    """Derive a conservative :class:`AttentionRecord` for one target (#11952).

    First read-only exposure of the #11951 attention read model. No durable
    attention source is wired yet, so this never fabricates an
    owner/review/blocked/stalled signal: it only distinguishes a cleanly
    identified target (``healthy``, reason ``no_attention_source``) from one
    whose identity itself is ambiguous / unreadable (``unknown``). Later
    extraction tasks feed real durable / observed signals into the same pure
    :func:`derive_attention`; this stays an additive projection and is never used
    for routing / target selection. Delegates to the shared
    :func:`~mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention.conservative_attention` so this and the
    cockpit ``/api/units`` join (#12007) cannot drift.
    """
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
        CONFIDENCE_NONE,
        ROLE_SOURCE_UNKNOWN,
    )
    from mozyo_bridge.e_120_operations_cockpit.f_150_attention_freshness_projection.domain.attention import (
        ROLE_CLAUDE,
        ROLE_CODEX,
        conservative_attention,
    )

    identity_readable = (
        candidate.role in (ROLE_CLAUDE, ROLE_CODEX)
        and candidate.confidence != CONFIDENCE_NONE
        and candidate.role_source != ROLE_SOURCE_UNKNOWN
    )
    return conservative_attention(
        observed_at=observed_at,
        role=candidate.role,
        identity_readable=identity_readable,
        contradictory=bool(candidate.ambiguous),
        host=candidate.host or "local",
        workspace_id=candidate.workspace_id or "",
        lane_id=candidate.lane_id or "default",
        pane_id=candidate.pane_id,
    )


@dataclass(frozen=True)
class AttentionProjectionEntry:
    """Per-target outcome of the attention projection.

    ``applied_ok`` is ``None`` in preview (no write attempted), and ``True`` /
    ``False`` once a write was attempted (``False`` if any of the pane's
    ``set-option`` writes failed — best-effort, never raised).
    """

    pane_id: Optional[str]
    attention: object
    plan: tuple[tuple[str, ...], ...]
    applied_ok: Optional[bool]


class ProjectAttentionUseCase:
    """Derive attention for each candidate and best-effort project it via a port.

    The tmux pane-option write side effect is delegated to the injected
    :class:`TmuxOptionWriterPort`, so the apply decision (preview vs. write, and
    the best-effort failure posture) is unit-testable with a fake writer.
    """

    def __init__(self, writer: TmuxOptionWriterPort) -> None:
        self._writer = writer

    def execute(
        self, candidates, observed_at: str, *, apply: bool
    ) -> list[AttentionProjectionEntry]:
        entries: list[AttentionProjectionEntry] = []
        for candidate in candidates:
            record = _attention_for_candidate(candidate, observed_at)
            plan = build_attention_option_plan(candidate.pane_id, record)
            applied_ok: Optional[bool] = None
            if apply:
                # Apply happens once here, before any caller render branch, so
                # `--json --apply` and text `--apply` perform identical writes and
                # both report the true outcome (Redmine #11954 review #58539).
                applied_ok = True
                for argv in plan:
                    if not self._writer.set_option(argv):
                        # Best-effort: a failed option write is recorded, not
                        # raised (projection-cache posture); the run still finishes.
                        applied_ok = False
            entries.append(
                AttentionProjectionEntry(
                    pane_id=candidate.pane_id,
                    attention=record,
                    plan=tuple(tuple(argv) for argv in plan),
                    applied_ok=applied_ok,
                )
            )
        return entries


def _discover_candidates(args: argparse.Namespace) -> list:
    # The shared discovery pipeline still lives in ``commands`` (residual; see
    # module docstring). Resolve it at call time so a test that patches
    # ``commands._agents_target_candidates`` (or the discovery deps it reads on
    # ``commands``) still intercepts the call.
    from mozyo_bridge.application.commands import _agents_target_candidates

    return _agents_target_candidates(args)


def cmd_agents_attention_project(args: argparse.Namespace) -> int:
    """Project derived attention onto tmux pane user options (Redmine #11954).

    Writes a re-derivable **projection cache** of the #11951 ``AttentionRecord``
    (derived conservatively, #11952) onto each discovered target's tmux pane user
    options: ``@mozyo_attention_state`` / ``@mozyo_attention_severity`` /
    ``@mozyo_attention_reason`` / ``@mozyo_attention_updated_at``.

    Boundaries:

    - **Projection cache only.** The source of truth stays the durable state /
      the ``derive_attention`` read model; these user options are a cache that
      can be deleted and re-derived. They are never consulted for routing /
      handoff preflight / target resolution.
    - **Safe by default.** Default is a preview (no tmux mutation) that prints
      the exact ``set-option`` plan per pane; ``--apply`` performs the writes
      best-effort (a failed option write never aborts the run, like other
      best-effort projections). ``--dry-run`` forces preview and wins over
      ``--apply``.
    - **No color / ``agent-ui.conf`` / iTerm changes** here — this task only
      writes the machine-readable user options; rendering them is a later task.
    """
    require_tmux()
    candidates = _discover_candidates(args)

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    # Safe default: preview unless --apply is given; --dry-run always wins.
    apply = bool(getattr(args, "apply", False)) and not bool(
        getattr(args, "dry_run", False)
    )

    entries = ProjectAttentionUseCase(LiveTmuxOptionWriter()).execute(
        candidates, observed_at, apply=apply
    )

    if getattr(args, "as_json", False):
        payload = [
            {
                "pane_id": entry.pane_id,
                "attention": entry.attention.as_payload(),
                "applied": apply,
                "applied_ok": entry.applied_ok,
                "plan": [list(argv) for argv in entry.plan],
            }
            for entry in entries
        ]
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    if not entries:
        print("no agent targets discovered; nothing to project")
        return 0

    for entry in entries:
        record = entry.attention
        label = (
            f"{entry.pane_id or '-'} {record.attention_state}/{record.severity} "
            f"({record.reason_code})"
        )
        if not apply:
            print(f"(dry-run) {label}")
            for argv in entry.plan:
                print("  tmux " + " ".join(argv))
            continue
        print(
            f"projected {label}"
            if entry.applied_ok
            else f"warning: partial projection {label}"
        )
    return 0


def cmd_agents_list(args: argparse.Namespace) -> int:
    """Cross-session agent discovery surface (Redmine #10332, #11628).

    Emits one row per ``pane_id`` — the agent identity key (Redmine #11628):
    a pane that belongs to several grouped tmux sessions is ONE agent whose
    memberships are folded into ``views``; the top-level session / window
    fields describe the canonical view (the session matching the workspace's
    canonical session name, resolved registry → anchor → derivation). Each
    row carries the structured fields a sender needs to name an explicit
    cross-workspace handoff target: session, window name and index, pane id
    and index, active flag, classified ``agent_kind`` (``claude`` /
    ``codex`` / ``unknown``), foreground process, inferred ``repo_root``
    (walked up via REPO_ROOT_MARKERS from the pane's ``cwd``), the pane's
    ``cwd``, and an ``ambiguous`` flag when any view's ``(session,
    window_name)`` pair spans multiple windows in its session. ``--session``
    matches the canonical session or any grouped view.

    Read-only. Does not change tmux state, does not interact with
    Asana / Redmine, and is intentionally separate from the legacy ``list`` /
    ``status`` surfaces so existing scripts that scrape those outputs keep
    working. Single tmux server assumed; a multi-server deployment would
    key on ``(socket, pane_id)``.
    """
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
        agent_kinds,
        fold_agents_by_pane,
    )

    require_tmux()
    # The runtime handler validates and classifies against the SAME snapshot the parser
    # composition injected (Redmine #13569 R2-F1), threaded through `args.snapshot`, so a
    # synthetic provider accepted by `--agent` is also recognized here — not re-rejected
    # against a fixed built-in set. `None` uses the built-in providers, byte-identical.
    snapshot = getattr(args, "snapshot", None)
    known = agent_kinds(snapshot)
    agent_filter = getattr(args, "agent", None)
    if agent_filter is not None and agent_filter not in known:
        die(f"--agent must be one of {sorted(known)}; got {agent_filter!r}")
    session_filter = getattr(args, "session", None)
    records = filter_agents(
        fold_agents_by_pane(
            discover_agents(snapshot=snapshot),
            resolve_canonical=lambda root: resolve_canonical_session(root).name,
        ),
        session=session_filter,
        agent_kind=agent_filter,
    )
    if getattr(args, "as_json", False):
        payload = [record.to_dict() for record in records]
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    print(
        "SESSION\tWINDOW\tIDX\tPANE\tACTIVE\tKIND\tROLE_SOURCE\tCONFIDENCE\t"
        "PROCESS\tREPO_ROOT\tCWD\tAMBIGUOUS\tOTHER_VIEWS"
    )
    for record in records:
        other_views = ",".join(
            view.session for view in record.views if not view.canonical
        )
        print(
            "\t".join(
                [
                    record.session or "-",
                    record.window_name or "-",
                    record.window_index or "-",
                    record.pane_id or "-",
                    "1" if record.pane_active else "0",
                    record.agent_kind,
                    record.role_source,
                    record.confidence,
                    record.process or "-",
                    record.repo_root or "-",
                    record.cwd or "-",
                    "1" if record.ambiguous else "0",
                    other_views or "-",
                ]
            )
        )
    return 0


def _scan_progress_note(event) -> None:
    """Render one project-scope scan progress event as a stderr note (#12985).

    The bounded per-root live scan behind project-scope resolution used to run
    silently, so a large root looked like a hang for 30s+. This prints the
    minimal operator-facing progress the issue asks for: one line when a live
    scan starts, a single still-scanning line if it runs past the slow
    threshold, and a completion line only for a scan that was slow enough to
    have warranted one. Memoized (cache-hit) lookups emit no events, so the
    fast path stays quiet. stderr only — the parseable stdout table / ``--json``
    payload never changes shape here.
    """
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
        DEFAULT_SLOW_SCAN_NOTICE_SECONDS,
        SCAN_PROGRESS_DONE,
        SCAN_PROGRESS_SLOW,
        SCAN_PROGRESS_START,
    )

    if event.kind == SCAN_PROGRESS_START:
        print(
            f"note: scanning project scopes under {event.repo_root} "
            "(first scan in this process; result is cached) ...",
            file=sys.stderr,
        )
    elif event.kind == SCAN_PROGRESS_SLOW:
        print(
            f"note: still scanning project scopes under {event.repo_root} "
            f"({event.elapsed_seconds:.0f}s elapsed) ...",
            file=sys.stderr,
        )
    elif (
        event.kind == SCAN_PROGRESS_DONE
        and event.elapsed_seconds >= DEFAULT_SLOW_SCAN_NOTICE_SECONDS
    ):
        print(
            f"note: project scope scan finished under {event.repo_root} "
            f"({event.elapsed_seconds:.0f}s, {event.adopted_count} scopes adopted)",
            file=sys.stderr,
        )


def _emit_herdr_backend_note(args: argparse.Namespace) -> None:
    """Print a ``herdr backend active`` demotion note for ``agents targets`` (Redmine #13446).

    ``agents targets`` lists the tmux discovery pool; under ``terminal_transport.backend:
    herdr`` that pool is empty and the surface reads as a dead tmux-era listing — the same
    harness gap (#13435 j#74176 -> j#74177) where an agent reaches for tmux selection while
    the herdr workspace's agents are live. Emit a stderr note that names this a tmux-era
    primitive/debug surface and points at the standard ``sublane create --execute`` dispatch,
    but do not fail: the command stays a read-only listing. Guarded by
    :func:`herdr_backend_active`, so the tmux-backend stdout / stderr stays byte-identical.
    """
    from mozyo_bridge.application.commands_common import repo_root_from_args
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_entrypoint_preflight import (
        herdr_backend_active,
        herdr_backend_guidance,
    )

    try:
        repo_root = repo_root_from_args(args)
    except (OSError, ValueError):
        return
    if not herdr_backend_active(repo_root):
        return
    print(f"note: {herdr_backend_guidance()}", file=sys.stderr)


def cmd_agents_targets(args: argparse.Namespace) -> int:
    """Canonical handoff-target projection for LLM / operator use (#11811, #11907).

    Read-only. Prints the classified agent panes as candidate handoff targets
    with just the fields needed to pick an explicit ``pane_id`` without parsing
    titles: role + resolver provenance (``role_source`` / ``confidence`` /
    ``ambiguous``, #11822), workspace id + label, checkout lane (#11820), a short
    repo identifier, current branch, liveness, location, and the projection
    ``view_kind`` (``cockpit_pane`` / ``normal_window``, #11907) so normal local
    and cockpit targets read with one ``TargetRecord`` vocabulary. Text keeps the
    original column order and appends ``VIEW_KIND`` / ``BRANCH`` and the additive
    ``ATTENTION`` / ``REASON`` columns (#11952); ``--json`` renders the nested
    canonical ``TargetRecord`` projection (host / runtime / identity / repo /
    view) plus an additive ``attention`` record — a projection, never a saved
    file. Attention is a conservative read-only projection (#11951 read model):
    no durable attention source is wired yet, so a cleanly-identified target
    derives ``healthy`` (reason ``no_attention_source``) and an ambiguous /
    unreadable one derives ``unknown`` — never a fabricated owner/review signal,
    and never used for routing. Builds on the same
    ``discover_agents`` → ``fold_agents_by_pane`` pipeline as ``agents list`` so
    the two never drift, and resolves workspace identity through the registry →
    anchor → derivation chain. Compact text hides absolute paths (basename
    only); ``--json`` carries ``repo_root`` / ``cwd`` (the exposure ``agents
    list`` already allows). Listing is non-selecting: same-role candidates stay
    distinguishable by workspace / lane / pane and the caller must name the
    explicit pane, so a natural name never auto-crosses a safety boundary. Live
    tmux remains the liveness source (``active``); registry / anchor are
    identity hints only.
    """
    require_tmux()
    # herdr-backend demotion note (#13446): read-only, stderr-only, tmux byte-invariant.
    _emit_herdr_backend_note(args)

    # Silent-hang fix (#12985): the per-root bounded project-scope scan behind
    # discovery can walk a large root for 30s+. Install the stderr note listener
    # only around this command's discovery pass, so the cockpit / handoff shared
    # paths stay silent and the memoized cache-hit path emits nothing.
    from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application.project_discovery import (
        scan_progress,
    )

    with scan_progress(_scan_progress_note):
        candidates = _discover_candidates(args)

    # Delegated-coordinator-tree display projection (#12466), consuming the
    # closed #12465 `delegation_projection` foundation. Derived once across all
    # candidates because depth / root are a function of the whole parent chain;
    # display-only and never a routing key. JSON gains a `delegation` record per
    # target, text appends KIND / DEPTH / PARENT columns.
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.delegation_display import (
        delegation_cells,
        derive_targets_delegation,
    )

    delegation_map = derive_targets_delegation(candidates)

    # Desired delegated-coordinator window-separation policy (#12467), an additive
    # `delegation_window` JSON projection alongside the #12466 `delegation` record
    # (which stays byte-identical). `delegation_window_policy` is a repo-local
    # display preference, so it is read per distinct repo (memoized) from
    # `.mozyo-bridge/config.yaml` and resolved per candidate against the #12466
    # breadcrumb. Display-only and fail-soft: any load / parse failure falls back
    # to the documented default (`shared`, #13085) and never blocks this read-only
    # table, and the resolved fields are never folded into the canonical
    # `TargetRecord` routing projection (`to_dict`).
    from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.presentation_grouping import (
        DEFAULT_DELEGATION_WINDOW_POLICY,
        resolve_delegation_window_display,
    )

    _window_policy_by_repo: dict[object, str] = {}

    def _delegation_window_policy_for(repo_root: object) -> str:
        if repo_root in _window_policy_by_repo:
            return _window_policy_by_repo[repo_root]
        policy = DEFAULT_DELEGATION_WINDOW_POLICY
        if repo_root:
            try:
                from mozyo_bridge.application.repo_local_config_loader import (
                    load_repo_local_config,
                )

                policy = (
                    load_repo_local_config(repo_root)
                    .presentation.grouping.delegation_window_policy
                )
            except Exception:  # noqa: BLE001 - fail-soft read-only display
                policy = DEFAULT_DELEGATION_WINDOW_POLICY
        _window_policy_by_repo[repo_root] = policy
        return policy

    def _delegation_window_payload(candidate) -> dict:
        breadcrumb = delegation_map[candidate.pane_id]
        unit = f"{candidate.workspace_id or ''}/{candidate.lane_id or ''}"
        return resolve_delegation_window_display(
            _delegation_window_policy_for(candidate.repo_root),
            lane_kind=breadcrumb.lane_kind,
            delegation_depth=breadcrumb.delegation_depth,
            delegation_unit=unit,
            delegation_root=breadcrumb.delegation_root,
            status=breadcrumb.status,
        ).as_payload()

    # Live project-gateway lane identity projection (#12708): the visible
    # distinction the GK3500 smoke (#12698) lacked between the Cloud Drive
    # project gateway and the GK3500 department root. Derived purely from each
    # candidate's already-resolved identity (role provenance + #12658 project
    # scope), display-only and never folded into the canonical `TargetRecord`
    # routing projection (`to_dict`). JSON gains a `gateway` record per target,
    # text appends a TARGET_KIND column.
    from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.project_gateway_identity import (
        classify_target_kind,
        gateway_projection,
    )

    # Single observation timestamp for this read; the pure attention read model
    # is clock-free (caller-supplied `observed_at`), so the I/O layer stamps it
    # here once. Attention is an additive projection (#11952): JSON gains an
    # `attention` key per target, text appends ATTENTION / REASON columns.
    from datetime import datetime, timezone

    observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if getattr(args, "as_json", False):
        payload = [
            {
                **candidate.to_dict(),
                "attention": _attention_for_candidate(candidate, observed_at).as_payload(),
                "delegation": delegation_map[candidate.pane_id].as_payload(),
                "delegation_window": _delegation_window_payload(candidate),
                "gateway": gateway_projection(candidate),
            }
            for candidate in candidates
        ]
        print(_json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    # Compatibility-preserving text projection (#11907, #11952, #12466): the
    # original column run (PANE..WINDOW) keeps its order so existing parsers stay
    # valid; VIEW_KIND / BRANCH (#11907), ATTENTION / REASON (#11952) and the
    # delegation KIND / DEPTH / PARENT breadcrumb (#12466) are appended. KIND /
    # DEPTH / PARENT are a derived projection, never a routing key.
    print(
        "PANE\tROLE\tROLE_SOURCE\tCONF\tAMBIG\tWORKSPACE\tLANE\tREPO\tACTIVE\t"
        "SESSION\tWINDOW\tVIEW_KIND\tBRANCH\tATTENTION\tREASON\tKIND\tDEPTH\tPARENT\t"
        # Project-scoped cockpit identity (#12658). Appended after the existing
        # run so the workspace (department) identity and the project scope are
        # visible simultaneously; `PROJECT` / `PROJECT_PATH` are `-` for a
        # single-repo workspace pane, preserving display compatibility.
        "PROJECT\tPROJECT_PATH\t"
        # Live project-gateway lane identity (#12708). The final column names the
        # derived gateway target_kind so a project gateway (`project_gateway`) is
        # told apart from the department root (`workspace_root`), a worker
        # (`worker`), or an unbindable pane (`unknown`) — the distinction the
        # GK3500 smoke needed. Derived projection, never a routing key.
        "TARGET_KIND"
    )
    for c in candidates:
        lane = c.lane_id if not c.lane_label else f"{c.lane_id}({c.lane_label})"
        attention = _attention_for_candidate(c, observed_at)
        kind_cell, depth_cell, parent_cell = delegation_cells(
            delegation_map.get(c.pane_id)
        )
        # Show the human label when present, else the redmine project id; the
        # repo-relative project path rides in its own column (never an absolute
        # private path).
        project_cell = c.project_label or c.project_scope or "-"
        print(
            "\t".join(
                [
                    c.pane_id or "-",
                    c.role,
                    c.role_source,
                    c.confidence,
                    "1" if c.ambiguous else "0",
                    c.workspace_label or c.workspace_id or "-",
                    lane,
                    c.repo_short or "-",
                    "1" if c.active else "0",
                    c.session or "-",
                    c.window_name or "-",
                    c.view_kind,
                    c.branch or "-",
                    attention.attention_state,
                    attention.reason_code,
                    kind_cell,
                    depth_cell,
                    parent_cell,
                    project_cell,
                    c.project_path or "-",
                    classify_target_kind(c),
                ]
            )
        )
    return 0


def cmd_list(_: argparse.Namespace) -> int:
    """Legacy flat pane dump (the original ``list`` surface).

    Read-only TARGET/LOCATION/PROCESS/WINDOW/CWD table over the live tmux panes.
    Kept verbatim and re-exported as ``commands.cmd_list`` so existing scripts that
    scrape this output and the ``list`` parser binding are unchanged; superseded
    for agent routing by ``agents list`` / ``agents targets`` but retained for
    compatibility.
    """
    require_tmux()
    print("TARGET\tLOCATION\tPROCESS\tWINDOW\tCWD")
    for pane in pane_lines():
        print(
            "\t".join(
                [
                    pane["id"],
                    pane["location"],
                    pane["command"],
                    pane.get("window_name") or "-",
                    pane["cwd"],
                ]
            )
        )
    return 0
