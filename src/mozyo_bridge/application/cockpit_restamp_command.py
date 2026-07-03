"""Cockpit lane-identity restamp boundary (Redmine #13160).

After a workspace-registry ``canonical_path`` repair (#13152), a cockpit pane
that was stamped *while the registry was polluted* keeps a stale
``@mozyo_lane_id`` — e.g. a main-checkout pane that should be the ``default``
lane carries a hashed ``lane-<digest>`` id because, at stamp time, its
``repo_root`` looked *relocated* against the wrong canonical path. The registry
repair fixes the source of truth but does not re-derive the already-stamped pane
options; before this boundary the only recovery was a manual ``tmux
set-option``.

``mozyo cockpit restamp`` is the sanctioned re-derivation path. For the target
workspace's cockpit panes it recomputes the lane identity from each pane's
authoritative ``@mozyo_repo_root`` (reading the *current* registry canonical),
diffs it against the stamped ``@mozyo_lane_id`` / ``@mozyo_lane_label``, and
re-applies ``set-option`` **only** to panes whose stamp drifted. A pane already
in sync gets no ``set-option`` at all (a strict no-op). It changes no routing
authority and performs no destructive tmux action (no pane kill / move / split):
it only re-derives identity metadata.

Safety contract, mirroring the other read-first cockpit sub-actions:

- ``--dry-run`` and ``--json`` are non-mutating previews: they show the target
  panes and the ``stamped -> recomputed`` diff but issue no ``set-option``.
- Panes bound to a *different* workspace, and panes carrying no mozyo identity
  (no ``@mozyo_workspace_id`` / no ``@mozyo_repo_root`` to recompute from), are
  never touched.
- An absent cockpit window is a benign no-op.

The pure planner (:func:`build_restamp_plan`) takes an already-read pane list
and an injected ``recompute`` callable, so it is fully testable without tmux;
:class:`LiveCockpitRestampOps` resolves the read / recompute / apply seams
*through the* :mod:`commands` *module at call time* (so the ``commands.run_tmux``
/ ``commands._resolve_workspace_lane`` patch seams keep intercepting and no
import cycle is introduced).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional, Protocol, runtime_checkable

from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (
    COCKPIT_WINDOW,
    LANE_LABEL_OPTION,
    LANE_OPTION,
    normalize_lane,
)


# The ``list-panes -F`` template for the restamp read: pane id + the identity
# fields the lane recompute + drift diff need (workspace id, stamped lane id /
# label, and the authoritative repo root the recompute derives from). Kept as a
# constant so the read and the tests share one source.
RESTAMP_FIELDS = (
    "#{pane_id}\t#{@mozyo_workspace_id}\t#{@mozyo_lane_id}"
    "\t#{@mozyo_lane_label}\t#{@mozyo_repo_root}"
)


def restamp_target(session: str) -> str:
    """The ``list-panes`` target for the shared cockpit window's restamp read."""

    return f"{session}:{COCKPIT_WINDOW}"


def project_restamp_panes(stdout: Any) -> list[dict]:
    """Parse a restamp read's tab-separated ``-F`` output.

    One dict per pane carrying a ``pane_id``; a short line right-pads to 5 fields
    so a pane missing the lane / label / repo-root options reads as ``""`` (never
    an ``IndexError``).
    """

    panes: list[dict] = []
    for line in (stdout or "").splitlines():
        parts = line.split("\t")
        if not parts or not parts[0]:
            continue
        parts = (parts + [""] * 5)[:5]
        panes.append(
            {
                "pane_id": parts[0],
                "workspace_id": parts[1],
                "lane_id": parts[2],
                "lane_label": parts[3],
                "repo_root": parts[4],
            }
        )
    return panes


@dataclass(frozen=True)
class RestampDrift:
    """One in-scope pane whose stamped lane identity drifted from the recompute."""

    pane_id: str
    workspace_id: str
    repo_root: str
    stamped_lane_id: str
    stamped_lane_label: str
    recomputed_lane_id: str
    recomputed_lane_label: str
    # The ``tmux`` argv(s) to re-apply the recomputed identity to this pane.
    commands: tuple[tuple[str, ...], ...]

    def as_dict(self) -> dict:
        return {
            "pane_id": self.pane_id,
            "workspace_id": self.workspace_id,
            "repo_root": self.repo_root,
            "stamped": {
                "lane_id": self.stamped_lane_id,
                "lane_label": self.stamped_lane_label,
            },
            "recomputed": {
                "lane_id": self.recomputed_lane_id,
                "lane_label": self.recomputed_lane_label,
            },
            "commands": [list(argv) for argv in self.commands],
        }


