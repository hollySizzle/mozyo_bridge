"""Doctor rules section boundary (#12844).

The ``doctor_rules_section`` collector historically mixed three responsibilities
in one free-function body: the *external read* that introspects the installed
central rules presets (``rules_status`` row scan + ``MOZYO_BRIDGE_HOME`` home
resolution), the *verdict authority* that maps those preset rows to the section
``status`` and the operator ``next_action`` guidance, and the *legacy section
dict assembly*. This module carves the collector slice out of the ``doctor`` body
into an OOP-first boundary (#12638 / #12843 follow-up):

- :class:`RulesSectionVerdict` is the typed value object for the verdict
  (status + the ordered next-action guidance).
- :func:`evaluate_rules_section` is the pure domain policy that decides the
  verdict from a rules read-view alone (no filesystem access, no env access).
- :class:`RulesReads` is the port for reading the rules-preset view and
  :class:`LiveRulesReads` is the live adapter over the ``doctor`` module's
  ``rules_status`` helper + ``MOZYO_BRIDGE_HOME`` resolution.
- :class:`RulesSectionUseCase` composes the port and the policy and re-assembles
  the legacy section dict byte-for-byte.

The live adapter resolves the ``doctor`` ``rules_status`` helper *at call time*
(a localized lazy import), mirroring
:class:`mozyo_bridge.application.doctor_health.LiveDoctorSections` and the other
``doctor_*`` section adapters. The pure policy / value object can now be
specified directly with a synthetic rules read-view — no real preset install /
``MOZYO_BRIDGE_HOME`` env seeding is needed to test the verdict authority.

The ``--home``-aware install command is a read concern (it depends on whether a
custom home was diagnosed and on the home value), so the live adapter resolves
the concrete ``mozyo-bridge rules install [--home <home>]`` string and threads it
into the read view. The policy only decides *whether* to emit it, mirroring the
``install_hint`` threading in
:class:`mozyo_bridge.application.doctor_codex_skill.LiveCodexSkillReads`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


# The base install command is local to this section: it is the only next_action
# the section emits. The live adapter appends ``--home <home>`` when a custom
# home was diagnosed (so a fresh tester or CI can run the suggestion verbatim
# against the same home) and threads the resolved command into the read view.
RULES_INSTALL_COMMAND = "mozyo-bridge rules install"

# The default central rules home, used only for the section's ``home`` display
# string when no custom ``--home`` was diagnosed.
DEFAULT_RULES_HOME = "~/.mozyo_bridge"


@dataclass(frozen=True)
class RulesSectionVerdict:
    """Typed verdict for the ``rules`` doctor section.

    ``status`` mirrors the historical section status (``ok`` when every installed
    preset row is ``ok``; ``missing-or-outdated`` when any row is not ``ok``).
    ``next_action`` carries the ordered operator guidance (empty when ``ok``).
    """

    status: str
    next_action: tuple[str, ...] = ()


def evaluate_rules_section(view: Mapping[str, Any]) -> RulesSectionVerdict:
    """Pure policy: derive the section verdict from a rules read-view.

    The view is the mapping returned by the read port (the ``rules_status``
    preset rows + the resolved install command). This preserves the legacy
    branching: any preset row whose ``status`` is not ``ok`` makes the section
    ``missing-or-outdated`` with the install command as the next action; an
    all-ok set is ``ok`` with no next action.
    """

    any_bad = any(row["status"] != "ok" for row in view["presets"])
    if any_bad:
        return RulesSectionVerdict(
            status="missing-or-outdated",
            next_action=(view["install_command"],),
        )
    return RulesSectionVerdict(status="ok")


@runtime_checkable
class RulesReads(Protocol):
    """Port: read the central rules-preset install view.

    Implementations own the external read (``rules_status`` preset scan + home
    resolution) and resolve the ``--home``-aware install command. The use case
    and policy depend only on the returned read-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveRulesReads:
    """Live adapter: introspect the rules presets via the ``doctor`` helper.

    ``rules_status`` is resolved through a localized lazy import *at call time*
    so the read stays cheap at module import, avoids a circular import
    (``doctor`` imports this module), and mirrors the call-time resolution
    discipline of ``doctor_health.LiveDoctorSections``.
    """

    def __init__(self, home: Path | None) -> None:
        self._home = home

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor

        rows = _doctor.rules_status(self._home)
        home_path = str(
            self._home
            or Path(
                os.environ.get("MOZYO_BRIDGE_HOME") or DEFAULT_RULES_HOME
            ).expanduser()
        )
        if self._home is not None:
            # When the diagnosed home is a custom path (passed via --home),
            # reflect it in the install command so a fresh tester or CI can
            # execute the suggestion verbatim and have it target the same home.
            install_command = f"{RULES_INSTALL_COMMAND} --home {self._home}"
        else:
            install_command = RULES_INSTALL_COMMAND
        return {
            "presets": rows,
            "home": home_path,
            "install_command": install_command,
        }


class RulesSectionUseCase:
    """Use case: read the rules-preset view, apply the verdict policy.

    Returns the legacy ``doctor_rules_section`` dict shape byte-for-byte so the
    ``run_doctor`` aggregation, JSON output, and ``format_doctor_text`` rendering
    are unchanged.
    """

    def __init__(self, reads: RulesReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_rules_section(view)
        return {
            "status": verdict.status,
            "home": view["home"],
            "presets": view["presets"],
            "next_action": list(verdict.next_action),
        }
