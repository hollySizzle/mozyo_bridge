#!/usr/bin/env python3
"""Compute a unique PEP 440 developmental version for TestPyPI dev publishing.

Reads the ``[project].version`` field from ``pyproject.toml`` and appends a
``.dev<N>`` developmental-release segment so that repeated ``main`` commits do
not collide on TestPyPI (which rejects re-uploads of an existing version).

This helper is used ONLY by the automated TestPyPI dev-publish job in
``.github/workflows/testpypi.yml``. With ``--write`` it rewrites the version
field in the ephemeral CI checkout; that rewrite is never committed, so the
committed pyproject keeps the real release version and production builds
(``publish.yml``, ``release: published``) are unaffected.

Local versions (``+local``) are deliberately NOT used: PyPI / TestPyPI reject
uploads carrying a local version identifier, so the commit SHA cannot live in
the version string. The workflow records the version <-> SHA mapping in its run
summary instead (see ``skills/mozyo-bridge-agent/references/release.md``).

The script is intentionally dependency-free (stdlib only) so it runs in a fresh
CI environment before the package is installed.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

# Same version-field shape the release helper pins
# (release-version mirror set, pyproject ``[project].version``).
_VERSION_PATTERN = re.compile(r'^(?P<prefix>version\s*=\s*")(?P<value>[^"]+)(?P<suffix>")', re.MULTILINE)

# PEP 440 release segment, optional pre/post, REQUIRED trailing ``.devN``.
# The base must not already carry a ``.dev`` segment (we refuse double-dev).
_DEV_VERSION_PATTERN = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?\.dev[0-9]+$"
)

_DEV_NUMBER_PATTERN = re.compile(r"^[0-9]+$")


class DevVersionError(ValueError):
    """Raised when a base version or dev number is unusable."""


def read_base_version(pyproject_text: str) -> str:
    """Return the ``[project].version`` value from pyproject text."""
    match = _VERSION_PATTERN.search(pyproject_text)
    if match is None:
        raise DevVersionError("could not find a `version = \"...\"` field in pyproject.toml")
    return match.group("value")


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


def rewrite_version(pyproject_text: str, new_version: str) -> str:
    """Return pyproject text with the version field replaced by ``new_version``."""
    if _VERSION_PATTERN.search(pyproject_text) is None:
        raise DevVersionError("could not find a `version = \"...\"` field to rewrite")
    return _VERSION_PATTERN.sub(
        lambda m: f"{m.group('prefix')}{new_version}{m.group('suffix')}",
        pyproject_text,
        count=1,
    )


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute a unique PEP 440 .devN version for TestPyPI dev publishing."
    )
    parser.add_argument(
        "--pyproject",
        type=Path,
        default=Path("pyproject.toml"),
        help="Path to pyproject.toml (default: ./pyproject.toml).",
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
        help="Rewrite the version field in pyproject.toml in place (CI checkout only).",
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
    try:
        text = args.pyproject.read_text(encoding="utf-8")
        base = read_base_version(text)
        new_version = build_dev_version(base, args.dev_number)
        if args.write:
            args.pyproject.write_text(rewrite_version(text, new_version), encoding="utf-8")
    except (DevVersionError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # The computed version is the only thing on stdout so the workflow can
    # capture it with command substitution.
    print(new_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