@dataclass(frozen=True)
class RestampPlan:
    """The restamp decision over the target workspace's cockpit panes.

    ``considered`` is the count of in-scope panes (target workspace + a
    recomputable ``repo_root``); ``drifts`` are the subset whose stamp changed.
    A pane with no drift contributes to ``considered`` but yields no command, so
    an all-in-sync cockpit produces an empty ``drifts`` and issues nothing.
    """

    session: str
    workspace_id: str
    considered: int
    drifts: tuple[RestampDrift, ...]

    @property
    def would_apply(self) -> bool:
        return bool(self.drifts)


def _restamp_commands(
    pane_id: str, lane_id: str, lane_label: str, stamped_label: str
) -> tuple[tuple[str, ...], ...]:
    """The ``set-option`` argv(s) that re-apply the recomputed lane identity.

    Always restamps ``@mozyo_lane_id``. Restamps ``@mozyo_lane_label`` to the
    recomputed value when it is non-empty; when the recompute has no label but a
    stale one is stamped, the label is *unset* (``-u``) so the pane never keeps a
    label that no longer matches its lane.
    """

    commands: list[tuple[str, ...]] = [
        ("set-option", "-p", "-t", pane_id, LANE_OPTION, lane_id)
    ]
    if lane_label:
        commands.append(
            ("set-option", "-p", "-t", pane_id, LANE_LABEL_OPTION, lane_label)
        )
    elif stamped_label:
        commands.append(("set-option", "-p", "-u", "-t", pane_id, LANE_LABEL_OPTION))
    return tuple(commands)


def build_restamp_plan(
    panes: Any,
    *,
    session: str,
    workspace_id: str,
    recompute: Callable[[str, str], Any],
) -> RestampPlan:
    """Plan the lane-identity restamp for ``workspace_id``'s cockpit panes (pure).

    Filters the read panes to the target workspace with a recomputable
    ``repo_root``, recomputes the lane via ``recompute(repo_root, workspace_id)``,
    and records a :class:`RestampDrift` only where the recomputed
    ``lane_id`` / ``lane_label`` differs from the stamped value. Panes bound to a
    different workspace, panes with no ``@mozyo_workspace_id``, and panes with no
    ``@mozyo_repo_root`` to recompute from are skipped (never touched).
    """

    drifts: list[RestampDrift] = []
    considered = 0
    for pane in panes or []:
        pane_id = (pane.get("pane_id") or "").strip()
        if not pane_id:
            continue
        pane_ws = pane.get("workspace_id") or ""
        # Out of scope: no mozyo workspace identity, or a different workspace.
        if not pane_ws or pane_ws != workspace_id:
            continue
        repo_root = (pane.get("repo_root") or "").strip()
        if not repo_root:
            # No authoritative root to recompute from — leave the pane untouched.
            continue
        considered += 1
        stamped_lane = normalize_lane(pane.get("lane_id"))
        stamped_label = pane.get("lane_label") or ""
        identity = recompute(repo_root, pane_ws)
        recomputed_lane = normalize_lane(getattr(identity, "lane_id", None))
        recomputed_label = getattr(identity, "lane_label", None) or ""
        if recomputed_lane == stamped_lane and recomputed_label == stamped_label:
            # Strict no-op: the stamp already matches the recompute.
            continue
        drifts.append(
            RestampDrift(
                pane_id=pane_id,
                workspace_id=pane_ws,
                repo_root=repo_root,
                stamped_lane_id=stamped_lane,
                stamped_lane_label=stamped_label,
                recomputed_lane_id=recomputed_lane,
                recomputed_lane_label=recomputed_label,
                commands=_restamp_commands(
                    pane_id, recomputed_lane, recomputed_label, stamped_label
                ),
            )
        )
    return RestampPlan(
        session=session,
        workspace_id=workspace_id,
        considered=considered,
        drifts=tuple(drifts),
    )


def render_restamp_lines(plan: RestampPlan, *, dry_run: bool, applied: bool) -> list:
    """The human-facing restamp preview / result lines (pure)."""

    import shlex as _shlex

    lines = [
        f"cockpit restamp: session={plan.session} workspace={plan.workspace_id} "
        f"considered={plan.considered} drift={len(plan.drifts)}"
    ]
    if not plan.drifts:
        lines.append(
            "  every in-scope pane's stamped lane identity already matches the "
            "recomputed value — nothing to restamp."
        )
        return lines
    for drift in plan.drifts:
        detail = f"lane_id {drift.stamped_lane_id!r} -> {drift.recomputed_lane_id!r}"
        if drift.stamped_lane_label != drift.recomputed_lane_label:
            detail += (
                f", lane_label {drift.stamped_lane_label!r} -> "
                f"{drift.recomputed_lane_label!r}"
            )
        lines.append(f"  pane {drift.pane_id}: {detail}")
        for argv in drift.commands:
            lines.append("    tmux " + " ".join(_shlex.quote(tok) for tok in argv))
    if dry_run or not applied:
        lines.append("  run `mozyo cockpit restamp` (without --dry-run) to apply.")
    else:
        lines.append(f"  restamped {len(plan.drifts)} pane(s).")
    return lines


