"""Identity-stability oracle for ``_hash_untracked`` (Redmine #14655).

``_hash_untracked`` must fail closed whenever the object it *classified* is not the object
it *hashed* — an inode swap, a symlink retarget, or a mid-read rewrite. It proves that by
re-observing the object at three points and comparing selected ``stat`` fields:

===============  ====================================  ==========================================
observation      taken at                              compared against
===============  ====================================  ==========================================
``info``         ``lstat`` before anything             (kind gates; the ``open`` re-check)
``after``        ``lstat`` after ``readlink``          ``info``  (symlink observation window)
``opened``       ``fstat`` right after ``open``        ``info``  (lstat -> open window)
``settled``      ``fstat`` after the last ``read``     ``opened`` (the read window)
===============  ====================================  ==========================================

**Why this module exists.** The previous oracle (``HashUntrackedIdentityStabilityTest`` in
``test_sublane_hibernate.py``) produced the drift by asking the *real* filesystem for it, so
two of its cases silently inherited filesystem-specific behaviour and were not CI-hermetic
(reproduced on Linux overlayfs/tmpfs in #14580 review j#91949):

- the same-size, mtime-restored mid-read rewrite left ``st_ctime_ns`` as the ONLY
  discriminator, so it needed sub-second ``ctime`` granularity. On a filesystem that stores
  second-granular ``ctime`` (ext4 with 128-byte inodes, and overlayfs over it) a rewrite that
  completes inside one second drifts nothing and production legitimately returns a digest;
- the symlink swap removed and recreated the link, so it needed the recreated symlink to get
  a *different* inode number. Linux reuses just-freed inode numbers, and with a coarse
  ``ctime`` the recreated link is indistinguishable from the original.

The fix separates the two concerns the acceptance calls out:

1. :class:`UntrackedIdentityFieldOracle` — **hermetic, always runs.** It injects the drift
   into the observation itself (one ``stat`` field, one observation, one delta) instead of
   asking the filesystem to produce it, so every compared field is exercised on every
   platform. Each field is checked against its own zero-delta control through the *same*
   wrapper, so the fail-closed verdict is attributable to the delta and nothing else. This
   layer is what keeps every field mutation-covered; it never skips.
2. :class:`UntrackedIdentityRealFilesystemTest` — **real filesystem.** Only this layer can
   pin *when* production takes its observations (a synthetic drift is delivered to whichever
   call site exists, so it cannot tell "re-stat after the read" from "re-stat before it").
   Three of its four races are made deterministic by keeping both objects alive across the
   swap, so a distinct inode is guaranteed rather than hoped for. The fourth genuinely needs
   filesystem support and is gated on a typed, measured capability probe.
3. :class:`UntrackedIdentityCoverageTest` — a differential oracle over the production source:
   the per-field table below is compared against the fields production actually reads, so a
   newly compared field cannot land without a test.

A capability that is absent produces a typed skip, never a silent pass — and never a coverage
hole either, because layer 1 covers the same field unconditionally.

Refs: Redmine #14655 filesystem hermeticity; #14580 review j#91949; #13843 review j#83853 /
j#83889 (the fail-closed contract itself, which this module does not change).
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import enum
import inspect
import os
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Iterator, Optional
from unittest import mock

import mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_hibernate_boundary as B  # noqa: E501

# Every ``stat`` field production reads off each re-observation, and therefore every field
# this module must exercise. ``UntrackedIdentityCoverageTest`` re-derives this from the
# production source; it is NOT free-form documentation.
_OBSERVED_FIELDS: dict[str, frozenset[str]] = {
    # The first ``lstat``. ``st_mode`` drives the kind gates (symlink / regular / everything
    # else fails closed) and is covered by the kind tests in ``test_sublane_hibernate.py``;
    # ``st_dev`` / ``st_ino`` / ``st_ctime_ns`` are the baseline the later windows compare to.
    "info": frozenset({"st_mode", "st_dev", "st_ino", "st_ctime_ns"}),
    "after": frozenset({"st_dev", "st_ino", "st_ctime_ns"}),
    "opened": frozenset({"st_mode", "st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"}),
    "settled": frozenset({"st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns"}),
}


# ---------------------------------------------------------------------------
# Synthetic observation drift (hermetic).
# ---------------------------------------------------------------------------


class _StatView:
    """A read-through view of a real ``os.stat_result`` with exactly ONE field overridden.

    Every other field keeps its real value, so a production check that reads a field this
    module does not model still sees a coherent, unmodified observation.
    """

    __slots__ = ("_real", "_field", "_value")

    def __init__(self, real: os.stat_result, field: str, value: int) -> None:
        self._real = real
        self._field = field
        self._value = value

    def __getattr__(self, name: str) -> object:
        if name == self._field:
            return self._value
        return getattr(self._real, name)


def _drift(observed: os.stat_result, field: str, delta: int) -> int:
    """The value to substitute for ``field``. ``delta == 0`` reproduces the real value."""
    if field == "st_mode":
        # A "kind" drift is not an increment: swap the file-type bits regular -> FIFO,
        # leaving the permission bits alone.
        if delta == 0:
            return observed.st_mode
        return (observed.st_mode ^ stat.S_IFMT(observed.st_mode)) | stat.S_IFIFO
    return getattr(observed, field) + delta


@contextlib.contextmanager
def _observation_drift(syscall: str, ordinal: int, field: str, delta: int) -> Iterator[dict]:
    """Substitute ``field`` on the ``ordinal``-th ``os.<syscall>`` call made by production.

    ``delta == 0`` still routes that observation through :class:`_StatView`, so a control run
    and a drift run differ ONLY in the delta — a wrapper defect cannot masquerade as a pass.
    """
    real = getattr(B.os, syscall)
    seen = {"calls": 0}

    def wrapper(*args: object, **kwargs: object) -> object:
        result = real(*args, **kwargs)
        seen["calls"] += 1
        if seen["calls"] == ordinal:
            return _StatView(result, field, _drift(result, field, delta))
        return result

    with mock.patch.object(B.os, syscall, side_effect=wrapper):
        yield seen


class UntrackedIdentityFieldOracle(unittest.TestCase):
    """Every ``stat`` field production compares must be load-bearing, on every filesystem.

    One test per (observation, field). Each asserts BOTH directions: with a zero delta the
    path hashes, and with a nonzero delta on that single field it fails closed. Dropping the
    field from the production comparison turns exactly one of these red.
    """

    def _assert_field_is_load_bearing(
        self,
        *,
        make_path: str,
        syscall: str,
        ordinal: int,
        field: str,
        delta: int = 7,
    ) -> None:
        for label, applied, expect_digest in (("control", 0, True), ("drift", delta, False)):
            with self.subTest(case=label, field=field, syscall=syscall, ordinal=ordinal):
                with tempfile.TemporaryDirectory() as tmp:
                    self._materialise(Path(tmp), make_path)
                    with _observation_drift(syscall, ordinal, field, applied) as seen:
                        result = B._hash_untracked(Path(tmp), make_path.encode())
                    self.assertGreaterEqual(
                        seen["calls"],
                        ordinal,
                        f"production made {seen['calls']} os.{syscall} call(s); the drift was "
                        f"never delivered to observation #{ordinal}, so this test proved "
                        f"nothing about {field}",
                    )
                    if expect_digest:
                        self.assertIsNotNone(
                            result, "the zero-delta control must still hash the path"
                        )
                    else:
                        self.assertIsNone(
                            result, f"a drifted {field} must fail the fingerprint closed"
                        )

    @staticmethod
    def _materialise(root: Path, name: str) -> None:
        if name == "link":
            os.symlink("some-target", root / name)
        else:
            (root / name).write_bytes(b"payload" * 4096)

    # -- ``after``: the symlink readlink window (2nd lstat vs the 1st). --------------------

    def test_symlink_window_dev_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="link", syscall="lstat", ordinal=2, field="st_dev"
        )

    def test_symlink_window_ino_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="link", syscall="lstat", ordinal=2, field="st_ino"
        )

    def test_symlink_window_ctime_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="link", syscall="lstat", ordinal=2, field="st_ctime_ns"
        )

    # -- ``opened`` vs ``info``: the lstat -> open window. ---------------------------------
    #
    # ``st_dev`` / ``st_ino`` are drifted on the *lstat* side, not on ``opened``: ``opened``
    # is also the baseline for the read window, so drifting it there would let the read-window
    # comparison catch the mismatch and mask the removal of this check. Drifting ``info``
    # leaves ``settled == opened``, so a fail-closed verdict can only come from this window.

    def test_open_window_dev_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="lstat", ordinal=1, field="st_dev"
        )

    def test_open_window_ino_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="lstat", ordinal=1, field="st_ino"
        )

    def test_open_window_kind_is_load_bearing(self) -> None:
        # The kind re-check lives on ``opened`` only, and no later comparison reads
        # ``st_mode``, so drifting it there is already isolated to this window.
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="fstat", ordinal=1, field="st_mode"
        )

    # -- ``settled`` vs ``opened``: the read window (2nd fstat vs the 1st). ----------------

    def test_read_window_dev_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="fstat", ordinal=2, field="st_dev"
        )

    def test_read_window_ino_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="fstat", ordinal=2, field="st_ino"
        )

    def test_read_window_size_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="fstat", ordinal=2, field="st_size"
        )

    def test_read_window_mtime_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="fstat", ordinal=2, field="st_mtime_ns"
        )

    def test_read_window_ctime_is_load_bearing(self) -> None:
        self._assert_field_is_load_bearing(
            make_path="regular.bin", syscall="fstat", ordinal=2, field="st_ctime_ns"
        )


# ---------------------------------------------------------------------------
# Typed filesystem capability probing.
# ---------------------------------------------------------------------------


class FsCapability(enum.Enum):
    """A filesystem behaviour a real-filesystem race needs in order to mean anything."""

    #: A same-size, in-place rewrite with ``mtime`` restored still drifts ``st_ctime_ns``.
    #: Absent wherever ``ctime`` is second-granular (ext4 with 128-byte inodes, overlayfs
    #: over it) and the rewrite completes inside one tick.
    CTIME_DRIFT_ON_SAME_SIZE_INPLACE_REWRITE = "ctime_drift_on_same_size_inplace_rewrite"

    #: Two paths that exist at the same time on the same device have different inode numbers.
    #: Required by every race that swaps one live object over another.
    DISTINCT_INODE_FOR_COEXISTING_PATHS = "distinct_inode_for_coexisting_paths"


@dataclasses.dataclass(frozen=True)
class CapabilityVerdict:
    """The measured answer for one :class:`FsCapability` on one directory's filesystem."""

    capability: FsCapability
    present: bool
    detail: str

    def require(self, case: unittest.TestCase) -> None:
        """Skip ``case`` with a typed reason when the capability is absent.

        The skip removes only the *real-filesystem* reproduction. The field this race would
        have exercised stays covered unconditionally by
        :class:`UntrackedIdentityFieldOracle`, so an absent capability never leaves the
        contract unverified.
        """
        if not self.present:
            raise unittest.SkipTest(f"capability_absent:{self.capability.value}: {self.detail}")


