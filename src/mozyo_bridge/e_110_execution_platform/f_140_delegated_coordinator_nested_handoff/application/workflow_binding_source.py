"""Resolve the workflow role -> provider binding from repo-local config (Redmine #13157).

The pure #12673 :class:`~...domain.role_provider_binding.RoleProviderBinding` seam and the
#13157 :class:`~...domain.role_provider_binding_config.RoleProviderBindingConfig` closed
config sub-record own the *meaning* of a rebind; this thin application helper owns the *IO*
of turning a repo root into a resolved binding, so the workflow-aware CLI surfaces
(``workflow runtime`` / ``workflow resume`` / ``workflow watch``) all live-wire the seam the
same way and none of them re-implements the config load.

Behavior-preserving by construction: a repo with no ``.mozyo-bridge/config.yaml`` — or no
``provider_binding`` block — resolves to :meth:`RoleProviderBinding.default` (the legacy
codex/claude map), so an unconfigured repo threads exactly the same binding the commands
used before #13157. A present-but-broken config fails closed through the loader's
``RepoLocalConfigError`` rather than silently defaulting.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (
    RoleProviderBinding,
)


def load_workflow_binding(
    repo_root: Union[str, Path, None] = None,
) -> tuple[RoleProviderBinding, tuple[str, ...]]:
    """Load the resolved role->provider binding + advisory warnings for a repo.

    Reads ``.mozyo-bridge/config.yaml`` via the repo-local loader (a missing file / block is
    the behavior-preserving default) and returns the resolved
    :class:`~...domain.role_provider_binding.RoleProviderBinding` together with the config's
    advisory (non-blocking) warnings — e.g. the auditor and implementer resolving to the
    same provider (#13157). The caller threads the binding into the workflow enrichment and
    surfaces the warnings; it never hard-blocks on them.
    """
    config = load_repo_local_config(repo_root).provider_binding
    return config.binding, config.advisory_warnings()


def _repo_root_from_args(args: object) -> Optional[str]:
    """The ``--repo`` value from a parsed argparse namespace, or ``None``."""
    value = getattr(args, "repo", None)
    return value if value else None


__all__ = ("load_workflow_binding", "_repo_root_from_args")
