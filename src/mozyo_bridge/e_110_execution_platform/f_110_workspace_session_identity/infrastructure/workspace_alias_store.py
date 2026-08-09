"""Workspace-local read/write for the nested-workspace alias declaration (#15190).

The declaration lives at ``<workspace>/.mozyo-bridge/workspace-alias.json``,
next to the identity anchor but deliberately separate from it (see
:data:`...domain.workspace_alias.ALIAS_RELATIVE`).

Workspace-local storage — not the home registry — is the point. The acceptance
boundary for #15190 requires the declaration to survive **registry loss and
recovery**: after the home registry is moved aside and rebuilt from anchors, a
nested workspace must still fold into its parent instead of quietly becoming
launchable again. A row in ``registry.sqlite`` would be destroyed by exactly the
recovery procedure it has to outlive, and would also require a schema bump on
the identity store this rail is forbidden to hand-edit.

Reads never create, never repair, and never raise: an unreadable or malformed
declaration is reported as a typed refusal so the caller fails closed with a
nameable reason rather than crashing or, worse, degrading to "no declaration".
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    ALIAS_RELATIVE,
    ALIAS_SCHEMA_VERSION,
    REASON_DECLARATION_UNREADABLE,
    AliasResolution,
    WorkspaceAliasDeclaration,
    parse_declaration,
    refused,
)


def alias_path(repo_root: Path | str) -> Path:
    """The declaration path for ``repo_root`` (also the write target)."""
    return Path(repo_root) / ALIAS_RELATIVE


def declaration_exists(repo_root: Path | str) -> bool:
    """Whether ``repo_root`` carries a declaration file at all.

    Used for the cycle check, which must distinguish "target declares nothing"
    from "target declares something unparseable" without itself parsing.
    """
    try:
        return alias_path(repo_root).is_file()
    except OSError:
        return False


def read_declaration(
    repo_root: Path | str,
) -> Optional[WorkspaceAliasDeclaration] | AliasResolution:
    """Read ``repo_root``'s declaration.

    Returns ``None`` when no declaration file exists (the common case), a parsed
    :class:`WorkspaceAliasDeclaration`, or an :class:`AliasResolution` refusal
    when the file exists but cannot be read or parsed.
    """
    path = alias_path(repo_root)
    try:
        if not path.is_file():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{path}: {exc}")
    except ValueError as exc:
        return refused(REASON_DECLARATION_UNREADABLE, f"{path}: invalid JSON ({exc})")
    return parse_declaration(raw)


def write_declaration(
    repo_root: Path | str, declaration: WorkspaceAliasDeclaration
) -> Path:
    """Write ``declaration`` to ``repo_root``, preserving ``created_at``.

    Idempotent by construction: re-declaring the same alias keeps the original
    ``created_at`` so the durable record shows when the routing decision was
    first made, not when it was last re-affirmed.
    """
    path = alias_path(repo_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    created_at = declaration.created_at
    if not created_at:
        existing = read_declaration(repo_root)
        if isinstance(existing, WorkspaceAliasDeclaration) and existing.created_at:
            created_at = existing.created_at
        else:
            created_at = now

    payload = dict(declaration.as_payload())
    payload["schema_version"] = ALIAS_SCHEMA_VERSION
    payload["created_at"] = created_at
    payload["updated_at"] = now
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def clear_declaration(repo_root: Path | str) -> bool:
    """Remove ``repo_root``'s declaration. True when a file was removed.

    This removes only the file this module writes. It never touches the identity
    anchor, the registry, or any tracked scaffold / catalog / skills content in
    the nested workspace — reversing the routing decision must not cost the
    workspace its identity or its contents.
    """
    path = alias_path(repo_root)
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


__all__ = (
    "alias_path",
    "clear_declaration",
    "declaration_exists",
    "read_declaration",
    "write_declaration",
)
