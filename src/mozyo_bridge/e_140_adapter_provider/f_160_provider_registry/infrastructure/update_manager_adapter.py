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
    """One provider's built-in update binding: which manager, which package, which bin.

    ``bin_name`` is the key the package's own manifest maps to the executable this lane
    runs. It is part of the binding rather than derived from the provider id because the
    two are independent facts: a package may ship several bins, or one whose name differs
    from the provider token, and guessing that mapping is exactly the kind of proxy
    (j#95741 F2) this module exists to stop using.
    """

    manager: str
    package: str
    bin_name: str


#: The closed manager registry. The argv is a literal here and nowhere else.
_BUILTIN_MANAGERS: dict[str, _ManagerQuery] = {
    # `npm root -g` prints the global node_modules directory — the directory
    # `npm install -g <pkg>` writes into. Read-only: it reports a path and installs
    # nothing.
    "npm": _ManagerQuery(command="npm", argv=("root", "-g")),
}

#: The closed provider->update binding registry. Adding a provider is a source edit.
_PROVIDER_UPDATE_BINDINGS: dict[str, _UpdateBinding] = {
    "codex": _UpdateBinding(manager="npm", package="@openai/codex", bin_name="codex"),
}

#: Which of a provider's DECLARED startup blockers mean "an update is happening here".
#:
#: The ids are the ``startup_blockers[].id`` tokens in ``agent_provider_profiles.yaml``.
#: This mapping lives here, beside the manager binding, for the same reason (Answer j#96167
#: item 2): "screen X means this provider is updating" is provider-specific knowledge, and
#: the launch/relaunch consumers must keep seeing a typed cause, never a provider name or a
#: screen id. Closed and source-edit-only like every other registry in this module.
#:
#: The correspondence with the profile data is NOT self-enforcing — an id renamed in the
#: YAML would silently stop matching here, which is a fail-OPEN drift of exactly the kind
#: the blocker schema's own bounds exist to prevent. It is pinned instead by a regression
#: that asserts this mapping and the shipped profiles agree in BOTH directions.
_PROVIDER_UPDATE_BLOCKERS: dict[str, frozenset] = {
    "codex": frozenset({"update_prompt_available", "update_in_progress"}),
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


# --- Exact executable identity (Design Answer j#96872 item 2) ---------------------------

#: A manifest larger than this is not the small `package.json` we are reading; refuse
#: rather than parse an arbitrarily large file found at a path we expected a manifest at.
MAX_MANIFEST_BYTES = 1_000_000
#: A version longer than this is not a version. Bounded so a malformed manifest cannot put
#: an unbounded string into a digest input.
MAX_VERSION_LEN = 128

#: The manifest could not be opened / read / decoded as a JSON object.
REASON_MANIFEST_UNREADABLE = "manifest_unreadable"
#: The manifest parsed, but its shape is not the strict one this resolver requires.
REASON_MANIFEST_MALFORMED = "manifest_malformed"
#: The manifest's own `name` is not the package this binding is about.
REASON_PACKAGE_NAME_MISMATCH = "package_name_mismatch"
#: No canonical, non-empty `version`.
REASON_VERSION_UNUSABLE = "version_unusable"
#: `bin` does not map this binding's bin name to a usable target inside the package.
REASON_BIN_UNUSABLE = "bin_unusable"
#: The managed exec target is not, exactly, the bin the updater's package owns.
REASON_EXEC_TARGET_MISMATCH = "exec_target_mismatch"

#: Digest scheme tag. Bumping it invalidates every stored receipt by construction, which is
#: the intended behaviour if the identity definition ever changes.
_IDENTITY_DIGEST_SCHEME = "mzb1"


@dataclass(frozen=True)
class ProviderIdentity:
    """An exact provider executable identity, as an OPAQUE digest.

    ``digest`` is non-empty only when ``resolved``. It is a one-way function of the exact
    bin realpath AND the manifest version, so two identities compare equal iff both halves
    are equal — and neither half can be recovered from it. That is deliberate: the whole
    point of carrying an identity is to compare it and to store it, never to report it, and
    a stored path is a stored path however carefully a caller promises not to print it
    (Design Answer j#96872 item 2: "path/version/env は outcome・journal へ出さない").

    ``reason`` is a fixed token, safe on a durable record.
    """

    digest: str = ""
    resolved: bool = False
    reason: str = REASON_PROVIDER_UNREGISTERED


def _identity_digest(bin_realpath: str, version: str) -> str:
    """The opaque identity token for one (exact bin, exact version) pair."""
    import hashlib

    # NUL-separated so no (path, version) pair can be re-spelled as another one by moving
    # the boundary — a path may legally contain almost anything except NUL.
    payload = f"{bin_realpath}\0{version}".encode("utf-8", "surrogatepass")
    return f"{_IDENTITY_DIGEST_SCHEME}:{hashlib.sha256(payload).hexdigest()}"


def _contained(child: str, parent: str) -> bool:
    """True iff ``child`` is ``parent`` or sits under it (both already realpath-resolved).

    Compared on the path-component boundary, so ``/a/bc`` is not read as being inside
    ``/a/b`` — a prefix test would, and a sibling directory whose name merely starts with
    the package's is precisely the confusion this containment check exists to prevent.
    """
    if child == parent:
        return True
    return child.startswith(parent.rstrip(os.sep) + os.sep)


def _read_manifest(manifest_path: str) -> Tuple[Optional[dict], str]:
    """Read + decode the manifest under strict bounds (never raises)."""
    import json

    try:
        if not os.path.isfile(manifest_path):
            # Deliberately `isfile`, so a directory / fifo / device at this path is not
            # opened. A manifest is a regular file or it is not a manifest.
            return (None, REASON_MANIFEST_UNREADABLE)
        if os.path.getsize(manifest_path) > MAX_MANIFEST_BYTES:
            return (None, REASON_MANIFEST_UNREADABLE)
        with open(manifest_path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except Exception:  # noqa: BLE001 - unreadable / undecodable / not utf-8 / not JSON
        return (None, REASON_MANIFEST_UNREADABLE)
    if not isinstance(document, dict):
        return (None, REASON_MANIFEST_MALFORMED)
    return (document, REASON_OK)


def _manifest_version(document: dict) -> Tuple[str, str]:
    """The canonical, bounded, non-empty ``version`` string, or a typed reason."""
    version = document.get("version")
    if not isinstance(version, str):
        return ("", REASON_VERSION_UNUSABLE)
    version = version.strip()
    if not version or len(version) > MAX_VERSION_LEN:
        return ("", REASON_VERSION_UNUSABLE)
    if any(ch.isspace() or not ch.isprintable() for ch in version):
        # Whitespace or a control character inside a version means the manifest is not
        # saying what it appears to say. Refuse rather than normalise it into a digest.
        return ("", REASON_VERSION_UNUSABLE)
    return (version, REASON_OK)


def _manifest_bin_target(document: dict, package_dir: str, bin_name: str) -> Tuple[str, str]:
    """The exact realpath the manifest's ``bin`` maps ``bin_name`` to, or a typed reason.

    ``bin`` may be a bare string (the single-bin form, which npm names after the package)
    or an object. Both are accepted, and both must resolve to a REGULAR FILE inside the
    package directory: a bin pointing outside its own package is not this package's
    identity, whatever the manifest claims.
    """
    declared = document.get("bin")
    if isinstance(declared, str):
        # The single-bin form is named after the package's own (unscoped) name.
        expected = str(document.get("name") or "").split("/")[-1]
        if expected != bin_name:
            return ("", REASON_BIN_UNUSABLE)
        relative = declared
    elif isinstance(declared, dict):
        relative = declared.get(bin_name)
    else:
        return ("", REASON_BIN_UNUSABLE)
    if not isinstance(relative, str) or not relative.strip():
        return ("", REASON_BIN_UNUSABLE)
    target = os.path.realpath(os.path.join(package_dir, relative.strip()))
    if not _contained(target, package_dir) or not os.path.isfile(target):
        return ("", REASON_BIN_UNUSABLE)
    return (target, REASON_OK)


def resolve_provider_identity(
    provider_id: str,
    env: Optional[Mapping[str, str]] = None,
    *,
    exec_target: str = "",
    runner: Optional[Runner] = None,
) -> ProviderIdentity:
    """Resolve the exact identity of the executable ``provider_id``'s updater owns.

    Design Answer j#96872 item 2, and the answer to "where does a version come from without
    running the provider?". It comes from the package manager's own manifest: ``npm root
    -g`` already tells us the global prefix, the package directory is inside it, and
    ``package.json`` states the name, the version, and which file the bin is. Reading a
    manifest is not executing a provider — no provider argv is ever built, nothing is
    spawned but the manager's existing read-only ``root -g`` query, and there is no repo
    script, plugin, or operator-supplied command anywhere in this path.

    Resolves ONLY when every link corresponds: the manager answers, the package directory
    is inside the answered root, the manifest is a small regular JSON object, its ``name``
    is exactly this binding's package, its ``version`` is canonical and non-empty, its
    ``bin`` maps this binding's bin name to a regular file inside the package, and — when
    ``exec_target`` is supplied — that file is EXACTLY the realpath the managed launch
    runs. Anything else is a typed refusal with an empty digest, which every caller must
    treat as zero-actuation.

    Passing no ``exec_target`` asks a different, legitimate question: "what does the
    updater's package currently own?" — the *bound* half of the relaunch comparison
    (j#96872 item 5). Passing one asks "is what I am about to launch that same thing?".
    """
    env = os.environ if env is None else env

    binding = _PROVIDER_UPDATE_BINDINGS.get(str(provider_id or "").strip())
    if binding is None:
        return ProviderIdentity(reason=REASON_PROVIDER_UNREGISTERED)

    # Reuse the SAME positively-resolved package directory the authority axis uses, so the
    # two axes can never disagree about which install they are describing.
    target = resolve_updater_target(provider_id, env, runner=runner)
    if not target.resolved or not target.roots:
        return ProviderIdentity(reason=target.reason)
    package_dir = target.roots[0]

    document, reason = _read_manifest(os.path.join(package_dir, "package.json"))
    if document is None:
        return ProviderIdentity(reason=reason)
    if str(document.get("name") or "").strip() != binding.package:
        return ProviderIdentity(reason=REASON_PACKAGE_NAME_MISMATCH)

    version, reason = _manifest_version(document)
    if not version:
        return ProviderIdentity(reason=reason)

    bin_target, reason = _manifest_bin_target(document, package_dir, binding.bin_name)
    if not bin_target:
        return ProviderIdentity(reason=reason)

    if exec_target:
        if os.path.realpath(str(exec_target).strip()) != bin_target:
            # The managed launch runs a different file than the updater's package owns.
            # This is the #14741 split, stated as an identity rather than as containment.
            return ProviderIdentity(reason=REASON_EXEC_TARGET_MISMATCH)

    return ProviderIdentity(
        digest=_identity_digest(bin_target, version), resolved=True, reason=REASON_OK
    )


def is_update_derived_blocker(provider_id: str, blocker_id: str) -> bool:
    """True iff ``blocker_id`` is a screen that means ``provider_id`` is updating.

    The one provider-specific question the relaunch composition root asks before it decides
    a launch is update-derived (Design Answer j#96374 item 2). Everything the caller learns
    is a boolean, so no consumer sees a screen id, a provider name, or a package manager.

    Unregistered provider, unknown id, blank input — all False. False here means only "this
    observation does not establish an update", never "no update is happening": the caller
    maps it to :data:`...LAUNCH_CAUSE_GENERIC_FRESH`, which is the *unarmed* case and
    therefore byte-invariant with the pre-#14741 launch, exactly as Q1 ruled. This is the
    one place in the #14741 fence where the absence of a signal is not a refusal, and it is
    deliberate: arming a launch that nobody tied to an update is what regressed 210 tests.
    """
    declared = _PROVIDER_UPDATE_BLOCKERS.get(str(provider_id or "").strip())
    if not declared:
        return False
    return str(blocker_id or "").strip() in declared


def update_derived_blocker_ids(provider_id: str) -> frozenset:
    """The declared update-derived blocker ids for ``provider_id`` (empty when none).

    Exposed for the profile-correspondence regression: the drift this guards against is
    invisible at runtime, so the check needs to read the mapping rather than probe it one
    id at a time.
    """
    return _PROVIDER_UPDATE_BLOCKERS.get(str(provider_id or "").strip(), frozenset())


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
    "MAX_MANIFEST_BYTES",
    "MAX_VERSION_LEN",
    "ProviderIdentity",
    "REASON_BIN_UNUSABLE",
    "REASON_EXEC_TARGET_MISMATCH",
    "REASON_MANIFEST_MALFORMED",
    "REASON_MANIFEST_UNREADABLE",
    "REASON_PACKAGE_NAME_MISMATCH",
    "REASON_VERSION_UNUSABLE",
    "resolve_provider_identity",
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
    "is_supported_provider",
    "is_update_derived_blocker",
    "resolve_updater_target",
    "update_derived_blocker_ids",
)
