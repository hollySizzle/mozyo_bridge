"""Doctor health verdict boundary (#12833).

`run_doctor` historically mixed two responsibilities in one body: the
*external reads* that collect each diagnostic section (`doctor_*_section`) and
the *authority-bearing verdict* that maps the collected section statuses to the
overall ``ok`` boolean (which in turn becomes the ``mozyo-bridge doctor`` exit
code). This module carves both out of the `run_doctor` free-function body:

- :class:`DoctorHealthVerdict` is the typed value object for the verdict.
- :func:`evaluate_doctor_health` is the pure domain policy that decides health
  from section statuses alone (no ``argparse.Namespace``, no I/O).
- :class:`DoctorSectionReads` is the port for collecting the section map, and
  :class:`LiveDoctorSections` is the live adapter that drives the existing
  ``doctor`` module section collectors.
- :class:`RunDoctorUseCase` composes the port and the policy.

The live adapter resolves the ``doctor`` module section functions *at call
time* (a localized lazy import), so existing integration tests that
``patch("mozyo_bridge.application.doctor.doctor_cli_section", ...)`` keep
intercepting the reads unchanged. The pure policy / value object can now be
specified directly with synthetic section maps — no monkeypatch of the section
collectors and no ``commands.*`` doctor monkeypatch is needed to test the
verdict authority.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


# Section statuses that make the overall doctor result not-ok. ``warning`` is
# handled separately by the policy (and by ``doctor_instruction``) so this set
# stays the canonical "hard bad" set re-exported as ``doctor.BAD_SECTION_STATUSES``.
UNHEALTHY_SECTION_STATUSES = frozenset(
    {
        "missing",
        "missing-or-outdated",
        "outdated",
        "incomplete",
        "invalid",
        "drifted",
        "error",
    }
)


@dataclass(frozen=True)
class DoctorHealthVerdict:
    """Typed verdict for the aggregate doctor health check.

    ``ok`` mirrors the historical ``run_doctor`` boolean (exit code 0 vs 1).
    ``unhealthy_sections`` names the sections that pulled the verdict down, in
    section-collection order, so callers no longer have to re-derive *why* the
    environment is unhealthy from the raw section map.
    """

    ok: bool
    unhealthy_sections: tuple[str, ...] = ()


def _section_status(section: Mapping[str, Any]) -> Any:
    return section.get("status")


def evaluate_doctor_health(
    sections: Mapping[str, Mapping[str, Any]]
) -> DoctorHealthVerdict:
    """Pure policy: derive the doctor verdict from collected section statuses.

    A section drags the verdict to not-ok when its status is in
    :data:`UNHEALTHY_SECTION_STATUSES` or is exactly ``"warning"``. This
    preserves the legacy ``run_doctor`` aggregation semantics (any bad/warning
    section => ``ok = False``).
    """

    unhealthy = tuple(
        name
        for name, section in sections.items()
        if _section_status(section) in UNHEALTHY_SECTION_STATUSES
        or _section_status(section) == "warning"
    )
    return DoctorHealthVerdict(ok=not unhealthy, unhealthy_sections=unhealthy)


@runtime_checkable
class DoctorSectionReads(Protocol):
    """Port: collect the doctor diagnostic section map.

    Implementations own the external reads (CLI / rules / skills / scaffold /
    registry / state store / nagger / launch policy / tmux / otel / delivery
    env). The use case and policy depend only on the returned ``{name: section}``
    mapping.
    """

    def collect_sections(self) -> dict[str, dict[str, Any]]:
        ...


class LiveDoctorSections:
    """Live adapter: drive the ``doctor`` module section collectors.

    The section functions are resolved through the ``doctor`` module *at call
    time* so existing ``patch("...doctor.doctor_*_section")`` integration tests
    keep intercepting them. The ``argparse.Namespace`` propagation now stops
    here at the adapter boundary; the use case and policy never see ``args``.
    """

    def __init__(self, args: argparse.Namespace) -> None:
        self._args = args

    def collect_sections(self) -> dict[str, dict[str, Any]]:
        from mozyo_bridge.application import doctor as _doctor

        args = self._args
        tmux_section = _doctor.doctor_tmux_section(args)
        # Attach the governed-preset tmux-ui artifact state to the existing
        # tmux section so the diagnostic surface stays small. The artifact
        # status is independent of tmux server availability — operators
        # without a running tmux server still need to know whether the
        # `.mozyo-bridge/tmux/agent-ui.conf` snippet landed on the target.
        tmux_section["artifact"] = _doctor.doctor_tmux_ui_artifact_info(
            _doctor.doctor_target(args)
        )
        return {
            "cli": _doctor.doctor_cli_section(_doctor.doctor_target(args)),
            "rules": _doctor.doctor_rules_section(_doctor.doctor_home(args)),
            "codex_skill": _doctor.doctor_codex_skill_section(),
            "claude_skill": _doctor.doctor_claude_skill_section(args),
            "scaffold": _doctor.doctor_scaffold_section(args),
            "workspace_registry": _doctor.doctor_workspace_registry_section(args),
            "state_store": _doctor.doctor_state_store_section(args),
            "claude_nagger": _doctor.doctor_claude_nagger_section(args),
            "claude_launch_policy": _doctor.doctor_claude_launch_policy_section(),
            "tmux": tmux_section,
            "otel": _doctor.doctor_otel_section(args),
            "delivery_env": _doctor.doctor_delivery_env_section(args),
        }


class RunDoctorUseCase:
    """Use case: collect sections via the port, apply the verdict policy.

    Returns the legacy ``run_doctor`` result shape (``{"ok": bool, "sections":
    {...}}``) so the CLI adapter, JSON output, and ``format_doctor_text`` are
    byte-for-byte unchanged.
    """

    def __init__(self, reads: DoctorSectionReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        sections = self._reads.collect_sections()
        verdict = evaluate_doctor_health(sections)
        return {"ok": verdict.ok, "sections": sections}
