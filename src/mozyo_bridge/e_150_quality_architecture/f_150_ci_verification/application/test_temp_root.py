"""I/O side of the runner's temp-root handling (Redmine #15710).

Two responsibilities, both shared by ``tests run`` / ``tests profile`` /
``tests parallel`` through :func:`...commands_test_run.guarded_isolated_run`:

- :func:`resolve_tests_temp_base` — honour the operator's declarative
  ``MOZYO_TESTS_TMPDIR`` escape from a quota-pressured ``/tmp``, failing
  closed on an unusable declaration instead of silently falling back.
- :func:`diagnose_disk_pressure` — measure the temp base (block/inode
  usage, count of ``mozyo-tests-home-*`` roots) for the typed
  environmental note. Measuring only: nothing here deletes a root — the
  runner cannot positively prove a root has no live or foreign owner, so
  cleanup stays with the operator (see ``domain/test_disk_pressure.py``).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_disk_pressure import (
    DiskPressureDiagnosis,
)
from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    TESTS_TEMP_BASE_ENV,
    path_label,
)

#: The task-root prefix, mirrored from ``commands_test_run`` without importing
#: it (that module imports this one).
TESTS_HOME_PREFIX = "mozyo-tests-home-"


class TempRootUnavailable(RuntimeError):
    """The runner cannot (or must not) materialise its task temp root.

    Raised for an unusable ``MOZYO_TESTS_TMPDIR`` declaration and for a
    capacity refusal (EDQUOT / ENOSPC) while creating the root. Fail-closed
    like the fence errors on the same rail: a run whose temp root cannot be
    trusted is refused, not silently relocated.

    ``str(exc)`` is the DEFAULT message and carries no absolute path — like
    every other verdict surface it may be pasted into a ticket or a CI log
    (review j#108141 finding_pathleak). ``revealed`` is the local-debug
    variant with the raw paths, printed only under ``--reveal-paths``.
    """

    def __init__(
        self,
        message: str,
        diagnosis: DiskPressureDiagnosis | None = None,
        revealed: str | None = None,
    ) -> None:
        super().__init__(message)
        self.diagnosis = diagnosis
        self.revealed = revealed if revealed is not None else message


def resolve_tests_temp_base(
    env: dict[str, str] | os._Environ | None = None,
) -> Path | None:
    """The operator-declared base for task roots, or ``None`` for the default.

    A declared base that is missing, not a directory, or not writable raises
    :class:`TempRootUnavailable` — the operator asked for a specific base, so
    quietly using ``/tmp`` instead would run in exactly the environment the
    declaration exists to escape. The default message identifies the value by
    the env var name and a role/digest label only; the raw path rides on
    ``revealed`` for ``--reveal-paths`` (review j#108141 finding_pathleak).
    """
    source = os.environ if env is None else env
    declared = source.get(TESTS_TEMP_BASE_ENV, "").strip()
    if not declared:
        return None
    base = Path(declared).expanduser()

    def refusal(problem: str) -> TempRootUnavailable:
        tail = (
            f"{problem}; refusing to fall back to the default temp root "
            "silently"
        )
        return TempRootUnavailable(
            f"the {TESTS_TEMP_BASE_ENV} declaration "
            f"({path_label(str(base), 'declared-temp-base')}) {tail} "
            "(--reveal-paths prints the declared path)",
            revealed=f"{TESTS_TEMP_BASE_ENV}={declared} {tail}",
        )

    if not base.is_dir():
        raise refusal("is not an existing directory")
    if not os.access(base, os.W_OK | os.X_OK):
        raise refusal("is not writable")
    return base.resolve()


def effective_temp_base(env: dict[str, str] | os._Environ | None = None) -> Path:
    """Where task roots land: the declared base, else ``tempfile.gettempdir()``.

    For diagnosis only, so an unusable declaration degrades to the default
    here rather than raising — the probe must never mask the original error.
    """
    try:
        declared = resolve_tests_temp_base(env)
    except TempRootUnavailable:
        declared = None
    return declared if declared is not None else Path(tempfile.gettempdir())


def diagnose_disk_pressure(
    base: Path, markers: tuple[str, ...], stage: str
) -> DiskPressureDiagnosis:
    """Measure ``base`` and build the typed note. Never raises, never writes."""
    used = inodes = roots = None
    try:
        stats = os.statvfs(base)
        if stats.f_blocks:
            used = round(100 * (1 - stats.f_bavail / stats.f_blocks))
        if stats.f_files:
            inodes = round(100 * (1 - stats.f_favail / stats.f_files))
    except OSError:
        pass
    try:
        roots = sum(
            1
            for entry in base.iterdir()
            if entry.name.startswith(TESTS_HOME_PREFIX) and entry.is_dir()
        )
    except OSError:
        pass
    return DiskPressureDiagnosis(
        stage=stage,
        markers=markers,
        temp_base=str(base),
        used_percent=used,
        inode_percent=inodes,
        existing_roots=roots,
    )


__all__ = (
    "TESTS_HOME_PREFIX",
    "TempRootUnavailable",
    "diagnose_disk_pressure",
    "effective_temp_base",
    "resolve_tests_temp_base",
)
