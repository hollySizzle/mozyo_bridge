"""Nested-workspace alias resolution use case (#15190).

Gathers the filesystem / git / identity observations the pure decision core
(:mod:`...domain.workspace_alias`) needs, and exposes the one function the
launch authority calls: :func:`resolve_launch_root`.

Placement rationale — why the launch authority and not
:func:`shared.paths.resolve_repo_root`: that resolver has ~57 call sites across
release tooling, doctor surfaces, discovery, and config loading. Rerouting it
would silently redefine what ``--repo`` means for every one of them, including
read-only surfaces whose whole job is to report on the nested workspace *as
itself* (``workspace inspect``, ``docs resolve``, ``scaffold status``). The
defect in #15190 is specific and so is the fix: it is the *launch* of a default
coordinator pair that must not duplicate per repository. Everything else keeps
addressing the nested root exactly as before, which is also what keeps the
nested tree usable as a Rails code/docs working root.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from mozyo_bridge.core.state.workspace_registry import resolve_canonical_session
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.workspace_alias import (  # noqa: E501
    GIT_BINDING_DIFFERENT,
    GIT_BINDING_NOT_MEASURABLE,
    GIT_BINDING_SAME,
    GIT_BINDING_UNAVAILABLE,
    STATE_NO_DECLARATION,
    AliasResolution,
    AliasTargetObservation,
    WorkspaceAliasDeclaration,
    build_alias_resolution,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure.workspace_alias_store import (  # noqa: E501
    declaration_exists,
    read_declaration,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.infrastructure.workspace_git_probe import (  # noqa: E501
    GIT_PROBE_GIT,
    GIT_PROBE_NON_GIT,
    probe_workspace_git,
)


def git_binding(source_root: Path, target_root: Path) -> str:
    """Classify whether two roots belong to the same repository.

    Equality is on ``git_common_dir``, not ``git_dir``: a linked worktree shares
    its common dir with the main checkout and is legitimately the same
    repository, while a **submodule** has its own common dir even though it sits
    physically inside the parent's tree. The observed #15190 case is exactly a
    submodule (``projects/nihonidenshi``), so path containment alone would have
    been the wrong test — it is true for a submodule that must NOT be aliased
    into its superproject.

    A root that is positively observed not to be a git checkout is measurable
    only against another positively non-git root; a git/non-git pair is reported
    as different.  A missing executable, timeout, unexpected Git error, malformed
    output, or unsafe discovery marker is ``unavailable`` and always fails closed.
    """
    source = probe_workspace_git(source_root)
    target = probe_workspace_git(target_root)
    if source.state not in {GIT_PROBE_GIT, GIT_PROBE_NON_GIT}:
        return GIT_BINDING_UNAVAILABLE
    if target.state not in {GIT_PROBE_GIT, GIT_PROBE_NON_GIT}:
        return GIT_BINDING_UNAVAILABLE
    if source.state == GIT_PROBE_NON_GIT and target.state == GIT_PROBE_NON_GIT:
        return GIT_BINDING_NOT_MEASURABLE
    if source.state != target.state:
        return GIT_BINDING_DIFFERENT
    return (
        GIT_BINDING_SAME
        if source.common_dir == target.common_dir
        else GIT_BINDING_DIFFERENT
    )


def _is_strict_ancestor(candidate: Path, descendant: Path) -> bool:
    return candidate != descendant and candidate in descendant.parents


def observe_target(
    source_root: Path, canonical_path: str, *, home: Optional[Path] = None
) -> AliasTargetObservation:
    """Measure the declared canonical target for ``source_root``.

    The identity read uses ``derive_unregistered=False`` so an unregistered
    target degrades to a path-hash name with ``workspace_id=None`` rather than
    reading workspace-local defaults — the target must prove a *durable*
    identity (registry row or anchor) to be aliasable, and a derived name is not
    one.
    """
    target = Path(canonical_path).expanduser()
    try:
        resolved_target = target.resolve()
    except OSError:
        resolved_target = target

    exists = False
    is_dir = False
    try:
        exists = resolved_target.exists()
        is_dir = resolved_target.is_dir()
    except OSError:
        pass

    workspace_id = ""
    binding = GIT_BINDING_DIFFERENT
    ancestor = False
    declares_alias = False
    if is_dir:
        try:
            resolved = resolve_canonical_session(
                resolved_target, home=home, derive_unregistered=False
            )
            workspace_id = resolved.workspace_id or ""
        except Exception:  # pragma: no cover - identity read must never crash launch
            workspace_id = ""
        binding = git_binding(source_root, resolved_target)
        ancestor = _is_strict_ancestor(resolved_target, source_root)
        declares_alias = declaration_exists(resolved_target)

    return AliasTargetObservation(
        exists=exists,
        is_dir=is_dir,
        workspace_id=workspace_id,
        git_binding=binding,
        is_ancestor_of_source=ancestor,
        declares_alias=declares_alias,
    )


def resolve_launch_root(
    repo_root: Path | str, *, home: Optional[Path] = None
) -> AliasResolution:
    """Resolve the effective launch root for ``repo_root``.

    Read-only: it reads a workspace-local file and (for an alias) the target's
    identity, and writes nothing anywhere. A workspace with no declaration
    returns :data:`STATE_NO_DECLARATION` carrying the unchanged root, so the
    overwhelmingly common path is unchanged.
    """
    source_root = Path(repo_root).expanduser()
    try:
        source_root = source_root.resolve()
    except OSError:
        pass

    declaration = read_declaration(source_root)
    if isinstance(declaration, AliasResolution):
        # The file exists but could not be read / parsed: already a typed refusal.
        return declaration
    if declaration is None:
        return AliasResolution(
            state=STATE_NO_DECLARATION, launch_root=str(source_root)
        )

    target = None
    if isinstance(declaration, WorkspaceAliasDeclaration) and declaration.canonical_path:
        target = observe_target(source_root, declaration.canonical_path, home=home)

    resolution = build_alias_resolution(
        source_root=str(source_root),
        declaration=declaration,
        target=target,
    )
    if resolution.redirected:
        # Normalize the redirect target the same way the source was normalized,
        # so the launch root the caller receives is a resolved absolute path.
        try:
            normalized = str(Path(resolution.launch_root).expanduser().resolve())
        except OSError:
            normalized = resolution.launch_root
        if normalized != resolution.launch_root:
            from dataclasses import replace

            resolution = replace(resolution, launch_root=normalized)
    return resolution


__all__ = (
    "git_binding",
    "observe_target",
    "resolve_launch_root",
)
