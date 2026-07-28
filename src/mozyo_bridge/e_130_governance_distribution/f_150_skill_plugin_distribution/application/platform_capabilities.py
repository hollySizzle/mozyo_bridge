"""Host capability preflight for the legacy mirror sync (Redmine #14651).

:mod:`.legacy_mirror_sync` is built entirely on descriptor-relative primitives,
and where a host cannot provide them it fails closed with
:data:`~..domain.legacy_mirror_contract.PLATFORM_UNSUPPORTED` rather than
degrading to path-based I/O. This module answers the one question that decision
rests on: *does this host accept the calls the service is about to make?*

Membership in ``os.supports_dir_fd`` is not that fact
-----------------------------------------------------
The previous check asked whether each primitive appeared in
``os.supports_dir_fd``. That set is a hand-maintained advertisement in
``os.py``, and it disagrees with the interpreter that ships it: CPython 3.12 on
Linux omits ``os.lstat`` although ``os.lstat(name, dir_fd=...)`` works there
(3.13 added the missing entry), and no CPython version lists ``os.replace``
even though it accepts ``src_dir_fd`` / ``dst_dir_fd``. On the Linux CI runner
that turned a fully capable host into ``missing: lstat(dir_fd=)`` and collapsed
every legacy mirror path into ``platform_unsupported`` — 91 failures from a host
that supports everything the service needs (GitHub Actions run 30383304588).
``os.replace`` had already been worked around by probing ``os.rename`` in its
place, which is the same mistake spelled differently: the advertisement was
being patched rather than distrusted.

So the probe calls the primitive instead of reading the advertisement, in the
exact keyword form the service uses. CPython decides whether ``dir_fd`` is
available while converting the arguments, before the syscall runs, and raises
``NotImplementedError`` when it is not — documented on ``os.stat`` ("dir_fd and
follow_symlinks may not be implemented on your platform. If they are
unavailable, using them will raise a NotImplementedError") and the same
exception that walked straight past the fail-closed path in j#90450 R7-F4. An
``OSError`` says the opposite: the call form was accepted and the host answered.
Anything else is unknown, and unknown counts as missing.

The errno is deliberately not read. The same fact — "that descriptor is not a
directory" — comes back as ``ENOTDIR`` on Linux and ``ENOTSUP`` on macOS, so
classifying by errno would reintroduce a per-platform table of exactly the kind
this module exists to remove.

The probe touches nothing
-------------------------
Every probe passes a **pipe descriptor** where the service passes a directory
descriptor. POSIX requires the ``*at()`` calls to reject a non-directory
descriptor before they resolve the relative name, so no probe can create,
rename or remove an entry: the absence of side effects is structural, not a
matter of having chosen harmless arguments. A pipe also keeps the probe off the
filesystem entirely — no temp directory, no cwd, no ``os.open`` — so a host
whose temp directory is unwritable is not mistaken for a host without
``openat``, and a test that replaces ``os.open`` to emulate a missing primitive
does not break the probe's own setup along with it.

The primitives are resolved through ``os`` at call time, not captured at import.
The manifest used to hold the function objects, so replacing ``os.lstat`` left
the check reading the object it had captured before the replacement.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from contextlib import contextmanager

from .owned_descriptors import _close_quietly

#: Names the probes pass where the service passes a real entry name. Neither is
#: ever resolved: the anchor is not a directory, so the host rejects each call
#: before it looks at the name.
_PROBE_NAME = ".mozyo-legacy-mirror-capability-probe"
_PROBE_RENAME_TARGET = ".mozyo-legacy-mirror-capability-probe.renamed"

#: Flags the no-follow walk is built out of. These are plain attributes, so
#: presence is the whole question and there is nothing to call.
_REQUIRED_FLAGS: tuple[str, ...] = ("O_NOFOLLOW", "O_DIRECTORY", "O_NONBLOCK")

#: Reported when the probe could not be set up at all. It does not claim a
#: primitive is missing; it says the host could not be measured, which fails
#: closed for the same reason a missing primitive does.
PROBE_UNAVAILABLE = "capability probe (no descriptor to probe with)"


def _probe_open(anchor: int) -> None:
    fd = os.open(_PROBE_NAME, os.O_RDONLY, dir_fd=anchor)
    # Unreachable against a pipe anchor. It is here because a stub that answers
    # the probe with a descriptor would otherwise leak one on every audit.
    _close_quietly(fd)


def _probe_lstat(anchor: int) -> None:
    os.lstat(_PROBE_NAME, dir_fd=anchor)


def _probe_unlink(anchor: int) -> None:
    os.unlink(_PROBE_NAME, dir_fd=anchor)


def _probe_mkdir(anchor: int) -> None:
    os.mkdir(_PROBE_NAME, 0o755, dir_fd=anchor)


def _probe_replace(anchor: int) -> None:
    os.replace(_PROBE_NAME, _PROBE_RENAME_TARGET, src_dir_fd=anchor, dst_dir_fd=anchor)


def _probe_scandir(anchor: int) -> None:
    entries = os.scandir(anchor)
    try:
        # `os.scandir` can defer the `fdopendir` to the first step, so a probe
        # that merely constructed the iterator would pass on a host that cannot
        # open a directory by descriptor at all.
        next(iter(entries), None)
    finally:
        entries.close()


#: Every platform-dependent primitive the sync actually calls, paired with the
#: call form it calls it in. Review j#90450 R7-F4: the manifest listed
#: ``os.stat``, which nothing here calls, and omitted ``os.lstat(dir_fd=)``,
#: which every type decision goes through — so a host without it passed the
#: preflight and then raised ``NotImplementedError`` past the fail-closed path.
#: The manifest is the call surface, not a plausible-looking sample of it, and
#: a test fences it against the modules that do the calling.
_REQUIRED_DIR_FD_CALLS: tuple[tuple[str, str, Callable[[int], None]], ...] = (
    ("open", "open(dir_fd=)", _probe_open),
    ("lstat", "lstat(dir_fd=)", _probe_lstat),
    ("unlink", "unlink(dir_fd=)", _probe_unlink),
    ("mkdir", "mkdir(dir_fd=)", _probe_mkdir),
    ("replace", "replace(src_dir_fd=, dst_dir_fd=)", _probe_replace),
    ("scandir", "scandir(fd)", _probe_scandir),
)


@contextmanager
def _probe_anchor() -> Iterator[int | None]:
    """Yield a descriptor that is valid but is not a directory.

    Both pipe ends are closed on the way out: the write end is never used, but
    leaving it open would leak a descriptor per audit.
    """
    try:
        read_fd, write_fd = os.pipe()
    except OSError:
        yield None
        return
    try:
        yield read_fd
    finally:
        _close_quietly(read_fd)
        _close_quietly(write_fd)


def _accepts(probe: Callable[[int], None], anchor: int) -> bool:
    """Did this host accept the call form, whatever it then answered?

    ``OSError`` means accepted — the arguments converted and the host replied.
    ``NotImplementedError`` is how CPython reports an unavailable ``dir_fd``,
    and a stub without the keyword raises ``TypeError``; both mean the service
    must not run. Every other exception is unknown, and unknown fails closed.
    ``BaseException`` is deliberately not caught, so an interrupt is not read as
    a missing capability.
    """
    try:
        probe(anchor)
    except OSError:
        return True
    except Exception:
        return False
    return True


def missing_platform_capabilities() -> tuple[str, ...]:
    """Primitives this service refuses to run without."""
    missing = [flag for flag in _REQUIRED_FLAGS if not hasattr(os, flag)]
    with _probe_anchor() as anchor:
        if anchor is None:
            missing.append(PROBE_UNAVAILABLE)
            return tuple(missing)
        missing.extend(
            label
            for _name, label, probe in _REQUIRED_DIR_FD_CALLS
            if not _accepts(probe, anchor)
        )
    return tuple(missing)
