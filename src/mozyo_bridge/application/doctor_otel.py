"""Doctor OTel receiver-health / observation-gap section boundary (#12892).

The ``doctor_otel_section`` collector historically mixed three responsibilities
in one free-function body: the *external read* (the OTel store path / existence /
counts, the receiver ``/healthz`` probe, the activity summary that yields the
set of already-observed ``(session, agent)`` pairs, and the tmux agent discovery
that folds panes into per-pane agent records), the *verdict authority* that
assembles the legacy section dict (the ``ok`` status, the receiver-unreachable
note, the observation-gap detection, the ``unobserved_agents`` list, and the gap
note), and the *legacy section dict assembly*. This module carves the collector
slice out of the ``doctor`` body into an OOP-first boundary
(#12638 / #12881 follow-up):

- :func:`evaluate_otel_section` is the pure domain policy that derives the whole
  section dict from an OTel read-view alone (no store, no HTTP, no tmux). It owns
  the receiver-unreachable note wording, the observation-gap detection (an
  already-observed-pair intersection over the discovered agent records), the
  ``unobserved_agents`` list, and the gap note. It re-assembles the legacy
  section dict byte-for-byte (key order ``status`` / ``store_path`` /
  ``store_exists`` / ``notes`` / counts / ``receiver_reachable`` /
  ``receiver_error`` / ``unobserved_agents``) so the ``run_doctor`` aggregation,
  JSON output, and ``format_doctor_text`` rendering are unchanged.
- :class:`OtelDoctorReads` is the port for the *external read* and
  :class:`LiveOtelDoctorReads` is the live adapter over the real
  :class:`~mozyo_bridge.otel_store.OtelEventStore`, the receiver ``/healthz``
  endpoint, the activity summary, and the tmux agent discovery. The adapter
  reduces the folded :class:`AgentRecord` objects to plain dicts so the policy
  stays free of the discovery domain types and is exercisable with synthetic
  views. ``try_pane_lines`` is imported inside the adapter at call time, exactly
  as the legacy collector did, so the existing OTel doctor characterization test
  (which patches ``...tmux_client.try_pane_lines``) keeps working unchanged.
- :class:`OtelSectionUseCase` composes the port and the policy.

The receiver being down is NOT an error: OTLP is push-based, so an unreachable
receiver means "telemetry is being lost by design until restart". Agent panes
whose ``(session, agent)`` pair has never produced a store source are surfaced
as observation gaps, the blind-spot class the owner decision (#11639 constraint
3) requires doctor to expose.
"""

from __future__ import annotations

import argparse
from typing import Any, Protocol, runtime_checkable

RECEIVER_UNREACHABLE_NOTE = (
    "receiver not reachable: telemetry sent now is lost BY DESIGN "
    "(best-effort store, not an error). Start it with "
    "`mozyo-bridge otel serve`; use `agents list` / `session list` "
    "for liveness in the meantime."
)

TMUX_UNAVAILABLE_NOTE = "tmux unavailable: observation-gap check skipped"


def _gap_note(count: int) -> str:
    return (
        f"{count} agent pane(s) have never emitted telemetry "
        "(OTel env not injected, launched before injection, or the "
        "CLI does not emit). Restart them via `mozyo` / `mozyo-bridge "
        "init <agent>` to inject; until then their activity is "
        "`unknown` and falls back to tmux liveness."
    )


