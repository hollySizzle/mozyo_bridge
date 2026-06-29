"""Doctor CLI section boundary (#12845).

The ``doctor_cli_section`` collector historically mixed three responsibilities
in one free-function body: the *external read* that introspects the running CLI
install (running ``__version__``, ``mozyo-bridge`` executable on ``PATH``,
``mozyo_bridge`` package path, ``sys.executable``, the expected subcommands) plus
the repo-local source-drift detection, the *verdict authority* that maps a drift
record to the section ``status`` and the operator ``next_action`` guidance, and
the *legacy section dict assembly*. This module carves the collector slice out of
the ``doctor`` body into an OOP-first boundary (#12638 / #12844 follow-up):

- :class:`CliSectionVerdict` is the typed value object for the verdict
  (status + the ordered next-action guidance).
- :func:`evaluate_cli_section` is the pure domain policy that decides the
  verdict — including the drift warning message — from a CLI read-view alone
  (no filesystem access, no ``PATH`` lookup, no source parsing).
- :class:`CliReads` is the port for reading the CLI install view and
  :class:`LiveCliReads` is the live adapter over the running package
  introspection plus the ``doctor`` module's existing ``repo_local_source_drift``
  source-drift detector.
- :class:`CliSectionUseCase` composes the port and the policy and re-assembles
  the legacy section dict byte-for-byte.

The source-drift *detection* (the filesystem read that parses the checkout's
``src/mozyo_bridge/__init__.py`` and compares it against the running package) is
a read concern and stays in :func:`mozyo_bridge.application.doctor.repo_local_source_drift`
— the established source-version helper. The live adapter resolves it (and the
running-package introspection) *at call time* through a localized lazy import of
the ``doctor`` module, mirroring
:class:`mozyo_bridge.application.doctor_health.LiveDoctorSections` and the other
``doctor_*`` section adapters. That keeps the existing integration tests that
call ``doctor.repo_local_source_drift`` / ``doctor.doctor_cli_section`` directly
unchanged, and it avoids a circular import (``doctor`` imports this module).

The pure policy / value object can now be specified directly with a synthetic
drift record — no real checkout / stale-install topology is needed to test the
drift verdict authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol, runtime_checkable


@dataclass(frozen=True)
class CliSectionVerdict:
    """Typed verdict for the ``cli`` doctor section.

    ``status`` mirrors the historical section status (``ok`` when the running CLI
    is the whole story — no repo-local source drift to flag; ``warning`` when a
    checkout's repo-local source differs from the running install).
    ``next_action`` carries the ordered operator guidance (empty when ``ok``).
    """

    status: str
    next_action: tuple[str, ...] = ()


def evaluate_cli_section(view: Mapping[str, Any]) -> CliSectionVerdict:
    """Pure policy: derive the section verdict from a CLI read-view.

    The view is the mapping returned by the read port. ``drift`` is either
    ``None`` (no checkout, or the running CLI already *is* the repo-local
    source — nothing to warn about) or the source-drift record produced by
    ``repo_local_source_drift``.

    Any non-``None`` drift record warrants the warning: the running CLI is the
    installed CLI rather than the repo-local source, so it may lag the checkout's
    commits regardless of whether the version string matches (Redmine #11855
    review j#57416). The ``same-version`` relation gets an extra clause spelling
    out that an equal version string does not guarantee equal commits during
    dogfooding. The message wording is preserved byte-for-byte from the legacy
    collector.
    """

    drift = view.get("drift")
    if drift is None:
        return CliSectionVerdict(status="ok")

    version = view["version"]
    source_label = drift["source_version"] or "version unknown"
    message = (
        "running mozyo-bridge is the installed CLI "
        f"(version {version}) but this checkout has repo-local "
        f"source (src/mozyo_bridge {source_label}); during active "
        "development run the repo-local CLI instead: "
        f"{drift['repo_local_invocation']} <args>"
    )
    if drift["relation"] == "same-version":
        # The originating case: equal version, different commits. Spell it out
        # so an equal-version match is not mistaken for parity.
        message += (
            " (same version string does not guarantee the same commits "
            "during dogfooding; the install can lack newer subcommands)"
        )
    return CliSectionVerdict(status="warning", next_action=(message,))


@runtime_checkable
class CliReads(Protocol):
    """Port: read the running CLI install view (+ optional source drift).

    Implementations own the external reads (running ``__version__``, executable
    on ``PATH``, package path, ``sys.executable``, expected subcommands) and the
    repo-local source-drift detection. The use case and policy depend only on the
    returned read-view mapping.
    """

    def describe(self) -> dict[str, Any]:
        ...


class LiveCliReads:
    """Live adapter: introspect the running CLI via the ``doctor`` helpers.

    The running-package introspection and ``repo_local_source_drift`` are
    resolved through a localized lazy import *at call time* so the read stays
    cheap at module import, avoids a circular import (``doctor`` imports this
    module), and mirrors the call-time resolution discipline of
    ``doctor_health.LiveDoctorSections``. Source-drift is detected only when a
    ``target`` checkout is diagnosed (matching the legacy ``target is not None``
    guard); outside a checkout the running CLI is the whole story and ``drift``
    is ``None``.
    """

    def __init__(self, target: Path | None) -> None:
        self._target = target

    def describe(self) -> dict[str, Any]:
        import shutil
        import sys

        import mozyo_bridge
        from mozyo_bridge import __version__
        from mozyo_bridge.application import doctor as _doctor

        package_path = Path(mozyo_bridge.__file__).resolve().parent
        executable = shutil.which("mozyo-bridge")
        drift = None
        if self._target is not None:
            drift = _doctor.repo_local_source_drift(
                self._target, package_path, __version__
            )
        return {
            "version": __version__,
            "executable": executable or "",
            "package_path": str(package_path),
            "python": sys.executable,
            "subcommands": list(_doctor.EXPECTED_SUBCOMMANDS),
            "drift": drift,
        }


class CliSectionUseCase:
    """Use case: read the CLI install view, apply the verdict policy.

    Returns the legacy ``doctor_cli_section`` dict shape byte-for-byte so the
    ``run_doctor`` aggregation, JSON output, and ``format_doctor_text`` rendering
    are unchanged. The ``source_drift`` key is present only when a drift record
    was detected, matching the legacy collector (the key is absent otherwise).
    """

    def __init__(self, reads: CliReads) -> None:
        self._reads = reads

    def execute(self) -> dict[str, Any]:
        view = self._reads.describe()
        verdict = evaluate_cli_section(view)
        section: dict[str, Any] = {
            "status": verdict.status,
            "version": view["version"],
            "executable": view["executable"],
            "package_path": view["package_path"],
            "python": view["python"],
            "subcommands": list(view["subcommands"]),
            "next_action": list(verdict.next_action),
        }
        if view.get("drift") is not None:
            # Preserve the legacy key insertion order: source_drift is appended
            # last (after next_action) when a drift record is present.
            section["source_drift"] = view["drift"]
        return section
