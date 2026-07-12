#!/usr/bin/env python3
"""Compute a unique PEP 440 developmental version for TestPyPI dev publishing.

Reads the release version from the repo's canonical release-version mirror set
(``pyproject.toml`` ``[project].version`` and ``src/mozyo_bridge/__init__.py``
``__version__``) and appends a ``.dev<N>`` developmental-release segment so
that repeated ``main`` commits do not collide on TestPyPI (which rejects
re-uploads of an existing version).

This helper is used ONLY by the automated TestPyPI dev-publish job in
``.github/workflows/testpypi.yml``. With ``--write`` it rewrites the version
field in EVERY mirror-set file of the ephemeral CI checkout, so the wheel
METADATA and the runtime ``__version__`` (and therefore ``mozyo-bridge
--version`` / ``mozyo --version``) all carry the SAME exact dev version. That
rewrite is never committed, so the committed mirror keeps the real release
version and production builds (``publish.yml``, ``release: published``) are
unaffected. Rewriting only ``pyproject.toml`` used to leave ``__version__`` on
the committed base version, so the wheel METADATA and the installed CLI version
disagreed (Redmine #13586); mirroring the whole set fixes that.

The mirror set is NOT hardcoded here: it is read from the same contract doc
(``vibes/docs/logics/release-helper-contract.md``) and rewritten through the
same stdlib-only primitives (the ``version_mirror`` module in the release
version-governance Feature package) that the installed ``mozyo-bridge release
bump`` helper uses, so both stay in lockstep and this script does not duplicate
the "which files / how to rewrite" logic.

Local versions (``+local``) are deliberately NOT used: PyPI / TestPyPI reject
uploads carrying a local version identifier, so the commit SHA cannot live in
the version string. The workflow records the version <-> SHA mapping in its run
summary instead (see ``skills/mozyo-bridge-agent/references/release.md``).

The script is intentionally dependency-free (standard library only) so it runs
in a fresh CI environment before the package is installed. It imports the
shared mirror primitive straight from the checked-out ``src/`` tree; that
module is stdlib-only for exactly this reason, so no ``pip install`` is needed
before this runs.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Import the canonical, stdlib-only mirror primitives directly from the
# checked-out source tree (the package is not installed yet at this point in
# CI). ``version_mirror`` and its only intra-package deps are stdlib-only, so
# importing it here does not require the package's third-party dependencies.
_REPO_ROOT = Path(__file__).resolve().parents[1]
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import (  # noqa: E402
    version_mirror,
)

# PEP 440 release segment, optional pre/post, REQUIRED trailing ``.devN``.
# The base must not already carry a ``.dev`` segment (we refuse double-dev).
_DEV_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?\.dev[0-9]+$"
)

_DEV_NUMBER_PATTERN = re.compile(r"^[0-9]+$")


class DevVersionError(ValueError):
    """Raised when a base version or dev number is unusable."""


def build_dev_version(base_version: str, dev_number: str) -> str:
    """Return ``<base>.dev<dev_number>`` after validating both inputs."""
    if not _DEV_NUMBER_PATTERN.match(dev_number):
        raise DevVersionError(f"dev number must be all digits, got: {dev_number!r}")
    if ".dev" in base_version:
        raise DevVersionError(
            f"base version already carries a .dev segment: {base_version!r}; "
            "refuse to append a second one"
        )
    candidate = f"{base_version}.dev{dev_number}"
    if not _DEV_VERSION_PATTERN.match(candidate):
        raise DevVersionError(f"computed version is not a PEP 440 dev release: {candidate!r}")
    return candidate


def read_mirror_base_version(
    mirror: list[tuple[Path, dict[str, object]]],
) -> tuple[str, list[tuple[Path, dict[str, object], str]]]:
    """Read + validate the release version from every mirror-set file.

    Returns ``(base_version, entries)`` where ``entries`` is one
    ``(path, handler, current_text)`` tuple per mirror file (so a later write
    phase does not re-read the files). Raises ``DevVersionError`` if any
    mirror file is missing its version literal, or if the mirror files
    disagree on the base version — in either case the caller must NOT write,
    so the checkout is never left partially rewritten.
    """
    entries: list[tuple[Path, dict[str, object], str]] = []
    values: dict[Path, str] = {}
    for path, handler in mirror:
        text = path.read_text(encoding="utf-8")
        try:
            current = version_mirror.extract_version(text, handler)
        except version_mirror.MirrorError as exc:
            raise DevVersionError(f"{exc} (file: {path})") from exc
        entries.append((path, handler, text))
        values[path] = current
    distinct = set(values.values())
    if len(distinct) != 1:
        detail = ", ".join(
            f"{path.name}={value!r}" for path, value in values.items()
        )
        raise DevVersionError(
            "release-version mirror set disagrees before dev bump "
            f"({detail}); refuse to derive a dev version from a broken mirror"
        )
    return distinct.pop(), entries


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute a unique PEP 440 .devN version for TestPyPI dev publishing."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help=(
            "Path to pyproject.toml (default: ./pyproject.toml). Its parent "
            "directory is used as the repo root from which the canonical "
            "mirror set is resolved."
        ),
    )
    parser.add_argument(
        "--dev-number",
        default=os.environ.get("MOZYO_BRIDGE_DEV_NUMBER"),
        help=(
            "Unique monotonic dev-segment digits (e.g. a UTC timestamp or CI run "
            "number). Falls back to the MOZYO_BRIDGE_DEV_NUMBER env var."
        ),
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help=(
            "Rewrite the version field in every mirror-set file in place "
            "(CI checkout only)."
        ),
    )
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if not args.dev_number:
        print(
            "error: --dev-number (or MOZYO_BRIDGE_DEV_NUMBER) is required",
            file=sys.stderr,
        )
        return 2
    repo_root = args.pyproject.resolve().parent
    try:
        mirror = version_mirror.load_mirror_set(repo_root)
        base, entries = read_mirror_base_version(mirror)
        new_version = build_dev_version(base, args.dev_number)
        if args.write:
            # Two-phase: compute every rewrite first (which raises before any
            # write if a literal cannot be rewritten), then write them all.
            # Validation failures raise here, before any file is touched.
            rewrites: list[tuple[Path, str, str]] = []
            for path, handler, text in entries:
                rewrites.append(
                    (path, text, version_mirror.replace_version(text, handler, new_version))
                )
            # Write phase with rollback: a write-time I/O failure on a later
            # mirror file (e.g. a read-only or full disk) must not leave the
            # set half-rewritten. Restore every already-written file to its
            # original bytes before propagating the error, so the postcondition
            # "a failure never leaves the mirror set partially updated" holds
            # for I/O failures too, not just validation failures.
            written: list[tuple[Path, str]] = []  # (path, original_text)
            try:
                for path, original_text, new_text in rewrites:
                    path.write_text(new_text, encoding="utf-8")
                    written.append((path, original_text))
            except OSError:
                for done_path, original_text in reversed(written):
                    try:
                        done_path.write_text(original_text, encoding="utf-8")
                    except OSError:
                        # Best-effort rollback; the original error is the one
                        # that matters and is re-raised below.
                        pass
                raise
    except (DevVersionError, version_mirror.MirrorError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # The computed version is the only thing on stdout so the workflow can
    # capture it with command substitution.
    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
