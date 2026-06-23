"""Project-router delegation resolution for delegated-coordinator handoff (Redmine #12438).

US #12437 wants a ``gk-3500-it-operations`` coordinator to delegate
mozyo_bridge-related implementation work to the *canonical* mozyo_bridge Codex as
a ``delegated_coordinator`` (``domain.role_profile`` ``ROLE_DELEGATED_COORDINATOR``)
instead of editing the gk submodule directly. gk's ``projects.yaml`` classifies
``giken-3800-mozyo-bridge`` as an ``external-submodule`` /
``external_dependency_reference`` whose source changes belong in the canonical
repository / Redmine project, and the safe manual route today is an explicit
target Codex pane + ``--target-repo`` identity gate. This module is the pure,
fail-closed core that lets a single high-level command encode that route.

Three pure pieces, no filesystem / tmux / git I/O of their own (the caller loads
the YAML and supplies the discovered target candidates):

- :func:`resolve_delegation_target` reads a parsed gk-style ``projects.yaml``
  mapping and decides whether a named target project is an external-submodule
  whose changes belong in a canonical repo / project, returning the canonical
  repo root and project identity. It fails closed (``ProjectRouterError``) when
  the project is absent, is *not* an external-submodule (so it must not be
  delegated — direct edit is appropriate), or has no canonical repo root.
- :func:`select_delegation_codex_pane` picks the unique target-project Codex
  gateway pane by canonical-repo-root match from discovered candidates, failing
  closed when none match (``no_target`` — operator must launch the Unit) or more
  than one usable candidate matches (``ambiguous_target``). It never auto-launches
  a Unit and never selects an ambiguous candidate.
- :func:`delegated_coordinator_profile_fields` derives the
  ``delegated_coordinator`` role-profile placeholder values
  (``parent_project`` / ``child_project`` / ``parent_callback_target`` /
  ``parent_issue`` / ``redmine_project``) from the router decision plus explicit
  operator overrides.

Schema note: the exact gk ``projects.yaml`` schema is pinned by the on-hardware
verification issue #12439. This parser accepts a small set of *documented* key
aliases (:data:`_CLASSIFICATION_KEYS` etc.) and fails closed on anything it
cannot positively classify, so a schema mismatch surfaces as an explicit error
rather than a wrong delegation. Repo-root identity comparison mirrors the
``--target-repo`` gate in :func:`mozyo_bridge.application.commands.orchestrate_handoff`
exactly (``Path(...).expanduser().resolve()`` then
:func:`mozyo_bridge.shared.paths.normalize_path_unicode`) so the selector and the
send-time gate can never disagree.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Mapping, Optional

from mozyo_bridge.domain.agent_discovery import AGENT_KIND_CODEX
from mozyo_bridge.shared.paths import normalize_path_unicode


class ProjectRouterError(ValueError):
    """A delegation target / gateway could not be resolved (fail-closed).

    Carries a stable :attr:`code` so callers and tests can distinguish the
    fail-closed reason without depending on the structured-outcome ``reason``
    enum (which has no ``operator_action_required`` / ``ambiguous_target``
    member). The human message stays in ``str(exc)``.
    """

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


# Fail-closed reason codes (see :class:`ProjectRouterError.code`).
CODE_PROJECT_NOT_FOUND = "project_not_found"
CODE_NOT_EXTERNAL_SUBMODULE = "not_external_submodule"
CODE_NO_CANONICAL_ROOT = "no_canonical_repo_root"
CODE_MALFORMED_CONFIG = "malformed_config"
CODE_NO_TARGET = "no_target"
CODE_AMBIGUOUS_TARGET = "ambiguous_target"

# --- gk `projects.yaml` schema (documented assumption, pinned by #12439) -------
#
# The parser is deliberately tolerant of a few key spellings because the exact
# gk schema is confirmed on hardware in #12439, but it is *strict* about the
# decision: a project is only delegable when it positively classifies as an
# external submodule AND declares a canonical repo root.

# Keys under the top-level config that hold the project map / list.
_PROJECTS_CONTAINER_KEYS = ("projects", "external_projects", "entries")
# Keys on a project entry that hold its own identifier (when entries are a list).
# ``redmine_project`` is the *fallback* identity (Redmine #12444): the live gk
# `projects.yaml` list entries identify themselves with ``redmine_project`` and
# carry no explicit ``id``, so an explicit ``id`` (when present) still wins and
# ``redmine_project`` matches when it is the only identity the entry declares.
_PROJECT_ID_KEYS = ("id", "name", "project", "key", "slug", "redmine_project")
# Keys whose value classifies the entry. ``status`` is the live gk classification
# field (Redmine #12444: ``status: external-submodule``); it is ordered before
# the legacy ``relation`` key so a scalar status wins over a ``relation`` mapping.
_CLASSIFICATION_KEYS = ("classification", "status", "kind", "type", "category", "relation")
# Classification values that mean "external submodule; changes belong upstream".
_EXTERNAL_SUBMODULE_VALUES = frozenset(
    {
        "external-submodule",
        "external_submodule",
        "externalsubmodule",
        "external_dependency_reference",
        "external-dependency-reference",
    }
)
# Sub-mapping holding canonical (upstream) coordinates.
_CANONICAL_CONTAINER_KEYS = ("canonical", "upstream", "source_of_truth")
# Keys (flat or under a canonical container) for the canonical repo root.
_CANONICAL_ROOT_KEYS = (
    "repo_root",
    "repository_root",
    "canonical_repo_root",
    "repository",
    "repo",
    "root",
    "path",
    "canonical_path",
)
# Keys for the canonical project id (the mozyo_bridge-side project).
_CANONICAL_PROJECT_KEYS = (
    "project",
    "canonical_project",
    "name",
    "id",
    "project_id",
)
# Keys for the canonical Redmine project identifier.
_CANONICAL_REDMINE_KEYS = (
    "redmine_project",
    "redmine",
    "canonical_redmine_project",
    "redmine_project_identifier",
)
# Keys for the delegating (parent) project's own identifier at the config top.
_PARENT_PROJECT_KEYS = ("project", "id", "name", "slug", "workspace")


@dataclass(frozen=True)
class DelegationTarget:
    """A resolved external-submodule delegation target (Redmine #12438).

    ``canonical_repo_root`` is the path as declared in the config (not yet
    resolved); :func:`select_delegation_codex_pane` and the send-time
    ``--target-repo`` gate both normalize it the same way before comparison.
    """

    target_project: str
    classification: str
    canonical_repo_root: str
    child_project: str
    redmine_project: Optional[str]
    parent_project: Optional[str]


def _first_present(mapping: Mapping[str, object], keys: Iterable[str]) -> Optional[object]:
    """Return the first non-empty value among ``keys`` in ``mapping``."""
    for key in keys:
        if key in mapping:
            value = mapping[key]
            if value is not None and value != "":
                return value
    return None


def _as_str(value: object) -> Optional[str]:
    """Coerce a scalar config value to a stripped string, else ``None``.

    Containers (mappings / lists) are rejected (``None``) rather than coerced via
    ``str(...)`` (Redmine #12444): a ``relation: {kind: ...}`` mapping in a
    classification slot must not be mis-read as an opaque ``"{...}"`` string.
    """
    if value is None or isinstance(value, (Mapping, list, tuple)):
        return None
    text = str(value).strip()
    return text or None


def _find_project_entry(
    config: Mapping[str, object], target_project: str
) -> Mapping[str, object]:
    """Locate the entry for ``target_project`` in a parsed projects config.

    Accepts the project container as either a mapping keyed by project id or a
    list of entries each carrying an id field. Fails closed when the container
    is missing/malformed or the project is absent.
    """
    container = _first_present(config, _PROJECTS_CONTAINER_KEYS)
    if container is None:
        raise ProjectRouterError(
            "projects config has no recognized project container "
            f"(expected one of {list(_PROJECTS_CONTAINER_KEYS)})",
            code=CODE_MALFORMED_CONFIG,
        )

    if isinstance(container, Mapping):
        entry = container.get(target_project)
        if entry is None:
            raise ProjectRouterError(
                f"project {target_project!r} not found in projects config "
                f"(known: {sorted(str(k) for k in container.keys())})",
                code=CODE_PROJECT_NOT_FOUND,
            )
        if not isinstance(entry, Mapping):
            raise ProjectRouterError(
                f"project {target_project!r} entry is not a mapping",
                code=CODE_MALFORMED_CONFIG,
            )
        return entry

    if isinstance(container, list):
        for raw in container:
            if not isinstance(raw, Mapping):
                continue
            entry_id = _as_str(_first_present(raw, _PROJECT_ID_KEYS))
            if entry_id == target_project:
                return raw
        raise ProjectRouterError(
            f"project {target_project!r} not found in projects config list",
            code=CODE_PROJECT_NOT_FOUND,
        )

    raise ProjectRouterError(
        "projects config container is neither a mapping nor a list",
        code=CODE_MALFORMED_CONFIG,
    )


def _canonical_lookup(entry: Mapping[str, object], keys: Iterable[str]) -> Optional[str]:
    """Look up a canonical field on a project entry.

    Prefers a value inside a canonical/upstream sub-mapping, then falls back to a
    flat key on the entry itself, so both ``canonical: {repo_root: ...}`` and a
    flat ``canonical_repo_root: ...`` shape resolve.
    """
    container = _first_present(entry, _CANONICAL_CONTAINER_KEYS)
    if isinstance(container, Mapping):
        nested = _as_str(_first_present(container, keys))
        if nested is not None:
            return nested
    return _as_str(_first_present(entry, keys))


def resolve_delegation_target(
    config: Optional[Mapping[str, object]], target_project: str
) -> DelegationTarget:
    """Resolve an external-submodule delegation target from a projects config.

    ``config`` is the parsed gk ``projects.yaml`` mapping (the caller loads it).
    ``target_project`` is the external-submodule id to delegate (e.g.
    ``giken-3800-mozyo-bridge``).

    Fails closed (:class:`ProjectRouterError`) when the config is malformed, the
    project is absent, the project does not classify as an external-submodule
    (delegation would be wrong — direct edit is appropriate), or it declares no
    canonical repo root. The function is pure and deterministic.
    """
    if not target_project:
        raise ProjectRouterError(
            "target_project must be non-empty", code=CODE_MALFORMED_CONFIG
        )
    if not isinstance(config, Mapping):
        raise ProjectRouterError(
            "projects config must be a mapping (parsed YAML document)",
            code=CODE_MALFORMED_CONFIG,
        )

    entry = _find_project_entry(config, target_project)

    classification = _as_str(_first_present(entry, _CLASSIFICATION_KEYS))
    if classification is None:
        raise ProjectRouterError(
            f"project {target_project!r} has no classification "
            f"(expected one of {list(_CLASSIFICATION_KEYS)})",
            code=CODE_NOT_EXTERNAL_SUBMODULE,
        )
    normalized_classification = classification.replace(" ", "").lower()
    if normalized_classification not in _EXTERNAL_SUBMODULE_VALUES:
        raise ProjectRouterError(
            f"project {target_project!r} is classified {classification!r}, "
            "not an external-submodule; delegation only applies to external "
            "submodules whose source changes belong in the canonical project. "
            "Edit it directly in its own repository instead of delegating.",
            code=CODE_NOT_EXTERNAL_SUBMODULE,
        )

    canonical_repo_root = _canonical_lookup(entry, _CANONICAL_ROOT_KEYS)
    if canonical_repo_root is None:
        raise ProjectRouterError(
            f"project {target_project!r} declares no canonical repo root "
            f"(expected one of {list(_CANONICAL_ROOT_KEYS)} flat or under "
            f"{list(_CANONICAL_CONTAINER_KEYS)}); identity unestablished, "
            "fail-closed.",
            code=CODE_NO_CANONICAL_ROOT,
        )

    # The canonical project id defaults to the entry id itself when the config
    # does not declare a separate canonical project name.
    child_project = (
        _canonical_lookup(entry, _CANONICAL_PROJECT_KEYS) or target_project
    )
    redmine_project = _canonical_lookup(entry, _CANONICAL_REDMINE_KEYS)
    parent_project = _as_str(_first_present(config, _PARENT_PROJECT_KEYS))

    return DelegationTarget(
        target_project=target_project,
        classification=classification,
        canonical_repo_root=canonical_repo_root,
        child_project=child_project,
        redmine_project=redmine_project,
        parent_project=parent_project,
    )


def normalize_repo_root(path: Optional[str]) -> Optional[str]:
    """Normalize a repo-root path for identity comparison.

    Mirrors the send-time ``--target-repo`` gate exactly
    (:func:`mozyo_bridge.application.commands.orchestrate_handoff`):
    ``Path(...).expanduser().resolve()`` then
    :func:`mozyo_bridge.shared.paths.normalize_path_unicode`. So the pre-send
    pane selection and the in-send identity gate can never disagree on whether a
    pane lives in the canonical repo.
    """
    if not path:
        return None
    try:
        resolved = str(Path(path).expanduser().resolve())
    except (OSError, RuntimeError):
        return None
    return normalize_path_unicode(resolved)


def select_delegation_codex_pane(
    candidates: Iterable[object],
    *,
    canonical_repo_root: str,
):
    """Select the unique Codex gateway pane in the canonical repo.

    ``candidates`` are projected target candidates (e.g.
    :class:`mozyo_bridge.domain.agent_discovery.TargetCandidate`); each must
    expose ``role`` / ``repo_root`` / ``pane_id`` / ``ambiguous``. Only Codex
    candidates whose ``repo_root`` normalizes equal to ``canonical_repo_root``
    are considered, and an ``ambiguous`` candidate is never selected.

    Fails closed (:class:`ProjectRouterError`):

    - ``no_target`` — no usable (non-ambiguous) Codex pane lives in the canonical
      repo. The target Unit is likely not loaded; operator action is required and
      this route never auto-launches one.
    - ``ambiguous_target`` — more than one usable Codex pane matches; the caller
      must name the exact ``%pane``.

    Returns the single matching candidate object unchanged. Pure: no I/O.
    """
    wanted = normalize_repo_root(canonical_repo_root)
    if wanted is None:
        raise ProjectRouterError(
            f"canonical_repo_root {canonical_repo_root!r} did not normalize to a "
            "comparable path; fail-closed.",
            code=CODE_NO_CANONICAL_ROOT,
        )

    in_repo_codex = [
        c
        for c in candidates
        if getattr(c, "role", None) == AGENT_KIND_CODEX
        and normalize_repo_root(getattr(c, "repo_root", None)) == wanted
    ]
    usable = [c for c in in_repo_codex if not getattr(c, "ambiguous", False)]

    if not usable:
        if in_repo_codex:
            raise ProjectRouterError(
                f"the only Codex pane(s) in canonical repo {canonical_repo_root!r} "
                "have an ambiguous role and cannot be safely targeted; name the "
                "exact `%pane` after resolving the ambiguity. No pane was "
                "selected and no Unit was launched.",
                code=CODE_AMBIGUOUS_TARGET,
            )
        raise ProjectRouterError(
            f"no Codex gateway pane found in canonical repo {canonical_repo_root!r}; "
            "the target Unit is likely not loaded. Launch the canonical "
            "mozyo_bridge Codex Unit (operator action required) and retry — this "
            "route never auto-launches a hidden worker.",
            code=CODE_NO_TARGET,
        )
    if len(usable) > 1:
        panes = ", ".join(sorted(getattr(c, "pane_id", "?") for c in usable))
        raise ProjectRouterError(
            f"multiple Codex gateway panes in canonical repo {canonical_repo_root!r} "
            f"({panes}); ambiguous. Name the exact `%pane` with --target.",
            code=CODE_AMBIGUOUS_TARGET,
        )
    return usable[0]


def delegated_coordinator_profile_fields(
    target: DelegationTarget,
    *,
    parent_project: Optional[str] = None,
    parent_issue: Optional[str] = None,
    parent_callback_target: Optional[str] = None,
) -> dict[str, str]:
    """Derive ``delegated_coordinator`` role-profile placeholder values.

    Maps a :class:`DelegationTarget` plus explicit operator overrides onto the
    ``ROLE_DELEGATED_COORDINATOR`` template placeholders
    (``domain.role_profile``): ``parent_project`` / ``child_project`` /
    ``parent_callback_target`` / ``parent_issue`` / ``redmine_project``. Explicit
    arguments win over config-derived values. Only non-empty fields are returned,
    so an unsupplied placeholder is left for the resolver to report as
    unresolved rather than filled with an empty string.
    """
    resolved_parent_project = _as_str(parent_project) or target.parent_project
    fields: dict[str, str] = {"child_project": target.child_project}
    if resolved_parent_project:
        fields["parent_project"] = resolved_parent_project
    if target.redmine_project:
        fields["redmine_project"] = target.redmine_project
    parent_issue_value = _as_str(parent_issue)
    if parent_issue_value:
        fields["parent_issue"] = parent_issue_value
    callback_value = _as_str(parent_callback_target)
    if callback_value:
        fields["parent_callback_target"] = callback_value
    return fields


__all__ = (
    "ProjectRouterError",
    "CODE_PROJECT_NOT_FOUND",
    "CODE_NOT_EXTERNAL_SUBMODULE",
    "CODE_NO_CANONICAL_ROOT",
    "CODE_MALFORMED_CONFIG",
    "CODE_NO_TARGET",
    "CODE_AMBIGUOUS_TARGET",
    "DelegationTarget",
    "resolve_delegation_target",
    "normalize_repo_root",
    "select_delegation_codex_pane",
    "delegated_coordinator_profile_fields",
)
