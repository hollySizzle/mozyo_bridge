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
import os
import stat
import subprocess
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

#: Redmine #13843 review (j#83805) F1: bound the untracked content fingerprint so it can
#: never hang or read without limit. A worker's residue is small code; anything past these
#: caps is anomalous and fails the fingerprint CLOSED (never a silent partial hash).
_MAX_UNTRACKED_FILE_BYTES = 64 * 1024 * 1024  # 64 MiB per untracked file
_MAX_UNTRACKED_FILES = 20_000  # total untracked paths hashed

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
    RECOVERY_ACTION_DETAIL,
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

    **Content-sensitive digest (Redmine #13843 review F1, R2 hardening).** ``git status``
    rows encode only a path's *status code* (`` M path`` / ``?? path``), not its content, so
    the digest folds in the actual CONTENT of every change:

    - **tracked** content via ``git diff HEAD --no-ext-diff --binary`` — the ``--binary`` full
      patch is content-sensitive even for binary files (a plain ``git diff`` collapses a binary
      change to ``Binary files ... differ``); ``--no-ext-diff`` pins the output to git's own
      diff, immune to a repo-configured external differ.
    - **untracked** content via a per-file SHA-256 of the file bytes — a ``(size, mtime)`` stat
      alone misses a same-size rewrite whose mtime a worker preserves / restores.
    - paths are enumerated from ``--porcelain=v1 -z`` (NUL-separated, UNQUOTED) so a
      special-character path is matched exactly, not folded to a quoted / MISSING token.

    So a content change to an ALREADY-listed (already-dirty / already-untracked) path flips the
    digest. Order-independent (sorted records / sorted untracked paths). Any unreadable git
    read (status / diff) fails closed.
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
        repo_root, timeout,
        "status", "--porcelain=v1", "-z", "--untracked-files=all",
        text=False,
    )
    if status is None or status.returncode != 0:
        return WorktreeMutationFingerprint(readable=False)
    records, untracked_paths = _parse_porcelain_z(status.stdout or b"")
    dirty = any(not r.startswith(b"??") for r in records)
    untracked = bool(untracked_paths)

    # Tracked content (binary-safe, external-differ-proof). ``--no-ext-diff`` + ``--no-textconv``
    # pin the output to git's own raw diff, immune to a repo-configured external / textconv diff
    # driver that could otherwise collapse distinct contents. An unreadable diff -> fail closed.
    diff = _run_git(
        repo_root, timeout,
        "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--binary",
        text=False,
    )
    if diff is None or diff.returncode != 0:
        return WorktreeMutationFingerprint(readable=False)

    digest = hashlib.sha256()
    for record in sorted(records):
        digest.update(record)
        digest.update(b"\0")
    digest.update(b"DIFF\0")
    digest.update(diff.stdout or b"")
    digest.update(b"\0UNTRACKED\0")
    if len(untracked_paths) > _MAX_UNTRACKED_FILES:
        # Anomalously many untracked paths -> fail closed (never a partially-hashed fingerprint).
        return WorktreeMutationFingerprint(readable=False)
    for path in sorted(untracked_paths):
        content = _hash_untracked(repo_root, path)
        if content is None:
            # A non-regular kind (symlink swap / FIFO / device), an lstat race, or a
            # size-cap breach -> fail closed (Redmine #13843 review j#83805 F1).
            return WorktreeMutationFingerprint(readable=False)
        digest.update(path)
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    return WorktreeMutationFingerprint(
        readable=True, dirty=dirty, untracked=untracked, digest=digest.hexdigest()
    )


