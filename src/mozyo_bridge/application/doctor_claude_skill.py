"""Doctor claude-skill section boundary (#12843).

The ``doctor_claude_skill_section`` collector historically mixed three
responsibilities in one free-function body: the *external reads* that introspect
the Claude skill install on the filesystem across *three* surfaces (the personal
/ global skill home, the project-local ``.claude`` skill dir, and the plugin
marketplace cache), the *verdict authority* that maps those reads to the section
``status`` plus the precedence ``warnings`` and operator ``next_action``
guidance, and the *legacy section dict assembly*. This module carves the
collector slice out of the ``doctor`` body into an OOP-first boundary (#12638 /
#12835 / #12836 follow-up):

- :class:`ClaudeSkillSectionVerdict` is the typed value object for the verdict
  (status + the ordered precedence warnings + the ordered next-action guidance).
- :func:`evaluate_claude_skill_section` is the pure domain policy that decides
  the verdict from a skill read-view alone (no filesystem access, no env
  access). It owns the global/project/plugin precedence and warning rules.
- :class:`ClaudeSkillReads` is the port for reading the three Claude skill views
  and :class:`LiveClaudeSkillReads` is the live adapter over the ``doctor``
  module's ``claude_skill_global_home`` / ``claude_skill_project_dir`` /
  ``claude_plugin_skill_root`` / ``_check_skill_dir`` / ``_check_plugin_install``
  filesystem helpers.
- :class:`ClaudeSkillSectionUseCase` composes the port and the policy and
  re-assembles the legacy section dict byte-for-byte.

The live adapter resolves the ``doctor`` filesystem helpers and the
``CLAUDE_GLOBAL_SKILL_INSTALL_HINT`` guidance constant *at call time* (a
localized lazy import), mirroring
:class:`mozyo_bridge.application.doctor_codex_skill.LiveCodexSkillReads` and
:class:`mozyo_bridge.application.doctor_launch_policy.LiveLaunchPolicyReads`. The
pure policy / value object can now be specified directly with a synthetic skill
read-view — no ``MOZYO_BRIDGE_CLAUDE_HOME`` / ``MOZYO_BRIDGE_CLAUDE_PROJECT_DIR``
env patching and no real temp-dir skill seeding is needed to test the verdict
authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


# The two reference-sync hints are local to this section: they are the only
# next_actions the section emits that are not the shared install hint. The
# install hint itself (``CLAUDE_GLOBAL_SKILL_INSTALL_HINT``) stays sourced from
# the ``doctor`` module — it is shared with ``doctor_instruction`` — and is
# threaded into the read view by the live adapter so the policy stays decoupled
# from ``doctor``.
GLOBAL_SYNC_REFERENCES_HINT = (
    "re-run scripts/install_claude_skill.sh to sync the global skill references"
)
PROJECT_SYNC_REFERENCES_HINT = (
    "re-run scripts/install_claude_skill.sh against this project to sync references"
)


def precedence_warning(global_path: str, project_path: str) -> str:
    """Render the personal>project precedence warning string.

    Local to this section: the warning fires only when both the legacy global
    and the legacy project skills are present, because Claude Code's personal
    skill silently overrides the project one.
    """

    return (
        "personal/global Claude skill at "
        + global_path
        + " overrides project skill at "
        + project_path
        + " (Claude Code precedence: personal > project)"
    )


@dataclass(frozen=True)
class ClaudeSkillSectionVerdict:
    """Typed verdict for the ``claude_skill`` doctor section.

    ``status`` mirrors the historical section status: ``plugin-managed`` when
    only the plugin marketplace cache supplies the skill, ``missing`` when no
    skill is installed anywhere, ``incomplete`` when an installed skill lacks
    shared references, ``warning`` when the legacy global+project precedence
    collision is the only issue, ``ok`` otherwise. ``warnings`` carries the
    ordered precedence warnings (empty unless the legacy collision is detected)
    and ``next_action`` carries the ordered operator guidance (empty when there
    is nothing to do).
    """

    status: str
    warnings: tuple[str, ...] = ()
    next_action: tuple[str, ...] = ()


def evaluate_claude_skill_section(
    view: Mapping[str, Any]
) -> ClaudeSkillSectionVerdict:
    """Pure policy: derive the section verdict from a Claude skill read-view.

    The view is the mapping returned by the read port (the three skill presence
    views + the install hint). This preserves the legacy branching exactly:

    - both global and project absent -> ``plugin-managed`` when the plugin cache
      supplies the skill (no install hint emitted), else ``missing`` with the
      install hint.
    - global present but missing shared references -> ``incomplete`` with the
      global reference-sync hint.
    - global absent, project present but missing references -> ``incomplete``
      with the project reference-sync hint.
    - otherwise ``warning`` when the legacy global+project precedence collision
      is present, else ``ok``.

    The precedence ``warnings`` list is populated independently of the status
    branch (it is computed first), matching the legacy body.
    """

    global_info = view["global"]
    project_info = view["project"]
    plugin_info = view["plugin"]

    warnings: list[str] = []
    if global_info["present"] and project_info["present"]:
        warnings.append(
            precedence_warning(global_info["path"], project_info["path"])
        )

    next_action: list[str] = []
    status = "ok"
    if not global_info["present"] and not project_info["present"]:
        if plugin_info["present"]:
            # plugin marketplace install is the source of the skill; no legacy
            # install hint should be emitted. Treated as healthy and excluded
            # from BAD_SECTION_STATUSES so the overall doctor result stays ok.
            status = "plugin-managed"
        else:
            status = "missing"
            next_action.append(view["install_hint"])
    elif global_info["present"] and global_info["references_missing"]:
        status = "incomplete"
        next_action.append(GLOBAL_SYNC_REFERENCES_HINT)
    elif (
        not global_info["present"]
        and project_info["present"]
        and project_info["references_missing"]
    ):
        status = "incomplete"
        next_action.append(PROJECT_SYNC_REFERENCES_HINT)
    elif warnings:
        status = "warning"

    return ClaudeSkillSectionVerdict(
        status=status,
        warnings=tuple(warnings),
        next_action=tuple(next_action),
    )


@runtime_checkable
class ClaudeSkillReads(Protocol):
    """Port: read the Claude skill install view across all three surfaces.

    Implementations own the external reads (global skill home + project skill
    dir + plugin marketplace cache scan) and supply the install-hint guidance
    constant. The use case and policy depend only on the returned read-view
    mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveClaudeSkillReads:
    """Live adapter: introspect the Claude skill via the ``doctor`` helpers.

    ``claude_skill_global_home`` / ``claude_skill_project_dir`` /
    ``claude_plugin_skill_root`` / ``_check_skill_dir`` /
    ``_check_plugin_install`` and the ``CLAUDE_GLOBAL_SKILL_INSTALL_HINT``
    constant are resolved through a localized lazy import *at call time* so the
    reads stay cheap at module import, avoid a circular import (``doctor``
    imports this module), and mirror the call-time resolution discipline of
    ``doctor_codex_skill.LiveCodexSkillReads``.

    ``args`` carries the parsed CLI namespace; the project skill dir resolution
    reads ``args.repo`` (and the ``MOZYO_BRIDGE_CLAUDE_PROJECT_DIR`` override) at
    call time, so the existing env-patching section tests are unchanged.
    """

    def __init__(self, args: Any) -> None:
        self._args = args

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor

        global_home = _doctor.claude_skill_global_home()
        global_dir = global_home / "skills" / "mozyo-bridge-agent"
        global_info = _doctor._check_skill_dir(global_dir)

        project_dir = _doctor.claude_skill_project_dir(self._args)
        project_skill_dir = project_dir / ".claude" / "skills" / "mozyo-bridge-agent"
        project_info = _doctor._check_skill_dir(project_skill_dir)

        plugin_info = _doctor._check_plugin_install(
            _doctor.claude_plugin_skill_root(global_home)
        )

        return {
            "global_home": str(global_home),
            "global": global_info,
            "project_dir": str(project_dir),
            "project": project_info,
            "plugin": plugin_info,
            "install_hint": _doctor.CLAUDE_GLOBAL_SKILL_INSTALL_HINT,
        }


class ClaudeSkillSectionUseCase:
    """Use case: read the Claude skill views, apply the verdict policy.

    Returns the legacy ``doctor_claude_skill_section`` dict shape byte-for-byte
    so the ``run_doctor`` aggregation, JSON output, and ``format_doctor_text``
    rendering are unchanged.
    """

    def __init__(self, reads: ClaudeSkillReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_claude_skill_section(view)
        return {
            "status": verdict.status,
            "global_home": view["global_home"],
            "global": view["global"],
            "project_dir": view["project_dir"],
            "project": view["project"],
            "plugin": view["plugin"],
            "warnings": list(verdict.warnings),
            "next_action": list(verdict.next_action),
        }