def _identity(observed: os.stat_result) -> dict[str, int]:
    return {
        "dev": observed.st_dev,
        "ino": observed.st_ino,
        "size": observed.st_size,
        "mtime_ns": observed.st_mtime_ns,
        "ctime_ns": observed.st_ctime_ns,
    }


def _drifted_fields(before: os.stat_result, after: os.stat_result) -> tuple[str, ...]:
    lhs, rhs = _identity(before), _identity(after)
    return tuple(sorted(key for key in lhs if lhs[key] != rhs[key]))


def probe_ctime_drift(directory: Path, samples: int = 5) -> CapabilityVerdict:
    """Measure whether a same-size, mtime-restored rewrite drifts ``ctime`` on THIS filesystem.

    Repeated ``samples`` times and required to hold EVERY time: on a second-granular
    filesystem a single rewrite occasionally straddles a tick boundary and drifts by luck, and
    a probe that accepted one lucky sample would re-admit the flake it exists to prevent. The
    probe writes a small file, so it is strictly faster than the race it gates — a filesystem
    that always drifts for the probe always drifts for the (longer) real test.
    """
    capability = FsCapability.CTIME_DRIFT_ON_SAME_SIZE_INPLACE_REWRITE
    payload = 4096
    for attempt in range(samples):
        target = directory / f"probe-{attempt}"
        target.write_bytes(b"A" * payload)
        before = os.stat(target)
        with open(target, "r+b") as handle:
            handle.seek(0)
            handle.write(b"B" * payload)
        os.utime(target, ns=(before.st_atime_ns, before.st_mtime_ns))
        observed = _drifted_fields(before, os.stat(target))
        os.remove(target)
        if observed != ("ctime_ns",):
            return CapabilityVerdict(
                capability,
                False,
                f"sample {attempt + 1}/{samples} drifted {observed or '()'}, expected "
                "('ctime_ns',) — this filesystem cannot isolate a ctime-only drift",
            )
    return CapabilityVerdict(
        capability, True, f"{samples}/{samples} samples drifted ctime_ns and nothing else"
    )


