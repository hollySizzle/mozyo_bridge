"""`sublane hibernate` release-boundary IO orchestration (Redmine #13843).

The thin IO layer that drives the pure :mod:`sublane_hibernate_toctou` fence: the live
``git status`` → :class:`WorktreeMutationFingerprint` probe and the small
``ops``-driven helpers the use case calls at the release boundary (T1) and post-release
(T2). Split out of :mod:`sublane_hibernate` so the use-case module stays under the
module-health ceiling (mirroring the :mod:`sublane_hibernate_assertions` /
:mod:`sublane_hibernate_toctou` leaf extractions). All policy — what counts as a
divergence, what a block means — lives in the pure :mod:`sublane_hibernate_toctou` leaf;
this module only *observes* and *wires*.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    unit_slots,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    CLEAN_WORKTREE_FINGERPRINT,
    PostReleaseCheck,
    WorktreeMutationFingerprint,
    post_release_check,
    revalidate_release_boundary,
)

# The shared inventory-unreadable reason (kept in sync with :mod:`sublane_hibernate`): a
# boundary re-read that becomes unreadable fails closed on the same vocabulary the preflight
# uses, so the operator sees one consistent reason.
BLOCK_INVENTORY_UNREADABLE = "inventory_unreadable"


def read_live_worktree_fingerprint(
    repo_root: Path, timeout: float
) -> WorktreeMutationFingerprint:
    """A fresh live worktree mutation fingerprint from ``git status`` (Redmine #13843).

    Reduces ``git status`` over ``repo_root`` to a stable, comparable fingerprint. Tri-state,
    so a genuine git-invocation failure is never mistaken for a clean non-git lane:

    - the ``git`` invocation itself failed (binary missing / timeout) -> unreadable (fail
      closed — never "clean");
    - ``git`` ran but this is NOT a work tree (a non-git scaffold lane) -> readable, clean
      (there is no VCS diff surface — such a lane hibernates on its assertions, unchanged);
    - a work tree whose ``status`` could not be read -> unreadable (fail closed);
    - a readable work tree -> the parsed dirty / untracked / digest fingerprint.

    The digest is taken over the SORTED porcelain status lines, so it flips when any file's
    modified / untracked status changes (the concrete #13843 signal) and is independent of
    row order. The ``mutation_in_flight`` / ``pending_composer`` activity flags are left
    ``False`` here: the live worktree digest IS the observable worktree-mutation signal, and
    a richer live composer / turn probe is reserved for a future wiring rather than fabricated.
    """
    probe = _run_git(repo_root, timeout, "rev-parse", "--is-inside-work-tree")
    if probe is None:
        # The git invocation itself failed -> fail closed (never "clean").
        return WorktreeMutationFingerprint(readable=False)
    if probe.returncode != 0 or probe.stdout.strip() != "true":
        # git ran and reported this is not a work tree: a non-git scaffold lane.
        return WorktreeMutationFingerprint(readable=True)
    result = _run_git(
        repo_root, timeout, "status", "--porcelain=v1", "--untracked-files=all"
    )
    if result is None or result.returncode != 0:
        # An unreadable status -> fail closed (never a "clean" fingerprint).
        return WorktreeMutationFingerprint(readable=False)
    lines = sorted(line for line in result.stdout.splitlines() if line.strip())
    dirty = any(not line.startswith("??") for line in lines)
    untracked = any(line.startswith("??") for line in lines)
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return WorktreeMutationFingerprint(
        readable=True, dirty=dirty, untracked=untracked, digest=digest
    )


def _run_git(
    repo_root: Path, timeout: float, *args: str
) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def read_fingerprint(ops: object) -> WorktreeMutationFingerprint:
    """Read a live worktree mutation fingerprint via the ops port, fail-closed (#13843).

    The ``read_worktree_mutation`` port method is optional so a pre-#13843 fake that never
    exercises the fence sees a clean, quiescent worktree (the fence is then a no-op); a probe
    that raises is folded to an unreadable fingerprint (fail closed — the release-boundary
    re-validation then blocks rather than trusting it).
    """
    reader = getattr(ops, "read_worktree_mutation", None)
    if reader is None:
        return CLEAN_WORKTREE_FINGERPRINT
    try:
        return reader()
    except Exception:  # noqa: BLE001 — unreadable fingerprint -> fail closed
        return WorktreeMutationFingerprint(readable=False)


def revalidate_boundary(
    *,
    ops: object,
    rows0: Sequence[Mapping[str, object]],
    fingerprint_preflight: WorktreeMutationFingerprint,
    workspace_id: str,
    lane: str,
) -> tuple[
    Sequence[Mapping[str, object]], WorktreeMutationFingerprint, tuple[str, ...]
]:
    """Release-boundary (T1) fresh re-read + re-validation (Redmine #13843).

    Re-reads a FRESH inventory + worktree fingerprint via ``ops`` and compares them to the
    preflight (T0) snapshot. Returns ``(rows_boundary, fingerprint_boundary, reasons)``: a
    non-empty ``reasons`` means the caller must NOT proceed (zero close / zero transition). An
    unreadable fresh inventory blocks on the shared ``inventory_unreadable`` reason; otherwise
    the pure :func:`revalidate_release_boundary` decides on the worktree fingerprint drift +
    live managed-slot drift. ``rows_boundary`` is the fresh snapshot the release close then
    uses (never the stale preflight rows).
    """
    rows1, readable1 = ops.read_inventory()  # type: ignore[attr-defined]
    fingerprint_boundary = read_fingerprint(ops)
    if not readable1:
        return rows1, fingerprint_boundary, (BLOCK_INVENTORY_UNREADABLE,)
    reval = revalidate_release_boundary(
        fingerprint_preflight=fingerprint_preflight,
        fingerprint_boundary=fingerprint_boundary,
        slots_preflight=unit_slots(rows0, workspace_id, lane),
        slots_boundary=unit_slots(rows1, workspace_id, lane),
    )
    return rows1, fingerprint_boundary, reval.reasons


def post_release_residue(
    *, ops: object, fingerprint_boundary: WorktreeMutationFingerprint
) -> PostReleaseCheck:
    """Post-release (T2) worktree post-check via the ops port (Redmine #13843).

    Re-reads the worktree fingerprint after the close and detects an unexpected dirty mutation
    that raced in during the close window (or an unreadable post fingerprint). The lane is
    never rolled back; only the success report is withheld.
    """
    return post_release_check(
        fingerprint_boundary=fingerprint_boundary,
        fingerprint_post=read_fingerprint(ops),
    )


def redrive_detail(
    *,
    redrive_ok: bool,
    boundary_reasons: tuple[str, ...],
    post_residue: bool,
) -> str:
    """The human detail string for an already-hibernated redrive outcome (#13843)."""
    if not redrive_ok:
        return (
            "lane already hibernated; release re-drive blocked (preservation gate "
            "unmet or inventory unreadable)"
        )
    if boundary_reasons:
        return (
            "lane already hibernated; release re-drive blocked by release-boundary "
            "re-validation (" + ", ".join(boundary_reasons) + ")"
        )
    if post_residue:
        return (
            "lane already hibernated; resumed release but post-release worktree residue "
            "detected — success withheld, converge to recovery/boundary-record"
        )
    return "lane already hibernated; resumed release"


__all__ = (
    "BLOCK_INVENTORY_UNREADABLE",
    "post_release_residue",
    "read_fingerprint",
    "read_live_worktree_fingerprint",
    "redrive_detail",
    "revalidate_boundary",
)
