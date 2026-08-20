"""Place a provider's first-run onboarding defaults before it starts (Redmine #15744).

The use case behind the ``onboarding_seed`` profile block
(:mod:`...domain.agent_provider_onboarding_seed`). It resolves the provider's own config
document from the trusted environment, and — only when the provider's own declared
first-run defaults are ABSENT — writes them, so the provider boots straight to a
composer instead of an onboarding screen.

WHY THIS EXISTS. #13760 taught the launch layer to recognise pre-composer startup
screens and refuse to send into them. That made the failure honest but did not remove
it: #15722 j#108276 sat on ``first_run_theme`` until an operator answered it by hand,
and a whole wave stopped behind that one pane. Recognition is the wrong end of the
problem for a screen that is entirely predictable before the process starts.

WHY IT IS NOT AUTO-ANSWERING A PROMPT. The boundary #13760 drew — mozyo never answers a
provider's UI — is untouched here, and the difference is not a matter of degree:

- an auto-answer acts on a *rendered question*, after the provider has asked the
  operator something, and its keystroke is indistinguishable from the operator's;
- a seed acts on a *config document*, before any process is running, writing values the
  provider itself documents as its defaults.

The schema is what keeps that distinction from eroding: a seed may not declare a
credential, a login state, or a trust / permission acceptance, so the screens that
matter (``login_required``, ``workspace_trust_confirmation``,
``directory_trust_confirmation``) remain operator-resolved no matter what a future
profile edit tries to add.

**Non-destructive by construction.** A key that is already present is never rewritten,
and when NO declared key is missing the document is not opened for writing at all — so
an already-onboarded operator's config is byte-identical after a managed launch,
including its formatting and its mtime. That is the acceptance criterion this module is
built around, not an optimisation.

**Honest limit on concurrency.** A fresh document is created race-free (a temp file
linked into place, which fails rather than clobbers if another writer won). Merging into
an EXISTING document is atomic in the filesystem sense (``os.replace``, so no reader ever
sees a torn file) but is a read-modify-write, so a provider process writing the same
document in the same instant could have its update overwritten. That window is only
reachable while the document exists AND still lacks a declared key — i.e. while a
provider is mid-onboarding, which is precisely the state a managed launch is not
supposed to be racing. It is not closed by a lock here because the provider does not
take one this code could share; naming the limit is more useful than a lock that would
imply a mutual exclusion that does not exist.

**Never raises, never blocks the boot.** Every outcome is a typed, value-free token. The
caller is the startup wrapper, and a config that cannot be seeded must degrade to "the
operator may see an onboarding screen", never to a dead pane.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_onboarding_seed import (  # noqa: E501
    OnboardingSeedDeclaration,
    OnboardingSeedDocument,
)

#: The provider declares no seed (Codex today), or is not a registered provider at all.
#: The byte-invariant outcome: nothing was read, nothing was written.
SEED_STATUS_NOT_DECLARED = "not_declared"
#: Every declared default was already present. **No write was attempted** — the
#: already-onboarded config is byte-identical, mtime included.
SEED_STATUS_ALREADY_COMPLETE = "already_complete"
#: At least one declared default was missing and the document now carries it.
SEED_STATUS_SEEDED = "seeded"
#: The seed could not be applied. The provider still launches; the operator may meet the
#: onboarding screen, which is the pre-#15744 behavior and never worse than it.
SEED_STATUS_FAILED = "failed"

#: No usable absolute home directory in the passed environment, so no document location
#: can be composed. Never guessed from the ambient process state.
SEED_REASON_HOME_UNRESOLVED = "home_unresolved"
#: The resolved base directory could not be created or is not a directory.
SEED_REASON_BASE_UNUSABLE = "base_unusable"
#: The document exists but could not be read or is not valid JSON. Fail closed: a
#: document this code cannot parse is one it must not rewrite.
SEED_REASON_DOCUMENT_UNREADABLE = "document_unreadable"
#: The document parsed but is not a JSON object, so it carries no top-level keys to seed.
SEED_REASON_DOCUMENT_NOT_MAPPING = "document_not_mapping"
#: The document belongs to another user. Writing another account's provider config is
#: outside what a managed launch may do, whatever the filesystem permits.
SEED_REASON_FOREIGN_OWNER = "foreign_owner"
#: The write itself failed (permissions, a full or read-only filesystem, ...).
SEED_REASON_WRITE_FAILED = "write_failed"

#: Mode for a config document this code CREATES. A provider's global config later
#: accumulates account state, so it is owner-only from the moment it exists rather than
#: inheriting whatever the process umask happened to be. An EXISTING document's mode is
#: preserved untouched — the seed never widens, and never narrows, what the operator set.
_CREATED_DOCUMENT_MODE = 0o600

#: Mode for a base directory this code creates, for the same reason.
_CREATED_BASE_MODE = 0o700

#: Private sentinel: the race-free create lost to another writer, so the document now
#: exists and the caller should re-read and merge. Deliberately NOT one of the public
#: ``SEED_REASON_*`` tokens — it never reaches an outcome, because it describes a step
#: this module recovers from rather than a state a caller has to reason about.
_RACE_LOST = "race_lost"


@dataclass(frozen=True)
class OnboardingSeedOutcome:
    """What the pre-seed did, as closed tokens plus the keys it added.

    ``seeded_keys`` names only keys the *profile itself declares*, so reporting them
    discloses nothing about the operator's configuration beyond the fact that mozyo
    supplied a default it had already committed. The document's own contents never
    appear here, in a log, or in a durable record.
    """

    status: str
    reason: str = ""
    document_id: str = ""
    seeded_keys: tuple[str, ...] = ()

    @property
    def changed(self) -> bool:
        """Whether this outcome wrote to the provider's config document."""
        return self.status == SEED_STATUS_SEEDED


