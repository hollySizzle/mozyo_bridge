"""Doctor codex-skill section boundary (#12836).

The ``doctor_codex_skill_section`` collector historically mixed three
responsibilities in one free-function body: the *external read* that introspects
the Codex skill install on the filesystem (skill home + ``SKILL.md`` presence +
missing shared references), the *verdict authority* that maps that read to the
section ``status`` and the operator ``next_action`` guidance, and the *legacy
section dict assembly*. This module carves the collector slice out of the
``doctor`` body into an OOP-first boundary (#12638 / #12835 follow-up):

- :class:`CodexSkillSectionVerdict` is the typed value object for the verdict
  (status + the ordered next-action guidance).
- :func:`evaluate_codex_skill_section` is the pure domain policy that decides the
  verdict from a skill read-view alone (no filesystem access, no env access).
- :class:`CodexSkillReads` is the port for reading the Codex skill view and
  :class:`LiveCodexSkillReads` is the live adapter over the ``doctor`` module's
  ``codex_skill_home`` / ``_check_skill_dir`` filesystem helpers.
- :class:`CodexSkillSectionUseCase` composes the port and the policy and
  re-assembles the legacy section dict byte-for-byte.

The live adapter resolves the ``doctor`` filesystem helpers and the
``CODEX_SKILL_INSTALL_HINT`` guidance constant *at call time* (a localized lazy
import), mirroring :class:`mozyo_bridge.application.doctor_health.LiveDoctorSections`
and :class:`mozyo_bridge.application.doctor_launch_policy.LiveLaunchPolicyReads`.
The pure policy / value object can now be specified directly with a synthetic
skill read-view — no ``CODEX_HOME`` env patching and no real temp-dir skill
seeding is needed to test the verdict authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


# The reference-sync guidance is local to this section: it is the only
# next_action the section emits that is not the shared install hint. The install
# hint itself (``CODEX_SKILL_INSTALL_HINT``) stays sourced from the ``doctor``
# module — it is shared with ``doctor_instruction`` — and is threaded into the
# read view by the live adapter so the policy stays decoupled from ``doctor``.
SYNC_REFERENCES_HINT = "re-run scripts/install_codex_skill.sh to sync references"


@dataclass(frozen=True)
class CodexSkillSectionVerdict:
    """Typed verdict for the ``codex_skill`` doctor section.

    ``status`` mirrors the historical section status (``missing`` when no
    ``SKILL.md`` is installed, ``incomplete`` when shared references are absent,
    ``ok`` otherwise). ``next_action`` carries the ordered operator guidance
    (empty when ``ok``).
    """

    status: str
    next_action: tuple[str, ...] = ()


def evaluate_codex_skill_section(
    view: Mapping[str, Any]
) -> CodexSkillSectionVerdict:
    """Pure policy: derive the section verdict from a Codex skill read-view.

    The view is the mapping returned by the read port (skill presence + missing
    shared references + the install hint). This preserves the legacy branching:
    a missing skill is ``missing`` with the install hint, a present-but-thin
    skill is ``incomplete`` with the reference-sync hint, and a complete skill
    is ``ok`` with no next action.
    """

    if not view["present"]:
        return CodexSkillSectionVerdict(
            status="missing",
            next_action=(view["install_hint"],),
        )
    if view["references_missing"]:
        return CodexSkillSectionVerdict(
            status="incomplete",
            next_action=(SYNC_REFERENCES_HINT,),
        )
    return CodexSkillSectionVerdict(status="ok")


@runtime_checkable
class CodexSkillReads(Protocol):
    """Port: read the Codex skill install view.

    Implementations own the external read (skill home resolution + ``SKILL.md``
    presence + shared-reference scan) and supply the install-hint guidance
    constant. The use case and policy depend only on the returned read-view
    mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveCodexSkillReads:
    """Live adapter: introspect the Codex skill via the ``doctor`` helpers.

    ``codex_skill_home`` / ``_check_skill_dir`` and the
    ``CODEX_SKILL_INSTALL_HINT`` constant are resolved through a localized lazy
    import *at call time* so the read stays cheap at module import, avoids a
    circular import (``doctor`` imports this module), and mirrors the call-time
    resolution discipline of ``doctor_health.LiveDoctorSections``.
    """

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor

        home = _doctor.codex_skill_home()
        skill_dir = home / "skills" / "mozyo-bridge-agent"
        info = _doctor._check_skill_dir(skill_dir)
        return {
            "home": str(home),
            "skill_dir": info["path"],
            "skill_md": info["skill_md"],
            "present": info["present"],
            "references_missing": info["references_missing"],
            "install_hint": _doctor.CODEX_SKILL_INSTALL_HINT,
        }


class CodexSkillSectionUseCase:
    """Use case: read the Codex skill view, apply the verdict policy.

    Returns the legacy ``doctor_codex_skill_section`` dict shape byte-for-byte so
    the ``run_doctor`` aggregation, JSON output, and ``format_doctor_text``
    rendering are unchanged.
    """

    def __init__(self, reads: CodexSkillReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_codex_skill_section(view)
        return {
            "status": verdict.status,
            "home": view["home"],
            "skill_dir": view["skill_dir"],
            "skill_md": view["skill_md"],
            "present": view["present"],
            "references_missing": view["references_missing"],
            "next_action": list(verdict.next_action),
        }
