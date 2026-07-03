"""Doctor workspace-registry section boundary (#12924).

The ``doctor_workspace_registry_section`` collector historically mixed two
responsibilities in the ``doctor`` body: the *external read* (the home registry
health probe, the registry row load by path, the on-disk workspace anchor read,
the anchor name-compat resolution, the canonical-session resolution, and the
live tmux session enumeration) and the *verdict policy* that folds those reads
into the four-layer section dict (home registry / registration / anchor +
consistency / runtime) plus the overall status and next-action wording. This
module carves the collector slice out of the ``doctor`` body into an OOP-first
boundary (#12638 / #12892 / #12893 follow-up):

- :class:`WorkspaceRegistryReads` is the port for the *external reads* and
  :class:`LiveWorkspaceRegistryReads` the live adapter over the real
  :mod:`mozyo_bridge.workspace_registry` queries and the tmux liveness probe.
  The adapter owns every registry / anchor / tmux read; the verdict policy never
  touches the registry, the filesystem, or tmux directly.
- :func:`evaluate_workspace_registry_section` is the verdict policy. It calls the
  port and re-assembles the legacy section dict byte-for-byte (key order, status
  vocabulary, and note wording unchanged) so ``run_doctor`` aggregation, JSON
  output, and ``format_doctor_text`` rendering are unchanged.
- :class:`WorkspaceRegistrySectionUseCase` composes the port and the policy.

Like the state-store boundary (#12893) this section's reads are *conditional*:
the registry row is only loaded once the health probe says the registry is
actually usable, and an unusable registry leaves registration state ``None``
("unknown") rather than guessing. The verdict policy therefore orchestrates the
reads through the injected port in the legacy order, keeping the read sequence
(and the strict read-only invariant) byte-identical to the legacy collector.

Read-only contract (#11426 / #11920 / #11921): the surface never creates the
registry, never writes ``last_seen``, and never touches the anchor. Only genuine
problems flip the section red — an unreadable registry (``error``), an
unsupported schema (``invalid``), or a registry/anchor workspace-id ``drift``
(``drifted``). A never-registered workspace, a missing registry with a recovery
anchor, and a missing anchor are all normal, recoverable states and stay ``ok``
with an actionable hint. Liveness is a tmux question, never a registry one: the
registry ``last_seen`` is a registration-time cache, explained — never conflated
— with live runtime state.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from mozyo_bridge.workspace_registry import (
    ANCHOR_LEGACY_RELATIVE,
    ANCHOR_RELATIVE,
    REGISTRY_HEALTH_INVALID_SCHEMA,
    REGISTRY_HEALTH_MISSING,
    REGISTRY_HEALTH_OK,
    REGISTRY_HEALTH_UNREADABLE,
)


@runtime_checkable
class WorkspaceRegistryReads(Protocol):
    """Port: the read-only registry / anchor / tmux probes the section depends on.

    Implementations own every registry, anchor, and tmux access. The verdict
    policy depends only on the returned records and dicts — it never opens the
    registry, reads the filesystem, or queries tmux itself, so it is exercisable
    with synthetic reads. Every method is strictly read-only: it must never
    create the registry, write ``last_seen``, or touch the anchor.
    """

    def inspect_registry_health(self, home: Path | None) -> dict[str, Any]:
        ...

    def load_workspace_by_path(self, target: Path, home: Path | None) -> Any | None:
        ...

    def read_anchor(self, target: Path) -> dict[str, Any] | None:
        ...

    def anchor_resolution(self, target: Path) -> Any:
        ...

    def resolve_canonical_session(self, target: Path, home: Path | None) -> Any:
        ...

    def anchor_path(self, target: Path) -> Path:
        ...

    def legacy_anchor_path(self, target: Path) -> Path:
        ...

    def live_session_names(self) -> set[str] | None:
        ...

    def probe_canonical_liveness(self, canonical_path: str | None) -> dict[str, Any]:
        ...


class LiveWorkspaceRegistryReads:
    """Live adapter: the real workspace-registry / anchor / tmux reads (#11426).

    Mirrors the legacy collector exactly. All registry / anchor reads degrade
    safely (``load``/``read`` return ``None`` on damage) and the verdict policy
    only trusts a registry row once :meth:`inspect_registry_health` reports the
    registry usable. :meth:`live_session_names` resolves the tmux liveness probe
    (``doctor._live_session_names``) through the ``doctor`` module at call time so
    the existing ``doctor._live_session_names``-patching characterization tests
    (which keep the registry section suite hermetic / off a real tmux server) stay
    valid; any tmux failure collapses to ``None`` so the runtime layer degrades to
    ``unknown`` rather than guessing a workspace is live or dead.
    """

    def inspect_registry_health(self, home: Path | None) -> dict[str, Any]:
        from mozyo_bridge import workspace_registry as wr

        return wr.inspect_registry_health(home)

    def load_workspace_by_path(self, target: Path, home: Path | None) -> Any | None:
        from mozyo_bridge import workspace_registry as wr

        return wr.load_workspace_by_path(target, home=home)

    def read_anchor(self, target: Path) -> dict[str, Any] | None:
        from mozyo_bridge import workspace_registry as wr

        return wr.read_anchor(target)

    def anchor_resolution(self, target: Path) -> Any:
        from mozyo_bridge import workspace_registry as wr

        return wr.anchor_resolution(target)

    def resolve_canonical_session(self, target: Path, home: Path | None) -> Any:
        from mozyo_bridge import workspace_registry as wr

        return wr.resolve_canonical_session(target, home=home)

    def anchor_path(self, target: Path) -> Path:
        from mozyo_bridge import workspace_registry as wr

        return wr.anchor_path(target)

    def legacy_anchor_path(self, target: Path) -> Path:
        from mozyo_bridge import workspace_registry as wr

        return wr.legacy_anchor_path(target)

    def live_session_names(self) -> set[str] | None:
        # Liveness is a tmux question, never a registry one. The tmux read
        # concern stays in ``doctor._live_session_names`` and is resolved through
        # the ``doctor`` module at call time so the existing
        # ``doctor._live_session_names``-patching characterization tests stay
        # valid; any failure collapses to ``None`` (section degrades to unknown).
        from mozyo_bridge.application import doctor as _doctor

        return _doctor._live_session_names()

    def probe_canonical_liveness(self, canonical_path: str | None) -> dict[str, Any]:
        # Identity invariant (#13152): the registered canonical_path must be a
        # live directory and, if a git checkout, the main worktree — otherwise
        # `coordinator` resolution silently fails closed. Read-only.
        from mozyo_bridge import workspace_registry as wr

        return wr.probe_canonical_liveness(canonical_path)


def evaluate_workspace_registry_section(
    target: Path, home: Path | None, reads: WorkspaceRegistryReads
) -> dict[str, Any]:
    """Derive the legacy workspace-registry section dict from the read port.

    Diagnoses home registry / workspace anchor / runtime identity (#11426).
    Strictly read-only and additive: it never creates the registry, never writes
    ``last_seen``, and never touches the anchor. It reports four layers:

    - **home registry** existence / schema / readability (safe error state);
    - **workspace registration** for the target repo;
    - **anchor** presence and anchor-vs-registry consistency;
    - **runtime** relationship between the registry's ``last_seen`` cache and
      live tmux state, explained — never conflated — so the registry is not
      mistaken for live runtime state.

    Only genuine problems flip the section red: an unreadable registry
    (``error``), an unsupported schema (``invalid``), or a registry/anchor
    workspace-id ``drift`` (``drifted``). A never-registered workspace, a missing
    registry with a recovery anchor, or a missing anchor are all normal,
    recoverable states and stay ``ok`` with an actionable hint.

    Also reports an **identity** layer (Redmine #13152): whether the registered
    ``canonical_path`` is a live directory and the git *main* worktree. A dead
    path or a linked-worktree canonical_path flips the section ``drifted`` with a
    repair hint, because it silently breaks ``coordinator`` resolution.

    Dict key order: ``status`` / ``target`` / ``home_registry`` / ``registration``
    / ``anchor`` / ``consistency`` / ``runtime`` / ``identity`` / ``next_action``.
    """
    health = reads.inspect_registry_health(home)
    registry_usable = health["status"] in (
        REGISTRY_HEALTH_OK,
        REGISTRY_HEALTH_MISSING,
    )

    # All reads below degrade safely (load/read return None on damage), but we
    # only trust a registry row when the health probe says the registry is
    # actually usable; otherwise registration state is "unknown".
    record = reads.load_workspace_by_path(target, home) if registry_usable else None
    anchor = reads.read_anchor(target)
    anchor_names = reads.anchor_resolution(target)
    resolved = reads.resolve_canonical_session(target, home)

    next_action: list[str] = []

    # --- registration layer -------------------------------------------------
    if not registry_usable:
        registered: bool | None = None
    else:
        registered = record is not None
    registration = {
        "registered": registered,
        "workspace_id": record.workspace_id if record else None,
        "canonical_session": record.canonical_session if record else None,
        "display_path": record.display_path if record else None,
        "preset": record.preset if record else None,
        "preset_version": record.preset_version if record else None,
    }

    # Anchor name compatibility (Redmine #11920 / #11921): report which name is
    # on disk so the legacy / both-exist migration states are visible.
    if anchor_names.both_exist:
        name_state = "both"
    elif anchor_names.using_legacy:
        name_state = "legacy"
    elif anchor_names.new_exists:
        name_state = "new"
    else:
        name_state = "none"
    anchor_info = {
        "path": str(reads.anchor_path(target)),
        "legacy_path": str(reads.legacy_anchor_path(target)),
        "name_state": name_state,
        "present": anchor is not None,
        "workspace_id": anchor.get("workspace_id") if anchor else None,
        "canonical_session": anchor.get("canonical_session") if anchor else None,
    }

    # --- consistency layer --------------------------------------------------
    if not registry_usable:
        consistency_status = "unknown"
        consistency_detail = (
            "home registry is not usable; registration/consistency cannot be "
            "determined until it is repaired"
        )
    elif record is not None and anchor is not None:
        if record.workspace_id == anchor["workspace_id"]:
            consistency_status = "ok"
            consistency_detail = "registry row and anchor agree on workspace_id"
        else:
            consistency_status = "drift"
            consistency_detail = (
                "registry row and anchor disagree on workspace_id "
                f"(registry {record.workspace_id} vs anchor {anchor['workspace_id']})"
            )
    elif record is not None and anchor is None:
        consistency_status = "registry-only"
        consistency_detail = "registered, but the workspace-local anchor is missing"
    elif record is None and anchor is not None:
        consistency_status = "anchor-only"
        consistency_detail = (
            "anchor present but the home registry has no row for this workspace "
            "(registry loss or never upserted); resolution still works from the anchor"
        )
    else:
        consistency_status = "unregistered"
        consistency_detail = (
            "workspace is not registered; session name resolves via path "
            "derivation (pre-registry behavior)"
        )

    # --- runtime / last_seen layer (tmux is the liveness source) -----------
    canonical_session = resolved.name
    live_sessions = reads.live_session_names()
    if live_sessions is None:
        session_live: bool | None = None
        runtime_status = "unknown"
        runtime_reason = (
            "tmux unavailable; liveness unknown. registry last_seen is a "
            "registration-time cache, not live runtime state"
        )
    elif canonical_session in live_sessions:
        session_live = True
        runtime_status = "active"
        runtime_reason = (
            f"canonical session '{canonical_session}' is live in tmux now; "
            "last_seen reflects the last `workspace register`, not this liveness"
        )
    else:
        session_live = False
        runtime_status = "stale"
        runtime_reason = (
            f"canonical session '{canonical_session}' is not live in tmux; "
            "last_seen is the last registration touch, not runtime activity"
        )
    runtime = {
        "last_seen": record.last_seen if record else None,
        "canonical_session": canonical_session,
        "session_live": session_live,
        "status": runtime_status,
        "reason": runtime_reason,
    }

    # --- identity invariant layer (Redmine #13152) -------------------------
    # A registered workspace's canonical_path must be a live directory and, when
    # it is a git checkout, the *main* worktree. A dead path or a linked-worktree
    # canonical_path is the failure mode that breaks `coordinator` resolution
    # (pane_resolver finds no default-lane Codex). Only checked when we trust a
    # registry row; otherwise the invariant is "unknown".
    if record is not None and registry_usable:
        liveness = reads.probe_canonical_liveness(record.canonical_path)
        if not liveness.get("exists") or not liveness.get("is_dir"):
            identity_status = "missing"
            identity_detail = (
                f"registered canonical_path {record.canonical_path} does not "
                "exist; `coordinator` resolution cannot find the main checkout"
            )
        elif liveness.get("is_git") and liveness.get("is_main_worktree") is False:
            identity_status = "not-main-worktree"
            identity_detail = (
                f"registered canonical_path {record.canonical_path} is a linked "
                "git worktree, not the main checkout; the coordinator lane was "
                "relocated onto a sublane"
            )
        else:
            identity_status = "ok"
            identity_detail = "registered canonical_path is a live main checkout"
    else:
        liveness = reads.probe_canonical_liveness(None)
        identity_status = "unknown"
        identity_detail = (
            "no trusted registry row; canonical_path invariants not checked"
        )
    identity = {
        "status": identity_status,
        "detail": identity_detail,
        "canonical_path": liveness.get("canonical_path"),
        "exists": liveness.get("exists"),
        "is_dir": liveness.get("is_dir"),
        "is_git": liveness.get("is_git"),
        "is_main_worktree": liveness.get("is_main_worktree"),
    }

    # --- overall status + next actions -------------------------------------
    if health["status"] == REGISTRY_HEALTH_UNREADABLE:
        section_status = "error"
        next_action.append(
            f"home registry {health['path']} is unreadable; move the corrupt "
            "file aside and re-register from each workspace's anchor "
            "(`mozyo-bridge workspace register`)"
        )
    elif health["status"] == REGISTRY_HEALTH_INVALID_SCHEMA:
        section_status = "invalid"
        next_action.append(
            f"home registry {health['path']} has schema version "
            f"{health['schema_version']}, but this mozyo-bridge supports "
            f"{health['expected_schema_version']}; upgrade mozyo-bridge, or "
            "move the registry aside and re-register from anchors "
            "(`mozyo-bridge workspace register`)"
        )
    elif anchor_names.both_exist:
        section_status = "drifted"
        next_action.append(
            f"both {ANCHOR_RELATIVE.as_posix()} and "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} exist; the new name is "
            f"authoritative — remove the legacy "
            f"{ANCHOR_LEGACY_RELATIVE.as_posix()} after confirming the new "
            "anchor (no silent merge)"
        )
    elif consistency_status == "drift":
        section_status = "drifted"
        next_action.append(
            "registry row and anchor disagree on workspace_id; run "
            "`mozyo-bridge workspace register` to reconcile (the anchor wins)"
        )
    else:
        section_status = "ok"
        if anchor_names.using_legacy:
            next_action.append(
                f"anchor uses the legacy name "
                f"{ANCHOR_LEGACY_RELATIVE.as_posix()}; run `mozyo-bridge "
                f"workspace register` to migrate it to "
                f"{ANCHOR_RELATIVE.as_posix()} (the legacy name still reads)"
            )
        if consistency_status == "anchor-only":
            next_action.append(
                "home registry has no row for this workspace; run "
                "`mozyo-bridge workspace register` to restore it from the anchor"
            )
        elif consistency_status == "registry-only":
            next_action.append(
                "workspace anchor is missing; run `mozyo-bridge workspace "
                "register` to rewrite it (keeps the existing identity)"
            )
        elif consistency_status == "unregistered":
            next_action.append(
                "workspace is not registered; run `mozyo-bridge workspace "
                "register` to pin a durable identity (optional — resolution "
                "already falls back to path derivation)"
            )

    # Identity-invariant escalation (#13152): a dead / worktree canonical_path is
    # a real defect — flip the section non-green and point at the true fix (run
    # `workspace register` from the main checkout) regardless of the other layers.
    if identity_status in ("missing", "not-main-worktree"):
        if section_status == "ok":
            section_status = "drifted"
        if identity_status == "missing":
            next_action.append(
                f"registered canonical_path {record.canonical_path} does not "
                "exist; run `mozyo-bridge workspace register` from the "
                "workspace's main checkout to repair it (Redmine #13152)"
            )
        else:
            next_action.append(
                f"registered canonical_path {record.canonical_path} is a linked "
                "git worktree, not the main checkout; run `mozyo-bridge workspace "
                "register --move` from the main checkout to restore the "
                "coordinator lane (Redmine #13152)"
            )

    return {
        "status": section_status,
        "target": str(target),
        "home_registry": health,
        "registration": registration,
        "anchor": anchor_info,
        "consistency": {
            "status": consistency_status,
            "detail": consistency_detail,
        },
        "runtime": runtime,
        "identity": identity,
        "next_action": next_action,
    }


class WorkspaceRegistrySectionUseCase:
    """Use case: probe the registry / anchor / tmux state and apply the verdict.

    Returns the legacy ``doctor_workspace_registry_section`` dict shape
    byte-for-byte for an already-resolved ``target`` / ``home``.
    """

    def __init__(self, reads: WorkspaceRegistryReads) -> None:
        self._reads = reads

    def execute(self, target: Path, home: Path | None) -> dict[str, Any]:
        return evaluate_workspace_registry_section(target, home, self._reads)
