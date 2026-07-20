"""`sublane hibernate` release-boundary IO orchestration (Redmine #13843).

The IO layer that drives the pure :mod:`sublane_hibernate_toctou` fence: the live
worktree fingerprint probe, the live lane-activity probe (worker-busy / pending-composer),
and the ``store`` + ``ops``-driven re-validation the use case runs at the release boundary
(T1) and post-release (T2). Split out of :mod:`sublane_hibernate` so the use-case module
stays under the module-health ceiling. All *policy* (what counts as a divergence, what a
block means) lives in the pure :mod:`sublane_hibernate_toctou` leaf; this module only
*observes* live state and *wires*.

The release boundary re-validation is the exact-generation heart of the fence (IR j#83536
item 2): before the process release it re-reads, on ONE fresh snapshot, the worktree
fingerprint, the live worker activity / pending composer, the live managed-slot set, the
lane lifecycle revision, and — for a project-gateway lane — the exact declared generation +
startup attestation, and blocks on any drift from the preflight (T0) capture.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.core.state.lane_lifecycle import (
    LaneLifecycleError,
    LaneLifecycleKey,
    LaneLifecycleStore,
    ProcessPinError,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_process_release import (  # noqa: E501
    declared_generation_attested,
    declared_generation_exactly_live,
    unit_slots,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_toctou import (  # noqa: E501
    BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT,
    BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT,
    BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT,
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


# ---------------------------------------------------------------------------
# Live worktree fingerprint (git status + tracked diff content + untracked stats).
# ---------------------------------------------------------------------------


def read_live_worktree_fingerprint(
    repo_root: Path, timeout: float
) -> WorktreeMutationFingerprint:
    """A fresh live worktree mutation fingerprint from ``git`` (Redmine #13843).

    Tri-state on the work-tree probe (Redmine #13843 review F4), so a genuine git-invocation
    failure is never mistaken for a clean non-git lane:

    - the ``git`` invocation itself failed (binary missing / timeout) -> unreadable (fail
      closed — never "clean");
    - ``git`` ran and reported a work tree (``rev-parse --is-inside-work-tree`` == ``true``)
      -> read the mutation fingerprint;
    - ``git`` ran and reported NOT a work tree (``false``), or a genuine "not a git
      repository" error -> readable, clean (a non-git scaffold lane has no VCS diff surface,
      so it hibernates on its assertions, unchanged);
    - ANY OTHER non-zero ``rev-parse`` (permission / dubious-ownership / cwd I/O) or an
      unreadable ``status`` / ``diff`` -> unreadable (fail closed). A blanket "non-zero ->
      clean" would fail OPEN on a worktree we merely could not inspect.

    **Content-sensitive digest (Redmine #13843 review F1).** ``git status --porcelain`` rows
    encode only a path's *status code* (`` M path`` / ``?? path``), not its content — so a
    worker writing MORE into an already-modified / already-untracked path leaves the porcelain
    rows unchanged. The digest therefore folds in the tracked diff CONTENT (``git diff HEAD``)
    and each untracked path's ``(size, mtime)``, so a content change to an already-listed path
    flips the digest (the concrete #13843 residue signal). The digest is order-independent
    (sorted rows / sorted untracked stats).
    """
    probe = _run_git(repo_root, timeout, "rev-parse", "--is-inside-work-tree")
    if probe is None:
        return WorktreeMutationFingerprint(readable=False)
    stdout = (probe.stdout or "").strip()
    if probe.returncode == 0 and stdout == "true":
        pass  # a work tree — read the fingerprint below
    elif probe.returncode == 0 and stdout == "false":
        # git dir / bare — not a lane work tree, no diff surface.
        return WorktreeMutationFingerprint(readable=True)
    elif probe.returncode != 0 and "not a git repository" in (probe.stderr or "").lower():
        # A genuine non-git scaffold lane (git ran and said so).
        return WorktreeMutationFingerprint(readable=True)
    else:
        # Any other failure (permission / dubious ownership / cwd I/O) -> fail closed.
        return WorktreeMutationFingerprint(readable=False)

    status = _run_git(
        repo_root, timeout, "status", "--porcelain=v1", "--untracked-files=all"
    )
    if status is None or status.returncode != 0:
        return WorktreeMutationFingerprint(readable=False)
    lines = sorted(line for line in status.stdout.splitlines() if line.strip())
    dirty = any(not line.startswith("??") for line in lines)
    untracked = any(line.startswith("??") for line in lines)

    # Tracked content: `git diff HEAD` is the total tracked change vs the committed state
    # (staged + unstaged). Its output changes when any already-modified file's content
    # changes. An unreadable diff -> fail closed (never a "clean" fingerprint).
    diff = _run_git(repo_root, timeout, "diff", "HEAD")
    if diff is None or diff.returncode != 0:
        return WorktreeMutationFingerprint(readable=False)
    untracked_stats = _untracked_stats(repo_root, lines)

    digest = hashlib.sha256()
    digest.update("\n".join(lines).encode("utf-8"))
    digest.update(b"\0DIFF\0")
    digest.update(diff.stdout.encode("utf-8", "surrogatepass"))
    digest.update(b"\0UNTRACKED\0")
    digest.update("\n".join(untracked_stats).encode("utf-8", "surrogatepass"))
    return WorktreeMutationFingerprint(
        readable=True, dirty=dirty, untracked=untracked, digest=digest.hexdigest()
    )


def _untracked_stats(repo_root: Path, porcelain_lines: Sequence[str]) -> list[str]:
    """``path\\0size\\0mtime_ns`` for each untracked path (Redmine #13843 review F1).

    Folds each untracked path's ``(size, mtime_ns)`` into the digest so a content change to
    an *already-untracked* file (whose ``?? path`` row does not change) still flips the
    fingerprint. A path that cannot be ``stat``-ed (removed / quoted / permission) contributes
    a stable ``MISSING`` marker rather than crashing — its ``?? path`` row already tracks
    presence, and the fail-closed diff/probe guards cover an unreadable worktree.
    """
    stats: list[str] = []
    for line in porcelain_lines:
        if not line.startswith("??"):
            continue
        path = line[3:].strip()
        try:
            st = (repo_root / path).stat()
            stats.append(f"{path}\0{st.st_size}\0{st.st_mtime_ns}")
        except OSError:
            stats.append(f"{path}\0MISSING")
    return sorted(stats)


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


# ---------------------------------------------------------------------------
# Live lane activity (worker running a turn / pending composer input).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LaneActivityObservation:
    """A fresh live observation of a lane's worker activity (Redmine #13843 review F2).

    ``readable`` fails closed by default: an un-observed activity state is treated as a
    divergence, never as "quiescent". ``worker_busy`` is any managed slot running a turn;
    ``composer_pending`` is a pending composer input (ghost-empty-refined so an idle
    placeholder does not false-positive).
    """

    readable: bool = False
    worker_busy: bool = False
    composer_pending: bool = False


def read_activity(
    ops: object, workspace_id: str, lane: str, rows: Sequence[Mapping[str, object]]
) -> LaneActivityObservation:
    """Read a live lane-activity observation via the ops port, fail-closed (#13843 F2).

    The ``read_lane_activity`` port method is optional so a pre-#13843 fake that never
    exercises the fence sees a quiescent (readable, not-busy, no-pending) lane; a probe that
    raises is folded to an unreadable observation (fail closed — the boundary then blocks).
    """
    reader = getattr(ops, "read_lane_activity", None)
    if reader is None:
        return LaneActivityObservation(readable=True)
    try:
        return reader(workspace_id, lane, rows)
    except Exception:  # noqa: BLE001 — unreadable activity -> fail closed
        return LaneActivityObservation(readable=False)


def read_live_lane_activity(
    rows: Sequence[Mapping[str, object]],
    workspace_id: str,
    lane: str,
    *,
    repo_root: Path,
    env: Mapping[str, str],
    runner: object,
    timeout: float,
) -> LaneActivityObservation:
    """Observe a lane's live worker-busy / pending-composer state at action time (F2).

    Mirrors the ``sublane_quarantine`` inspect path: for each live managed slot it reads the
    runtime state (``herdr agent get`` -> ``busy`` == a running turn) and the composer text
    (``read_pane`` -> :func:`observe_composer_text`, ghost-empty-refined so an idle
    placeholder is not a false pending). Fail-closed: an unresolved binary, an unreadable
    runtime state (``unknown``), or an unreadable composer read yields
    ``LaneActivityObservation(readable=False)`` — the boundary then blocks rather than
    trusting an un-observed lane. An empty live slot set (nothing to observe) is vacuously
    readable-quiescent (there is nothing to release either).
    """
    slots = unit_slots(rows, workspace_id, lane)
    if not slots:
        return LaneActivityObservation(readable=True)
    # Heavy provider surface — import lazily so the pure fence path never pays for it.
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
        _resolve_binary_or_die,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_state import (  # noqa: E501
        HerdrCliAgentStateReader,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.infrastructure.herdr_transport import (  # noqa: E501
        HerdrCliTransport,
    )
    from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.agent_state import (  # noqa: E501
        RUNTIME_BUSY,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_quarantine import (  # noqa: E501
        ComposerObservation,
        observe_composer_text,
    )
    from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_ghost_composer_observation import (  # noqa: E501
        apply_ghost_empty,
        default_ghost_policy,
        read_render_ghost_facts,
    )

    try:
        binary = _resolve_binary_or_die(env)
    except Exception:  # noqa: BLE001 — unresolved binary -> fail closed
        return LaneActivityObservation(readable=False)
    # ``runner`` is ``Optional`` on the live adapter (default ``None``); passing ``None`` would
    # override the readers' ``subprocess.run`` default, so resolve it here.
    effective_runner = runner if runner is not None else subprocess.run
    state_reader = HerdrCliAgentStateReader(binary, runner=effective_runner, timeout=timeout)
    transport = HerdrCliTransport(binary, runner=effective_runner, timeout=timeout)
    ghost_policy = default_ghost_policy()

    worker_busy = False
    composer_pending = False
    for _role, (_assigned_name, locator) in slots.items():
        try:
            state = state_reader.read_agent_state(locator)
        except Exception:  # noqa: BLE001 — transport failure -> fail closed
            return LaneActivityObservation(readable=False)
        if not state.ok:
            # An unreadable / unknown runtime state -> fail closed (never "idle").
            return LaneActivityObservation(readable=False)
        if state.state == RUNTIME_BUSY:
            worker_busy = True
        try:
            read = transport.read_pane(locator, lines=80)
        except Exception:  # noqa: BLE001 — pane read failure -> fail closed
            return LaneActivityObservation(readable=False)
        observation = (
            observe_composer_text(read.content) if read.ok else ComposerObservation(False, None)
        )
        if not observation.readable:
            return LaneActivityObservation(readable=False)
        effective_pending = apply_ghost_empty(
            observation.has_pending,
            policy=ghost_policy,
            repo_root=repo_root,
            env=env,
            locator=locator,
            facts_reader=read_render_ghost_facts,
        )
        if effective_pending is True:
            composer_pending = True
    return LaneActivityObservation(
        readable=True, worker_busy=worker_busy, composer_pending=composer_pending
    )


# ---------------------------------------------------------------------------
# Boundary (T1) re-validation + post-release (T2) check.
# ---------------------------------------------------------------------------


def read_fingerprint(ops: object) -> WorktreeMutationFingerprint:
    """Read a live WORKTREE fingerprint via the ops port, fail-closed (#13843).

    Worktree-only (dirty / untracked / digest); the lane-activity flags are folded in
    separately by :func:`revalidate_boundary`. A missing port method (a pre-#13843 fake)
    yields a clean, quiescent worktree; a raising probe -> unreadable (fail closed).
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
    store: LaneLifecycleStore,
    key: LaneLifecycleKey,
    rec0: object,
    rows0: Sequence[Mapping[str, object]],
    fingerprint_preflight: WorktreeMutationFingerprint,
    workspace_id: str,
    lane: str,
    project_scope: str,
) -> tuple[
    Sequence[Mapping[str, object]], WorktreeMutationFingerprint, tuple[str, ...]
]:
    """Release-boundary (T1) fresh re-read + full exact-generation re-validation (#13843).

    On ONE fresh snapshot, re-reads and re-validates every action-time dimension IR j#83536
    item 2 names, comparing to the preflight (T0) capture. Returns ``(rows_boundary,
    fingerprint_boundary, reasons)``; a non-empty ``reasons`` means the caller performs zero
    lifecycle transition / zero process close. Dimensions:

    - **worktree fingerprint + live activity** — the fresh worktree fingerprint with the live
      worker-busy / pending-composer flags folded in; any divergence / running mutation /
      pending composer / unreadable is a :func:`revalidate_release_boundary` block.
    - **live managed-slot set** — a changed ``assigned_name -> locator`` map is generation
      drift.
    - **lifecycle revision** — a revision that advanced since the preflight read (another
      process bumped it) is stale-authority drift.
    - **exact declared generation + startup attestation** (project-gateway lane) — the fresh
      inventory must still carry the lane's exact declared generation AND every live slot must
      still carry an action-time, generation-matched startup attestation.

    An unreadable fresh inventory blocks on the shared ``inventory_unreadable`` reason.
    ``rows_boundary`` is the fresh snapshot the release close then uses (never stale T0 rows).
    """
    rows1, readable1 = ops.read_inventory()  # type: ignore[attr-defined]
    fingerprint_worktree = read_fingerprint(ops)
    activity = read_activity(ops, workspace_id, lane, rows1)
    # Fold the live activity into the boundary fingerprint (worker-busy / pending-composer are
    # absolute-at-boundary signals; an unreadable activity read makes the fingerprint
    # unreadable -> the pure gate blocks).
    fingerprint_boundary = replace(
        fingerprint_worktree,
        mutation_in_flight=activity.worker_busy,
        pending_composer=activity.composer_pending,
        readable=fingerprint_worktree.readable and activity.readable,
    )
    if not readable1:
        return rows1, fingerprint_boundary, (BLOCK_INVENTORY_UNREADABLE,)

    reasons: list[str] = list(
        revalidate_release_boundary(
            fingerprint_preflight=fingerprint_preflight,
            fingerprint_boundary=fingerprint_boundary,
            slots_preflight=unit_slots(rows0, workspace_id, lane),
            slots_boundary=unit_slots(rows1, workspace_id, lane),
        ).reasons
    )

    # Exact-generation / attestation / lifecycle-revision fresh re-validation (F3).
    try:
        rec1 = store.get(key)
    except (LaneLifecycleError, OSError):
        rec1 = None
    if rec1 is None or rec0 is None or rec1.revision != getattr(rec0, "revision", None):
        reasons.append(BLOCK_RELEASE_BOUNDARY_REVISION_DRIFT)
    if project_scope and rec1 is not None:
        try:
            generation_live = declared_generation_exactly_live(
                rec1.declared_pins, rows1, workspace_id=workspace_id, lane_id=lane
            )
        except ProcessPinError:
            generation_live = False
        if not generation_live and BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT not in reasons:
            reasons.append(BLOCK_RELEASE_BOUNDARY_GENERATION_DRIFT)
        if not declared_generation_attested(
            rows1, workspace_id, lane, _attestation_reader(ops)
        ):
            reasons.append(BLOCK_RELEASE_BOUNDARY_ATTESTATION_DRIFT)
    return rows1, fingerprint_boundary, tuple(reasons)


def _attestation_reader(ops: object) -> Callable[[str], object]:
    reader = getattr(ops, "read_attestation", None)
    if reader is None:
        return lambda _assigned_name: None
    return reader


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
    "LaneActivityObservation",
    "post_release_residue",
    "read_activity",
    "read_fingerprint",
    "read_live_lane_activity",
    "read_live_worktree_fingerprint",
    "redrive_detail",
    "revalidate_boundary",
)