def restamp_payload(
    plan: RestampPlan, *, present: bool, applied: bool, dry_run: bool
) -> dict:
    """The ``mozyo cockpit restamp --json`` audit payload (pure)."""

    return {
        "command": "cockpit restamp",
        "session": plan.session,
        "workspace_id": plan.workspace_id,
        "cockpit_present": present,
        "dry_run": dry_run,
        "applied": applied,
        "considered": plan.considered,
        "drift_count": len(plan.drifts),
        "drifts": [drift.as_dict() for drift in plan.drifts],
    }


@runtime_checkable
class CockpitRestampOps(Protocol):
    """Port: the side effects the restamp use case needs from its environment.

    ``read_panes`` lists the cockpit panes with the restamp fields (``None`` when
    the cockpit window is absent); ``recompute_lane`` re-derives the lane
    identity for a pane's ``repo_root`` (reading the current registry canonical);
    ``require_tmux`` gates the mutating path; ``apply_command`` runs one
    ``set-option`` argv and *returns* the tmux result (so a non-zero exit is
    observable, never swallowed); ``die`` is the fail-closed abort; ``emit`` is
    the stdout line sink.
    """

    def read_panes(self, session: str) -> Optional[list]: ...

    def recompute_lane(self, repo_root: str, workspace_id: str) -> Any: ...

    def require_tmux(self) -> None: ...

    def apply_command(self, argv: tuple) -> Any: ...

    def die(self, message: str) -> None: ...

    def emit(self, text: str) -> None: ...


class LiveCockpitRestampOps:
    """Live :class:`CockpitRestampOps` over the real ``commands`` seams.

    Every method resolves its target *through the* :mod:`commands` *module at
    call time* rather than binding it at import time, so the tests that patch
    ``commands.run_tmux`` / ``commands._resolve_workspace_lane`` keep
    intercepting, and this module never imports :mod:`commands` at module scope.
    """

    @staticmethod
    def _commands() -> Any:
        from mozyo_bridge.application import commands

        return commands

    def read_panes(self, session: str) -> Optional[list]:
        commands = self._commands()
        try:
            result = commands.run_tmux(
                "list-panes", "-t", restamp_target(session),
                "-F", RESTAMP_FIELDS, check=False,
            )
        except (Exception, SystemExit):
            return None
        if getattr(result, "returncode", 1) != 0:
            return None
        return project_restamp_panes(getattr(result, "stdout", "") or "")

    def recompute_lane(self, repo_root: str, workspace_id: str) -> Any:
        return self._commands()._resolve_workspace_lane(repo_root, workspace_id)

    def require_tmux(self) -> None:
        self._commands().require_tmux()

    def apply_command(self, argv: tuple) -> Any:
        # Return the result so the use case can fail closed on a non-zero exit
        # rather than silently reporting a half-restamp as success.
        return self._commands().run_tmux(*argv, check=False)

    def die(self, message: str) -> None:
        return self._commands().die(message)

    def emit(self, text: str) -> None:
        print(text)


def _describe_option(argv: tuple) -> str:
    """A compact ``'<option> <value>'`` description of a restamp ``set-option``.

    ``('set-option', '-p', '-t', pane, OPTION, VALUE)`` -> ``'OPTION VALUE'``;
    an unset ``('set-option', '-p', '-u', '-t', pane, OPTION)`` -> ``'OPTION
    (unset)'``. Pane-independent, so the same option reads the same everywhere.
    """

    if "-u" in argv:
        return f"'{argv[-1]} (unset)'"
    return f"'{argv[-2]} {argv[-1]}'"