def _parse_porcelain_z(raw: bytes) -> tuple[list[bytes], list[bytes]]:
    """Parse ``git status --porcelain=v1 -z`` bytes into (records, untracked_paths).

    Each record is ``XY SP <path>`` (unquoted, NUL-terminated); a rename / copy record is
    followed by a separate ``<origPath>`` NUL field which is consumed (not mistaken for its
    own record). ``records`` is the list of ``XY SP <path>`` fields; ``untracked_paths`` is
    the raw path bytes of the ``??`` entries.
    """
    fields = raw.split(b"\0")
    records: list[bytes] = []
    untracked: list[bytes] = []
    i = 0
    while i < len(fields):
        field = fields[i]
        if len(field) < 3:  # empty trailing field / malformed — skip
            i += 1
            continue
        xy = field[:2]
        path = field[3:]
        records.append(field)
        if xy == b"??":
            untracked.append(path)
        # A rename / copy (X in {R, C}) carries an extra source-path NUL field.
        if field[:1] in (b"R", b"C"):
            i += 2
        else:
            i += 1
    return records, untracked


def _hash_untracked(repo_root: Path, path: bytes) -> Optional[bytes]:
    """A path-kind-aware, no-follow, bounded content hash of an untracked path (#13843 F1 R4).

    Returns the content digest bytes, or ``None`` to fail the WHOLE fingerprint closed (the
    caller then returns an unreadable fingerprint). A content hash (not a ``(size, mtime)``
    stat) so a same-size / mtime-restored rewrite still flips the fingerprint.

    Redmine #13843 review j#83805 / j#83853 F1 hardening — kind-aware, no-follow, bounded,
    and IDENTITY-STABLE (the object we classify is the object we hash, unchanged throughout):

    - **symlink** — hashed by its OWN target *bytes* (``lstat`` + ``readlink``), NOT the
      followed target's content: a retarget (even to a same-content or dangling target) flips
      the digest, and a dangling link never reads a target. A re-``lstat`` after ``readlink``
      confirms the link's ``(st_dev, st_ino)`` did not change under us (observation-window
      swap -> fail closed).
    - **regular file** — opened ``O_RDONLY | O_NOFOLLOW | O_NONBLOCK`` and re-confirmed both
      REGULAR **and the SAME ``(st_dev, st_ino)`` as the ``lstat``** (a regular->regular inode
      swap in the ``lstat``->``open`` window is caught, review j#83853), then read up to
      :data:`_MAX_UNTRACKED_FILE_BYTES`, then re-``fstat`` to confirm the ``(dev, ino, size,
      mtime_ns)`` did not drift DURING the read (a mid-read mutation -> fail closed). A larger
      file fails closed rather than hashing a prefix.
    - **any other kind** (FIFO / device / socket / directory) — ``None`` (fail closed): a FIFO
      would otherwise BLOCK ``open`` indefinitely (outside git's timeout), and a device / socket
      has no bounded content.
    - an ``lstat`` failure / identity drift / read-window mutation — ``None`` (fail closed).
    """
    full = repo_root / os.fsdecode(path)
    try:
        info = os.lstat(full)  # NO-follow: classify the path itself, never its target.
    except OSError:
        return None  # race / permission -> fail closed
    if stat.S_ISLNK(info.st_mode):
        try:
            target = os.readlink(full)
            after = os.lstat(full)
        except OSError:
            return None
        if (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
            return None  # the link was swapped during the readlink window -> fail closed
        return hashlib.sha256(b"SYMLINK\0" + os.fsencode(target)).digest()
    if not stat.S_ISREG(info.st_mode):
        return None  # FIFO / device / socket / dir -> fail closed (never open)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(full, flags)
    except OSError:
        return None  # a swap to a symlink (ELOOP) / FIFO / vanished path -> fail closed
    try:
        opened = os.fstat(fd)
        # Same KIND and same IDENTITY as the lstat: a regular->regular inode swap in the
        # lstat->open window opens a DIFFERENT object, caught here (review j#83853).
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
            info.st_dev,
            info.st_ino,
        ):
            return None
        file_digest = hashlib.sha256(b"FILE\0")
        total = 0
        while True:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                return None  # e.g. EAGAIN on a non-blocking special file -> fail closed
            if not chunk:
                break
            total += len(chunk)
            if total > _MAX_UNTRACKED_FILE_BYTES:
                return None  # over the per-file cap -> fail closed
            file_digest.update(chunk)
        # The open fd pins the inode, but its CONTENT could be rewritten in place during the
        # read; a drift in identity / size / mtime means we hashed an inconsistent snapshot.
        settled = os.fstat(fd)
        if (settled.st_dev, settled.st_ino, settled.st_size, settled.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            return None  # mutated during the read -> fail closed
        return file_digest.digest()
    finally:
        os.close(fd)


def _run_git(
    repo_root: Path, timeout: float, *args: str, text: bool = True
) -> Optional[subprocess.CompletedProcess]:
    # ``text=False`` captures raw BYTES — required for ``-z`` (NUL-separated, unquoted paths)
    # and ``--binary`` diffs, which are not valid text and must not be utf-8 decoded.
    try:
        return subprocess.run(
            ["git", *args],
            cwd=repo_root,
            text=text,
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
    runtime state (``herdr agent get``) and the composer text (``read_pane`` ->
    :func:`observe_composer_text`, ghost-empty-refined so an idle placeholder is not a false
    pending).

    **State allowlist (Redmine #13843 review F2, R2 hardening).** Only an explicitly QUIESCENT
    runtime state (``awaiting_input`` / ``turn_ended``) is safe to release over. Fail-closed:
    an unresolved binary, a mechanically-failed read (``ok=False``), OR a *successful* read
    whose state is ``unknown`` (an observed-but-unrecognised state, ``agent_state.py``
    contract) yields ``readable=False`` — the boundary blocks. Any other observed state (a
    running ``busy`` turn OR a ``blocked`` permission-prompt in-flight) is NON-quiescent and
    sets ``worker_busy`` (never mistaken for idle). An empty live slot set (nothing to observe
    / nothing to release) is vacuously readable-quiescent.
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
        RUNTIME_AWAITING_INPUT,
        RUNTIME_TURN_ENDED,
        RUNTIME_UNKNOWN,
    )

    # Only these two states are quiescent (safe to hibernate over); everything else is either
    # non-quiescent (busy / blocked) or a fail-closed unknown.
    quiescent_states = {RUNTIME_AWAITING_INPUT, RUNTIME_TURN_ENDED}
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
        if not state.ok or state.state == RUNTIME_UNKNOWN:
            # A mechanically-failed read OR an observed-but-unrecognised state -> fail closed
            # (never "idle"); a novel/unknown observation must not authorize a release.
            return LaneActivityObservation(readable=False)
        if state.state not in quiescent_states:
            # busy (running turn) or blocked (permission prompt in-flight) -> non-quiescent.
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


def fresh_release_disposition(
    release, post_check: PostReleaseCheck
) -> tuple[bool, str, str]:
    """Resolve a fresh hibernate's ``(success_withheld, recovery_detail, detail)`` (#13843).

    A revision-drift admission block (review F3) OR a post-release residue withholds the
    success (the lane stays hibernated; the release is resumed later with current authority).
    A clean release is a plain success.
    """
    if getattr(release, "admission_blocked", False):
        return (
            True,
            post_check.recovery_detail or RECOVERY_ACTION_DETAIL,
            "lane hibernated; release admission blocked by revision drift — success withheld, "
            "re-drive with current authority via `sublane resume`",
        )
    if post_check.residue_detected:
        return (
            True,
            post_check.recovery_detail,
            "lane hibernated; managed processes released but post-release worktree residue "
            "detected — success withheld, converge to recovery/boundary-record",
        )
    return False, "", "lane hibernated; managed processes released"


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
    "fresh_release_disposition",
    "post_release_residue",
    "read_activity",
    "read_fingerprint",
    "read_live_lane_activity",
    "read_live_worktree_fingerprint",
    "redrive_detail",
    "revalidate_boundary",
)
