"""Adoption markers the Git-root-first walk passed on its way up (Redmine #15526).

Measured on main `59526e7a`: an operator who scaffolded `/myapp/Source/rails` inside a
Git repository rooted at `/myapp` was told by `mozyo` that the project was unadopted
and to scaffold it — the thing they had just done — because the resolver deliberately
walks past subtree markers (Git-root-first, #13641) and the refusal never mentioned the
marker it had walked past. The rule is correct; the silence was the defect. This module
supplies the missing fact.

Placement (review j#105978 finding_1): this is a workspace-adoption concern of exactly
one bounded context, so it lives in `f_110_workspace_session_identity/domain` rather
than in the frozen `shared` kernel. It *reads* the kernel's marker probe; the kernel
does not know about it.
"""

from __future__ import annotations

from pathlib import Path

from mozyo_bridge.shared.paths import workspace_adoption_marker

__all__ = ("nested_adoption_marker",)


def nested_adoption_marker(
    start: str | Path, root: str | Path
) -> tuple[Path, str] | None:
    """The nearest adoption marker from ``start`` (inclusive) up to ``root`` (exclusive).

    ``start`` is the directory the operator actually ran the command in, so a marker
    sitting right there — the live reproduction, where the CWD is the freshly
    scaffolded subdirectory — must be reported (review j#105978 finding_3 pinned this
    boundary: ``start`` is included, ``root`` is not). ``root`` itself is excluded
    because a marker *at* the resolved root is
    :func:`~mozyo_bridge.shared.paths.workspace_adoption_marker`'s question — an
    adopted workspace, not a stray subtree.

    Returns ``(directory, marker)`` for the nearest such marker, or ``None`` — also
    ``None`` when ``start`` is not under ``root`` at all (an explicit ``--repo``
    elsewhere), where naming anything would point at an unrelated tree.
    """
    base = Path(start).expanduser().resolve()
    top = Path(root).expanduser().resolve()
    if base == top:
        return None
    try:
        base.relative_to(top)
    except ValueError:
        return None
    for path in (base, *base.parents):
        if path == top:
            break
        marker = workspace_adoption_marker(path)
        if marker is not None:
            return (path, marker)
    return None
