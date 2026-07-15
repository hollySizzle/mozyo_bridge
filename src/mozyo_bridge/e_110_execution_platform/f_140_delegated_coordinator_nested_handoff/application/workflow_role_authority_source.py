"""Load the repo-local durable workflow-role binding declaration (Redmine #13583).

The pure authority (:mod:`...domain.workflow_role_authority`) owns the *meaning* of a binding —
the closed role vocabulary, the versioned project-gateway lane derivation, the fail-closed
validation, and the lane resolution. This thin application helper owns the *IO*: turning a repo
root into a parsed declaration by reading the tracked static artifact
``.mozyo-bridge/workflow-role-bindings.json`` (``vibes/docs/logics/managed-state-model.md``
repo-local static-artifact boundary).

Behavior-preserving by construction: an **absent** file resolves to
:meth:`ParsedRoleBindings.empty` (no bindings — every lane falls through to its existing herdr
classification, so a repo that never configured the topology behaves exactly as before). A
**present but broken** file (unreadable / not JSON) fails closed to
:meth:`ParsedRoleBindings.invalid` rather than silently degrading to "no bindings", so a
malformed authority never routes on a guess. Error detail is path-free (a repo-local absolute
path is never leaked into a pasteable record).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Union

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.workflow_role_authority import (
    BINDINGS_FILENAME,
    ParsedRoleBindings,
    parse_role_bindings,
)

#: The tracked static artifact's repo-relative location.
BINDINGS_RELATIVE_PATH = Path(".mozyo-bridge") / BINDINGS_FILENAME


def role_bindings_path(repo_root: Union[str, Path]) -> Path:
    """The repo-local path of the durable workflow-role binding declaration (pure)."""
    return Path(repo_root) / BINDINGS_RELATIVE_PATH


def load_parsed_role_bindings(repo_root: Union[str, Path]) -> ParsedRoleBindings:
    """Read + parse the repo-local binding declaration into a :class:`ParsedRoleBindings`.

    Returns :meth:`ParsedRoleBindings.empty` when the file is absent (behavior-preserving), a
    fail-closed :meth:`ParsedRoleBindings.invalid` when it is present but unreadable / not valid
    JSON, and otherwise the pure :func:`parse_role_bindings` result (which itself fails closed on
    a malformed declaration). Never raises for a missing / broken file — the authority simply
    fails closed so the caller keeps the current lane's existing classification only when there
    is genuinely no declaration.
    """
    path = role_bindings_path(repo_root)
    if not path.exists():
        return ParsedRoleBindings.empty()
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ParsedRoleBindings.invalid(
            f"{BINDINGS_FILENAME} is present but could not be read"
        )
    try:
        record = json.loads(text)
    except json.JSONDecodeError as exc:
        return ParsedRoleBindings.invalid(
            f"{BINDINGS_FILENAME} is not valid JSON: {exc.msg} (line {exc.lineno})"
        )
    return parse_role_bindings(record)


__all__ = (
    "BINDINGS_RELATIVE_PATH",
    "role_bindings_path",
    "load_parsed_role_bindings",
)