def resolve_document_path(
    document: OnboardingSeedDocument, env: Mapping[str, str], home: str
) -> str:
    """Compose one candidate document's absolute path (pure; no I/O).

    ``base_env``, when set in ``env`` to a non-empty value, replaces the home-relative
    base entirely — that is what a provider's config-relocation variable means. It is
    read from the passed mapping, never from the ambient process environment, so the
    caller decides which environment this launch is being evaluated against.
    """
    override = (env.get(document.base_env) or "").strip() if document.base_env else ""
    if override:
        base = override
    elif document.base_home_relative:
        base = os.path.join(home, *document.base_home_relative.split("/"))
    else:
        base = home
    return os.path.join(base, document.filename)


def _resolve_home(env: Mapping[str, str]) -> str:
    """The absolute home directory this launch's environment declares, or ``""``.

    Deliberately env-only. The wrapper runs as the process the provider will become, so
    ``env`` is exactly what the provider will inherit; falling back to the ambient user
    database would resolve a different home than the provider reads and seed a file
    nothing consumes.
    """
    home = (env.get("HOME") or "").strip()
    return home if home and os.path.isabs(home) else ""


def _read_document(path: str) -> "tuple[Optional[dict], str]":
    """Read one JSON document: ``(mapping, reason)`` with exactly one meaningful half.

    ``(None, "")`` means the document does not exist — the create path. A non-empty
    reason means it exists but must not be rewritten.
    """
    try:
        with open(path, "r", encoding="utf-8") as handle:
            parsed = json.load(handle)
    except FileNotFoundError:
        return None, ""
    except (OSError, ValueError, UnicodeDecodeError):
        return None, SEED_REASON_DOCUMENT_UNREADABLE
    if not isinstance(parsed, dict):
        return None, SEED_REASON_DOCUMENT_NOT_MAPPING
    return parsed, ""


def _select_document(
    declaration: OnboardingSeedDeclaration, env: Mapping[str, str], home: str
) -> "tuple[OnboardingSeedDocument, str]":
    """The declared document to seed, and its absolute path.

    The first candidate that already EXISTS wins, mirroring how the provider resolves
    its own config: seeding a different file than the one the provider reads would
    report success while changing nothing. When no candidate exists, the single
    ``create_when_absent`` document is the one a fresh install receives.
    """
    for document in declaration.documents:
        path = resolve_document_path(document, env, home)
        if os.path.exists(path):
            return document, path
    creatable = declaration.creatable_document
    return creatable, resolve_document_path(creatable, env, home)


