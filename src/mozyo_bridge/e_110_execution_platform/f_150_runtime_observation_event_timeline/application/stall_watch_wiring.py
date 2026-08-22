"""Production wiring for the stall-watch leg (Redmine #15855).

The factory the supervisor's composition root calls to obtain its ``stall_watch_leg_fn``.
Every collaborator the leg needs is resolved here and nowhere else, so
:mod:`...application.stall_watch_leg` stays a pure composition of injected seams and the
supervisor keeps no dependency on this feature.

Two things this module is responsible for beyond plumbing.

**The lane join comes from one authority.** ``generation_for`` and ``issue_for`` are both
read from the same :class:`LaneLifecycleStore` snapshot, taken once per pass. Resolving them
from two places — or from two reads of one place — would let a lane's generation and its
issue anchor come from different instants, which is exactly how a stall gets escalated onto
the issue a lane used to be working on. Only rows in this workspace with an **active**
disposition contribute: a superseded or retired lane is not something to watch or to write
about.

**Everything degrades to "watch nothing", never to "guess".** A missing config, an
unreadable lifecycle store, an unresolvable herdr binary and an unset write opt-in each
resolve to a leg that observes less (or writes less) rather than one that infers more. The
opt-in in particular is load-bearing: with ``MOZYO_REDMINE_DELIVERY_WRITE`` unset the
transport is ``None``, the canonical writer refuses with ``write_optin_unset``, and the
firing stays visible in the local pending queue instead of being silently dropped.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from mozyo_bridge.core.state.stall_escalation import StallEscalationStore
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_body_marker import (  # noqa: E501
    default_body_marker_resolver,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.stall_watch_leg import (  # noqa: E501
    build_journal_writer,
    run_stall_watch_leg,
)
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.stall_watch_policy import (  # noqa: E501
    POLICY_CONFIG_UNREADABLE,
    POLICY_INVALID,
    StallWatchPolicy,
)

#: Lane dispositions whose units are worth watching. A superseded / retired / hibernated
#: lane is not a stall candidate: nothing is expected to be progressing there.
WATCHABLE_DISPOSITIONS: frozenset[str] = frozenset({"active"})


#: How much of a loader error is carried into the policy detail. The text comes from this
#: repo's own validators ("stall_watch.cadence_seconds must be an integer, not str"), so it
#: names keys and types rather than values — but it is truncated anyway, because a detail
#: string ends up in an operator-facing status surface and an unbounded error message is not
#: something this layer should promise to keep small.
POLICY_DETAIL_LIMIT = 300

#: The marker that says a loader error was about THIS block rather than a sibling.
_STALL_WATCH_ERROR_MARKER = "stall_watch"


def _detail(exc: BaseException) -> str:
    text = " ".join(str(exc).split())
    return text[:POLICY_DETAIL_LIMIT]


def resolve_stall_watch_policy(repo_root: object) -> StallWatchPolicy:
    """Read the workspace's ``stall_watch`` policy, degrading to "watch nothing".

    Every failure watches nothing, but they are **not** the same off-state, and j#110121-2
    asks for a *typed* no-op rather than merely a silent one. Three outcomes are kept
    distinct because they need three different operator actions:

    - **absent** — no block is declared. Nothing to do; this is the shipped default.
    - **invalid** (:data:`POLICY_INVALID`) — this block is malformed. Fix the block; the
      detail names the offending key.
    - **unreadable** (:data:`POLICY_CONFIG_UNREADABLE`) — the config could not be read at
      all, or a *different* block is invalid. Fix the file; the stall-watch block may be
      perfectly fine.

    An earlier version collapsed all three into ``absent`` (review j#110132 finding_4),
    which told an operator with a mistyped cadence that they had simply never configured
    anything.
    """
    try:
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        return load_repo_local_config(Path(str(repo_root))).stall_watch
    except Exception as exc:  # noqa: BLE001 - every failure still watches nothing
        # The loader raises one error type for ANY invalid block, so the block that broke it
        # is identified from the message the loader itself composed
        # (``f"stall_watch config is invalid: {exc}"``). A sibling block's error must not be
        # reported as *this* block being malformed.
        detail = _detail(exc)
        if _STALL_WATCH_ERROR_MARKER in detail:
            return StallWatchPolicy.disabled(POLICY_INVALID, detail)
        return StallWatchPolicy.disabled(POLICY_CONFIG_UNREADABLE, detail)


def lane_facts_snapshot(lifecycle_store: object, workspace_id: str) -> dict[str, tuple[str, str]]:
    """``{lane_id: (generation, issue)}`` for this workspace's watchable lanes.

    One snapshot per pass, so a lane's generation and its issue anchor are always read from
    the same instant. A row missing either is simply absent from the map, which makes the
    discovery join drop that unit — the fail-closed direction (#15855 j#110121-4).
    """
    out: dict[str, tuple[str, str]] = {}
    try:
        records = lifecycle_store.records()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - an unreadable lifecycle store watches nothing
        return out
    ws = str(workspace_id or "").strip()
    for record in records or ():
        if str(getattr(record, "repo_workspace_id", "") or "").strip() != ws:
            continue
        if str(getattr(record, "lane_disposition", "") or "") not in WATCHABLE_DISPOSITIONS:
            continue
        lane_id = str(getattr(record, "lane_id", "") or "").strip()
        issue = str(getattr(record, "issue_id", "") or "").strip()
        generation = str(getattr(record, "lane_generation", "") or "").strip()
        if lane_id and issue and generation:
            out[lane_id] = (generation, issue)
    return out


def default_inventory_rows() -> list:
    """The live herdr ``agent list`` snapshot, or empty when the backend is unavailable."""
    try:
        import os

        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_herdr_projection import (  # noqa: E501
            list_herdr_agent_rows,
        )

        return list(list_herdr_agent_rows(os.environ))
    except Exception:  # noqa: BLE001 - no inventory -> nothing to watch this pass
        return []


def default_screen_reader():
    """The read-only visible-pane reader the send path already trusts, or ``None``."""
    try:
        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application.cli_workflow_stall_watch import (  # noqa: E501
            live_screen_reader,
        )

        return live_screen_reader()
    except Exception:  # noqa: BLE001 - a watcher that cannot read is blocked, not quiet
        return None


def default_note_transport():
    """The credential-gated, opt-in Redmine note transport, or ``None`` when unset."""
    try:
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (  # noqa: E501
            redmine_delivery_transport_from_env,
        )

        return redmine_delivery_transport_from_env()
    except Exception:  # noqa: BLE001 - no transport -> write_optin_unset, never a guess
        return None


def build_stall_watch_leg_fn(
    *,
    home: Optional[Path] = None,
    lifecycle_store: object,
    wake_store: object = None,
    redmine_source_fn: Optional[Callable[[object], object]] = None,
    inventory_rows: Callable[[], list] = default_inventory_rows,
    screen_reader: Callable[[], object] = default_screen_reader,
    note_transport: Callable[[], object] = default_note_transport,
    store: Optional[StallEscalationStore] = None,
    #: Sampling seams, injected only so a test can drive a whole pass without spending the
    #: real sampling interval. Production leaves them unset and the pass uses wall time.
    clock: Optional[Callable[[], float]] = None,
    sleep: Optional[Callable[[float], None]] = None,
    sample_interval_seconds: Optional[float] = None,
    body_marker_for: Optional[Callable[[str, str, str], str]] = None,
) -> Callable[..., object]:
    """Build the ``stall_watch_leg_fn`` the supervisor injects.

    The returned callable takes a ``SupervisedWorkspace`` plus the pass budget, and is safe
    to call on every tick: the cadence watermark makes the common case an immediate return
    with no reads of any kind.
    """
    escalation_store = store or StallEscalationStore(home=home)
    # The delivery-history join that makes `unsent_composer` reachable. Bound once (the
    # ledger handle is reused across passes); a host with no delivery history resolves every
    # lookup to "" and simply classifies without that evidence.
    resolve_marker = body_marker_for or default_body_marker_resolver(home)

    def _leg(ws, *, pass_budget=None):
        workspace_id = str(getattr(ws, "workspace_id", "") or "")
        policy = resolve_stall_watch_policy(getattr(ws, "canonical_path", ""))
        if not policy.enabled:
            # Resolve nothing else: an unconfigured host performs no lane reads at all.
            return run_stall_watch_leg(
                workspace_id=workspace_id,
                store=escalation_store,
                policy=policy,
                inventory_rows=list,
                read_screen=None,
            )

        lanes = lane_facts_snapshot(lifecycle_store, workspace_id)
        source = None
        if redmine_source_fn is not None:
            try:
                source = redmine_source_fn(ws)
            except Exception:  # noqa: BLE001 - no source -> the readback reports unrecorded
                source = None

        wake = None
        if wake_store is not None:
            def wake(workspace, issue):  # noqa: F811 - narrow local binding
                try:
                    return bool(wake_store.enqueue(workspace, issue))
                except Exception:  # noqa: BLE001 - a wake loss retries next pass
                    return False

        return run_stall_watch_leg(
            workspace_id=workspace_id,
            store=escalation_store,
            policy=policy,
            inventory_rows=inventory_rows,
            read_screen=screen_reader(),
            write_journal=build_journal_writer(
                policy=policy, transport=note_transport(), source=source
            ),
            wake=wake,
            generation_for=lambda lane_id: lanes.get(lane_id, ("", ""))[0],
            issue_for=lambda lane_id: lanes.get(lane_id, ("", ""))[1],
            body_marker_for=resolve_marker,
            budget=pass_budget,
            clock=clock,
            sleep=sleep,
            sample_interval_seconds=sample_interval_seconds,
        )

    return _leg


__all__ = (
    "POLICY_DETAIL_LIMIT",
    "WATCHABLE_DISPOSITIONS",
    "build_stall_watch_leg_fn",
    "default_inventory_rows",
    "default_note_transport",
    "default_screen_reader",
    "lane_facts_snapshot",
    "resolve_stall_watch_policy",
)
