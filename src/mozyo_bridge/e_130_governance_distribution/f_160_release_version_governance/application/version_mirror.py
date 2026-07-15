"""Canonical release-version mirror primitives (standard-library only).

Single source of truth for *which* files carry the release version and *how*
each file's version literal is shaped. Both the installed release helper
(``mozyo-bridge release bump`` in
``e_130_governance_distribution/f_160_release_version_governance/application/release.py``)
and the dependency-free TestPyPI dev-version script
(``scripts/compute_testpypi_dev_version.py``) build on these primitives so the
mirror set is defined in exactly one place and read from the contract doc at
runtime — never hardcoded as two divergent shapes.

The mirror set itself (the list of files) is declared in
``vibes/docs/logics/release-helper-contract.md`` and read from there; this
module only knows how to parse that declaration and how to read/rewrite the
version literal for each recognised file extension.

This module imports only the standard library. That is deliberate: the
pre-install TestPyPI dev script must import it directly from the checked-out
source tree in a fresh CI environment, before ``mozyo_bridge`` (or any of its
third-party dependencies) is installed. Do not add non-stdlib imports here.

It lives in this bounded context (not ``mozyo_bridge.shared``) because the
shared kernel module set is frozen (Redmine #12640,
``vibes/docs/logics/shared-kernel-freeze.md``); release-version-governance
concerns belong to this Feature package. The intra-package ``__init__`` chain
up to here is import-time inert (docstring-only), so importing this module does
not pull in the package's third-party dependencies.
"""

from __future__ import annotations

import re
from pathlib import Path

# Contract doc that declares the mirror set, relative to the repo root.
CONTRACT_DOC_RELATIVE = Path("vibes/docs/logics/release-helper-contract.md")

# Anchor phrase that immediately precedes the mirror-set bullet list in the
# contract doc. The list of files is read from the doc, never hardcoded.
MIRROR_SET_ANCHOR = "release-version mirror set は以下の"

# Per-file-extension version-field handlers. The set of file *extensions*
# handled here is this module's interpretation surface; the set of *files* is
# read from the contract doc at runtime. A mirror file whose extension is not
# represented here is a strict failure (see ``load_mirror_set``) rather than a
# silent skip, so neither consumer can miss a contract-mandated target.
MIRROR_KIND_HANDLERS: dict[str, dict[str, object]] = {
    ".toml": {
        "pattern": re.compile(r'^version\s*=\s*"([^"]+)"', re.MULTILINE),
        "format": 'version = "{value}"',
        "label": "[project].version",
    },
    ".py": {
        "pattern": re.compile(r'^__version__\s*=\s*"([^"]+)"', re.MULTILINE),
        "format": '__version__ = "{value}"',
        "label": "__version__",
    },
}

# Loose PEP 440 / SemVer hybrid recognizer. Tight enough to reject shell meta
# or paths, loose enough to admit the documented shapes (``0.1.0a1``,
# ``0.1.0``, ``0.1.0rc1``, ``0.1.1``, optional ``.postN`` / ``.devN``). It does
# not pick between alpha / beta / GA; it only validates the literal shape.
VERSION_RE = re.compile(
    r"^[0-9]+(?:\.[0-9]+)*(?:(?:a|b|rc)[0-9]+)?(?:\.post[0-9]+)?(?:\.dev[0-9]+)?$"
)


class MirrorError(ValueError):
    """Raised when the mirror set cannot be resolved, or a mirror file's
    version literal is missing or unrewritable.

    Callers translate this into their own failure surface: the release helper
    reports it via ``die`` (SystemExit); the dependency-free dev-version
    script reports it as a non-zero return so the ephemeral CI checkout is
    never left partially rewritten.
    """


