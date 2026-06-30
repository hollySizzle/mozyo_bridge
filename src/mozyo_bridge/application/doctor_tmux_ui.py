"""Doctor tmux-UI artifact section boundary (#12866).

The ``doctor_tmux_ui_artifact_info`` collector historically mixed three
responsibilities in one free-function body: the *external read* that introspects
the governed preset's tmux UI snippet on the target checkout (the scaffold
manifest's record of whether the operator opted into the ``agent-ui.conf``
category, the on-disk presence of the snippet, and the host tmux config's
managed-source wiring state via ``tmux_ui.compute_status``), the *verdict
authority* that maps that read-view to the section ``status``
(``skipped`` / ``ok`` / ``incomplete``) plus the operator ``next_action``
guidance for both the artifact landing and the host wiring, and the *legacy
section dict assembly*. This module carves the collector slice out of the
``doctor`` body into an OOP-first boundary (#12638 / #12859 follow-up):

- :class:`TmuxUiArtifactVerdict` is the typed value object for the verdict
  (status + the artifact-level next-action guidance + the host-wiring-level
  next-action guidance).
- :func:`evaluate_tmux_ui_artifact_section` is the pure domain policy that
  decides the verdict from a tmux-ui read-view alone (no filesystem access, no
  manifest parsing, no host-config inspection). It owns the manifest
  source-of-truth branching (``skipped`` / ``ok`` / ``incomplete``), the
  host-wiring remediation gating (only when the artifact is tracked and present),
  and the remediation-command wording.
- :class:`TmuxUiArtifactReads` is the port for reading the tmux-ui view and
  :class:`LiveTmuxUiArtifactReads` is the live adapter over the ``doctor``
  module's ``_scaffold_manifest_files`` manifest reader + the ``TMUX_UI_*``
  layout constants and the ``tmux_ui`` host-wiring probe.
- :class:`TmuxUiArtifactSectionUseCase` composes the port and the policy and
  re-assembles the legacy section dict byte-for-byte.

The manifest read (``_scaffold_manifest_files``) and the ``TMUX_UI_*`` layout
constants stay in ``doctor.py`` as the reusable read concern: the manifest
reader is shared with the ``doctor_claude_nagger_section`` collector and the
constants describe the on-disk layout. The live adapter resolves them (and the
``tmux_ui`` host-wiring probe) *at call time* through localized lazy imports of
the ``doctor`` and ``tmux_ui`` modules, mirroring
:class:`mozyo_bridge.application.doctor_claude_nagger.LiveClaudeNaggerReads` and
the other ``doctor_*`` section adapters. That keeps the existing CLI / integration
tests that exercise ``doctor_tmux_ui_artifact_info`` through the real checkout
unchanged, and it avoids a circular import (``doctor`` imports this module).

Unlike the other ``doctor_*`` collectors, ``doctor_tmux_ui_artifact_info`` takes
a resolved ``target`` :class:`~pathlib.Path` (not the parsed CLI namespace) — its
sole caller, ``doctor_health.LiveDoctorSections``, already resolves
``doctor_target(args)`` before attaching the artifact record to the ``tmux``
section — so the live adapter is constructed with the target path directly.

The pure policy / value object can now be specified directly with a synthetic
tmux-ui read-view — no real scaffolded checkout / host ``~/.tmux.conf`` wiring
topology is needed to test the verdict authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class TmuxUiArtifactVerdict:
    """Typed verdict for the ``tmux`` section's ``artifact`` sub-record.

    ``status`` mirrors the historical section status: ``skipped`` when the
    manifest does not track the tmux-ui category (or there is no manifest),
    ``ok`` when the manifest tracks it and the snippet is on disk, ``incomplete``
    when the manifest tracks it but the snippet was removed locally (real drift).
    ``next_action`` carries the ordered artifact-landing remediation guidance and
    ``host_wiring_next_action`` carries the ordered host-config wiring remediation
    guidance (both empty when nothing needs doing).
    """

    status: str
    next_action: tuple[str, ...] = ()
    host_wiring_next_action: tuple[str, ...] = ()


def evaluate_tmux_ui_artifact_section(
    view: Mapping[str, Any],
) -> TmuxUiArtifactVerdict:
    """Pure policy: derive the section verdict from a tmux-ui read-view.

    The view is the mapping returned by the read port: ``target`` (str),
    ``snippet_path`` (str), ``present`` (bool), ``manifest_tracks_tmux_ui``
    (bool), ``host_wiring`` (the raw ``tmux_ui.compute_status`` record), and the
    host-wiring state literals ``state_not_installed`` / ``state_drift`` for the
    comparison. This preserves the legacy branching exactly:

    - manifest does not track the tmux-ui category -> ``skipped`` with the
      rerun-without-``--skip-tmux-ui`` guidance. The scaffold manifest is the
      source-of-truth, so leftover ``.bak.*`` debris under
      ``.mozyo-bridge/tmux/`` after a ``--skip-tmux-ui --backup`` opt-out does
      not produce a false ``incomplete``.
    - tracked and the snippet present on disk -> ``ok``.
    - tracked but the snippet missing on disk -> ``incomplete`` with the
      restore command.

    The host-wiring remediation is gated on the artifact being both tracked and
    present (a snippet that is not installed has nothing to wire). When gated in,
    a ``not-installed`` host config yields the wire-it guidance and a ``drift``
    host config yields the refresh guidance. The command wording is preserved
    byte-for-byte from the legacy collector.
    """

    target = view["target"]
    snippet_path = view["snippet_path"]
    is_tracked = view["manifest_tracks_tmux_ui"]
    present = view["present"]
    host_wiring = view["host_wiring"]
    state_not_installed = view["state_not_installed"]
    state_drift = view["state_drift"]

    next_action: list[str] = []
    if not is_tracked:
        status = "skipped"
        next_action.append(
            "tmux UI helper is opt-out (manifest does not track agent-ui.conf); "
            f"rerun `mozyo-bridge scaffold apply <preset> --target {target}` "
            "without --skip-tmux-ui to install the snippet"
        )
    elif present:
        status = "ok"
    else:
        status = "incomplete"
        next_action.append(
            f"manifest tracks {snippet_path} but the file is missing; "
            "rerun scaffold apply --backup to restore"
        )

    wiring_actions: list[str] = []
    if is_tracked and present:
        if host_wiring["state"] == state_not_installed:
            wiring_actions.append(
                "host tmux config does not source agent-ui.conf; run "
                f"`mozyo-bridge tmux-ui install --target {target}` to wire it"
            )
        elif host_wiring["state"] == state_drift:
            wiring_actions.append(
                "host tmux config has a managed block pointing elsewhere "
                f"({host_wiring.get('drift_reason')}); rerun "
                f"`mozyo-bridge tmux-ui install --target {target} --force` to refresh"
            )

    return TmuxUiArtifactVerdict(
        status=status,
        next_action=tuple(next_action),
        host_wiring_next_action=tuple(wiring_actions),
    )


@runtime_checkable
class TmuxUiArtifactReads(Protocol):
    """Port: read the governed preset's tmux-ui artifact view on the target.

    Implementations own the external read (the scaffold manifest's record of the
    tmux-ui category + the on-disk snippet probe + the host tmux config's
    managed-source wiring state). The use case and policy depend only on the
    returned read-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveTmuxUiArtifactReads:
    """Live adapter: read the tmux-ui artifact view via the ``doctor`` helpers.

    The ``TMUX_UI_*`` layout constants and the ``_scaffold_manifest_files``
    manifest reader are resolved through a localized lazy import of the ``doctor``
    module *at call time*, and the host-wiring state through a lazy import of the
    ``tmux_ui`` module, so the read stays cheap at module import, avoids a
    circular import (``doctor`` imports this module), and mirrors the call-time
    resolution discipline of ``doctor_claude_nagger.LiveClaudeNaggerReads``. The
    manifest reader and constants stay in ``doctor.py`` because the
    ``doctor_claude_nagger_section`` collector reuses the same manifest read.

    ``target`` is the already-resolved checkout path (``doctor_health`` calls
    ``doctor_target(args)`` before constructing this adapter), so the read does
    not touch the CLI namespace.
    """

    def __init__(self, target: Any) -> None:
        self._target = target

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor
        from mozyo_bridge.application import tmux_ui as _tmux_ui

        target = self._target
        tracked = _doctor._scaffold_manifest_files(target)
        is_tracked = _doctor.TMUX_UI_MANIFEST_PATH in tracked
        snippet_path = target / _doctor.TMUX_UI_RELATIVE_PATH
        present = snippet_path.exists()

        host_conf = _tmux_ui.default_host_tmux_conf()
        host_wiring = _tmux_ui.compute_status(target, host_conf)

        return {
            "target": str(target),
            "snippet_path": str(snippet_path),
            "present": present,
            "manifest_tracks_tmux_ui": is_tracked,
            "host_wiring": {
                "state": host_wiring["state"],
                "tmux_conf": host_wiring["tmux_conf"],
                "tmux_conf_exists": host_wiring["tmux_conf_exists"],
                "current_source_path": host_wiring["current_source_path"],
                "expected_snippet": host_wiring["expected_snippet"],
                "drift_reason": host_wiring["drift_reason"],
            },
            "state_not_installed": _tmux_ui.STATE_NOT_INSTALLED,
            "state_drift": _tmux_ui.STATE_DRIFT,
        }


class TmuxUiArtifactSectionUseCase:
    """Use case: read the tmux-ui artifact view, apply the verdict policy.

    Returns the legacy ``doctor_tmux_ui_artifact_info`` dict shape byte-for-byte
    so the ``run_doctor`` aggregation (which attaches this record to the ``tmux``
    section), JSON output, ``format_doctor_text`` rendering, and
    ``doctor_instruction`` consumption are unchanged.
    """

    def __init__(self, reads: TmuxUiArtifactReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_tmux_ui_artifact_section(view)
        host_wiring = view["host_wiring"]
        return {
            "status": verdict.status,
            "path": view["snippet_path"],
            "present": view["present"],
            "manifest_tracks_tmux_ui": view["manifest_tracks_tmux_ui"],
            "host_wiring": {
                "state": host_wiring["state"],
                "tmux_conf": host_wiring["tmux_conf"],
                "tmux_conf_exists": host_wiring["tmux_conf_exists"],
                "current_source_path": host_wiring["current_source_path"],
                "expected_snippet": host_wiring["expected_snippet"],
                "drift_reason": host_wiring["drift_reason"],
                "next_action": list(verdict.host_wiring_next_action),
            },
            "next_action": list(verdict.next_action),
        }
