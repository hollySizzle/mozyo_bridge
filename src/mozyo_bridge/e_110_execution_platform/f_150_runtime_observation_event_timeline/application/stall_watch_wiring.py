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

import re
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
    POLICY_ABSENT,
    POLICY_CONFIG_UNREADABLE,
    POLICY_INVALID,
    STALL_WATCH_KEYS,
    StallWatchPolicy,
    StallWatchPolicyError,
)

#: Lane dispositions whose units are worth watching. A superseded / retired / hibernated
#: lane is not a stall candidate: nothing is expected to be progressing there.
WATCHABLE_DISPOSITIONS: frozenset[str] = frozenset({"active"})


#: The redaction rule for the policy ``detail`` an operator sees in ``--status``.
#:
#: ``detail`` reaches ``policy.telemetry()`` and therefore ``--status --json``. An earlier
#: version put ``str(exc)`` there and merely truncated it, on the assumption that loader
#: errors only name keys and types. That assumption holds for THIS block's own validator and
#: for nothing else: review j#110146 finding_2 reproduced a YAML parse failure whose message
#: carried both the absolute config path (``/home/alice/private/...``) and a fragment of the
#: file's own content. Truncation is not redaction.
#:
#: So no raw message is ever carried. The detail is assembled from a CLOSED vocabulary: the
#: exception's type name, plus -- only for this block's own validator -- whichever declared
#: ``stall_watch`` key the error names, matched by exact token against
#: :data:`STALL_WATCH_KEYS` rather than by scanning the message.
POLICY_DETAIL_LIMIT = 200

#: Emitted when the offending key cannot be identified by a closed match.
UNIDENTIFIED_KEY = "unidentified_key"

#: How far :func:`own_validator_error` follows ``__cause__``. ``__cause__`` is a writable
#: attribute, so a chain can be made cyclic; an unbounded walk over one hangs a supervisor
#: pass with no output at all. A genuine ``raise ... from`` chain is a few links deep.
CAUSE_CHAIN_LIMIT = 8

#: What an operator is told when the failure was NOT this block's own validator. It names
#: WHAT failed and where to look, never what the failure said.
CONFIG_UNREADABLE_DETAIL = (
    "the repo-local config could not be read, so the stall_watch policy is unknown; "
    "run `mozyo-bridge config status` for the underlying error"
)


def _stall_watch_key_in(message: str) -> str:
    """The declared ``stall_watch`` key an error names, by EXACT token match.

    Matching against the declared key set -- rather than searching the message for the
    substring ``stall_watch`` -- is what keeps a sibling block out of this classification.
    ``stall_watch_extra`` contains ``stall_watch`` but is not a declared key, so it matches
    nothing here (review j#110146 finding_2 reproduced it being misreported as *this* block
    being malformed).
    """
    tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(message)))
    named = sorted(key for key in STALL_WATCH_KEYS if key in tokens)
    return named[0] if named else UNIDENTIFIED_KEY


def own_validator_error(exc: BaseException) -> Optional[BaseException]:
    """The :class:`StallWatchPolicyError` this failure originated from, if any.

    The loader raises ONE error type for any invalid block and chains the original with
    ``raise ... from exc``, so the origin is available *structurally*. Reading the cause
    chain is what makes the invalid / unreadable split a type decision instead of a string
    decision.

    The walk is **bounded**, and that bound is load-bearing rather than defensive garnish:
    ``__cause__`` is an ordinary writable attribute, so a chain can be cyclic, and an
    unbounded walk over one hangs forever — inside a supervisor pass, silently. A real
    ``raise ... from`` chain is a handful of links deep, so a walk that has not found the
    validator within :data:`CAUSE_CHAIN_LIMIT` has not got one.
    """
    seen = 0
    current: Optional[BaseException] = exc
    while current is not None and seen < CAUSE_CHAIN_LIMIT:
        if isinstance(current, StallWatchPolicyError):
            return current
        current = current.__cause__
        seen += 1
    return None


def redacted_detail(exc: BaseException, *, own: Optional[BaseException]) -> str:
    """A detail built only from a closed vocabulary -- never the exception's own text."""
    if own is not None:
        detail = f"{type(own).__name__}: stall_watch.{_stall_watch_key_in(str(own))}"
    else:
        detail = f"{type(exc).__name__}: {CONFIG_UNREADABLE_DETAIL}"
    return detail[:POLICY_DETAIL_LIMIT]


def resolve_stall_watch_policy(repo_root: object) -> StallWatchPolicy:
    """Read the workspace's ``stall_watch`` policy, degrading to "watch nothing".

    Every failure watches nothing, but they are **not** the same off-state, and j#110121-2
    asks for a *typed* no-op rather than merely a silent one. Three outcomes are kept
    distinct because they need three different operator actions:

    - **absent** -- no block is declared. Nothing to do; this is the shipped default.
    - **invalid** (:data:`POLICY_INVALID`) -- THIS block is malformed. Fix the block; the
      detail names the offending key.
    - **unreadable** (:data:`POLICY_CONFIG_UNREADABLE`) -- the config could not be read at
      all, or a *different* block is invalid. Fix the file; the stall_watch block may be
      perfectly fine.

    Which of the last two applies is decided from the exception CHAIN
    (:func:`own_validator_error`), not from the text of the message, and the detail is
    assembled from a closed vocabulary (:func:`redacted_detail`). Both were review j#110146
    finding_2: a substring test misfiled a sibling key as this block's fault, and the raw
    message leaked an absolute path plus file content into an operator surface.
    """
    try:
        from mozyo_bridge.application.repo_local_config_loader import (
            load_repo_local_config,
        )

        return load_repo_local_config(Path(str(repo_root))).stall_watch
    except Exception as exc:  # noqa: BLE001 - every failure still watches nothing
        own = own_validator_error(exc)
        return StallWatchPolicy.disabled(
            POLICY_INVALID if own is not None else POLICY_CONFIG_UNREADABLE,
            redacted_detail(exc, own=own),
        )



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
    "CAUSE_CHAIN_LIMIT",
    "CONFIG_UNREADABLE_DETAIL",
    "POLICY_DETAIL_LIMIT",
    "UNIDENTIFIED_KEY",
    "own_validator_error",
    "redacted_detail",
    "WATCHABLE_DISPOSITIONS",
    "build_stall_watch_leg_fn",
    "default_inventory_rows",
    "default_note_transport",
    "default_screen_reader",
    "lane_facts_snapshot",
    "resolve_stall_watch_policy",
)