def parse_mirror_set_paths(contract_text: str) -> list[str]:
    """Extract the mirror-set bullet list from the contract doc text.

    The contract names the mirror set as a bullet list immediately following
    the anchor phrase ``MIRROR_SET_ANCHOR``. Each bullet's first
    backtick-quoted token is the file path. The list ends at the first blank
    line after the bullets begin.
    """
    start = contract_text.find(MIRROR_SET_ANCHOR)
    if start < 0:
        raise MirrorError(
            "release-helper-contract.md does not contain the mirror-set anchor "
            f"{MIRROR_SET_ANCHOR!r}; update the contract before operating on "
            "the mirror set"
        )
    paths: list[str] = []
    bullet_started = False
    for line in contract_text[start:].splitlines():
        stripped = line.lstrip()
        if stripped.startswith("- `"):
            bullet_started = True
            after = stripped[3:]
            close = after.find("`")
            if close < 0:
                continue
            paths.append(after[:close])
        elif bullet_started and not stripped:
            break
    if not paths:
        raise MirrorError(
            "release-helper-contract.md mirror-set section has no bullet "
            "entries; cannot determine which files to operate on"
        )
    return paths


def load_mirror_set(repo_root: Path) -> list[tuple[Path, dict[str, object]]]:
    """Return the contract-declared mirror set as ``(absolute_path, handler)``.

    The set is read from ``release-helper-contract.md`` so it stays in lockstep
    with the contract. Files whose extension is not represented in
    ``MIRROR_KIND_HANDLERS`` are strict failures rather than silently skipped —
    a caller would otherwise miss a contract-mandated target.

    Raises ``MirrorError`` on any missing doc / missing file / unhandled
    extension.
    """
    contract_path = repo_root / CONTRACT_DOC_RELATIVE
    if not contract_path.exists():
        raise MirrorError(
            f"contract doc not found at {contract_path}; cannot determine the "
            "release-version mirror set"
        )
    try:
        contract_text = contract_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise MirrorError(f"failed to read contract doc {contract_path}: {exc}")
    resolved: list[tuple[Path, dict[str, object]]] = []
    for raw in parse_mirror_set_paths(contract_text):
        path = repo_root / raw
        if not path.exists():
            raise MirrorError(
                f"contract names mirror set file {raw!r} but it does not exist "
                f"at {path}; update the contract or restore the file"
            )
        ext = path.suffix.lower()
        handler = MIRROR_KIND_HANDLERS.get(ext)
        if handler is None:
            raise MirrorError(
                f"contract names mirror set file {raw!r} but no version-field "
                f"handler exists for extension {ext!r}; update "
                "MIRROR_KIND_HANDLERS together with the contract"
            )
        resolved.append((path, handler))
    return resolved


def extract_version(text: str, handler: dict[str, object]) -> str:
    """Return the version literal found in ``text`` for ``handler``.

    Raises ``MirrorError`` when the handler's pattern does not match (the
    mirror file may have drifted from the contract-declared shape).
    """
    pattern = handler["pattern"]
    assert isinstance(pattern, re.Pattern)
    match = pattern.search(text)
    if match is None:
        raise MirrorError(
            "could not locate version literal using regex "
            f"{pattern.pattern!r}; mirror set may have drifted from the contract"
        )
    return match.group(1)


def replace_version(text: str, handler: dict[str, object], new_version: str) -> str:
    """Return ``text`` with its first version literal replaced by
    ``new_version``.

    Raises ``MirrorError`` when the handler's pattern does not match, so a
    caller aborts before leaving a partially-rewritten mirror set.
    """
    pattern = handler["pattern"]
    fmt = handler["format"]
    assert isinstance(pattern, re.Pattern) and isinstance(fmt, str)
    rewritten, count = pattern.subn(fmt.format(value=new_version), text, count=1)
    if count == 0:
        raise MirrorError(
            f"could not rewrite version literal; pattern {pattern.pattern!r} "
            "did not match. Aborting before partially-mutated mirror set."
        )
    return rewritten


def is_valid_version(value: str) -> bool:
    """Return True if ``value`` matches the accepted PEP 440 version shape."""
    return bool(VERSION_RE.match(value))
