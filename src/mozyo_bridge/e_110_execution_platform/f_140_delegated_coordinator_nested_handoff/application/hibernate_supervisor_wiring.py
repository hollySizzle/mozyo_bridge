"""Live wiring for the supervisor's hibernate mode leg (Redmine #14219 T2c step 2b).

Builds the ``hibernate_leg_fn`` the step-1 mode leg runs per leased workspace: the Fork B
enumeration, the live ports behind the T2b :class:`HibernateCandidateAssembler`, the Fork C
obligation observer, and the T2a pass over the public hibernate use case — all under the
ruling's conditions (j#86718):

* **Enumeration (Fork B).** ``early_hibernate`` enumerates every ACTIVE issue-bound lane of the
  leased workspace — an evaluation population, not a basis declaration: a lane with no evidence
  is a typed non-candidate, never an actuation. ``dependency_park`` enumerates ONLY a lane whose
  own journals carry a strictly-parsed canonical park evidence marker whose envelope matches the
  row's EXACT (workspace, lane, generation) — nothing is synthesized from idle/open/releasable,
  and a marker for another lane or a stale generation enumerates nothing.
* **Bounded provider reads.** TWO memoised fetches per enumerated issue per pass — one for the
  BUILD phase and one fresh for the ACTUATION phase (review j#86734 R2-F1) — plus at most one
  receipt read per issue with a current strictly-resolved delegation, all counted against ONE
  pass-wide budget the supervisor sweep shares across every workspace (review j#86734 R2-F3).
  At the budget the provider is not touched; an unreadable fetch is ``None`` (the assembler's
  typed unreadable), never retried within the pass.
* **Policy anchor (Fork A).** The issuer policy pointer is the COMMITTED config blob at the
  workspace HEAD (:func:`committed_config_policy_pointer`); an unreadable pointer resolves every
  issuer unknown — zero actuation, fail-closed.
* **Head observation.** The lane's ACTUAL checked-out branch from the typed worktree topology
  observation (never the lane id — review j#86739 R3-F2), whose origin head must equal the
  worktree's current local ``HEAD`` (review j#86757 R4-F1) — independent of every durable
  marker (T2b step 4b invariant), binding both the candidate head and ``commits_pushed``.
* **Obligations (Fork C).** The live four are observed from local authorities (the workspace's
  outbox pending partition, the lane owner's live runtime, the candidate-bound worktree). The
  projection four come from an injected explicit-projection port; the concrete default supplies
  NOTHING — under the ruling an obligation with no explicit, exactly-joined projection is
  ``False`` (zero-actuation), and this issue does not extend the glance projection to invent one.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.hibernate_redrive_intent import (
    HibernateRedriveIntentError,
    HibernateRedriveIntentStore,
    RedriveIntent,
)
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
    DISPOSITION_HIBERNATED,
    RELEASE_NOT_REQUESTED,
    RELEASE_RELEASED,
    RELEASE_STATES,
)
from mozyo_bridge.core.state.lane_lifecycle_readonly import load_lane_lifecycle_readonly
from mozyo_bridge.core.state.workflow_runtime_store import (
    CALLBACK_DEAD_LETTER,
    CALLBACK_INFLIGHT,
    CALLBACK_PENDING,
    CALLBACK_UNCERTAIN,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
    RUNTIME_AWAITING_INPUT,
    RUNTIME_TURN_ENDED,
)

from ..domain.hibernate_actuation import ActionTimeObligations, order_candidates
from ..domain.hibernate_basis_producer import (
    DogfoodReceipt,
    current_dogfood_delegation,
)
from ..domain.hibernate_candidate import (
    BASIS_DEPENDENCY_PARK,
    BASIS_EARLY_HIBERNATE,
    HibernateCandidate,
    SelectedLane,
)
from ..domain.hibernate_evidence_authority import EvidenceJournal
from ..domain.hibernate_evidence_marker import (
    EVIDENCE_DOGFOOD_DELEGATED,
    EVIDENCE_PARK_DECLARED,
    HibernateEvidence,
    parse_hibernate_evidence,
)
from ..domain.hibernate_issuer_policy import resolve_journal_issuer
from ..domain.redmine_journal_source import RedmineJournalEntry, marker_fields_in_note
from .hibernate_lane_topology import (
    LaneTopologyObservation,
    _full_sha,
    committed_config_policy_pointer,
    observe_branch_origin_head,
    observe_lane_push,
    observe_lane_topology,
    observe_worktree_clean,
    observe_worktree_head,
    push_from_topology,
    resolve_candidate_worktree,
)
from .hibernate_actuation_leg import (
    ATTEMPT_LEASE_LOST,
    ATTEMPT_RELEASE_STATE_UNKNOWN,
    LEG_REASON_LEASE_LOST,
    LEG_REASON_REDRIVE_INTENT_ABSENT,
    LEG_REASON_REDRIVE_INTENT_MISMATCH,
    LEG_REASON_REDRIVE_INTENT_UNREADABLE,
    HibernateAttempt,
    HibernatePassResult,
    RedriveResult,
    run_hibernate_pass,
    run_hibernate_redrives,
    stamp_drain_metrics,
)
from .hibernate_candidate_assembler import AssemblyRequest, HibernateCandidateAssembler
from .sublane_process_release import unit_slots
from .sublane_hibernate import HibernateRequest, LiveSublaneHibernateOps, SublaneHibernateUseCase
from .sublane_hibernate_assertions import HibernateAssertions

#: The release issue's receipt gate (ruling j#85530 Q3: the delegation is a delegation only when
#: the RELEASE issue records what it received — source issue + exact SHA). Read-only grammar for
#: the strict receipt reader; NOT a callback-bearing gate.
DOGFOOD_RECEIPT_GATE = "dogfood_receipt"

#: The EXPLICIT per-pass ticket-provider read budget (ruling j#86718 Fork B / review j#86726
#: R1-F3 / j#86734 R2-F3): TWO memoised page reads per enumerated issue per pass — one for the
#: BUILD phase and one fresh for the ACTUATION phase — plus at most one receipt page per issue
#: with a current strictly-resolved delegation, all counted against this ONE pass-wide budget
#: the supervisor sweep shares across every workspace. At the budget every further read is
#: refused WITHOUT touching the provider — the affected issue reads as the typed unreadable
#: (zero-actuation), never a partial page.
MAX_PROVIDER_READS_PER_PASS = 64

#: The callback states that are UNRESOLVED debt for the drain obligation (review j#86734
#: R2-F4): everything short of a positively delivered outcome blocks the auto path.
_UNRESOLVED_CALLBACK_STATES = (
    CALLBACK_PENDING,
    CALLBACK_INFLIGHT,
    CALLBACK_UNCERTAIN,
    CALLBACK_DEAD_LETTER,
)

#: The lane owner's live runtime states that explicitly express each live obligation — the
#: CANONICAL normalized receiver-state vocabulary (``agent_state.map_agent_status`` output),
#: imported rather than re-spelled (checkpoint j#86726 R1-F4: a raw-herdr spelling made the
#: ordinary ``awaiting_input`` receiver read as working, and unreachable tokens sat in the
#: set). Settled = quietly waiting for input, or the assistant turn just finished; anything
#: else (working / blocked / unknown / blank) leaves the flags ``False``.
RUNTIME_NOT_WORKING = frozenset({RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED})
RUNTIME_NO_PENDING_PROMPT = frozenset({RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED})

EntriesReader = Callable[[str], Optional[Sequence[RedmineJournalEntry]]]


def _park_evidences(notes: str) -> "list[HibernateEvidence]":
    """Every strictly-parseable park evidence in a note (canonical parser, no second grammar)."""
    found = []
    for _channel, fields in marker_fields_in_note(notes or ""):
        if str(fields.get("gate", "") or "").strip() != EVIDENCE_PARK_DECLARED:
            continue
        parsed = parse_hibernate_evidence(fields, kind=EVIDENCE_PARK_DECLARED)
        if isinstance(parsed, HibernateEvidence):
            found.append(parsed)
    return found


def enumerate_requests(
    rows: Sequence[object], workspace_id: str, entries_fn: EntriesReader
) -> tuple[AssemblyRequest, ...]:
    """The Fork B evaluation population for one leased workspace (ruling j#86718).

    Discovery is NOT inference: the park basis needs a strictly-parsed park evidence whose
    envelope equals the row's exact (workspace, lane, generation) — a marker for another lane,
    a stale generation, or a malformed envelope enumerates nothing, and an unreadable journal
    page enumerates only the early basis (whose own read will then be the typed unreadable).
    """
    requests: list[AssemblyRequest] = []
    for row in rows or ():
        if getattr(row, "binding_kind", "") != BINDING_KIND_ISSUE:
            continue
        if getattr(row, "lane_disposition", "") != DISPOSITION_ACTIVE:
            continue
        if getattr(row, "repo_workspace_id", "") != workspace_id:
            continue
        issue = str(getattr(row, "issue_id", "") or "").strip()
        if not issue:
            continue
        selected = SelectedLane(
            issue_id=issue,
            repo_workspace_id=row.repo_workspace_id,
            lane_id=row.lane_id,
            lane_generation=int(getattr(row, "lane_generation", 0) or 0),
            revision=int(getattr(row, "revision", 0) or 0),
        )
        requests.append(AssemblyRequest(selected=selected, basis=BASIS_EARLY_HIBERNATE))
        page = entries_fn(issue)
        if page is None:
            continue
        for entry in page:
            if any(
                evidence.envelope.workspace == selected.repo_workspace_id
                and evidence.envelope.lane == selected.lane_id
                and evidence.envelope.lane_generation == selected.lane_generation
                for evidence in _park_evidences(entry.notes)
            ):
                requests.append(
                    AssemblyRequest(selected=selected, basis=BASIS_DEPENDENCY_PARK)
                )
                break
    return tuple(requests)


def read_dogfood_receipts(
    journals, selected: SelectedLane, entries_fn: EntriesReader
) -> Mapping[str, DogfoodReceipt]:
    """Strict receipts for the CURRENT delegation only (review j#86726 R1-F3).

    The read set derives from the producer's own supersession + strict resolution
    (:func:`current_dogfood_delegation`) — never a raw field scan — and only when the current
    delegation's envelope is EXACTLY the enumerated lane. That yields AT MOST ONE release-issue
    read per issue per pass; a malformed / conflicting / superseded / foreign-lane delegation
    triggers zero external reads. On the release issue, identical receipt claims collapse and
    DIFFERING claims prove nothing (that release issue yields no receipt).
    """
    evidence = current_dogfood_delegation(journals or ())
    if evidence is None:
        return {}
    envelope = evidence.envelope
    if (
        envelope.workspace != selected.repo_workspace_id
        or envelope.lane != selected.lane_id
        or envelope.lane_generation != selected.lane_generation
    ):
        return {}
    release_issue = str(evidence.extra.get("release_issue", "") or "").strip()
    if not release_issue:
        return {}
    release_page = entries_fn(release_issue)
    if release_page is None:
        return {}
    claims: set[tuple[str, str]] = set()
    for entry in release_page:
        for _channel, fields in marker_fields_in_note(entry.notes or ""):
            if str(fields.get("gate", "") or "").strip() != DOGFOOD_RECEIPT_GATE:
                continue
            claimed_source = str(fields.get("source_issue", "") or "").strip()
            head = str(fields.get("head", "") or "").strip()
            if claimed_source and _full_sha(head):
                claims.add((claimed_source, head))
    if len(claims) != 1:
        return {}
    claimed_source, head = claims.pop()
    return {
        release_issue: DogfoodReceipt(
            release_issue=release_issue, source_issue=claimed_source, head=head
        )
    }


@dataclass(frozen=True)
class RedriveEnumeration:
    """The hibernated-row triage for one workspace pass (review j#86776 R5-F2 / R5-F5).

    ``redrives`` are the rows to finish through the public ``already_hibernated`` path;
    ``unknown_release`` are rows whose ``process_release`` is not a canonical token — a typed
    uncertain state the wiring refuses to hand the release rail (R5-F5), never a redrive.
    """

    redrives: tuple
    unknown_release: tuple


def enumerate_hibernated_redrives(
    rows: Sequence[object],
    workspace_id: str,
    *,
    live_slot_fn: Optional[Callable[[object], Optional[bool]]] = None,
) -> RedriveEnumeration:
    """Triage hibernated issue-lane rows into redrive debt vs uncertain state (R4-F2 / R5-F2/F5).

    A partial or crashed prior actuation leaves ``lane_disposition=hibernated`` with a
    ``process_release`` short of ``released`` — a row the ACTIVE-only enumeration would otherwise
    skip forever, stranding live processes behind a hibernated row until a manual sweep. The
    triage, per canonical release token:

    * ``released`` — terminal (the generation finished); never enumerated.
    * ``requested`` / ``partial`` — unresolved release debt; a redrive.
    * ``not_requested`` (review j#86776 R5-F2) — the crash landed the CAS but never opened the
      release, so the lane's slots may still be live. Enumerated as debt ONLY when the lane still
      has a live managed slot, OR the inventory is unreadable (fail-closed via ``live_slot_fn``
      returning ``None`` — or no ``live_slot_fn`` supplied). A CONFIRMED-empty inventory
      (``live_slot_fn`` -> ``False``) is terminal: the processes are already gone, there is no
      release to finish, and re-opening one every pass would be a false mutation.
    * any other token (review j#86776 R5-F5) — a non-canonical / uncertain ``process_release``.
      NEVER a redrive (the public rail's else-branch would falsely report it ``released``); it is
      returned as ``unknown_release`` for a typed uncertain block (zero execute, zero mutation).
    """
    redrives = []
    unknown = []
    for row in rows or ():
        if getattr(row, "binding_kind", "") != BINDING_KIND_ISSUE:
            continue
        if getattr(row, "lane_disposition", "") != DISPOSITION_HIBERNATED:
            continue
        if getattr(row, "repo_workspace_id", "") != workspace_id:
            continue
        if not str(getattr(row, "issue_id", "") or "").strip():
            continue
        release = str(getattr(row, "process_release", "") or "").strip()
        if release == RELEASE_RELEASED:
            continue  # terminal for the generation
        if release == RELEASE_NOT_REQUESTED:
            live = live_slot_fn(row) if live_slot_fn is not None else None
            if live is False:
                continue  # confirmed no live slot -> terminal (processes already gone)
            redrives.append(row)  # live slot present, or unreadable inventory (fail-closed)
            continue
        if release in RELEASE_STATES:  # requested / partial
            redrives.append(row)
            continue
        unknown.append(row)  # non-canonical token -> typed uncertain (R5-F5)
    return RedriveEnumeration(redrives=tuple(redrives), unknown_release=tuple(unknown))


def combine_hibernate_pass_results(
    *,
    unknown_attempts: tuple,
    redrive_result: RedriveResult,
    fresh_pass: Optional[HibernatePassResult],
    deferred_candidates: Sequence[HibernateCandidate],
    clock_fn: "Optional[Callable[[], str]]" = None,
) -> HibernatePassResult:
    """Fold the redrive prelude, the (optional) fresh pass, and the R5-F5 uncertain attempts.

    Review j#86776 R5-F4: when the redrive prelude STOPPED (a lease lost at renew or at the
    commit boundary), the fresh pass is NOT run at all — ``fresh_pass`` is ``None`` and every
    fresh candidate becomes a typed ``lease_lost`` attempt (zero use-case call, zero provider
    read, zero further mutation): a taken-over runner must not double-actuate. Otherwise the two
    passes' attempts / mutations add up as before. The R5-F5 ``unknown_attempts`` are
    informational — surfaced so an uncertain row is never silently dropped, but consuming
    nothing (they neither count as mutations nor mark the pass non-empty on their own... though a
    non-empty attempt list is a non-empty pass).
    """
    # Review j#87224 R5-F2: the R5-F5 ``unknown_attempts`` are built raw (they never run the
    # actuation loop), so stamp their closed-vocabulary time-to-drain status here — otherwise they
    # carry an empty status outside the ``completed|pending|uncertain|unavailable`` enum. They have no
    # trusted start/end (an unknown-release row), so the kind alone drives the status, latency null.
    stamped_unknown = tuple(
        stamp_drain_metrics(attempt, "", "") for attempt in unknown_attempts
    )
    if fresh_pass is None:
        # Review j#87214 R4-F2/F3: stamp each stopped-redrive lease-lost attempt with ITS candidate's
        # exact ``drain_ready_at`` — an uncertain end (no trusted terminal), so a null latency.
        fresh_attempts = tuple(
            stamp_drain_metrics(
                HibernateAttempt(
                    candidate.issue_id,
                    candidate.anchor.lane_id,
                    ATTEMPT_LEASE_LOST,
                    LEG_REASON_LEASE_LOST,
                ),
                str(getattr(candidate, "drain_ready_at", "") or ""),
                "",
            )
            for candidate in order_candidates(deferred_candidates)
        )
        return HibernatePassResult(
            attempts=stamped_unknown + redrive_result.attempts + fresh_attempts,
            mutations=redrive_result.mutations,
            empty_pass=False,
        )
    return HibernatePassResult(
        attempts=stamped_unknown + redrive_result.attempts + fresh_pass.attempts,
        mutations=redrive_result.mutations + fresh_pass.mutations,
        empty_pass=(
            fresh_pass.empty_pass
            and not redrive_result.attempts
            and not unknown_attempts
        ),
    )


def unresolved_callback_debt(outbox, workspace_id: str) -> Optional[int]:
    """The workspace's UNRESOLVED callback debt count, or ``None`` when unreadable.

    Review j#86734 R2-F4: every state short of a positively delivered outcome blocks — a
    claimed (``inflight``), outcome-unknown (``uncertain``) or dead-lettered row is exactly the
    "uncertain prior action" the hard stop exists for, not only the pending queue. An
    unreadable outbox is never a drained one (``None`` -> the flag stays ``False``).
    """
    try:
        rows = outbox.read(states=list(_UNRESOLVED_CALLBACK_STATES))
    except Exception:  # noqa: BLE001 - an unreadable outbox is not a drained one
        return None
    return sum(
        1 for row in rows if getattr(row.key, "workspace_id", "") == workspace_id
    )


@dataclass(frozen=True)
class ObligationSources:
    """The Fork C observation ports (ruling j#86718).

    ``projection_fn`` supplies the EXPLICIT glance-projection obligations — only keys it
    actually projects, exactly joined to the candidate; every unsupplied key is ``False``. The
    concrete default supplies nothing: no current projection expresses these obligations with
    the exact issue/workspace/lane/generation/revision join the ruling requires, and this issue
    does not extend the projection — so the projection four stay fail-closed until one exists.
    """

    outbox_pending_fn: Callable[[str], Optional[int]]
    runtime_fn: Callable[[str, str], str]
    worktree_clean_fn: Callable[[HibernateCandidate], Optional[bool]]
    projection_fn: Callable[[HibernateCandidate], Mapping[str, bool]] = lambda candidate: {}


def observe_obligations(
    candidate: HibernateCandidate, sources: ObligationSources
) -> ActionTimeObligations:
    """The Fork C observer: live four from local authorities, projection four fail-closed.

    Every unobservable input leaves its flag ``False`` (zero-actuation): an unreadable outbox,
    a blank runtime, an unresolvable worktree, and any projection key the port did not
    explicitly supply.
    """
    workspace = candidate.anchor.repo_workspace_id
    lane = candidate.anchor.lane_id

    pending = sources.outbox_pending_fn(workspace)
    callbacks_drained = pending == 0

    runtime = str(sources.runtime_fn(workspace, lane) or "").strip()
    not_working = runtime in RUNTIME_NOT_WORKING
    no_pending_prompt = runtime in RUNTIME_NO_PENDING_PROMPT

    clean = sources.worktree_clean_fn(candidate)
    worktree_clean = clean is True

    projected = dict(sources.projection_fn(candidate) or {})
    return ActionTimeObligations(
        callbacks_drained=callbacks_drained,
        no_review_pending=bool(projected.get("no_review_pending", False)),
        no_owner_approval_pending=bool(projected.get("no_owner_approval_pending", False)),
        no_integration_pending=bool(projected.get("no_integration_pending", False)),
        no_pending_prompt=no_pending_prompt,
        not_working=not_working,
        worktree_clean=worktree_clean,
        boundary_recorded=bool(projected.get("boundary_recorded", False)),
    )


def build_hibernate_leg_fn(
    *,
    home: Optional[Path],
    outbox,
    source_fn,
    runtime_fn: Optional[Callable[[str, str], str]] = None,
    worktree_clean_fn: Optional[Callable[[HibernateCandidate], Optional[bool]]] = None,
    projection_fn: Optional[Callable[[HibernateCandidate], Mapping[str, bool]]] = None,
    inventory_fn: Optional[Callable[[], "tuple[Sequence[Mapping[str, object]], bool]"]] = None,
    clock_fn: Optional[Callable[[], str]] = None,
):
    """Build the production ``hibernate_leg_fn`` for :func:`build_supervisor`.

    ``source_fn(ws)`` is the supervisor's own per-workspace Redmine source factory;
    ``outbox`` its shared callback outbox. The optional observer ports default to the
    fail-closed concretes: no runtime observer -> blank runtime -> ``False`` flags; no worktree
    resolver -> ``False``; no projection -> the projection four stay ``False`` (see
    :class:`ObligationSources`). ``inventory_fn`` supplies the live herdr inventory
    ``(rows, readable)`` the R5-F2 not_requested-redrive triage reads for live-slot presence;
    the default reads it once per pass from the live herdr binary (an unreadable inventory is
    fail-closed — a not_requested row is still enumerated as debt).
    """

    def leg(ws, renew, budget=None, restrict_issues=None) -> HibernatePassResult:
        repo_root = Path(str(ws.canonical_path or "") or ".")
        pointer = committed_config_policy_pointer(repo_root)

        source = None
        try:
            source = source_fn(ws)
        except Exception:  # noqa: BLE001 - an unbuildable source reads nothing (typed below)
            source = None

        # The provider-read budget is SHARED across the whole supervisor pass when the sweep
        # supplies it (review j#86734 R2-F3) — never reset per workspace.
        shared = budget if budget is not None else {"reads": 0}

        # The live herdr inventory, read at most ONCE per workspace pass (review j#86776 R5-F2):
        # the not_requested-redrive triage's live-slot check reads this snapshot. The default
        # reads the live binary; an unreadable inventory is ``(rows, False)`` (fail-closed).
        inventory_box: dict = {}

        def inventory_snapshot() -> "tuple[Sequence[Mapping[str, object]], bool]":
            if "inv" not in inventory_box:
                if inventory_fn is not None:
                    inventory_box["inv"] = inventory_fn()
                else:
                    inventory_box["inv"] = LiveSublaneHibernateOps(
                        repo_root=repo_root, env=dict(os.environ)
                    ).read_inventory()
            return inventory_box["inv"]

        def make_entries_fn():
            cache: dict[str, Optional[tuple[RedmineJournalEntry, ...]]] = {}

            def entries_fn(issue: str) -> Optional[tuple[RedmineJournalEntry, ...]]:
                issue = str(issue).strip()
                if issue not in cache:
                    if source is None or shared["reads"] >= MAX_PROVIDER_READS_PER_PASS:
                        cache[issue] = None
                    else:
                        shared["reads"] += 1
                        try:
                            cache[issue] = tuple(source.read_entries(issue))
                        except Exception:  # noqa: BLE001 - unreadable page is typed, not empty
                            cache[issue] = None
                return cache[issue]

            return entries_fn

        def outbox_pending(workspace_id: str) -> Optional[int]:
            return unresolved_callback_debt(outbox, workspace_id)

        lane_by_issue: dict[str, SelectedLane] = {}
        # Redmine #14219 T3 review j#87196 R2-F2(a): the provider ``created_on`` of every journal read
        # this pass, keyed by journal id — the authority for a candidate's drain-ready start. Populated
        # as ``journals_fn`` projects the evidence; a candidate's ``drain_ready_at`` is the created_on
        # of its EXACT basis decision journal (never guessed from the id, never a local observation).
        created_on_by_jid: dict[str, str] = {}

        def make_topology_fn(records_fn):
            # ONE typed topology observation per lane identity per phase (review j#86757
            # R4-F1): the push head, the obligations worktree and the use-case binding all
            # read the SAME capture — never separate re-reads of the same physical worktree.
            memo: dict = {}

            def topology_fn(workspace: str, lane: str, generation: int):
                key = (workspace, lane, generation)
                if key not in memo:
                    memo[key] = observe_lane_topology(
                        repo_root,
                        records_fn(),
                        workspace=workspace,
                        lane=lane,
                        generation=generation,
                    )
                return memo[key]

            return topology_fn

        def candidate_topology(topology_fn, candidate: HibernateCandidate):
            anchor = candidate.anchor
            return topology_fn(
                anchor.repo_workspace_id, anchor.lane_id, anchor.lane_generation
            )

        def make_assembler(entries_fn, records_fn, topology_fn):
            def journals_fn(issue: str):
                page = entries_fn(issue)
                if page is None:
                    return None
                journals = []
                for entry in page:
                    created = str(getattr(entry, "created_on", "") or "")
                    if created:
                        created_on_by_jid.setdefault(str(entry.journal_id), created)
                    journals.append(EvidenceJournal(
                        journal_id=str(entry.journal_id),
                        notes=entry.notes,
                        issuer=resolve_journal_issuer(
                            str(entry.journal_id), entry.notes, policy_pointer=pointer
                        ),
                        created_on=created,
                    ))
                return journals

            def receipts_fn(issue: str) -> Mapping[str, DogfoodReceipt]:
                selected = lane_by_issue.get(str(issue).strip())
                page = journals_fn(issue)
                if selected is None or page is None:
                    return {}
                return read_dogfood_receipts(page, selected, entries_fn)

            def worktree_lookup(candidate: HibernateCandidate) -> Optional[Path]:
                observation = candidate_topology(topology_fn, candidate)
                return None if observation is None else observation.worktree

            pass_sources = sources
            if worktree_clean_fn is None:
                pass_sources = ObligationSources(
                    outbox_pending_fn=sources.outbox_pending_fn,
                    runtime_fn=sources.runtime_fn,
                    worktree_clean_fn=lambda candidate: observe_worktree_clean(
                        worktree_lookup(candidate)
                    ),
                    projection_fn=sources.projection_fn,
                )
            return HibernateCandidateAssembler(
                records_fn=records_fn,
                journals_fn=journals_fn,
                push_fn=lambda selected: push_from_topology(
                    topology_fn(
                        selected.repo_workspace_id, selected.lane_id, selected.lane_generation
                    ),
                    selected,
                ),
                obligations_fn=lambda candidate: observe_obligations(
                    candidate, pass_sources
                ),
                dogfood_receipts_fn=receipts_fn,
            )

        sources = ObligationSources(
            outbox_pending_fn=outbox_pending,
            runtime_fn=runtime_fn or (lambda workspace, lane: ""),
            worktree_clean_fn=worktree_clean_fn or (lambda candidate: None),
            projection_fn=projection_fn or (lambda candidate: {}),
        )

        # BUILD phase: its own page cache + a lifecycle snapshot for enumeration.
        build_entries = make_entries_fn()
        rows_build = load_lane_lifecycle_readonly(home=home)
        requests = enumerate_requests(rows_build or (), str(ws.workspace_id), build_entries)
        if restrict_issues is not None:
            # Redmine #14219 T3 review j#87154 R1-F2: a wake-bound pass (``local_wake``) hibernates
            # ONLY the lanes of the exact woken issues — an unrelated drain-ready lane is out of this
            # pass's authority scope. The whole-workspace candidate scan is the timer fallback alone.
            allowed = frozenset(str(i).strip() for i in restrict_issues)
            requests = [r for r in requests if str(r.selected.issue_id).strip() in allowed]
        for request in requests:
            lane_by_issue.setdefault(request.selected.issue_id, request.selected)

        topology_build = make_topology_fn(lambda: rows_build)
        assembler_build = make_assembler(build_entries, lambda: rows_build, topology_build)
        assembled = assembler_build.assemble_all(requests)
        # R2-F2(a): bind each candidate's drain-ready START to the provider ``created_on`` of its
        # EXACT basis decision journal (``AssembledCandidate.decision_journal``). ``created_on_by_jid``
        # was populated as the assembler read the evidence above; an absent / unread created_on leaves
        # ``drain_ready_at`` blank (a later ``unavailable`` status, never a guessed or substituted time).
        candidates = []
        for item in assembled:
            cand = item.candidate
            if cand is None:
                continue
            drain_ready = created_on_by_jid.get(str(item.decision_journal), "")
            candidates.append(replace(cand, drain_ready_at=drain_ready) if drain_ready else cand)

        # ACTUATION phase (review j#86734 R2-F1): a SECOND assembler whose caches start empty,
        # so the pass seams' one-fresh-observation memo actually re-reads the provider pages
        # AND the lifecycle authority after build — within the same shared read budget. A later
        # review_request, a lifecycle revision/generation drift, or any evidence lapse between
        # build and actuation now surfaces as the typed stale zero-actuation.
        fresh_entries = make_entries_fn()
        rows_fresh_box: dict = {}

        def fresh_rows():
            if "rows" not in rows_fresh_box:
                rows_fresh_box["rows"] = load_lane_lifecycle_readonly(home=home)
            return rows_fresh_box["rows"]

        topology_fresh = make_topology_fn(fresh_rows)
        assembler_fresh = make_assembler(fresh_entries, fresh_rows, topology_fresh)
        seams = assembler_fresh.pass_seams()

        def bound_use_case(
            observation: LaneTopologyObservation, *, head_fence: bool
        ) -> SublaneHibernateUseCase:
            # Commit-point guard (review j#86757 R4-F1 / review j#86776 R5-F1): the use case's
            # ``lease_guard`` fires immediately before the irreversible CAS / redrive close, so
            # composing the expected-head fence here closes the whole observation -> commit
            # window. A clean rebranch or HEAD switch after the fresh observation refuses with
            # zero transition / zero close, through the same commit-point-refusal channel as a
            # lost lease. R5-F1: the guard also re-reads the branch's CURRENT origin head — the
            # observation bound the candidate head from origin (``observation.pushed`` required
            # ``local_head == origin_head``), so an origin advance / force-push / ref delete
            # AFTER the fresh observation drifts the evidence head off origin even while the
            # local worktree is untouched. Requiring ``local HEAD == current origin head ==
            # observation.origin_head`` (and the local (head, branch) unchanged) binds the CAS to
            # the exact still-origin-reachable evidence head. The redrive path
            # (``head_fence=False``) keeps the lease-only guard: it resumes a STORED action
            # authority (preservation, not head-bound evidence), and its live mutation safety is
            # the use case's own T1 boundary fence.
            def commit_guard() -> bool:
                if not renew():
                    return False
                if not head_fence:
                    return True
                if observe_worktree_head(observation.worktree) != (
                    observation.local_head,
                    observation.branch,
                ):
                    return False
                return (
                    observe_branch_origin_head(observation.worktree, observation.branch)
                    == observation.origin_head
                )

            return SublaneHibernateUseCase(
                ops=LiveSublaneHibernateOps(
                    repo_root=observation.worktree, env=dict(os.environ)
                ),
                store=LaneLifecycleStore(home=home),
                lease_guard=commit_guard,
            )

        def use_case_for(candidate: HibernateCandidate) -> Optional[SublaneHibernateUseCase]:
            # Per-candidate actuation binding (review j#86726 R1-F2 / j#86734 R2-F5): the
            # target worktree comes from the SAME fresh typed topology observation the push
            # head and the obligations used — unresolvable binds nothing (typed zero-call).
            observation = candidate_topology(topology_fresh, candidate)
            if observation is None:
                return None
            return bound_use_case(observation, head_fence=True)

        # The durable redrive-intent store (review j#86776 R5-F3): the fresh actuation records a
        # typed intent pre-CAS; the redrive reconstructs the row's PROVEN basis from it (never
        # fabricated), and reading it is a home-scoped read (the redrive touches the provider 0
        # times).
        intent_store = HibernateRedriveIntentStore(home=home)

        def _resolve_valid_intent(row):
            # The row's durable redrive intent, ONLY when it is the SAME action authority the redrive
            # would resume — bound to the row's exact identity (workspace / lane / generation) AND its
            # ``matches_row`` cycle (issue / decision journal / action id). A foreign-cycle intent (same
            # row identity, different cycle) is refused: the request derivation rejects it as
            # ``redrive_intent_mismatch``, so the drain-ready start MUST NOT trust it either (review
            # j#87244 R7-F1). Raises :class:`HibernateRedriveIntentError` on an unreadable / corrupt DB.
            intent = intent_store.get(
                str(getattr(row, "repo_workspace_id", "")),
                str(getattr(row, "lane_id", "")),
                int(getattr(row, "lane_generation", 0) or 0),
            )
            if intent is None:
                return None
            if not intent.matches_row(
                issue_id=str(getattr(row, "issue_id", "")),
                decision_journal=str(getattr(row, "decision_journal", "")),
                action_id=f"hibernate:{str(getattr(row, 'lane_id', ''))}",
            ):
                return None
            return intent

        def _redrive_drain_ready(row) -> str:
            # R2-F2(a) item 4 / j#87236 R6-F1 / j#87244 R7-F1: a redrive's ORIGINAL drain-ready start is
            # THIS row's own VALIDATED intent (same-cycle authority) — never an issue-collapsed map and
            # never a foreign-cycle intent's start. A deferred row (deferred before its request ran) is
            # resolved the same way. Absent / mismatched / unreadable -> a blank start (later unavailable).
            try:
                intent = _resolve_valid_intent(row)
            except HibernateRedriveIntentError:
                return ""
            return str(getattr(intent, "drain_ready_at", "") or "") if intent is not None else ""

        def record_intent(candidate: HibernateCandidate, fields) -> bool:
            # Persist the fresh actuation's derived intent immediately before its CAS, and REPORT
            # whether it was durably stored (review j#86928 R6-F1). A write / unreadable / schema
            # / validation failure returns ``False`` so the leg refuses the irreversible CAS —
            # a hibernate whose intent could not be persisted would strand the live process on a
            # post-CAS crash (the redrive could never reconstruct the basis).
            try:
                intent_store.record(
                    RedriveIntent(
                        workspace_id=candidate.anchor.repo_workspace_id,
                        lane_id=candidate.anchor.lane_id,
                        lane_generation=candidate.anchor.lane_generation,
                        issue_id=candidate.issue_id,
                        decision_journal=fields.journal,
                        basis=candidate.basis,
                        action_id=f"hibernate:{candidate.anchor.lane_id}",
                        assertion_flags=fields.assertion_flags,
                        # R2-F2(a): persist the ORIGINAL drain-ready start pre-CAS so a crash-redrive
                        # measures time-to-drain from when the lane FIRST became drain-ready.
                        drain_ready_at=str(getattr(candidate, "drain_ready_at", "") or ""),
                    )
                )
            except HibernateRedriveIntentError:
                return False
            return True

        # Crash-redrive prelude (review j#86757 R4-F2 / review j#86776 R5-F2/F3/F5): finish a
        # prior pass's interrupted release BEFORE any fresh mutation — convergence precedes new
        # work, under the same pass-wide one-mutation budget.
        def live_slot(row) -> Optional[bool]:
            # Review j#86776 R5-F2: does the hibernated lane still have a live managed slot? A
            # readable inventory yields True/False; an unreadable one yields ``None`` (fail-closed
            # — the not_requested row is still enumerated as debt).
            rows_inv, readable = inventory_snapshot()
            if not readable:
                return None
            return bool(
                unit_slots(
                    rows_inv,
                    str(getattr(row, "repo_workspace_id", "")),
                    str(getattr(row, "lane_id", "")),
                )
            )

        enumeration = enumerate_hibernated_redrives(
            fresh_rows() or (), str(ws.workspace_id), live_slot_fn=live_slot
        )
        redrives = enumeration.redrives

        # Review j#86776 R5-F5: a hibernated row whose ``process_release`` is a non-canonical
        # token is an UNCERTAIN state — never driven (the public rail's else-branch would falsely
        # report it released). A typed uncertain block: zero execute, zero mutation, and it does
        # not consume the pass budget.
        unknown_attempts = tuple(
            HibernateAttempt(
                str(getattr(row, "issue_id", "")),
                str(getattr(row, "lane_id", "")),
                ATTEMPT_RELEASE_STATE_UNKNOWN,
                ATTEMPT_RELEASE_STATE_UNKNOWN,
            )
            for row in enumeration.unknown_release
        )

        def redrive_use_case(row) -> Optional[SublaneHibernateUseCase]:
            observation = topology_fresh(
                str(getattr(row, "repo_workspace_id", "")),
                str(getattr(row, "lane_id", "")),
                int(getattr(row, "lane_generation", 0) or 0),
            )
            if observation is None:
                return None
            return bound_use_case(observation, head_fence=False)

        def redrive_request(row) -> "HibernateRequest | str":
            # Review j#86776 R5-F3: the redrive's DURABLE basis flags come from the persisted
            # intent (never fabricated from the generic hibernated disposition), and only when an
            # intent EXISTS and matches the row's issue / decision journal / action. An absent
            # intent (dependency-park / manual / pre-R5 crash) or a mismatch is a typed zero-close
            # (a reason STRING, which the leg records as ``redrive_blocked`` without touching the
            # use case). The LIVE gates are re-observed action-time — a lane that has since started
            # working, gained a pending prompt, owes a callback, or dirtied its worktree is a typed
            # redrive block — and the use case's own T1 boundary fence re-probes busy/composer/
            # worktree immediately before the close. Reading the intent is home-scoped; ZERO
            # provider reads.
            workspace = str(getattr(row, "repo_workspace_id", ""))
            lane = str(getattr(row, "lane_id", ""))
            generation = int(getattr(row, "lane_generation", 0) or 0)
            issue = str(getattr(row, "issue_id", ""))
            journal = str(getattr(row, "decision_journal", ""))
            action_id = f"hibernate:{lane}"
            try:
                intent = intent_store.get(workspace, lane, generation)
            except HibernateRedriveIntentError:
                # Unreadable / corrupt intent (review j#86928 R6-F2: a non-bool / unknown-key /
                # malformed blob raises) -> no usable basis, a typed zero-close DISTINCT from a
                # genuinely absent intent.
                return LEG_REASON_REDRIVE_INTENT_UNREADABLE
            if intent is None:
                return LEG_REASON_REDRIVE_INTENT_ABSENT
            if not intent.matches_row(
                issue_id=issue, decision_journal=journal, action_id=action_id
            ):
                return LEG_REASON_REDRIVE_INTENT_MISMATCH
            debt = outbox_pending(workspace)
            runtime = sources.runtime_fn(workspace, lane)
            settled = runtime in RUNTIME_NOT_WORKING
            observation = topology_fresh(workspace, lane, generation)
            clean = (
                observation is not None
                and observe_worktree_clean(observation.worktree) is True
            )
            flags = dict(intent.assertion_flags)
            # Re-observe the LIVE gates fresh; every other (durable) flag is transcribed from the
            # intent the fresh CAS proved. A live gate must never be trusted from the stale intent.
            flags["callbacks_drained"] = debt == 0
            flags["no_pending_prompt"] = settled
            flags["not_working"] = settled
            flags["worktree_clean"] = clean
            try:
                assertions = HibernateAssertions(**flags)
            except TypeError:
                # A malformed / partial stored flag set is not a usable basis -> zero-close.
                return LEG_REASON_REDRIVE_INTENT_MISMATCH
            return HibernateRequest(
                issue=issue,
                lane=lane,
                journal=journal,
                assertions=assertions,
                expected_lane_generation=str(generation),
                expected_revision="",
            )

        # R2-F2(a) + review j#87214 R4-F2/F3 / j#87236 R6-F1: the drain-latency is stamped PER ATTEMPT
        # at its terminal disposition inside ``run_hibernate_pass`` / ``run_hibernate_redrives`` — each
        # attempt uses ITS candidate's / row's EXACT ``drain_ready_at`` (never an issue-collapsed one)
        # and the clock read at THAT terminal (never one pass-wide read). The redrive path resolves the
        # ORIGINAL start from THIS row's own intent (bound to workspace/lane/generation) via
        # :func:`_redrive_drain_ready` above — including a deferred row that never ran its request.
        redrive_result = run_hibernate_redrives(
            redrives,
            use_case_fn=redrive_use_case,
            request_fn=redrive_request,
            lease_renew_fn=renew,
            clock_fn=clock_fn,
            drain_ready_fn=_redrive_drain_ready,
        )

        # Review j#86776 R5-F4: a redrive that lost its lease STOPS the whole pass — the fresh
        # pass is not run (its use cases would double-actuate under a taken-over lease), and every
        # fresh candidate is a typed lease_lost with zero use-case call / zero provider read.
        if redrive_result.stopped:
            return combine_hibernate_pass_results(
                unknown_attempts=unknown_attempts,
                redrive_result=redrive_result,
                fresh_pass=None,
                deferred_candidates=candidates,
                clock_fn=clock_fn,
            )

        fresh_pass = run_hibernate_pass(
            candidates,
            refresh_fn=seams.refresh_fn,
            obligations_fn=seams.obligations_fn,
            journal_fn=seams.journal_fn,
            use_case_fn=use_case_for,
            lease_renew_fn=renew,
            record_intent_fn=record_intent,
            budget_consumed=redrive_result.mutations > 0,
            clock_fn=clock_fn,
        )
        return combine_hibernate_pass_results(
            unknown_attempts=unknown_attempts,
            redrive_result=redrive_result,
            fresh_pass=fresh_pass,
            deferred_candidates=candidates,
            clock_fn=clock_fn,
        )

    return leg


def default_hibernate_leg_fn(*, home, outbox, source_fn, clock_fn=None):
    """The production leg with the default observer ports (one call site in build_supervisor).

    The lane owner's runtime is read from the live herdr inventory for the WORKER provider
    (the lane's own agent); the worktree resolver and the explicit-projection port stay at
    their fail-closed defaults (see :class:`ObligationSources`).
    """
    from .reconcile_live_source import lane_worker_runtime

    return build_hibernate_leg_fn(
        home=home,
        outbox=outbox,
        source_fn=source_fn,
        runtime_fn=lambda workspace_id, lane_id: lane_worker_runtime(
            workspace_id, lane_id, "implementation_worker"
        ),
        clock_fn=clock_fn,
    )


__all__ = [
    "default_hibernate_leg_fn",
    "DOGFOOD_RECEIPT_GATE",
    "ObligationSources",
    "build_hibernate_leg_fn",
    "unresolved_callback_debt",
    "committed_config_policy_pointer",
    "enumerate_requests",
    "enumerate_hibernated_redrives",
    "RedriveEnumeration",
    "combine_hibernate_pass_results",
    "LaneTopologyObservation",
    "observe_worktree_head",
    "observe_branch_origin_head",
    "push_from_topology",
    "observe_lane_push",
    "observe_lane_topology",
    "observe_obligations",
    "read_dogfood_receipts",
]
