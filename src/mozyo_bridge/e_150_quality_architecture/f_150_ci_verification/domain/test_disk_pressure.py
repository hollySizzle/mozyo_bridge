"""Pure core of temp-root disk-pressure diagnosis (Redmine #15710).

A full ``mozyo-bridge tests run`` intermittently failed with
``OSError: [Errno 122] Disk quota exceeded`` while writing under the
``mozyo-tests-home-*`` task root, producing dozens of false test errors
(48 / 41 in one evening) although ``df`` showed /tmp at 3% blocks / 1%
inodes — a per-user tmpfs quota or transient pressure, not a diff defect.
The runner must make that failure *identifiable as environmental* in its
own summary, so a red run under quota pressure is not misread as a
regression introduced by the change under test.

This module owns the decisions only — which errnos count as disk
pressure, how stderr is scanned for their tracebacks, and what the typed
note says. Probing the filesystem (statvfs, counting leftover task
roots) lives in ``application/test_temp_root.py``.

The diagnosis is an *annotation*, never a verdict: it does not flip a
red suite green, does not retry anything, and does not delete anything.
Leftover ``mozyo-tests-home-*`` roots are counted and reported, never
removed — the runner cannot positively prove a root has no live or
foreign owner, and the fail-closed principle of the isolation rail
(``test-process-home-isolation.md``) applies to cleanup too.
"""

from __future__ import annotations

import errno
from dataclasses import dataclass

from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_home_isolation import (
    path_label,
)

#: Errnos that mean "the filesystem refused for capacity reasons": quota
#: (EDQUOT, 122 on Linux) and out-of-space (ENOSPC, 28). EACCES/EROFS et al.
#: are deliberately NOT here — a permission refusal is the fence working,
#: not the environment failing.
DISK_PRESSURE_ERRNOS = (errno.EDQUOT, errno.ENOSPC)

#: The leading token of every diagnosis line. Stable so journals, CI logs
#: and follow-up greps identify the environmental note by one literal.
PRESSURE_NOTE = "environmental disk pressure suspected"

#: Byte patterns a CPython traceback renders for the two errnos. Scanned on
#: the suite's stderr because the errors happen inside the *child* test
#: process, where the runner sees only a non-zero exit code otherwise.
MARKER_PATTERNS: tuple[tuple[str, bytes], ...] = (
    ("EDQUOT", b"[Errno 122]"),
    ("EDQUOT", b"Disk quota exceeded"),
    ("ENOSPC", b"[Errno 28]"),
    ("ENOSPC", b"No space left on device"),
)


def is_disk_pressure_errno(code: int | None) -> bool:
    """True when ``code`` is a capacity refusal (EDQUOT / ENOSPC)."""
    return code in DISK_PRESSURE_ERRNOS


class MarkerScanner:
    """Incremental scanner for disk-pressure markers in a byte stream.

    Fed chunk by chunk as the child's stderr is pumped through to the
    terminal, so scanning never buffers the whole transcript. A tail of
    ``max(len(pattern)) - 1`` bytes is carried between chunks: a marker
    split across a chunk boundary is still counted, and a marker wholly
    inside the carried tail cannot fit, so nothing is counted twice.
    """

    def __init__(self) -> None:
        # Keyed per pattern: one traceback line carries BOTH the bracket form
        # and the strerror form ("OSError: [Errno 122] Disk quota exceeded"),
        # so summing patterns would double every occurrence. The per-token
        # count reported is the max over that token's patterns instead.
        self._counts: dict[bytes, int] = {}
        self._tail = b""
        self._keep = max(len(pattern) for _, pattern in MARKER_PATTERNS) - 1

    def feed(self, chunk: bytes) -> None:
        window = self._tail + chunk
        for _, pattern in MARKER_PATTERNS:
            found = window.count(pattern)
            if found:
                self._counts[pattern] = self._counts.get(pattern, 0) + found
        # Erase counted markers from the carried tail so a pattern that ends
        # exactly at the boundary is not recounted; shorter patterns nested in
        # longer ones (none today) would need per-pattern tails instead.
        for _, pattern in MARKER_PATTERNS:
            window = window.replace(pattern, b"\x00" * len(pattern))
        self._tail = window[-self._keep:] if self._keep else b""

    @property
    def suspected(self) -> bool:
        return bool(self._counts)

    @property
    def markers(self) -> tuple[str, ...]:
        """Rendered counts, e.g. ``("EDQUOT x48",)`` — names and counts only."""
        by_token: dict[str, int] = {}
        for token, pattern in MARKER_PATTERNS:
            count = self._counts.get(pattern, 0)
            if count:
                by_token[token] = max(by_token.get(token, 0), count)
        return tuple(
            f"{token} x{count}" if count > 1 else token
            for token, count in sorted(by_token.items())
        )