def probe_distinct_inode(directory: Path) -> CapabilityVerdict:
    """Measure that two simultaneously-live paths have distinct ``(st_dev, st_ino)``."""
    capability = FsCapability.DISTINCT_INODE_FOR_COEXISTING_PATHS
    one, two = directory / "probe-one", directory / "probe-two"
    one.write_text("one")
    two.write_text("two")
    left, right = os.lstat(one), os.lstat(two)
    os.remove(one)
    os.remove(two)
    if (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino):
        return CapabilityVerdict(
            capability, False, "two coexisting paths reported the same (st_dev, st_ino)"
        )
    return CapabilityVerdict(capability, True, "coexisting paths report distinct (st_dev, st_ino)")


# ---------------------------------------------------------------------------
# Real-filesystem races (they pin WHEN production observes).
# ---------------------------------------------------------------------------


class UntrackedIdentityRealFilesystemTest(unittest.TestCase):
    """Real races against a real filesystem, made deterministic where that is possible.

    A synthetic drift proves the comparison rejects a drifted observation, but it is delivered
    to whichever call site exists — it cannot distinguish "re-observe after the read" from
    "re-observe before it". These races can: the mutation happens between production's own
    observations, so an observation taken at the wrong moment sees nothing and the test fails.

    The three swap races avoid the two filesystem behaviours that made the previous oracle
    non-hermetic (inode-number reuse and ``ctime`` granularity) by keeping BOTH objects alive
    across the swap: two paths that coexist must have different inode numbers, so the drift is
    guaranteed by POSIX rather than by the filesystem's timestamp resolution.
    """

    def _hash(self, root: Path, name: str) -> Optional[bytes]:
        return B._hash_untracked(root, name.encode())

    def _assert_quiescent_baseline(self, root: Path, name: str) -> None:
        """The un-raced path hashes, so a later ``None`` is attributable to the race alone."""
        self.assertIsNotNone(
            self._hash(root, name),
            f"{name} does not hash even without a race; the race assertion would pass for "
            "the wrong reason",
        )

    def test_regular_inode_swap_in_open_window_fails_closed(self) -> None:
        # Both regular files are live when production takes its ``lstat``, so ``B``'s inode
        # number cannot be ``A``'s: no reliance on what the filesystem does with freed inodes.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe_distinct_inode(root).require(self)
            (root / "A").write_text("content-A")
            (root / "B").write_text("content-B-different-inode")
            self.assertNotEqual(
                os.lstat(root / "A").st_ino,
                os.lstat(root / "B").st_ino,
                "precondition: the two coexisting files must occupy different inodes",
            )
            self._assert_quiescent_baseline(root, "A")

            real_lstat = os.lstat
            fired = {"n": 0}

            def racing_lstat(path, *args, **kwargs):
                observed = real_lstat(path, *args, **kwargs)
                if str(path).endswith("A") and fired["n"] == 0:
                    fired["n"] = 1
                    os.replace(root / "B", root / "A")  # -> a different, still-live inode
                return observed

            with mock.patch.object(B.os, "lstat", side_effect=racing_lstat):
                result = self._hash(root, "A")
            self.assertEqual(fired["n"], 1, "the swap never fired; the race did not happen")
            self.assertIsNone(result)

    def test_symlink_swap_in_readlink_window_fails_closed(self) -> None:
        # The replacement symlink is created BEFORE the race and swapped in with ``os.replace``,
        # so it holds a distinct, concurrently-allocated inode. The previous version removed
        # and recreated the link, which on Linux commonly reuses the just-freed inode number
        # and then depends on ``ctime`` granularity to notice anything (#14655).
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe_distinct_inode(root).require(self)
            os.symlink("target-one", root / "link")
            os.symlink("target-two", root / "successor")
            self.assertNotEqual(
                os.lstat(root / "link").st_ino,
                os.lstat(root / "successor").st_ino,
                "precondition: the two coexisting symlinks must occupy different inodes",
            )
            self._assert_quiescent_baseline(root, "link")

            real_readlink = os.readlink
            fired = {"n": 0}

            def racing_readlink(path, *args, **kwargs):
                target = real_readlink(path, *args, **kwargs)
                if str(path).endswith("link") and fired["n"] == 0:
                    fired["n"] = 1
                    os.replace(root / "successor", root / "link")
                return target

            with mock.patch.object(B.os, "readlink", side_effect=racing_readlink):
                result = self._hash(root, "link")
            self.assertEqual(fired["n"], 1, "the swap never fired; the race did not happen")
            self.assertIsNone(result)

    def test_append_during_read_fails_closed(self) -> None:
        # An append drifts ``st_size``, which POSIX guarantees on every filesystem — no
        # timestamp resolution involved. This is the case that pins the read-window
        # observation to AFTER the last read.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "r").write_bytes(b"x" * 200000)
            self._assert_quiescent_baseline(root, "r")

            real_read = os.read
            fired = {"n": 0}

            def racing_read(fd, size):
                chunk = real_read(fd, size)
                if chunk and fired["n"] == 0:
                    fired["n"] = 1
                    with open(root / "r", "ab") as handle:
                        handle.write(b"MORE")
                return chunk

            with mock.patch.object(B.os, "read", side_effect=racing_read):
                result = self._hash(root, "r")
            self.assertEqual(fired["n"], 1, "the append never fired; the race did not happen")
            self.assertIsNone(result)

    def test_same_size_mtime_restored_rewrite_fails_closed(self) -> None:
        # The ctime-only case: same inode, same size, ``mtime`` restored with ``utime``, so
        # ``st_ctime_ns`` is the sole discriminator. It is the one race no fixture can make
        # deterministic — a filesystem that does not resolve the rewrite in ``ctime`` simply
        # has nothing to observe — so it is gated on a measured capability. The field itself
        # stays covered by ``UntrackedIdentityFieldOracle.test_read_window_ctime_...``.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            probe_ctime_drift(root).require(self)
            payload = 200000
            target = root / "r"
            target.write_bytes(b"A" * payload)
            self._assert_quiescent_baseline(root, "r")
            baseline = os.stat(target)

            real_read = os.read
            fired = {"n": 0}

            def racing_read(fd, size):
                chunk = real_read(fd, size)
                if chunk and fired["n"] == 0:
                    fired["n"] = 1
                    with open(target, "r+b") as handle:
                        handle.seek(0)
                        handle.write(b"B" * payload)
                    os.utime(target, ns=(baseline.st_atime_ns, baseline.st_mtime_ns))
                return chunk

            with mock.patch.object(B.os, "read", side_effect=racing_read):
                result = self._hash(root, "r")
            self.assertEqual(fired["n"], 1, "the rewrite never fired; the race did not happen")
            settled = os.stat(target)
            self.assertEqual(
                _drifted_fields(baseline, settled),
                ("ctime_ns",),
                "the race must leave ctime as the ONLY discriminator, else it proves nothing "
                "about the ctime check",
            )
            self.assertIsNone(result)

    def test_stable_regular_file_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "r").write_text("stable")
            self.assertIsNotNone(self._hash(Path(tmp), "r"))

    def test_stable_symlink_hashes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            os.symlink("some-target", Path(tmp) / "link")
            self.assertIsNotNone(self._hash(Path(tmp), "link"))


