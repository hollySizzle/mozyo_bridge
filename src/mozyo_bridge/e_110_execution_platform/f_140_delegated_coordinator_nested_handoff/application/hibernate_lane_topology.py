"""Lane git-topology observation for the auto-hibernate supervisor (Redmine #14219 T2c).

The hermetic git/subprocess observation layer the supervisor's hibernate leg reads: the
committed Fork A policy pointer, a lane worktree's single typed topology fact (its canonical
path, ACTUAL checked-out branch, local ``HEAD`` and that branch's origin head), the push
observation derived from it, and the candidate worktree / cleanliness probes. Split out of
``hibernate_supervisor_wiring`` to keep that composition module under the module-health line
ceiling (review j#86776 R5 leaf extraction) — a cohesive, pure-over-subprocess unit with no
wiring state.

Every observation is fail-closed: an unreadable worktree, a detached HEAD, a non-unique
worktree-identity match, an absent origin ref, or a non-full-hex sha resolves to ``None`` /
``""`` rather than a guessed value. The single-typed-fact contract (review j#86757 R4-F1) lives
here: :class:`LaneTopologyObservation` captures the worktree, branch, local head, and origin
head in ONE observation so every downstream use (push head, obligations worktree, commit-point
fence) reads the same physical state, never a second re-read that could describe two states of
the same entity.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from ..domain.hibernate_basis_producer import PushObservation
from ..domain.hibernate_candidate import HibernateCandidate, SelectedLane
from ..domain.hibernate_issuer_policy import CONFIG_RELPATH, config_policy_pointer
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    derive_lane_workspace_token,
)

_HEX = frozenset("0123456789abcdef")


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


@dataclass(frozen=True)
class LaneTopologyObservation:
    """One lane worktree's SINGLE fresh Git topology fact (review j#86757 R4-F1).

    Every observation of the same physical worktree — its canonical path, its ACTUAL
    checked-out branch, its local ``HEAD`` and that branch's origin head — is derived from
    this one typed capture, never from separate re-reads that could describe two different
    states of the same entity. ``origin_head`` is ``""`` when the branch is not observable
    on origin (absent ref / unreadable remote); every other field is always solid.
    """

    worktree: Path
    branch: str
    local_head: str
    origin_head: str = ""

    @property
    def pushed(self) -> bool:
        """The worktree's CURRENT local HEAD is exactly the origin head of its branch.

        This — not the mere existence of an origin branch — is the proof the lane's work
        is origin-reachable (review j#86757 R4-F1): a clean local commit ahead of origin,
        a behind checkout, or a diverged branch all read ``False`` (fail-closed).
        """
        return bool(self.origin_head) and self.local_head == self.origin_head


def _git_lines(args, *, cwd: Path) -> Optional[list[str]]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


def observe_worktree_head(worktree: Path) -> "Optional[tuple[str, str]]":
    """The worktree's CURRENT ``(local HEAD sha, checked-out branch)``, or ``None``.

    The commit-point re-read of the same two Git facts the topology observation captured —
    a detached HEAD, a malformed sha, or an unreadable worktree resolves nothing (fail-closed).
    """
    head_lines = _git_lines(["rev-parse", "HEAD"], cwd=worktree)
    if head_lines is None or len(head_lines) != 1 or not _full_sha(head_lines[0].strip()):
        return None
    branch_lines = _git_lines(["symbolic-ref", "--quiet", "HEAD"], cwd=worktree)
    prefix = "refs/heads/"
    if (
        branch_lines is None
        or len(branch_lines) != 1
        or not branch_lines[0].strip().startswith(prefix)
    ):
        return None  # detached HEAD carries no branch authority (fail-closed)
    return head_lines[0].strip(), branch_lines[0].strip()[len(prefix):]


def observe_branch_origin_head(cwd: Path, branch: str) -> str:
    """The branch's CURRENT origin head SHA from a fresh ``ls-remote``, or ``""``.

    The single authority for "what commit does ``origin/<branch>`` point at right now" — an
    unreadable remote, an absent ref, a non-unique match, or a non-full-hex sha all resolve to
    ``""`` (fail-closed). Both the topology observation and the commit-point origin fence
    (review j#86776 R5-F1) read the SAME helper, so the two never drift on how origin is read.
    """
    remote = _git_lines(["ls-remote", "origin", f"refs/heads/{branch}"], cwd=cwd)
    if remote is None:
        return ""
    refs = [line for line in remote if line.strip()]
    if len(refs) != 1:
        return ""
    sha = refs[0].split()[0].strip()
    return sha if _full_sha(sha) else ""


def observe_lane_topology(
    repo_root: Path, rows, *, workspace: str, lane: str, generation: int
) -> Optional[LaneTopologyObservation]:
    """The lane's single typed topology observation from FRESH Git facts, or ``None``.

    Review j#86739 R3-F2: ``lane_label`` and ``branch`` are INDEPENDENT caller-supplied fields
    of the public create contract, so the lane id is never inferred to be the branch. The join
    key is the lifecycle row's authoritative ``worktree_identity`` token alone: among the
    workspace repo's own ``git worktree list --porcelain`` entries, exactly one path must
    RE-DERIVE that token, and the branch is THAT entry's current Git fact — a detached HEAD,
    a missing row/token, a pruned path, or a non-unique match resolves nothing.

    Review j#86757 R4-F1: the observation also captures the worktree's local ``HEAD`` (required,
    fail-closed) and the branch's origin head (best-effort, ``""`` when unobservable), so pushed
    means ``local HEAD == origin head`` — never the mere existence of an origin ref.
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
    listing = _git_lines(["worktree", "list", "--porcelain"], cwd=repo_root)
    if listing is None:
        return None
    entries: list[tuple[str, str]] = []
    current_path: Optional[str] = None
    current_branch = ""
    for line in listing + [""]:
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
    branch = branch_ref[len(prefix):]
    current = observe_worktree_head(resolved)
    if current is None or current[1] != branch:
        # The worktree's own HEAD must corroborate the SAME topology fact — an unreadable
        # local HEAD, or a branch that moved between the two reads, observes nothing.
        return None
    local_head = current[0]
    origin_head = observe_branch_origin_head(repo_root, branch)
    return LaneTopologyObservation(
        worktree=resolved, branch=branch, local_head=local_head, origin_head=origin_head
    )


def observe_lane_push(
    repo_root: Path, rows, selected: SelectedLane
) -> Optional[PushObservation]:
    """The action-time observation of the lane's ACTUAL branch head, worktree-bound.

    The branch comes from the same typed topology observation the worktree binding uses
    (:func:`observe_lane_topology`) — never inferred from the lane id (review j#86739 R3-F2)
    — and the head binds ONLY when the worktree's current local ``HEAD`` equals that branch's
    origin head (review j#86757 R4-F1): a clean local commit ahead of origin, a behind or
    diverged checkout, a detached worktree, or an absent origin ref binds no head — the lane
    is a typed ``head_unbound`` non-candidate.
    """
    observation = observe_lane_topology(
        repo_root,
        rows,
        workspace=selected.repo_workspace_id,
        lane=selected.lane_id,
        generation=selected.lane_generation,
    )
    return push_from_topology(observation, selected)


def push_from_topology(
    observation: Optional[LaneTopologyObservation], selected: SelectedLane
) -> Optional[PushObservation]:
    """The push observation derived from an already-taken topology observation."""
    if observation is None or not observation.pushed:
        return None
    return PushObservation(
        workspace=selected.repo_workspace_id,
        lane=selected.lane_id,
        lane_generation=selected.lane_generation,
        head=observation.origin_head,
        reachable=True,
    )


def resolve_candidate_worktree(
    workspace_root: Path, rows, candidate: HibernateCandidate
) -> Optional[Path]:
    """The candidate lane's canonical worktree via the same typed topology join (or ``None``)."""
    anchor = candidate.anchor
    observation = observe_lane_topology(
        workspace_root,
        rows,
        workspace=anchor.repo_workspace_id,
        lane=anchor.lane_id,
        generation=anchor.lane_generation,
    )
    return None if observation is None else observation.worktree


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


__all__ = [
    "LaneTopologyObservation",
    "committed_config_policy_pointer",
    "observe_worktree_head",
    "observe_branch_origin_head",
    "observe_lane_topology",
    "observe_lane_push",
    "push_from_topology",
    "resolve_candidate_worktree",
    "observe_worktree_clean",
    "_full_sha",
]
