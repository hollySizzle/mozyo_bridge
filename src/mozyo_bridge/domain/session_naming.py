"""Derive a collision-safe ASCII tmux session name from a repo (Redmine #10796).

Background: a workspace whose basename is non-ASCII (e.g. ``2026PBL_ローカル``)
loses its identity when a launcher sanitizes the basename down to a
low-information name like ``2026PBL_____``. Two distinct workspaces can then
collide on the same ``____``-style name, and the repo-identity that the
``--target-repo`` handoff gate relies on becomes unrecoverable.

This module makes the tmux session identity derivable and stable:

- the **preferred** source is the workspace-local Redmine project identifier in
  ``<repo>/.mozyo-bridge/workspace-defaults.yaml``
  (``redmine.default_project.identifier``), which is already the single source
  of truth for the repo's Redmine project. The derived name is
  ``mozyo-<slug(identifier)>`` — ASCII-safe and stable across launchers;
- the **fallback** (no workspace-defaults / unreadable / identifier absent) is
  ``mozyo-<slug(basename)>-<hash>`` where ``hash`` is a short digest of the
  absolute repo path. The hash makes the name collision-safe: two repos that
  share a basename, or whose non-ASCII basenames slug to the same value, still
  get distinct session names. A non-ASCII basename is never collapsed to a
  bare ``____``-style name — when the slug is empty the name is ``mozyo-<hash>``.

The reader is intentionally **best-effort and non-fatal**: session naming must
always degrade to the fallback, never ``die``. It reads only the project
identifier (a non-secret project slug); the full schema/secret gate stays the
responsibility of ``mozyo-bridge workspace-defaults``. Session naming does not
gate on the verification flag — the identifier is a display/grouping identity,
not an issue-creation decision, so an unverified-but-present identifier is still
a valid stable grouping key.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from mozyo_bridge.shared.paths import normalize_path_unicode
from mozyo_bridge.workspace_defaults import WORKSPACE_DEFAULTS_INPUT_RELATIVE

# All derived names share this prefix so mozyo-bridge sessions are a clear,
# self-namespaced family and never collide with an arbitrary unrelated session.
SESSION_NAME_PREFIX = "mozyo"

# Length of the repo-path digest suffixed onto fallback names. 8 hex chars
# (32 bits) is ample to disambiguate the handful of repos one operator runs
# while keeping the session name short enough to read in a tmux status bar.
REPO_HASH_LENGTH = 8

# Source markers recorded on the result so callers / audits can tell which
# branch produced the name without re-deriving it.
SOURCE_WORKSPACE_DEFAULTS = "workspace-defaults-redmine-identifier"
SOURCE_REPO_FALLBACK = "repo-path-fallback"

# Workspace-local VS Code settings that pin the `tmux-integrated` session name.
# Only the workspace-local file is ever touched; user-global settings (which can
# carry credentials) are out of scope by design.
VSCODE_SETTINGS_RELATIVE = Path(".vscode/settings.json")
VSCODE_SESSION_NAME_KEY = "tmux-integrated.sessionName"

# tmux treats ``:`` as a window separator and ``.`` as a pane separator, so a
# session name must avoid them. We go further and keep only ``[a-z0-9]``,
# collapsing every other run (including non-ASCII bytes and underscores) into a
# single ``-``. This is what stops ``2026PBL_ローカル`` from becoming ``____``.
_SLUG_DISALLOWED_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class SessionName:
    """A derived tmux session name plus how it was derived.

    ``identifier`` carries the raw Redmine project identifier when the name
    came from workspace-defaults; it is ``None`` for the fallback branch.
    """

    name: str
    source: str
    repo_root: Path
    identifier: str | None


def slugify(text: str) -> str:
    """Lower-case ``text`` and reduce it to a ``[a-z0-9-]`` slug.

    Runs of disallowed characters (including non-ASCII and ``_``) collapse to a
    single ``-``; leading/trailing ``-`` are trimmed. Returns ``""`` when the
    input has no ASCII alphanumerics (e.g. an all-Japanese basename).
    """
    return _SLUG_DISALLOWED_RE.sub("-", text.strip().lower()).strip("-")


def _repo_path_hash(repo_root: Path) -> str:
    """Short, stable digest of the absolute repo path for collision safety.

    The path string is fixed to one Unicode normal form before hashing
    (Redmine #11625): the same directory arrives NFD from macOS readdir /
    shell completion but NFC from documents, Redmine, or agent-supplied
    strings, and hashing the raw bytes derived two different session names
    for one workspace. NFD (the macOS filesystem form) is the fixed form so
    names historically derived from real filesystem paths keep their value.
    """
    normalized = normalize_path_unicode(str(repo_root))
    digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
    return digest[:REPO_HASH_LENGTH]


def read_redmine_identifier(repo_root: Path) -> str | None:
    """Best-effort read of ``redmine.default_project.identifier``.

    Returns the stripped identifier when present and non-empty; ``None`` when
    the workspace-defaults file is missing, unreadable, not a mapping, or the
    identifier is absent / not a string. Never raises: session naming must fall
    back rather than fail.
    """
    source = repo_root / WORKSPACE_DEFAULTS_INPUT_RELATIVE
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        return None
    if not isinstance(raw, dict):
        return None
    redmine = raw.get("redmine")
    if not isinstance(redmine, dict):
        return None
    project = redmine.get("default_project")
    if not isinstance(project, dict):
        return None
    identifier = project.get("identifier")
    if not isinstance(identifier, str):
        return None
    return identifier.strip() or None


def derive_session_name(repo_root: Path | str) -> SessionName:
    """Derive a collision-safe ASCII tmux session name for ``repo_root``.

    Prefers the workspace-defaults Redmine identifier; otherwise returns a
    hash-suffixed fallback derived from the repo path. The result is always a
    non-empty ASCII name beginning with ``mozyo-``.
    """
    resolved = Path(repo_root).expanduser().resolve()

    identifier = read_redmine_identifier(resolved)
    if identifier:
        slug = slugify(identifier)
        if slug:
            return SessionName(
                name=f"{SESSION_NAME_PREFIX}-{slug}",
                source=SOURCE_WORKSPACE_DEFAULTS,
                repo_root=resolved,
                identifier=identifier,
            )

    # Fallback. Always suffix the path hash so distinct repos that share a
    # basename (or whose non-ASCII basenames slug to the same value) stay
    # distinct, and so an all-non-ASCII basename never collapses to ``____``.
    basename_slug = slugify(resolved.name)
    repo_hash = _repo_path_hash(resolved)
    if basename_slug:
        name = f"{SESSION_NAME_PREFIX}-{basename_slug}-{repo_hash}"
    else:
        name = f"{SESSION_NAME_PREFIX}-{repo_hash}"
    return SessionName(
        name=name,
        source=SOURCE_REPO_FALLBACK,
        repo_root=resolved,
        identifier=None,
    )


def merge_vscode_session_name(existing_text: str | None, session_name: str) -> str:
    """Return ``.vscode/settings.json`` text with the session-name key set.

    Preserves every other key in an existing plain-JSON file and only
    updates ``tmux-integrated.sessionName``. Raises ``ValueError`` when the
    existing content is non-empty but not valid JSON (e.g. JSONC with comments
    or trailing commas) so the caller can refuse to clobber it rather than
    silently dropping the operator's content. Output is 2-space-indented JSON
    with a trailing newline; insertion order of existing keys is preserved.
    """
    if existing_text is None or not existing_text.strip():
        data: dict = {}
    else:
        try:
            data = json.loads(existing_text)
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(data, dict):
            raise ValueError("settings.json root is not a JSON object")
    data[VSCODE_SESSION_NAME_KEY] = session_name
    return json.dumps(data, indent=2, ensure_ascii=False) + "\n"