def _ensure_base(path: str) -> str:
    """Make sure the document's parent directory exists; return a failure reason or ``""``."""
    base = os.path.dirname(path) or "."
    if os.path.isdir(base):
        return ""
    try:
        os.makedirs(base, mode=_CREATED_BASE_MODE, exist_ok=True)
    except OSError:
        return SEED_REASON_BASE_UNUSABLE
    return "" if os.path.isdir(base) else SEED_REASON_BASE_UNUSABLE


def _write_new_document(path: str, document_body: dict) -> str:
    """Create ``path`` with ``document_body``, refusing to clobber. Returns a reason or ``""``.

    Race-free by construction: the body is written to a temp file in the same directory
    and ``os.link``ed into place, which fails with ``FileExistsError`` if another writer
    got there first rather than overwriting whatever they wrote. The caller then falls
    back to the merge path and re-reads what actually landed. Returns :data:`_RACE_LOST`
    in that case — the one reason here that is not a caller-visible failure.

    ``os.link`` within a single directory is the atomic-create idiom; a filesystem that
    refuses it reaches the generic write failure rather than being papered over with a
    clobbering ``os.replace``, because silently overwriting a document another process
    just created is the one outcome this function exists to prevent.
    """
    base = os.path.dirname(path) or "."
    handle = None
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(dir=base, prefix=".mozyo-onboarding-seed-")
        handle = os.fdopen(fd, "w", encoding="utf-8")
        json.dump(document_body, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(temp_path, _CREATED_DOCUMENT_MODE)
        os.link(temp_path, path)
    except FileExistsError:
        return _RACE_LOST
    except (OSError, ValueError, TypeError):
        return SEED_REASON_WRITE_FAILED
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return ""


def _replace_document(path: str, document_body: dict, preserve_mode: int) -> str:
    """Atomically replace ``path``'s contents, keeping its existing mode. Reason or ``""``.

    ``os.replace`` is atomic, so a concurrent reader sees either the old document or the
    new one and never a partial write. The existing mode is carried onto the replacement
    so a seed cannot widen (or narrow) what the operator chose for the file.
    """
    base = os.path.dirname(path) or "."
    handle = None
    temp_path = ""
    try:
        fd, temp_path = tempfile.mkstemp(dir=base, prefix=".mozyo-onboarding-seed-")
        handle = os.fdopen(fd, "w", encoding="utf-8")
        json.dump(document_body, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        handle = None
        os.chmod(temp_path, preserve_mode)
        os.replace(temp_path, path)
        temp_path = ""
    except (OSError, ValueError, TypeError):
        return SEED_REASON_WRITE_FAILED
    finally:
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        if temp_path:
            try:
                os.unlink(temp_path)
            except OSError:
                pass
    return ""


def _lookup_declaration(
    provider_id: str, profile_lookup: Optional[Callable[[str], Any]]
) -> Optional[OnboardingSeedDeclaration]:
    """This provider's declared seed, or ``None`` for "nothing to do" (never raises)."""
    lookup = profile_lookup
    if lookup is None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (  # noqa: E501
            require_profile,
        )

        lookup = require_profile
    try:
        profile = lookup(provider_id)
    except Exception:  # noqa: BLE001 — an unknown provider is "no seed", not a failure
        return None
    declaration = getattr(profile, "onboarding_seed", None)
    return declaration if isinstance(declaration, OnboardingSeedDeclaration) else None


def preseed_provider_onboarding(
    provider_id: str,
    env: Mapping[str, str],
    *,
    profile_lookup: Optional[Callable[[str], Any]] = None,
) -> OnboardingSeedOutcome:
    """Place ``provider_id``'s declared first-run defaults, idempotently. Never raises.

    Returns a typed :class:`OnboardingSeedOutcome`. The three non-failure shapes are all
    normal: ``not_declared`` (this provider has no seed — byte-invariant),
    ``already_complete`` (nothing missing, and nothing written), and ``seeded``.

    ``env`` is the environment the provider will inherit; the caller passes
    ``os.environ`` from inside the launched process, which is the only place the
    provider's real ``HOME`` / config-relocation variables are truthfully readable — the
    same reason the identity self-attestation runs there (#13637).
    """
    declaration = _lookup_declaration(provider_id, profile_lookup)
    if declaration is None:
        return OnboardingSeedOutcome(status=SEED_STATUS_NOT_DECLARED)

    home = _resolve_home(env)
    if not home:
        return OnboardingSeedOutcome(
            status=SEED_STATUS_FAILED, reason=SEED_REASON_HOME_UNRESOLVED
        )

    document, path = _select_document(declaration, env, home)
    declared = declaration.completion_key_map

    # One retry: the create path can lose a race to another writer, and the honest
    # response is to re-read what actually landed and merge into it rather than to
    # report a failure the filesystem already resolved.
    for _attempt in (0, 1):
        body, reason = _read_document(path)
        if reason:
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=reason,
                document_id=document.document_id,
            )
        if body is None:
            base_reason = _ensure_base(path)
            if base_reason:
                return OnboardingSeedOutcome(
                    status=SEED_STATUS_FAILED,
                    reason=base_reason,
                    document_id=document.document_id,
                )
            write_reason = _write_new_document(path, dict(declared))
            if not write_reason:
                return OnboardingSeedOutcome(
                    status=SEED_STATUS_SEEDED,
                    document_id=document.document_id,
                    seeded_keys=tuple(declared),
                )
            if write_reason != _RACE_LOST:
                return OnboardingSeedOutcome(
                    status=SEED_STATUS_FAILED,
                    reason=write_reason,
                    document_id=document.document_id,
                )
            # Lost the create race — go around and merge into what the winner wrote.
            continue

        missing = {key: value for key, value in declared.items() if key not in body}
        if not missing:
            # The whole non-destructive contract: an already-onboarded document is not
            # opened for writing, so it stays byte-identical down to its mtime.
            return OnboardingSeedOutcome(
                status=SEED_STATUS_ALREADY_COMPLETE, document_id=document.document_id
            )
        try:
            stat = os.stat(path)
        except OSError:
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=SEED_REASON_DOCUMENT_UNREADABLE,
                document_id=document.document_id,
            )
        if stat.st_uid != os.geteuid():
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=SEED_REASON_FOREIGN_OWNER,
                document_id=document.document_id,
            )
        merged = dict(body)
        merged.update(missing)
        write_reason = _replace_document(path, merged, stat.st_mode & 0o7777)
        if write_reason:
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=write_reason,
                document_id=document.document_id,
            )
        return OnboardingSeedOutcome(
            status=SEED_STATUS_SEEDED,
            document_id=document.document_id,
            seeded_keys=tuple(sorted(missing)),
        )

    # Both attempts lost the create race, which means the document exists and something
    # else is writing it very actively. Report the gap rather than loop.
    return OnboardingSeedOutcome(
        status=SEED_STATUS_FAILED,
        reason=SEED_REASON_WRITE_FAILED,
        document_id=document.document_id,
    )


__all__ = (
    "SEED_REASON_BASE_UNUSABLE",
    "SEED_REASON_DOCUMENT_NOT_MAPPING",
    "SEED_REASON_DOCUMENT_UNREADABLE",
    "SEED_REASON_FOREIGN_OWNER",
    "SEED_REASON_HOME_UNRESOLVED",
    "SEED_REASON_WRITE_FAILED",
    "SEED_STATUS_ALREADY_COMPLETE",
    "SEED_STATUS_FAILED",
    "SEED_STATUS_NOT_DECLARED",
    "SEED_STATUS_SEEDED",
    "OnboardingSeedOutcome",
    "preseed_provider_onboarding",
    "resolve_document_path",
)