def build_restamp_failure_message(
    *,
    pane_id: str,
    failed_argv: tuple,
    detail: str,
    fully_restamped: int,
    total: int,
    applied_in_pane: tuple,
) -> str:
    """The command-grained fail-closed abort message (#13160 REV3).

    Distinguishes the failing pane from every other pane so a failure is
    never misreported (#13160 REV4 refines REV3 per j#71854):

    - ``fully_restamped`` panes had every ``set-option`` land;
    - the failing ``pane_id`` is reported PARTIAL when ``applied_in_pane`` is
      non-empty (the option(s) that already landed on it plus the one that
      failed), and "attempted but left unchanged" when its first command
      failed — it was attempted either way, so it is never counted as
      "not attempted";
    - only the panes after it were never attempted.
    """

    failed_desc = _describe_option(failed_argv)
    not_attempted = total - fully_restamped - 1
    parts = [
        f"cockpit restamp step failed (pane {pane_id}): "
        f"`tmux {' '.join(failed_argv)}` -> {detail}.",
        f"Restamped {fully_restamped} of {total} pane(s) fully.",
    ]
    if applied_in_pane:
        applied_descs = ", ".join(_describe_option(a) for a in applied_in_pane)
        parts.append(
            f"Pane {pane_id} is PARTIALLY restamped: {applied_descs} applied, "
            f"{failed_desc} failed."
        )
    else:
        # The failing pane's first command was issued and failed: attempted,
        # but nothing landed — state it explicitly, never as "not attempted".
        parts.append(
            f"Pane {pane_id} was attempted but left unchanged: "
            f"{failed_desc} failed."
        )
    parts.append(
        f"{not_attempted} pane(s) were left unchanged (not attempted). "
        f"Re-run `mozyo cockpit restamp` to reconcile "
        f"(already-correct panes are no-ops)."
    )
    return " ".join(parts)


class CockpitRestampUseCase:
    """Re-derive + re-apply cockpit pane lane identity through the injected port.

    Non-mutating under ``--dry-run`` / ``--json`` (preview only); otherwise it
    applies ``set-option`` to the drifted panes and to *only* those panes. An
    absent cockpit is a benign no-op. All paths return exit code ``0``.
    """

    def __init__(self, ops: CockpitRestampOps) -> None:
        self._ops = ops

    def _apply(self, plan: RestampPlan) -> None:
        """Apply the drift set-options, fail-closed with command-grained accounting.

        Matches the fail-fast flavor of the other cockpit mutators
        (``cockpit_repair_command._run_fail_fast`` for reset / rebalance /
        reconcile): each ``set-option`` result is inspected and the FIRST
        non-zero exit aborts via :meth:`CockpitRestampOps.die` rather than
        letting the caller report a half-restamp as ``applied=True``. Restamp is
        a set of independent per-pane metadata corrections (no cross-pane
        transaction / rollback), so a stop-on-first-failure abort is the safe
        choice — a ``set-option`` failure usually signals the pane / tmux is in
        an unexpected state where continuing would compound the confusion.

        A pane can carry more than one ``set-option`` (``@mozyo_lane_id`` plus a
        ``@mozyo_lane_label`` set/unset), so the accounting is *command-grained*
        (#13160 REV3): the abort message distinguishes the panes fully restamped,
        the failing pane's own partial state (the commands that already landed on
        it versus the one that failed), and the panes never attempted — so a pane
        left half-restamped is reported as PARTIAL, never as "left unchanged".
        Re-running is safe because already-correct panes are strict no-ops.
        """
        from mozyo_bridge.application.cockpit_repair_command import result_detail

        total = len(plan.drifts)
        for index, drift in enumerate(plan.drifts):
            applied_in_pane: list[tuple] = []
            for argv in drift.commands:
                result = self._ops.apply_command(argv)
                if getattr(result, "returncode", 0) != 0:
                    self._ops.die(
                        build_restamp_failure_message(
                            pane_id=drift.pane_id,
                            failed_argv=tuple(argv),
                            detail=result_detail(result) or "nonzero exit",
                            fully_restamped=index,
                            total=total,
                            applied_in_pane=tuple(applied_in_pane),
                        )
                    )
                applied_in_pane.append(tuple(argv))

    def handle(
        self,
        session: str,
        workspace_id: str,
        *,
        json_output: bool,
        dry_run: bool,
    ) -> int:
        """`mozyo cockpit restamp` — re-derive drifted pane lane identity (#13160)."""

        import json as _json

        panes = self._ops.read_panes(session)
        present = panes is not None
        plan = build_restamp_plan(
            panes or [],
            session=session,
            workspace_id=workspace_id,
            recompute=self._ops.recompute_lane,
        )

        applied = False
        if present and plan.would_apply and not dry_run and not json_output:
            self._ops.require_tmux()
            self._apply(plan)
            applied = True

        if json_output:
            self._ops.emit(
                _json.dumps(
                    restamp_payload(
                        plan, present=present, applied=applied, dry_run=dry_run
                    ),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
            )
            return 0

        if not present:
            self._ops.emit(
                f"cockpit restamp: no cockpit window for session {session!r} — "
                "nothing to restamp."
            )
            return 0

        for line in render_restamp_lines(plan, dry_run=dry_run, applied=applied):
            self._ops.emit(line)
        return 0
