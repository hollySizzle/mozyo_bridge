"""Built-in update-manager adapter: where does a provider's own updater write?

Redmine #14741, Design Consultation Answer j#96167 (option **D**). The question this
answers is the one the #14741 authority split turns on, and the two obvious places to put
the answer were both ruled out:

- **not provider profile data.** ``agent_provider_profiles.yaml`` fail-closed forbids a
  host path / argv / module path / entry point / callable. A read-only query is still an
  execution recipe, so declaring "ask npm with ``root -g``" as profile data would cross
  the pure-description boundary #13441 draws (Answer item 1, rejecting option A).
- **not a provider-neutral consumer.** The startup-admission / send / self-heal path must
  keep consuming a typed port and a typed result, never a provider name, a package-manager
  name, or a query argv (Answer item 2).

So the knowledge lives here, in **trusted built-in adapter code** with a closed registry
(Answer item 3). Everything about how to ask is fixed at import time: the manager list,
the query argv, and the mapping from a provider to its manager and package are literals in
this module. There is no plugin API, no operator-supplied argv, no repo-local script, and
no dynamic module loading — adding a manager is a source edit here, reviewed like any
other, not a configuration surface.

What "positive" means (Answer item 4)
------------------------------------
This adapter returns a write root **only** when it can positively correspond all of:
the manager to use, the query executable, the write root that query reports, and the
provider's own package directory inside it.

The query executable is resolved under the trusted-``PATH`` safety rules (absolute
components only, realpath-verified) and taken as the **effective** one — the first match,
which is what the updater's own shell lookup would run (Design Answer D2 j#96288 item 4).
A shadowed second install is therefore not ambiguity; an earlier cut treated it as such
and took a workspace offline. The resolved executable is then run **under that same env**,
never the ambient process env (review j#96360 F3).

Anything else — an unregistered provider, an unknown manager, no query executable at all,
an unsafe ``PATH``, a failed / malformed query, a package directory that is not there — is
a typed ``unresolved`` with a fixed reason token, which the caller must treat as ``unknown``
and therefore as zero-actuation. It never falls back to "where the provider's
binary happens to sit on PATH": that was the j#95741 F2 proxy, and it is the reason this
module exists.

Only **npm** is supported in this first cut (Answer item 5). pnpm / bun / brew / the curl
installer are real variants whose positive query and identity correspondence have not been
measured, and a guessed one would re-create the exact defect this issue is closing, so they
are reported as unsupported rather than approximated. The extension point is
:data:`_PROVIDER_UPDATE_BINDINGS` plus one small manager entry.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence, Tuple

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (  # noqa: E501
    resolve_trusted_command,
)

#: A runner with :func:`subprocess.run`'s shape, injected so tests never touch a live
#: package manager (the same port style as the herdr transport's ``Runner``).
Runner = Callable[..., object]

#: How long a read-only query may take before it is abandoned as unresolved. A package
#: manager that hangs must not hang a send.
QUERY_TIMEOUT_SECONDS = 10

# --- Fixed reason tokens ---------------------------------------------------------------
#
# These are the only things about a failed resolution that leave this module: no path, no
# stderr, no env value, so a caller may put them straight on a durable record.

REASON_OK = "resolved"
#: The provider has no built-in update binding registered here.
REASON_PROVIDER_UNREGISTERED = "provider_not_registered"
#: The provider's manager variant is known but not supported in this build.
REASON_MANAGER_UNSUPPORTED = "manager_unsupported"
#: The query executable did not resolve to exactly one trusted executable.
REASON_QUERY_EXECUTABLE_UNRESOLVED = "query_executable_unresolved"
#: The query failed to run, exited non-zero, timed out, or printed nothing usable.
REASON_QUERY_FAILED = "query_failed"
#: The query answered, but the provider's own package directory is not inside the answer,
#: so the write root cannot be corresponded to THIS provider.
REASON_IDENTITY_UNCORRESPONDED = "identity_uncorresponded"


@dataclass(frozen=True)
class UpdaterTargetResolution:
    """A typed resolution result. ``roots`` is non-empty only when ``resolved``."""

    roots: Tuple[str, ...] = ()
    resolved: bool = False
    reason: str = REASON_PROVIDER_UNREGISTERED

    def as_probe_result(self) -> Tuple[Sequence[str], bool]:
        """The shape the provider-neutral ``UpdaterTargetProbe`` port expects."""
        return (self.roots, self.resolved)


@dataclass(frozen=True)
class _ManagerQuery:
    """One manager's fixed, allowlisted, read-only interrogation."""

    command: str
    argv: Tuple[str, ...]


@dataclass(frozen=True)
class _UpdateBinding:
    """One provider's built-in update binding: which manager, and which package."""

    manager: str
    package: str


#: The closed manager registry. The argv is a literal here and nowhere else.
_BUILTIN_MANAGERS: dict[str, _ManagerQuery] = {
    # `npm root -g` prints the global node_modules directory — the directory
    # `npm install -g <pkg>` writes into. Read-only: it reports a path and installs
    # nothing.
    "npm": _ManagerQuery(command="npm", argv=("root", "-g")),
}

#: The closed provider->update binding registry. Adding a provider is a source edit.
_PROVIDER_UPDATE_BINDINGS: dict[str, _UpdateBinding] = {
    "codex": _UpdateBinding(manager="npm", package="@openai/codex"),
}


