"""Branch / binding evidence for a bound lane whose checkout is GONE (Redmine #15789).

``sublane reboot-audit`` describes a closed / live-zero / BOUND lane whose recorded worktree
was wiped by a host reboot as having TWO safe rails: restore the exact checkout
(``restore_worktree``), or terminalize the lifecycle metadata
(``terminalize_bound_metadata``) "when the checkout is not wanted back". The second one had no
executable path: the bound terminal retires (``--retire-active-live-zero`` /
``--retire-hibernated-bound``) keep the checkout in preflight scope and refuse the very shape
the audit prescribes them for, with ``integration_blocked`` /
``worktree_missing_after_reboot`` (measured live on #15151 j#108983, reproduced on #15789).

The gap is not one gate. A wiped checkout breaks the bound rails in three places:

1. the retire preflight's positively-measured ``worktree_missing`` fact blocks;
2. the ``worktree_branch_mismatch`` gate compares ``--branch`` against ``git -C <worktree>
   rev-parse --abbrev-ref HEAD``, which cannot run without the checkout;
3. the canonical binding token family (``wt_`` linked worktree vs ``dl_`` directory-scaffold
   lane) is decided by :func:`...sublane_herdr_projection.is_git_worktree_root`, a **live disk
   probe**. A missing path is not a directory, so the derivation flips to ``dl_`` and the
   attestation can never match the row's recorded ``wt_`` binding.

This module supplies the evidence that closes (2) and (3) **without lowering the bar**, from
git's own worktree administrative record rather than from the checkout:

    $ git -C <repo> worktree list --porcelain
    worktree /private/tmp/lane_xyz
    HEAD 7c4cac45...
    branch refs/heads/issue_15631_cockpit_actions
    prunable gitdir file points to non-existent location

That record survives the reboot (the #13490 j#89060 evidence is precisely "branches, commits
and the prunable administrative entries survived; only the checkouts are gone"), and it is the
SAME authority the present-checkout rail consults — ``rev-parse --abbrev-ref HEAD`` inside a
linked worktree reads the entry git keeps for it. So the branch ↔ lane tie is not weakened, it
is read from the surviving half of the same source. It additionally proves the path was a
worktree **of this repo**, which is what licenses asserting the ``wt_`` family in (3) instead
of probing a path that is no longer there.

Deliberately fail-closed, and deliberately NOT a general relaxation:

- a checkout that is actually PRESENT is refused (:data:`ABSENT_WT_WORKTREE_PRESENT`) — the
  ordinary bound rail owns that shape and there must be no silent overlap between the two;
- an administrative entry that git does not call ``prunable`` is refused
  (:data:`ABSENT_WT_NOT_PRUNABLE`): a ``locked`` entry is an operator's deliberate hold, and an
  entry git still considers live contradicts the caller's premise;
- an entry that was ALREADY pruned is refused (:data:`ABSENT_WT_NOT_REGISTERED`). Nothing then
  ties ``--branch`` to the lane, and inventing that tie from a display cache is exactly the
  substitution this module exists to avoid. Restore the checkout, or converge the row through
  a rail whose identity fence does not need a binding at all;
- a detached or differently-branched entry is refused
  (:data:`ABSENT_WT_BRANCH_MISMATCH`), the same refusal the present-checkout gate produces.

Head integration is NOT touched here and is not relaxed anywhere: the bound rails keep
measuring ``--branch``'s ancestry into ``--integration-branch`` from real refs, which never
needed a checkout. This module only restores the two facts the missing checkout took away.

No writes, no ``git worktree prune``, no ref mutation: every probe is read-only.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

#: The caller named no ``--worktree``. The bound rails resolve the lane unit from it, so the
#: absent-checkout evidence has nothing to be about.
ABSENT_WT_NO_WORKTREE_ANCHOR = "no_worktree_anchor"
#: The caller named no ``--branch``. The whole point of this evidence is to tie a branch to the
#: lane; without one there is nothing to tie.
ABSENT_WT_NO_BRANCH = "no_branch_anchor"
#: ``--worktree`` still exists on disk. The ordinary bound terminal retire attests it directly;
#: this rail must never become a second way to do the same thing.
ABSENT_WT_WORKTREE_PRESENT = "worktree_present"
#: ``git worktree list --porcelain`` could not be read (git absent, non-repo, non-zero exit).
#: A probe that did not run has proven nothing.
ABSENT_WT_LIST_UNREADABLE = "worktree_list_unreadable"
#: The repo's worktree administrative record carries no entry for ``--worktree``. Either it was
#: already pruned, or the path was never this repo's worktree.
ABSENT_WT_NOT_REGISTERED = "worktree_not_registered"
#: git does not consider the entry prunable — it is ``locked``, or git still believes the
#: checkout is live. Either contradicts "the reboot wiped it".
ABSENT_WT_NOT_PRUNABLE = "worktree_not_prunable"
#: The surviving entry names a different branch than ``--branch`` (or is detached, which
#: carries no ``branch`` line at all).
ABSENT_WT_BRANCH_MISMATCH = "worktree_branch_mismatch"

ABSENT_WT_REASONS = frozenset(
    {
        ABSENT_WT_NO_WORKTREE_ANCHOR,
        ABSENT_WT_NO_BRANCH,
        ABSENT_WT_WORKTREE_PRESENT,
        ABSENT_WT_LIST_UNREADABLE,
        ABSENT_WT_NOT_REGISTERED,
        ABSENT_WT_NOT_PRUNABLE,
        ABSENT_WT_BRANCH_MISMATCH,
    }
)


@dataclass(frozen=True)
class AbsentWorktreeEvidence:
    """What git's surviving administrative entry proves about a wiped lane checkout.

    ``admissible`` is the only field a rail may branch on. When it is false the rail refuses
    with ``reason`` / ``detail`` verbatim and writes nothing; when it is true, ``branch`` is the
    branch git records for the entry (already proven equal to the caller's ``--branch``),
    ``head_sha`` is the head git last recorded for it (reported for the durable record, never a
    gate), and ``metadata_token`` / ``legacy_token`` are the canonical binding tokens derived
    with the ``wt_`` family ASSERTED — licensed by git having just named the path as a worktree
    of this repo, and still subject to the rails' exact-equality attestation against the row.
    """

    admissible: bool
    reason: str = ""
    detail: str = ""
    worktree_path: str = ""
    branch: str = ""
    head_sha: str = ""
    metadata_token: str = ""
    legacy_token: str = ""

    def as_payload(self) -> dict:
        return {
            "admissible": self.admissible,
            "reason": self.reason,
            "detail": self.detail,
            "worktree_path": self.worktree_path,
            "branch": self.branch,
            "head_sha": self.head_sha,
        }


def _refused(reason: str, detail: str, *, worktree_path: str = "") -> AbsentWorktreeEvidence:
    return AbsentWorktreeEvidence(
        admissible=False, reason=reason, detail=detail, worktree_path=worktree_path
    )


@dataclass(frozen=True)
class _WorktreeEntry:
    """One ``git worktree list --porcelain`` stanza, decoded."""

    path: str = ""
    head: str = ""
    branch: str = ""
    prunable: bool = False
    locked: bool = False


def parse_worktree_list_porcelain(text: str) -> tuple[_WorktreeEntry, ...]:
    """Decode ``git worktree list --porcelain`` output into typed stanzas (pure).

    Stanzas are separated by a blank line; each opens with ``worktree <path>``. ``branch`` is
    reported as a full ``refs/heads/<name>`` ref and is stripped to the short name here; a
    detached entry simply carries no ``branch`` line and decodes to an empty ``branch``, which
    the caller then refuses as a mismatch rather than as an absence.
    """
    entries: list[_WorktreeEntry] = []
    current: dict[str, object] = {}

    def flush() -> None:
        if current.get("path"):
            entries.append(
                _WorktreeEntry(
                    path=str(current.get("path") or ""),
                    head=str(current.get("head") or ""),
                    branch=str(current.get("branch") or ""),
                    prunable=bool(current.get("prunable")),
                    locked=bool(current.get("locked")),
                )
            )
        current.clear()

    for raw in (text or "").splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            flush()
            continue
        key, _, value = line.partition(" ")
        value = value.strip()
        if key == "worktree":
            flush()
            current["path"] = value
        elif key == "HEAD":
            current["head"] = value
        elif key == "branch":
            current["branch"] = (
                value[len("refs/heads/") :] if value.startswith("refs/heads/") else value
            )
        elif key == "prunable":
            current["prunable"] = True
        elif key == "locked":
            current["locked"] = True
    flush()
    return tuple(entries)


def _resolved(path: str) -> Optional[Path]:
    """Normalize a path the same way the binding writer did, or ``None`` if unusable.

    ``Path.resolve()`` is non-strict, so a wiped tail still normalizes while any surviving
    symlinked parent (the ``/tmp`` -> ``/private/tmp`` shape every recorded lane worktree in the
    #13490 evidence carries) resolves exactly as it did when the binding was written.
    """
    try:
        return Path(path).expanduser().resolve()
    except (OSError, ValueError, RuntimeError):
        return None


def _run_git(repo_root: Path, args: Sequence[str]):
    return subprocess.run(
        ["git", "-C", str(repo_root), *args], text=True, capture_output=True
    )


def resolve_absent_worktree_evidence(
    repo_root: Path,
    *,
    worktree: Optional[str],
    branch: Optional[str],
    lane_label: str,
    runner: Optional[Callable[[Path, Sequence[str]], object]] = None,
) -> AbsentWorktreeEvidence:
    """Prove (or refuse) that ``worktree`` is this repo's wiped checkout of ``branch``.

    ``runner`` is an injectable seam so the decision can be exercised against canned git output
    without a repository; production passes nothing and the real ``git`` runs. Every failure
    mode returns a refusal — this function never raises into a retire rail.
    """
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
        lane_root_identity,
    )

    wanted_path = (worktree or "").strip()
    wanted_branch = (branch or "").strip()
    if not wanted_path:
        return _refused(
            ABSENT_WT_NO_WORKTREE_ANCHOR,
            "the absent-checkout terminal retire still resolves the lane unit from its "
            "--worktree anchor; without it no lane identity can be established",
        )
    if not wanted_branch:
        return _refused(
            ABSENT_WT_NO_BRANCH,
            "the absent-checkout terminal retire ties --branch to the lane through git's "
            "surviving worktree entry; without --branch there is nothing to tie",
            worktree_path=wanted_path,
        )
    resolved = _resolved(wanted_path)
    if resolved is None:
        return _refused(
            ABSENT_WT_NO_WORKTREE_ANCHOR,
            "--worktree does not normalize to a usable path",
            worktree_path=wanted_path,
        )
    if resolved.exists():
        return _refused(
            ABSENT_WT_WORKTREE_PRESENT,
            f"--worktree {resolved} still exists, so this is not the wiped-checkout shape. "
            "The ordinary bound terminal retire attests the real checkout directly; run it "
            "without --worktree-absent",
            worktree_path=str(resolved),
        )

    run = runner or _run_git
    try:
        result = run(repo_root, ("worktree", "list", "--porcelain"))
    except OSError as exc:
        return _refused(
            ABSENT_WT_LIST_UNREADABLE,
            f"`git worktree list --porcelain` could not be run in {repo_root} "
            f"({type(exc).__name__}); a probe that did not run proves nothing",
            worktree_path=str(resolved),
        )
    if getattr(result, "returncode", 1) != 0:
        return _refused(
            ABSENT_WT_LIST_UNREADABLE,
            f"`git worktree list --porcelain` failed in {repo_root} "
            f"({(getattr(result, 'stderr', '') or '').strip() or 'non-zero exit'})",
            worktree_path=str(resolved),
        )

    entry = None
    for candidate in parse_worktree_list_porcelain(getattr(result, "stdout", "") or ""):
        candidate_path = _resolved(candidate.path)
        if candidate_path is not None and candidate_path == resolved:
            entry = candidate
            break
    if entry is None:
        return _refused(
            ABSENT_WT_NOT_REGISTERED,
            f"the repo's worktree administrative record carries no entry for {resolved}; it "
            "was already pruned, or the path was never this repo's worktree. Nothing then "
            "ties --branch to this lane, so the terminal retire fails closed — restore the "
            "checkout, or converge the row through a rail that needs no binding",
            worktree_path=str(resolved),
        )
    if not entry.prunable:
        return _refused(
            ABSENT_WT_NOT_PRUNABLE,
            f"git does not report the entry for {resolved} as prunable"
            + (" (it is locked)" if entry.locked else "")
            + "; either an operator deliberately holds it, or git still believes the checkout "
            "is live. Both contradict the wiped-checkout premise",
            worktree_path=str(resolved),
        )
    if entry.branch != wanted_branch:
        return _refused(
            ABSENT_WT_BRANCH_MISMATCH,
            f"git's surviving entry for {resolved} records "
            f"{entry.branch or '<detached/unresolved>'}, not --branch {wanted_branch}; its "
            "integrated evidence cannot be attributed to the lane's branch, so the terminal "
            "retire fails closed",
            worktree_path=str(resolved),
        )

    # The ``wt_`` family is ASSERTED rather than probed: git has just named this exact path as a
    # worktree of this repo, which is strictly more than the live ``is_git_workspace`` probe
    # could establish on a path that no longer exists. The token still has to match the row's
    # recorded binding byte-for-byte at the rails' attestation, so a ``dl_`` row (a
    # directory-scaffold lane, which git would never list here anyway) cannot pass.
    identity = lane_root_identity(str(resolved), lane_label, git_worktree=True)
    return AbsentWorktreeEvidence(
        admissible=True,
        worktree_path=str(resolved),
        branch=entry.branch,
        head_sha=entry.head,
        metadata_token=identity.metadata_token,
        legacy_token=identity.legacy_token,
    )


__all__ = (
    "ABSENT_WT_BRANCH_MISMATCH",
    "ABSENT_WT_LIST_UNREADABLE",
    "ABSENT_WT_NOT_PRUNABLE",
    "ABSENT_WT_NOT_REGISTERED",
    "ABSENT_WT_NO_BRANCH",
    "ABSENT_WT_NO_WORKTREE_ANCHOR",
    "ABSENT_WT_REASONS",
    "ABSENT_WT_WORKTREE_PRESENT",
    "AbsentWorktreeEvidence",
    "parse_worktree_list_porcelain",
    "resolve_absent_worktree_evidence",
)
