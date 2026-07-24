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
* **Head observation.** ``git ls-remote origin refs/heads/<lane>`` — independent of every
  durable marker (T2b step 4b invariant), binding both the candidate head and ``commits_pushed``.
* **Obligations (Fork C).** The live four are observed from local authorities (the workspace's
  outbox pending partition, the lane owner's live runtime, the candidate-bound worktree). The
  projection four come from an injected explicit-projection port; the concrete default supplies
  NOTHING — under the ruling an obligation with no explicit, exactly-joined projection is
  ``False`` (zero-actuation), and this issue does not extend the glance projection to invent one.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import LaneLifecycleStore
from mozyo_bridge.core.state.lane_lifecycle_model import (
    BINDING_KIND_ISSUE,
    DISPOSITION_ACTIVE,
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

from ..domain.hibernate_actuation import ActionTimeObligations
from ..domain.hibernate_basis_producer import (
    DogfoodReceipt,
    PushObservation,
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
from ..domain.hibernate_issuer_policy import (
    CONFIG_RELPATH,
    config_policy_pointer,
    resolve_journal_issuer,
)
from ..domain.redmine_journal_source import RedmineJournalEntry, marker_fields_in_note
from .hibernate_actuation_leg import HibernatePassResult, run_hibernate_pass
from .hibernate_candidate_assembler import AssemblyRequest, HibernateCandidateAssembler
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    derive_lane_workspace_token,
)
from .sublane_hibernate import LiveSublaneHibernateOps, SublaneHibernateUseCase

#: The release issue's receipt gate (ruling j#85530 Q3: the delegation is a delegation only when
#: the RELEASE issue records what it received — source issue + exact SHA). Read-only grammar for
#: the strict receipt reader; NOT a callback-bearing gate.
DOGFOOD_RECEIPT_GATE = "dogfood_receipt"

#: The EXPLICIT per-pass ticket-provider read budget (ruling j#86718 Fork B / review j#86726
#: R1-F3): one page per enumerated issue plus at most one receipt page per issue with a current
#: strictly-resolved delegation, bounded well below this. At the budget every further read is
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

_HEX = frozenset("0123456789abcdef")

EntriesReader = Callable[[str], Optional[Sequence[RedmineJournalEntry]]]


def _full_sha(value: str) -> bool:
    return len(value) == 40 and set(value) <= _HEX


def committed_config_policy_pointer(repo_root: Path) -> str:
    """The Fork A policy pointer from the COMMITTED config blob at HEAD, or ``""`` (fail-closed)."""
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", f"HEAD:{CONFIG_RELPATH}"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    blob = proc.stdout.strip()
    if proc.returncode != 0 or not _full_sha(blob):
        return ""
    return config_policy_pointer(blob)




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


def observe_lane_topology(
    repo_root: Path, rows, *, workspace: str, lane: str, generation: int
) -> "Optional[tuple[Path, str]]":
    """The lane's ``(canonical worktree path, actual checked-out branch)`` from FRESH Git
    topology, or ``None`` (fail-closed).

    Review j#86739 R3-F2: ``lane_label`` and ``branch`` are INDEPENDENT caller-supplied fields
    of the public create contract, so the lane id is never inferred to be the branch. The join
    key is the lifecycle row's authoritative ``worktree_identity`` token alone: among the
    workspace repo's own ``git worktree list --porcelain`` entries, exactly one path must
    RE-DERIVE that token, and the branch is THAT entry's current Git fact — a detached HEAD,
    a missing row/token, a pruned path, or a non-unique match resolves nothing.
    """
    row = next(
        (
            record
            for record in rows or ()
            if getattr(record, "repo_workspace_id", "") == workspace
            and getattr(record, "lane_id", "") == lane
            and int(getattr(record, "lane_generation", 0) or 0) == generation
        ),
        None,
    )
    if row is None:
        return None
    token = str(getattr(row, "worktree_identity", "") or "").strip()
    if not token:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    entries: list[tuple[str, str]] = []
    current_path: Optional[str] = None
    current_branch = ""
    for line in proc.stdout.splitlines() + [""]:
        line = line.strip()
        if line.startswith("worktree "):
            current_path = line[len("worktree "):].strip()
            current_branch = ""
        elif line.startswith("branch ") and current_path:
            current_branch = line[len("branch "):].strip()
        elif not line and current_path:
            entries.append((current_path, current_branch))
            current_path, current_branch = None, ""
    matches: list[tuple[Path, str]] = []
    for path_text, branch_ref in entries:
        try:
            resolved = Path(path_text).expanduser().resolve()
        except OSError:
            continue
        if not resolved.is_dir():
            continue
        if derive_lane_workspace_token(str(resolved)) == token:
            matches.append((resolved, branch_ref))
    if len(matches) != 1:
        return None
    resolved, branch_ref = matches[0]
    prefix = "refs/heads/"
    if not branch_ref.startswith(prefix):
        return None  # detached HEAD carries no branch authority (fail-closed)
    return resolved, branch_ref[len(prefix):]


def observe_lane_push(
    repo_root: Path, rows, selected: SelectedLane
) -> Optional[PushObservation]:
    """The action-time git-remote observation of the lane's ACTUAL branch head.

    The branch comes from the same fresh topology join the worktree binding uses
    (:func:`observe_lane_topology`) — never inferred from the lane id (review j#86739 R3-F2).
    An unobservable topology, a detached worktree, or an origin that does not carry the branch
    binds no head: the lane is a typed ``head_unbound`` non-candidate.
    """
    topology = observe_lane_topology(
        repo_root,
        rows,
        workspace=selected.repo_workspace_id,
        lane=selected.lane_id,
        generation=selected.lane_generation,
    )
    if topology is None:
        return None
    _worktree, branch = topology
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "ls-remote", "origin", f"refs/heads/{branch}"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    lines = [line for line in proc.stdout.strip().splitlines() if line.strip()]
    if len(lines) != 1:
        return None
    sha = lines[0].split()[0].strip()
    if not _full_sha(sha):
        return None
    return PushObservation(
        workspace=selected.repo_workspace_id,
        lane=selected.lane_id,
        lane_generation=selected.lane_generation,
        head=sha,
        reachable=True,
    )


def resolve_candidate_worktree(
    workspace_root: Path, rows, candidate: HibernateCandidate
) -> Optional[Path]:
    """The candidate lane's canonical worktree via the same fresh topology join (or ``None``)."""
    anchor = candidate.anchor
    topology = observe_lane_topology(
        workspace_root,
        rows,
        workspace=anchor.repo_workspace_id,
        lane=anchor.lane_id,
        generation=anchor.lane_generation,
    )
    return None if topology is None else topology[0]


def observe_worktree_clean(worktree: Optional[Path]) -> Optional[bool]:
    """Whether the candidate-bound worktree is clean (``None`` = unresolvable/unreadable)."""
    if worktree is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "-C", str(worktree), "status", "--porcelain"],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() == ""


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
):
    """Build the production ``hibernate_leg_fn`` for :func:`build_supervisor`.

    ``source_fn(ws)`` is the supervisor's own per-workspace Redmine source factory;
    ``outbox`` its shared callback outbox. The optional observer ports default to the
    fail-closed concretes: no runtime observer -> blank runtime -> ``False`` flags; no worktree
    resolver -> ``False``; no projection -> the projection four stay ``False`` (see
    :class:`ObligationSources`).
    """

    def leg(ws, renew, budget=None) -> HibernatePassResult:
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

        def make_assembler(entries_fn, records_fn, worktree_lookup):
            def journals_fn(issue: str):
                page = entries_fn(issue)
                if page is None:
                    return None
                return [
                    EvidenceJournal(
                        journal_id=str(entry.journal_id),
                        notes=entry.notes,
                        issuer=resolve_journal_issuer(
                            str(entry.journal_id), entry.notes, policy_pointer=pointer
                        ),
                    )
                    for entry in page
                ]

            def receipts_fn(issue: str) -> Mapping[str, DogfoodReceipt]:
                selected = lane_by_issue.get(str(issue).strip())
                page = journals_fn(issue)
                if selected is None or page is None:
                    return {}
                return read_dogfood_receipts(page, selected, entries_fn)

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
                push_fn=lambda selected: observe_lane_push(repo_root, records_fn(), selected),
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
        for request in requests:
            lane_by_issue.setdefault(request.selected.issue_id, request.selected)

        def build_worktree(candidate: HibernateCandidate) -> Optional[Path]:
            return resolve_candidate_worktree(repo_root, rows_build, candidate)

        assembler_build = make_assembler(build_entries, lambda: rows_build, build_worktree)
        assembled = assembler_build.assemble_all(requests)
        candidates = [item.candidate for item in assembled if item.candidate is not None]

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

        def fresh_worktree(candidate: HibernateCandidate) -> Optional[Path]:
            return resolve_candidate_worktree(repo_root, fresh_rows(), candidate)

        assembler_fresh = make_assembler(fresh_entries, fresh_rows, fresh_worktree)
        seams = assembler_fresh.pass_seams()

        def use_case_for(candidate: HibernateCandidate) -> Optional[SublaneHibernateUseCase]:
            # Per-candidate actuation binding (review j#86726 R1-F2 / j#86734 R2-F5): the
            # target worktree comes from FRESH authoritative Git topology joined to the
            # candidate identity — unresolvable binds nothing (typed zero-call).
            worktree = fresh_worktree(candidate)
            if worktree is None:
                return None
            return SublaneHibernateUseCase(
                ops=LiveSublaneHibernateOps(repo_root=worktree, env=dict(os.environ)),
                store=LaneLifecycleStore(home=home),
                lease_guard=renew,
            )

        return run_hibernate_pass(
            candidates,
            refresh_fn=seams.refresh_fn,
            obligations_fn=seams.obligations_fn,
            journal_fn=seams.journal_fn,
            use_case_fn=use_case_for,
            lease_renew_fn=renew,
        )

    return leg


def default_hibernate_leg_fn(*, home, outbox, source_fn):
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
    )


__all__ = [
    "default_hibernate_leg_fn",
    "DOGFOOD_RECEIPT_GATE",
    "ObligationSources",
    "build_hibernate_leg_fn",
    "unresolved_callback_debt",
    "committed_config_policy_pointer",
    "enumerate_requests",
    "observe_lane_push",
    "observe_lane_topology",
    "observe_obligations",
    "read_dogfood_receipts",
]
