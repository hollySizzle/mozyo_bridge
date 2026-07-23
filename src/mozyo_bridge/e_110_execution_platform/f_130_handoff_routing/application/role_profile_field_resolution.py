"""Send-side role-profile field resolution glue (Redmine #13477).

Extends the pure role-profile resolver
(:mod:`mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile`)
with the two send-time field auto-fills that need runtime context the pure
resolver deliberately avoids:

- ``durable_anchor`` — filled from the handoff anchor pointer (Redmine #12388);
- ``redmine_project`` — auto-resolved from the *verified* workspace-local
  Redmine default (``<repo>/.mozyo-bridge/project-defaults.yaml``) when the
  requested role template carries that placeholder and no explicit
  ``--profile-field redmine_project=`` was given (Redmine #13477).

Resolution priority for ``redmine_project`` mirrors the workspace
default-project contract (``skills/mozyo-bridge-agent/references/workflow.md``
``### Default project 解決``): an explicit value always wins; otherwise the
*verified* default is used; a missing / unverified / ambiguous default fails
closed — it is never silently substituted as fact. Fail-closed is surfaced as a
:class:`RoleProfileError` so the caller reuses the existing ``blocked`` /
``invalid_args`` handoff outcome path.

This lives in the application layer, not the pure resolver, so
``role_profile.py`` stays IO-free (no cwd / worktree path read at send time,
per its self-contained/fail-closed invariant); this seam owns the single
filesystem read via :func:`resolve_default_project`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional

from mozyo_bridge.core.state.workspace_defaults import resolve_default_project
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.role_profile import (
    REDMINE_PROJECT_FIELD,
    RoleProfileError,
    check_explicit_profile_fields,
    parse_profile_fields,
    template_placeholders,
)

# Structured role-profile placeholder name with a send-time auto-fill source only.
DURABLE_ANCHOR_FIELD = "durable_anchor"


def resolve_handoff_profile_fields(
    role: str,
    raw_profile_fields: Optional[Iterable[str]],
    anchor_pointer: str,
    repo_root: Path,
) -> dict[str, str]:
    """Parse ``--profile-field`` pairs and apply the send-time auto-fills.

    Fails closed with :class:`RoleProfileError` on a malformed ``--profile-field``
    pair, an unknown role, or a role that needs ``redmine_project`` when it cannot
    be resolved from a verified workspace-local default.
    """
    fields = parse_profile_fields(raw_profile_fields)
    fields.setdefault(DURABLE_ANCHOR_FIELD, anchor_pointer)
    _autofill_redmine_project(role, fields, repo_root)
    return fields


def _autofill_redmine_project(
    role: str, fields: dict[str, str], repo_root: Path
) -> None:
    # `template_placeholders` fails closed on an unknown role (same contract as
    # `resolve_role_profile`), so an unknown role is rejected before any default
    # read.
    if REDMINE_PROJECT_FIELD not in template_placeholders(role):
        return
    # Explicit wins: a valid operator-supplied value is authoritative and is
    # never overridden by the workspace default. The RECORD-STATIC part of that
    # rule — an explicit empty / whitespace-only value is not a valid project
    # identifier and fails closed (Redmine #13477 review j#74496 finding_1) —
    # lives in the shared domain authority so the auto-hibernate evidence reader
    # applies the very same refusal (#14219 j#86687 R21-F1); only the
    # host-state-dependent default resolution below stays in this seam.
    check_explicit_profile_fields(role, fields)
    if REDMINE_PROJECT_FIELD in fields:
        return
    resolution = resolve_default_project(repo_root)
    if resolution.is_verified and resolution.identifier:
        fields[REDMINE_PROJECT_FIELD] = resolution.identifier
        return
    raise RoleProfileError(
        f"cannot auto-resolve role profile field {REDMINE_PROJECT_FIELD!r} for "
        f"role {role!r}: {resolution.detail}. Pass an explicit "
        f"--profile-field {REDMINE_PROJECT_FIELD}=<id>, or verify the "
        "workspace-local Redmine default (`mozyo-bridge workspace-defaults`)."
    )


__all__ = (
    "REDMINE_PROJECT_FIELD",
    "DURABLE_ANCHOR_FIELD",
    "resolve_handoff_profile_fields",
)
