"""Patch-equivalent integration fencing for terminal retire (Redmine #14066).

The #13845 hibernated bound terminal retire accepts a lane head as integrated only when
``--branch`` is a **literal ancestor** of ``--integration-branch`` (``git merge-base
--is-ancestor``). That is the standard ff-only integration. But the workflow's integration
disposition (central preset ``統合責務``) also admits a ``patch_equivalent`` integration: the
coordinator cherry-picks the review-approved commits onto the integration / staging branch and
records the stable patch-id / commit map in a durable integration journal. There the original
issue branch is **not** an ancestor of the integration branch (the cherry-picks carry different
commit hashes), so ``merge-base --is-ancestor`` reports ``False`` forever and a drained, closed,
hibernated / released lane can never reach the terminal ``retired`` disposition
(``head_not_integrated``). The live residual: #13846 (two rows) and #13879 (one row).

This pure domain supplies the action-time fence the application layer consults. It never runs
git, never reads a file, and never guesses: the application layer reads the coordinator's
durable integration disposition (the exact Redmine journal's structured block, captured to a
JSON observation) into :class:`PatchEquivalentDisposition`, **recomputes** the real git facts
(the current branch / integration heads, the unintegrated commit set, per-commit stable
patch-ids, origin reachability) into :class:`PatchEquivalentObservation`, and asks
:func:`evaluate_patch_equivalent_integrated` whether the two agree. The verdict is
``admissible`` only when every axis holds, each with a closed-vocabulary reason on refusal.

The bar is **positive, recomputed proof of patch-equivalence, never a coordinator assertion**.
The disposition's role is to (a) name which integration branch / head the coordinator
dispositioned (so the fence measures the right thing rather than guessing), (b) bound the
candidate integration commits (the commit map is the durable, finite set the fence verifies —
never an open history scan), and (c) carry the exact-journal anchor. Every claimed fact is
then cross-checked against a value the application layer measured from real git:

- **identity** — the disposition's ``issue`` / ``lane`` / ``branch`` / ``integration_branch``
  must equal the CLI-bound identity, so a disposition captured for another lane cannot license
  this one (:data:`PE_ISSUE_MISMATCH` / :data:`PE_LANE_MISMATCH` / :data:`PE_BRANCH_MISMATCH` /
  :data:`PE_INTEGRATION_BRANCH_MISMATCH`);
- **freshness** — the disposition's recorded ``source_head`` / ``integration_head`` must equal
  the branches' **current** resolved heads, so a stale disposition (captured before either
  branch moved) is refused (:data:`PE_SOURCE_HEAD_STALE` / :data:`PE_INTEGRATION_HEAD_STALE`);
- **coverage** — the mapped source commits must be EXACTLY the commits on ``branch`` not
  reachable from ``integration_branch`` (the real unintegrated-by-hash set): a missing commit
  means the map does not cover the whole head, an extra one means it claims a commit the branch
  does not carry (:data:`PE_COMMIT_MAP_INCOMPLETE`). An empty map proves nothing and is refused
  (:data:`PE_EMPTY_MAP`) — a genuinely integrated head is the literal-ancestor path, not this;
- **reachability** — every mapped integration commit must be reachable from the current
  integration head (:data:`PE_INTEGRATION_COMMIT_UNREACHABLE`);
- **equivalence** — for every pair, the recomputed stable patch-id of the source commit and of
  the integration commit must be non-empty, equal to each other, and equal to the disposition's
  recorded patch-id (a coordinator record that disagrees with the recomputed diff is refused:
  :data:`PE_PATCH_ID_UNRESOLVED` / :data:`PE_PATCH_ID_MISMATCH`);
- **origin reachability** — the integration head must be observed reachable from the recorded
  origin integration ref, and the disposition must assert it (:data:`PE_ORIGIN_UNREACHABLE`).

Missing / ambiguous / stale / mismatched evidence, an unreadable disposition, or a git probe
that could not answer all resolve to a refusal (the application layer maps an unreadable /
malformed disposition to its own fail-closed reason before this fence is even consulted).

Boundary (Redmine #14066): pure. No IO, no subprocess, no file read. Synthetic unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Mapping, Optional

# --- Closed-vocabulary admission reasons -----------------------------------
PE_OK = "ok"
PE_ISSUE_MISMATCH = "disposition_issue_mismatch"
PE_LANE_MISMATCH = "disposition_lane_mismatch"
PE_BRANCH_MISMATCH = "disposition_branch_mismatch"
PE_INTEGRATION_BRANCH_MISMATCH = "disposition_integration_branch_mismatch"
PE_SOURCE_HEAD_STALE = "source_head_stale"
PE_INTEGRATION_HEAD_STALE = "integration_head_stale"
PE_EMPTY_MAP = "empty_commit_map"
PE_COMMIT_MAP_INCOMPLETE = "commit_map_does_not_cover_head"
PE_INTEGRATION_COMMIT_UNREACHABLE = "integration_commit_unreachable"
PE_PATCH_ID_UNRESOLVED = "patch_id_unresolved"
PE_PATCH_ID_MISMATCH = "patch_id_mismatch"
PE_ORIGIN_UNREACHABLE = "integration_head_not_origin_reachable"


@dataclass(frozen=True)
class CommitPatchMapping:
    """One durable source -> integration commit pair with the coordinator's stable patch-id.

    ``source_commit`` is the original issue-branch commit hash; ``integration_commit`` is the
    cherry-picked commit hash on the integration branch; ``patch_id`` is the coordinator's
    recorded ``git patch-id --stable`` value the fence recomputes and cross-checks. All three
    are compared exactly (a short / non-canonical hash the application layer did not resolve to
    the same full value is a mismatch), so the disposition must record canonical values.
    """

    source_commit: str
    integration_commit: str
    patch_id: str


@dataclass(frozen=True)
class PatchEquivalentDisposition:
    """The coordinator's structured ``patch_equivalent`` integration disposition (claimed).

    Read by the application layer from the exact Redmine integration journal (captured to a
    durable JSON observation). Every field is a CLAIM the fence cross-checks against a value
    recomputed from real git; nothing here is trusted on its own.

    ``origin_ref`` is the remote-tracking ref the integration head is claimed reachable from
    (e.g. ``origin/int_13472_session_continuity``); ``origin_reachable`` is the coordinator's
    assertion, which the fence requires AND independently re-observes.
    """

    issue: str
    lane: str
    branch: str
    integration_branch: str
    source_head: str
    integration_head: str
    origin_ref: str
    origin_reachable: bool
    commit_map: tuple[CommitPatchMapping, ...] = ()
    journal_id: str = ""


@dataclass(frozen=True)
class PatchEquivalentObservation:
    """The action-time git facts the application layer recomputed (measured, never claimed).

    ``actual_source_head`` / ``actual_integration_head`` are the current resolved heads of the
    branch / integration branch (``git rev-parse``). ``unintegrated_source_commits`` is the set
    of commit hashes on ``branch`` NOT reachable from ``integration_branch`` (``git rev-list
    integration_branch..branch``) — the real unintegrated-by-hash set the map must cover exactly.
    ``integration_commit_reachable`` maps each mapped integration commit hash to whether it is an
    ancestor of the current integration head. ``patch_ids`` maps a commit hash to its recomputed
    ``git patch-id --stable`` value (empty string when the probe could not resolve one).
    ``integration_head_origin_reachable`` is whether the integration head was observed reachable
    from the disposition's ``origin_ref``.
    """

    actual_source_head: str
    actual_integration_head: str
    unintegrated_source_commits: frozenset[str]
    integration_commit_reachable: Mapping[str, bool] = field(default_factory=dict)
    patch_ids: Mapping[str, str] = field(default_factory=dict)
    integration_head_origin_reachable: bool = False


@dataclass(frozen=True)
class AdmissionResult:
    """Whether the patch-equivalent integration is admissible, with a fixed reason + detail."""

    admissible: bool
    reason: str = ""
    detail: str = ""

    def as_payload(self) -> dict:
        return {"admissible": self.admissible, "reason": self.reason, "detail": self.detail}


def _refuse(reason: str, detail: str) -> AdmissionResult:
    return AdmissionResult(False, reason, detail)


def evaluate_patch_equivalent_integrated(
    disposition: PatchEquivalentDisposition,
    observation: PatchEquivalentObservation,
    *,
    issue: str,
    lane: str,
    branch: str,
    integration_branch: str,
) -> AdmissionResult:
    """Fence: is the lane head patch-equivalent-integrated per the disposition? Pure.

    ``issue`` / ``lane`` / ``branch`` / ``integration_branch`` are the CLI-bound identity the
    disposition must describe. Returns an admissible verdict ONLY when every axis holds; every
    refusal carries a closed-vocabulary :data:`reason`. Nothing here reads git or a file — the
    application layer supplies both the claimed disposition and the recomputed observation.
    """
    # -- identity: the disposition must describe THIS lane ------------------
    if disposition.issue.strip() != (issue or "").strip():
        return _refuse(
            PE_ISSUE_MISMATCH,
            f"disposition issue {disposition.issue!r} != --issue {issue!r}",
        )
    if disposition.lane.strip() != (lane or "").strip():
        return _refuse(
            PE_LANE_MISMATCH,
            f"disposition lane {disposition.lane!r} != --lane-label {lane!r}",
        )
    if disposition.branch.strip() != (branch or "").strip():
        return _refuse(
            PE_BRANCH_MISMATCH,
            f"disposition branch {disposition.branch!r} != --branch {branch!r}",
        )
    if disposition.integration_branch.strip() != (integration_branch or "").strip():
        return _refuse(
            PE_INTEGRATION_BRANCH_MISMATCH,
            f"disposition integration_branch {disposition.integration_branch!r} != "
            f"--integration-branch {integration_branch!r}",
        )
    # -- freshness: the recorded heads must be the branches' CURRENT heads --
    src_head = disposition.source_head.strip()
    if not src_head or src_head != observation.actual_source_head.strip():
        return _refuse(
            PE_SOURCE_HEAD_STALE,
            f"disposition source_head {disposition.source_head!r} != current branch head "
            f"{observation.actual_source_head!r}; the disposition is stale or misbound",
        )
    int_head = disposition.integration_head.strip()
    if not int_head or int_head != observation.actual_integration_head.strip():
        return _refuse(
            PE_INTEGRATION_HEAD_STALE,
            f"disposition integration_head {disposition.integration_head!r} != current "
            f"integration head {observation.actual_integration_head!r}; the disposition is "
            "stale or misbound",
        )
    # -- coverage: the map must be EXACTLY the unintegrated-by-hash set -----
    if not disposition.commit_map:
        return _refuse(
            PE_EMPTY_MAP,
            "the disposition maps no commits; a patch-equivalent integration must enumerate "
            "the cherry-picked commits (an already-ff-integrated head is the literal-ancestor "
            "path, not this one)",
        )
    mapped_sources = [m.source_commit.strip() for m in disposition.commit_map]
    mapped_source_set = frozenset(mapped_sources)
    if len(mapped_sources) != len(mapped_source_set):
        return _refuse(
            PE_COMMIT_MAP_INCOMPLETE,
            "the disposition commit map lists a source commit more than once; it is ambiguous",
        )
    unintegrated = frozenset(c.strip() for c in observation.unintegrated_source_commits)
    if mapped_source_set != unintegrated:
        missing = sorted(unintegrated - mapped_source_set)
        extra = sorted(mapped_source_set - unintegrated)
        return _refuse(
            PE_COMMIT_MAP_INCOMPLETE,
            "the disposition commit map is not exactly the branch's unintegrated commit set "
            f"(unmapped on branch: {missing or 'none'}; mapped but not on branch: "
            f"{extra or 'none'}); every commit of --branch not already reachable from "
            "--integration-branch must be a mapped, patch-equivalent cherry-pick",
        )
    # -- per-pair reachability + equivalence -------------------------------
    for m in disposition.commit_map:
        src = m.source_commit.strip()
        integ = m.integration_commit.strip()
        claimed = m.patch_id.strip()
        if not observation.integration_commit_reachable.get(integ, False):
            return _refuse(
                PE_INTEGRATION_COMMIT_UNREACHABLE,
                f"mapped integration commit {integ!r} (for source {src!r}) is not reachable "
                "from the current integration head; the cherry-pick is not on the integration "
                "branch",
            )
        src_pid = (observation.patch_ids.get(src) or "").strip()
        integ_pid = (observation.patch_ids.get(integ) or "").strip()
        if not claimed or not src_pid or not integ_pid:
            return _refuse(
                PE_PATCH_ID_UNRESOLVED,
                f"a stable patch-id is missing (source {src!r}: {src_pid or 'unresolved'}, "
                f"integration {integ!r}: {integ_pid or 'unresolved'}, disposition: "
                f"{claimed or 'unrecorded'}); patch-equivalence cannot be proven",
            )
        if not (src_pid == integ_pid == claimed):
            return _refuse(
                PE_PATCH_ID_MISMATCH,
                f"patch-id disagreement for source {src!r} -> integration {integ!r}: "
                f"recomputed source {src_pid}, recomputed integration {integ_pid}, disposition "
                f"{claimed}; the cherry-pick is not patch-equivalent or the record is stale",
            )
    # -- origin reachability -----------------------------------------------
    if not disposition.origin_reachable or not observation.integration_head_origin_reachable:
        return _refuse(
            PE_ORIGIN_UNREACHABLE,
            "the integration head is not durably reachable from the recorded origin ref "
            f"({disposition.origin_ref!r}; disposition asserts={disposition.origin_reachable}, "
            f"observed={observation.integration_head_origin_reachable}); a terminal retire "
            "requires an origin-reachable integration",
        )
    return AdmissionResult(
        True,
        PE_OK,
        f"{len(disposition.commit_map)} commit(s) proven patch-equivalent and origin-reachable "
        f"on {disposition.integration_branch}",
    )


__all__ = (
    "PE_OK",
    "PE_ISSUE_MISMATCH",
    "PE_LANE_MISMATCH",
    "PE_BRANCH_MISMATCH",
    "PE_INTEGRATION_BRANCH_MISMATCH",
    "PE_SOURCE_HEAD_STALE",
    "PE_INTEGRATION_HEAD_STALE",
    "PE_EMPTY_MAP",
    "PE_COMMIT_MAP_INCOMPLETE",
    "PE_INTEGRATION_COMMIT_UNREACHABLE",
    "PE_PATCH_ID_UNRESOLVED",
    "PE_PATCH_ID_MISMATCH",
    "PE_ORIGIN_UNREACHABLE",
    "CommitPatchMapping",
    "PatchEquivalentDisposition",
    "PatchEquivalentObservation",
    "AdmissionResult",
    "evaluate_patch_equivalent_integrated",
)