@dataclass(frozen=True)
class DiskPressureDiagnosis:
    """A typed environmental note attached to one run's outcome.

    Value-free by default like every other verdict surface: the temp base
    is identified by role/digest label (it may be an operator-declared
    path), and the detail carries only percentages and counts. ``None``
    for a probe value means "could not be measured", which the rendering
    says explicitly rather than printing a fake zero.
    """

    #: Where the pressure was seen: ``"temp-root-setup"`` (the runner's own
    #: OSError while materialising the task root) or ``"suite-stderr"``
    #: (marker tracebacks scanned off the child suite's stderr).
    stage: str
    markers: tuple[str, ...]
    temp_base: str
    used_percent: int | None = None
    inode_percent: int | None = None
    #: ``mozyo-tests-home-*`` roots present in the temp base after the run —
    #: possibly leftover from crashed runs, possibly live concurrent runs.
    #: Counted for the operator's triage, never touched.
    existing_roots: int | None = None

    @property
    def suspected(self) -> bool:
        return bool(self.markers)

    @property
    def temp_base_label(self) -> str:
        return path_label(self.temp_base, "tests-temp-base")

    @property
    def reasons(self) -> tuple[str, ...]:
        """The typed note, phrased so a red run reads as environmental."""

        def pct(value: int | None) -> str:
            return "unmeasured" if value is None else f"{value}%"

        roots = (
            "unmeasured"
            if self.existing_roots is None
            else str(self.existing_roots)
        )
        # Deliberately probabilistic (review j#108141 finding_overclaim): the
        # markers are raw stderr substrings, which support suspicion and no
        # more — and a diff that increases temp usage can itself push a run
        # over the quota, so the note must not certify the change innocent.
        return (
            f"{PRESSURE_NOTE} ({self.stage}): {', '.join(self.markers)} -- "
            "the suspected cause is an environment condition (per-user temp "
            "quota / transient pressure); verify before attributing these "
            "failures to the change under test, which can still be involved "
            "(e.g. by increasing temp usage)",
            f"temp base {self.temp_base_label}: blocks {pct(self.used_percent)} "
            f"used, inodes {pct(self.inode_percent)} used; existing "
            f"mozyo-tests-home-* roots (leftover or concurrent, not removed): "
            f"{roots}",
            "recovery: remove leftover mozyo-tests-home-* roots you can "
            "attribute to finished runs, or point MOZYO_TESTS_TMPDIR at a "
            "roomier writable directory; the per-user quota itself is an "
            "operator environment concern",
        )

    def as_dict(self, *, reveal_paths: bool = False) -> dict:
        return {
            "suspected": self.suspected,
            "stage": self.stage,
            "markers": list(self.markers),
            "temp_base": self.temp_base if reveal_paths else self.temp_base_label,
            "used_percent": self.used_percent,
            "inode_percent": self.inode_percent,
            "existing_roots": self.existing_roots,
        }


__all__ = (
    "DISK_PRESSURE_ERRNOS",
    "MARKER_PATTERNS",
    "PRESSURE_NOTE",
    "DiskPressureDiagnosis",
    "MarkerScanner",
    "is_disk_pressure_errno",
)
