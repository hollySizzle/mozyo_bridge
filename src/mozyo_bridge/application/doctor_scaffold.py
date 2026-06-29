"""Doctor scaffold section boundary (#12853).

The ``doctor_scaffold_section`` collector historically mixed three
responsibilities in one free-function body: the *external read* that introspects
the per-repo scaffold readiness (``scaffold_status`` over the target checkout's
manifest + the central preset store), the *verdict authority* that maps the
scaffold-status detail to the section ``status`` and the operator ``next_action``
guidance (the manifest missing/invalid/clean/drifted branching plus the
central-status and per-file remediation commands), and the *legacy section dict
assembly*. This module carves the collector slice out of the ``doctor`` body into
an OOP-first boundary (#12638 / #12845 follow-up):

- :class:`ScaffoldSectionVerdict` is the typed value object for the verdict
  (status + the ordered next-action guidance).
- :func:`evaluate_scaffold_section` is the pure domain policy that decides the
  verdict from a scaffold read-view alone (no filesystem access, no manifest
  parsing). It owns the manifest missing/invalid/clean/drifted branching and the
  remediation-command wording (including the ``--home``-qualified variants).
- :class:`ScaffoldReads` is the port for reading the scaffold view and
  :class:`LiveScaffoldReads` is the live adapter over the ``doctor`` module's
  ``doctor_target`` / ``doctor_home`` argument resolution and the
  ``scaffold_status`` manifest/central-state detector.
- :class:`ScaffoldSectionUseCase` composes the port and the policy and
  re-assembles the legacy section dict byte-for-byte.

The scaffold *read* (the filesystem manifest parse and the central preset-store
comparison) is a read concern and stays sourced from
``mozyo_bridge.scaffold.rules.scaffold_status``. The live adapter resolves it
(and the ``doctor_target`` / ``doctor_home`` argument resolution) *at call time*
through a localized lazy import of the ``doctor`` module, mirroring
:class:`mozyo_bridge.application.doctor_cli.LiveCliReads` and the other
``doctor_*`` section adapters. That keeps the existing integration tests that
patch ``doctor.scaffold_status`` / call ``doctor.doctor_scaffold_section``
directly unchanged, and it avoids a circular import (``doctor`` imports this
module).

The pure policy / value object can now be specified directly with a synthetic
scaffold-status detail — no real scaffolded checkout / drifted-manifest topology
is needed to test the verdict authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable

from mozyo_bridge.scaffold.rules import PRESETS


@dataclass(frozen=True)
class ScaffoldSectionVerdict:
    """Typed verdict for the ``scaffold`` doctor section.

    ``status`` mirrors the historical section status: ``missing`` when no
    manifest exists, ``invalid`` when the manifest is unusable, ``ok`` when the
    scaffold is clean, ``drifted`` otherwise. ``next_action`` carries the ordered
    operator remediation commands (empty when ``ok``).
    """

    status: str
    next_action: tuple[str, ...] = ()


def evaluate_scaffold_section(view: Mapping[str, Any]) -> ScaffoldSectionVerdict:
    """Pure policy: derive the section verdict from a scaffold read-view.

    The view is the mapping returned by the read port: the ``scaffold_status``
    ``detail`` plus the diagnosed ``target`` (str) and ``home`` (``Path`` or
    ``None``). This preserves the legacy branching exactly:

    - ``manifest == "missing"`` -> ``missing`` with the ``scaffold apply <preset>``
      bootstrap command.
    - ``manifest == "invalid"`` -> ``invalid`` with the manifest-regeneration
      command.
    - otherwise clean (``detail["clean"]``) -> ``ok`` with no next_action.
    - otherwise ``drifted``: the central-status drives the rules-install /
      scaffold-apply remediation, and any non-``ok`` router file appends the
      review-and-restore guidance.

    The ``--home`` suffix is derived from ``home`` here (a pure derivation of the
    read view), matching the legacy ``home_suffix`` exactly. The command wording
    is preserved byte-for-byte from the legacy collector.
    """

    detail = view["detail"]
    target = view["target"]
    home = view["home"]
    home_suffix = f" --home {home}" if home is not None else ""

    manifest = detail.get("manifest")
    central_status = detail.get("central_status")
    next_action: list[str] = []

    if manifest == "missing":
        section_status = "missing"
        next_action.append(
            f"mozyo-bridge scaffold apply <{'|'.join(PRESETS)}> --target "
            + target
            + home_suffix
        )
    elif manifest == "invalid":
        section_status = "invalid"
        next_action.append(
            "regenerate manifest with `mozyo-bridge scaffold apply <preset> --target "
            + target
            + home_suffix
            + " --backup`"
        )
    elif detail.get("clean"):
        section_status = "ok"
    else:
        section_status = "drifted"
        preset_label = detail.get("preset") or "<preset>"
        if central_status == "missing":
            if home is not None:
                next_action.append(f"mozyo-bridge rules install --home {home}")
            else:
                next_action.append("mozyo-bridge rules install")
        elif central_status in {"drifted-content", "drifted-version", "ok-version-only"}:
            next_action.append(
                "mozyo-bridge scaffold apply "
                + str(preset_label)
                + " --target "
                + target
                + home_suffix
                + " --backup"
            )
        if any(row.get("status") != "ok" for row in detail.get("files", [])):
            next_action.append(
                "review router files; rerun `mozyo-bridge scaffold apply "
                + str(preset_label)
                + " --target "
                + target
                + home_suffix
                + " --backup` to restore"
            )

    return ScaffoldSectionVerdict(
        status=section_status, next_action=tuple(next_action)
    )


@runtime_checkable
class ScaffoldReads(Protocol):
    """Port: read the per-repo scaffold readiness view.

    Implementations own the external read (``doctor_target`` / ``doctor_home``
    argument resolution + the ``scaffold_status`` manifest/central-state scan).
    The use case and policy depend only on the returned read-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveScaffoldReads:
    """Live adapter: read the scaffold view via the ``doctor`` helpers.

    ``doctor_target`` / ``doctor_home`` and ``scaffold_status`` are resolved
    through a localized lazy import *at call time* so the read stays cheap at
    module import, avoids a circular import (``doctor`` imports this module), and
    mirrors the call-time resolution discipline of ``doctor_cli.LiveCliReads``.

    ``args`` carries the parsed CLI namespace; the target / home resolution reads
    ``args`` at call time so the existing section integration tests (which patch
    ``doctor.scaffold_status`` or pass crafted namespaces) are unchanged.
    """

    def __init__(self, args: Any) -> None:
        self._args = args

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor

        target = _doctor.doctor_target(self._args)
        home = _doctor.doctor_home(self._args)
        detail = _doctor.scaffold_status(target, home=home)
        return {
            "detail": detail,
            "target": str(target),
            "home": home,
        }


class ScaffoldSectionUseCase:
    """Use case: read the scaffold view, apply the verdict policy.

    Returns the legacy ``doctor_scaffold_section`` dict shape byte-for-byte so the
    ``run_doctor`` aggregation, JSON output, and ``format_doctor_text`` rendering
    are unchanged.
    """

    def __init__(self, reads: ScaffoldReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_scaffold_section(view)
        return {
            "status": verdict.status,
            "target": view["target"],
            "detail": view["detail"],
            "next_action": list(verdict.next_action),
        }
