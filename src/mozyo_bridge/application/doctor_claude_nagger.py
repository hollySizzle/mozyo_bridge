"""Doctor Claude-Nagger section boundary (#12859).

The ``doctor_claude_nagger_section`` collector historically mixed three
responsibilities in one free-function body: the *external read* that introspects
the governed preset's Claude Nagger skeleton on the target checkout (the
``.claude-nagger/`` example files on disk, the activated ``config.yaml``, and the
scaffold manifest's record of whether the operator opted into the nagger
category), the *verdict authority* that maps that read-view to the section
``status`` (``skipped`` / ``incomplete`` / ``ok`` / ``skeleton-only``) and the
operator ``next_action`` guidance, and the *legacy section dict assembly*. This
module carves the collector slice out of the ``doctor`` body into an OOP-first
boundary (#12638 / #12853 follow-up):

- :class:`ClaudeNaggerSectionVerdict` is the typed value object for the verdict
  (status + the ordered next-action guidance).
- :func:`evaluate_claude_nagger_section` is the pure domain policy that decides
  the verdict from a nagger read-view alone (no filesystem access, no manifest
  parsing). It owns the manifest source-of-truth branching
  (``skipped`` / ``incomplete`` / ``ok`` / ``skeleton-only``) and the
  remediation-command wording.
- :class:`ClaudeNaggerReads` is the port for reading the nagger view and
  :class:`LiveClaudeNaggerReads` is the live adapter over the ``doctor`` module's
  ``doctor_target`` argument resolution, the ``CLAUDE_NAGGER_*`` constants, and
  the ``_scaffold_manifest_files`` manifest reader.
- :class:`ClaudeNaggerSectionUseCase` composes the port and the policy and
  re-assembles the legacy section dict byte-for-byte.

The manifest read (``_scaffold_manifest_files``) and the ``CLAUDE_NAGGER_*``
constants stay in ``doctor.py`` as the reusable read concern: the
``doctor_tmux_ui_artifact_info`` collector reads the same manifest, and the
constants describe the on-disk layout. The live adapter resolves them (and the
``doctor_target`` argument resolution) *at call time* through a localized lazy
import of the ``doctor`` module, mirroring
:class:`mozyo_bridge.application.doctor_scaffold.LiveScaffoldReads` and the other
``doctor_*`` section adapters. That keeps the existing CLI / integration tests
that exercise ``doctor_claude_nagger_section`` through the real checkout
unchanged, and it avoids a circular import (``doctor`` imports this module).

The pure policy / value object can now be specified directly with a synthetic
nagger read-view — no real scaffolded checkout / opt-out-with-debris topology is
needed to test the verdict authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class ClaudeNaggerSectionVerdict:
    """Typed verdict for the ``claude_nagger`` doctor section.

    ``status`` mirrors the historical section status: ``skipped`` when the
    manifest does not track the nagger category (or there is no manifest),
    ``incomplete`` when the manifest tracks it but example files are missing on
    disk, ``ok`` when the skeleton is present and ``config.yaml`` is activated,
    ``skeleton-only`` when the skeleton is present but not yet activated.
    ``next_action`` carries the ordered operator remediation guidance (empty when
    ``ok``).
    """

    status: str
    next_action: tuple[str, ...] = ()


def evaluate_claude_nagger_section(
    view: Mapping[str, Any],
) -> ClaudeNaggerSectionVerdict:
    """Pure policy: derive the section verdict from a nagger read-view.

    The view is the mapping returned by the read port: ``target`` (str),
    ``manifest_tracks_nagger`` (bool), ``examples`` (``{name: {"path": str,
    "present": bool}}`` in skeleton order), and ``config_yaml`` (``{"path": str,
    "present": bool}``). This preserves the legacy branching exactly:

    - manifest does not track the nagger category -> ``skipped`` with the
      rerun-without-``--skip-nagger`` guidance. The scaffold manifest is the
      source-of-truth, so leftover ``.bak.*`` debris under ``.claude-nagger/``
      after a ``--skip-nagger --backup`` opt-out does not produce a false
      ``incomplete``.
    - tracked but any example missing on disk -> ``incomplete`` with a
      per-missing-file restore command.
    - tracked, all examples present, ``config.yaml`` activated -> ``ok`` with no
      next_action.
    - tracked, all examples present, ``config.yaml`` absent -> ``skeleton-only``
      with the copy-to-activate guidance.

    The command wording is preserved byte-for-byte from the legacy collector.
    """

    target = view["target"]
    nagger_tracked = view["manifest_tracks_nagger"]
    examples = view["examples"]
    config_yaml = view["config_yaml"]
    next_action: list[str] = []

    if not nagger_tracked:
        # Either no scaffold manifest at all, or the manifest exists
        # but the operator opted out of the nagger category. Both
        # collapse to `skipped`; the suggested remedy points operators
        # at a rerun without --skip-nagger when they want it back.
        status = "skipped"
        next_action.append(
            "Claude Nagger is opt-out (manifest does not track .claude-nagger/); "
            f"rerun `mozyo-bridge scaffold apply <preset> --target {target}` "
            "without --skip-nagger to install the skeleton"
        )
    elif not all(info["present"] for info in examples.values()):
        # Tracked by manifest but examples missing on disk → real drift.
        status = "incomplete"
        for name, info in examples.items():
            if not info["present"]:
                next_action.append(
                    f"missing {info['path']}; rerun scaffold apply --backup to restore"
                )
    elif config_yaml["present"]:
        status = "ok"
    else:
        # Skeleton present, but the operator has not copied it into
        # `config.yaml` yet. The nagger does nothing until they do.
        status = "skeleton-only"
        next_action.append(
            f"copy {examples['config.yaml.example']['path']} to {config_yaml['path']} "
            "to activate Claude Nagger"
        )

    return ClaudeNaggerSectionVerdict(status=status, next_action=tuple(next_action))


@runtime_checkable
class ClaudeNaggerReads(Protocol):
    """Port: read the governed preset's Claude Nagger view on the target.

    Implementations own the external read (``doctor_target`` argument
    resolution + the on-disk ``.claude-nagger/`` example/``config.yaml`` probe +
    the scaffold manifest's record of the nagger category). The use case and
    policy depend only on the returned read-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveClaudeNaggerReads:
    """Live adapter: read the nagger view via the ``doctor`` helpers.

    ``doctor_target``, the ``CLAUDE_NAGGER_*`` constants, and the
    ``_scaffold_manifest_files`` manifest reader are resolved through a localized
    lazy import *at call time* so the read stays cheap at module import, avoids a
    circular import (``doctor`` imports this module), and mirrors the call-time
    resolution discipline of ``doctor_scaffold.LiveScaffoldReads``. The manifest
    reader and constants stay in ``doctor.py`` because the
    ``doctor_tmux_ui_artifact_info`` collector reuses the same manifest read.

    ``args`` carries the parsed CLI namespace; the target resolution reads
    ``args`` at call time so the existing section integration tests are
    unchanged.
    """

    def __init__(self, args: Any) -> None:
        self._args = args

    def describe(self) -> dict[str, Any]:
        from mozyo_bridge.application import doctor as _doctor

        target = _doctor.doctor_target(self._args)
        nagger_dir = target / _doctor.CLAUDE_NAGGER_DIRNAME

        example_paths = {
            name: nagger_dir / name for name in _doctor.CLAUDE_NAGGER_EXAMPLES
        }
        examples = {
            name: {
                "path": str(path),
                "present": path.exists(),
            }
            for name, path in example_paths.items()
        }
        config_path = nagger_dir / "config.yaml"

        tracked = _doctor._scaffold_manifest_files(target)
        nagger_tracked = any(
            p.startswith(_doctor.CLAUDE_NAGGER_MANIFEST_PREFIX) for p in tracked
        )

        return {
            "target": str(target),
            "nagger_dir": str(nagger_dir),
            "manifest_tracks_nagger": nagger_tracked,
            "examples": examples,
            "config_yaml": {
                "path": str(config_path),
                "present": config_path.exists(),
            },
        }


class ClaudeNaggerSectionUseCase:
    """Use case: read the nagger view, apply the verdict policy.

    Returns the legacy ``doctor_claude_nagger_section`` dict shape byte-for-byte
    so the ``run_doctor`` aggregation, JSON output, ``format_doctor_text``
    rendering, and ``doctor_instruction`` consumption are unchanged.
    """

    def __init__(self, reads: ClaudeNaggerReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_claude_nagger_section(view)
        return {
            "status": verdict.status,
            "target": view["target"],
            "nagger_dir": view["nagger_dir"],
            "manifest_tracks_nagger": view["manifest_tracks_nagger"],
            "examples": view["examples"],
            "config_yaml": view["config_yaml"],
            "next_action": list(verdict.next_action),
        }