def evaluate_otel_section(view: dict[str, Any]) -> dict[str, Any]:
    """Pure policy: derive the legacy ``otel`` section dict from a read-view.

    The view is the mapping returned by the read port:

    - ``store_path`` (str): the OTel store path.
    - ``store_exists`` (bool): whether the store file exists.
    - ``counts`` (dict): the store ``counts()`` summary, merged into the section.
    - ``receiver_reachable`` (bool): whether the ``/healthz`` probe succeeded.
    - ``receiver_error`` (str): the probe failure string (only when not
      reachable).
    - ``observed_pairs`` (set[tuple[str, str]]): the ``(session, agent)`` pairs
      that already have a telemetry source in the store.
    - ``agent_records`` (list[dict] | None): the discovered agent panes, each a
      dict with ``pane_id`` / ``session`` / ``agent_kind`` / ``view_sessions``;
      ``None`` when tmux was unavailable (gap check skipped).
    - ``gap_check_error`` (str | None): the observation-gap read failure string,
      if the discovery read raised.

    The branching preserves the legacy collector exactly.
    """

    section: dict[str, Any] = {
        "status": "ok",
        "store_path": view["store_path"],
        "store_exists": view["store_exists"],
        "notes": [],
    }
    section.update(view["counts"])

    if view["receiver_reachable"]:
        section["receiver_reachable"] = True
    else:
        section["receiver_reachable"] = False
        section["receiver_error"] = view["receiver_error"]
        section["notes"].append(RECEIVER_UNREACHABLE_NOTE)

    observed_pairs = view["observed_pairs"]
    gaps: list[dict[str, str]] = []
    gap_check_error = view.get("gap_check_error")
    if gap_check_error is not None:
        # Diagnosis must never take doctor down: a failed discovery read is a
        # note, not a raise.
        section["notes"].append(f"observation-gap check failed: {gap_check_error}")
    elif view["agent_records"] is None:
        section["notes"].append(TMUX_UNAVAILABLE_NOTE)
    else:
        for record in view["agent_records"]:
            if record["agent_kind"] == "unknown":
                continue
            pairs = {
                (session, record["agent_kind"])
                for session in record["view_sessions"]
            }
            if not pairs & observed_pairs:
                gaps.append(
                    {
                        "pane_id": record["pane_id"],
                        "session": record["session"],
                        "agent": record["agent_kind"],
                    }
                )

    section["unobserved_agents"] = gaps
    if gaps:
        section["notes"].append(_gap_note(len(gaps)))
    return section


@runtime_checkable
class OtelDoctorReads(Protocol):
    """Port: read the OTel receiver-health / observation-gap view.

    Implementations own the external read (the store path / existence / counts,
    the receiver ``/healthz`` probe, the activity summary that yields the
    already-observed ``(session, agent)`` pairs, and the tmux agent discovery).
    The use case and policy depend only on the returned read-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveOtelDoctorReads:
    """Live adapter: read the OTel doctor view from the real environment.

    Mirrors the legacy collector exactly: the store path / existence / counts
    come from :class:`~mozyo_bridge.otel_store.OtelEventStore`, the receiver
    reachability from a 2-second ``/healthz`` GET (a down receiver is reported,
    never raised), the observed pairs from
    :func:`summarize_activity`, and the agent panes from
    :func:`fold_agents_by_pane` over :func:`discover_agents`. ``try_pane_lines``
    and the discovery domain functions are imported at call time so the existing
    characterization test that patches ``...tmux_client.try_pane_lines`` is
    unchanged. The agent-discovery read is wrapped exactly as the legacy
    collector wrapped it: any failure collapses to ``gap_check_error`` so
    diagnosis never takes doctor down.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args

    def describe(self) -> dict[str, Any]:
        import json as _json
        import urllib.error
        import urllib.request

        from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.domain.agent_activity import (
            summarize_activity,
        )
        from mozyo_bridge.otel_store import OtelEventStore

        store = OtelEventStore()
        view: dict[str, Any] = {
            "store_path": str(store.path),
            "store_exists": store.path.exists(),
            "counts": store.counts(),
        }

        healthz = "http://127.0.0.1:4318/healthz"
        try:
            with urllib.request.urlopen(healthz, timeout=2) as response:
                _json.loads(response.read().decode("utf-8"))
            view["receiver_reachable"] = True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            view["receiver_reachable"] = False
            view["receiver_error"] = str(exc)

        observed_pairs: set[tuple[str, str]] = set()
        for activity in summarize_activity(store):
            hints = activity.match_hints
            if isinstance(hints.get("session"), str) and isinstance(
                hints.get("agent"), str
            ):
                observed_pairs.add((hints["session"], hints["agent"]))
        view["observed_pairs"] = observed_pairs

        view["agent_records"] = None
        view["gap_check_error"] = None
        try:
            from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (
                discover_agents,
                fold_agents_by_pane,
            )
            from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client import (
                try_pane_lines,
            )

            panes = try_pane_lines()
            if panes is None:
                view["agent_records"] = None
            else:
                view["agent_records"] = [
                    {
                        "pane_id": record.pane_id,
                        "session": record.session,
                        "agent_kind": record.agent_kind,
                        "view_sessions": [v.session for v in record.views],
                    }
                    for record in fold_agents_by_pane(discover_agents(panes))
                ]
        except Exception as exc:  # diagnosis must never take doctor down
            view["gap_check_error"] = str(exc)
        return view


class OtelSectionUseCase:
    """Use case: read the OTel doctor view, apply the verdict policy.

    Returns the legacy ``doctor_otel_section`` dict shape byte-for-byte.
    """

    def __init__(self, reads: OtelDoctorReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        return evaluate_otel_section(view)