# ---------------------------------------------------------------------------
# Coverage differential: the declared table vs the production source.
# ---------------------------------------------------------------------------


class UntrackedIdentityCoverageTest(unittest.TestCase):
    """``_OBSERVED_FIELDS`` must equal what ``_hash_untracked`` actually reads.

    Hand-maintained coverage lists rot silently: a field added to one of the comparisons would
    simply go untested. Re-deriving the list from the production source instead turns that
    into a failure here, naming the field.
    """

    def test_declared_fields_match_the_production_source(self) -> None:
        tree = ast.parse(inspect.getsource(B._hash_untracked))
        observed: dict[str, set[str]] = {name: set() for name in _OBSERVED_FIELDS}
        extra: set[str] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Attribute) or not isinstance(node.value, ast.Name):
                continue
            if not node.attr.startswith("st_"):
                continue
            if node.value.id in observed:
                observed[node.value.id].add(node.attr)
            else:
                extra.add(f"{node.value.id}.{node.attr}")

        self.assertEqual(
            set(),
            extra,
            "_hash_untracked reads stat fields off an observation this module does not model; "
            "add it to _OBSERVED_FIELDS and give each field a load-bearing test",
        )
        self.assertEqual(
            {name: set(fields) for name, fields in _OBSERVED_FIELDS.items()},
            observed,
            "_OBSERVED_FIELDS has drifted from _hash_untracked; every field production reads "
            "off a re-observation needs a UntrackedIdentityFieldOracle test",
        )

    #: Fields read off an observation that do NOT need their own drift test here, each with
    #: the reason it is already covered. Derived-minus-excluded is what makes this oracle
    #: closed: a newly read field belongs to neither set and fails the test until classified.
    _COVERED_ELSEWHERE = {
        ("info", "st_mode"): (
            "the kind gates (symlink / regular / FIFO / directory), covered by the kind tests "
            "in test_sublane_hibernate.py"
        ),
        ("info", "st_ctime_ns"): (
            "the symlink window's baseline; drifting the `after` side is the same comparison"
        ),
        ("opened", "st_dev"): "the read window's baseline; exercised from the `settled` side",
        ("opened", "st_ino"): "the read window's baseline; exercised from the `settled` side",
        ("opened", "st_size"): "the read window's baseline; exercised from the `settled` side",
        ("opened", "st_mtime_ns"): "the read window's baseline; exercised from the `settled` side",
        ("opened", "st_ctime_ns"): "the read window's baseline; exercised from the `settled` side",
    }

    def test_every_compared_field_has_a_load_bearing_test(self) -> None:
        required = {
            (observation, field)
            for observation, fields in _OBSERVED_FIELDS.items()
            for field in fields
            if (observation, field) not in self._COVERED_ELSEWHERE
        }
        self.assertEqual(
            set(),
            set(self._COVERED_ELSEWHERE) - {
                (observation, field)
                for observation, fields in _OBSERVED_FIELDS.items()
                for field in fields
            },
            "_COVERED_ELSEWHERE excuses a field production no longer reads; drop the entry",
        )
        covered = {
            ("after", "st_dev"): "test_symlink_window_dev_is_load_bearing",
            ("after", "st_ino"): "test_symlink_window_ino_is_load_bearing",
            ("after", "st_ctime_ns"): "test_symlink_window_ctime_is_load_bearing",
            ("info", "st_dev"): "test_open_window_dev_is_load_bearing",
            ("info", "st_ino"): "test_open_window_ino_is_load_bearing",
            ("opened", "st_mode"): "test_open_window_kind_is_load_bearing",
            ("settled", "st_dev"): "test_read_window_dev_is_load_bearing",
            ("settled", "st_ino"): "test_read_window_ino_is_load_bearing",
            ("settled", "st_size"): "test_read_window_size_is_load_bearing",
            ("settled", "st_mtime_ns"): "test_read_window_mtime_is_load_bearing",
            ("settled", "st_ctime_ns"): "test_read_window_ctime_is_load_bearing",
        }
        self.assertEqual(required, set(covered), "a compared field has no load-bearing test")
        for (observation, field), test_name in covered.items():
            with self.subTest(observation=observation, field=field):
                self.assertTrue(
                    hasattr(UntrackedIdentityFieldOracle, test_name),
                    f"{test_name} is named as the cover for {observation}.{field} but does "
                    "not exist",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