def _package_directory(root: str, package: str) -> str:
    """``<root>/<package>``, with a scoped npm name (``@scope/name``) split into dirs."""
    return os.path.join(root, *package.split("/"))


def resolve_updater_target(
    provider_id: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    runner: Optional[Runner] = None,
) -> UpdaterTargetResolution:
    """Positively resolve where ``provider_id``'s own updater writes (never raises).

    Returns :class:`UpdaterTargetResolution`. Every failure mode is a distinct fixed
    reason and ``resolved=False``; the caller maps that to a typed ``unknown`` authority,
    which is zero-actuation (Answer item 4). Nothing here mutates anything: the only
    subprocess is the manager's own read-only query.
    """
    env = os.environ if env is None else env
    run: Runner = subprocess.run if runner is None else runner

    binding = _PROVIDER_UPDATE_BINDINGS.get(str(provider_id or "").strip())
    if binding is None:
        return UpdaterTargetResolution(reason=REASON_PROVIDER_UNREGISTERED)

    query = _BUILTIN_MANAGERS.get(binding.manager)
    if query is None:
        # A registered provider whose manager this build cannot interrogate. Named
        # distinctly from "unregistered" so the operator sees which extension is missing.
        return UpdaterTargetResolution(reason=REASON_MANAGER_UNSUPPORTED)

    executable = resolve_trusted_command(query.command, env)
    if not executable:
        # Missing, ambiguous, or an unsafe PATH. "Which npm?" having no single answer is
        # exactly "where would an update write?" having no single answer.
        return UpdaterTargetResolution(reason=REASON_QUERY_EXECUTABLE_UNRESOLVED)

    try:
        completed = run(
            [executable, *query.argv],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_SECONDS,
            check=False,
            # Redmine #14741 review j#96360 F3. The query MUST run under the same env the
            # executable was resolved from. Inheriting the ambient process env instead let
            # a stray `NPM_CONFIG_PREFIX` answer about a different global root than the one
            # being evaluated — i.e. asking the wrong authority, which is the very defect
            # this whole module exists to close.
            env=dict(env),
        )
    except Exception:  # noqa: BLE001 - a manager may fail in any number of ways
        return UpdaterTargetResolution(reason=REASON_QUERY_FAILED)

    if getattr(completed, "returncode", 1) != 0:
        return UpdaterTargetResolution(reason=REASON_QUERY_FAILED)
    raw = getattr(completed, "stdout", "") or ""
    if not isinstance(raw, str):
        return UpdaterTargetResolution(reason=REASON_QUERY_FAILED)
    root = raw.strip().splitlines()[0].strip() if raw.strip() else ""
    if not root or not os.path.isabs(root):
        # A relative answer would be resolved against this process's cwd, which is not
        # the manager's notion of anything. Refuse rather than normalise it.
        return UpdaterTargetResolution(reason=REASON_QUERY_FAILED)

    real_root = os.path.realpath(root)
    package_dir = _package_directory(real_root, binding.package)
    if not os.path.isdir(package_dir):
        # The manager writes here, but THIS provider's package is not installed here, so
        # the write root cannot be corresponded to this provider's identity.
        return UpdaterTargetResolution(reason=REASON_IDENTITY_UNCORRESPONDED)

    # The correspondence is positive: the manager, its query executable, the write root,
    # and this provider's package directory inside it all line up. The provider's package
    # directory (not the bare manager root) is the write target, so containment against
    # the managed exec target is a statement about THIS provider.
    return UpdaterTargetResolution(
        roots=(os.path.realpath(package_dir),), resolved=True, reason=REASON_OK
    )


def is_supported_provider(provider_id: str) -> bool:
    """True iff a trusted built-in updater binding exists for ``provider_id``.

    The composition root asks this BEFORE arming the authority gate (Design Answer D2
    j#96288 item 1): a provider with no binding is out of this ticket's scope for a
    generic ready send and must stay ``not_evaluated``, not be promoted to ``unknown``.
    R3 armed everything and refused every Claude send on every host.
    """
    return str(provider_id or "").strip() in _PROVIDER_UPDATE_BINDINGS


def builtin_updater_target_probe(
    env: Optional[Mapping[str, str]] = None, *, runner: Optional[Runner] = None
) -> Callable[[str], Tuple[Sequence[str], bool]]:
    """The provider-neutral probe the send / launch fences consume (Answer item 2).

    Closes over the environment and the runner and exposes only ``(roots, resolved)``, so
    no consumer ever sees a provider name, a manager name, or a query argv.
    """

    def _probe(provider_id: str) -> Tuple[Sequence[str], bool]:
        return resolve_updater_target(provider_id, env, runner=runner).as_probe_result()

    return _probe


__all__ = (
    "QUERY_TIMEOUT_SECONDS",
    "REASON_IDENTITY_UNCORRESPONDED",
    "REASON_MANAGER_UNSUPPORTED",
    "REASON_OK",
    "REASON_PROVIDER_UNREGISTERED",
    "REASON_QUERY_EXECUTABLE_UNRESOLVED",
    "REASON_QUERY_FAILED",
    "Runner",
    "UpdaterTargetResolution",
    "builtin_updater_target_probe",
    "resolve_updater_target",
)
