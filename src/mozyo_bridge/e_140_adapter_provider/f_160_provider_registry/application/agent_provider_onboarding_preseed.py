"""Place a provider's first-run onboarding defaults before it starts (Redmine #15744).

The use case behind the ``onboarding_seed`` profile block
(:mod:`...domain.agent_provider_onboarding_seed`). It resolves the provider's own config
document from the trusted environment, and — only when the provider's own declared
first-run defaults are not yet honored — writes them, so the provider boots straight to
a composer instead of an onboarding screen.

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

The schema is what keeps that distinction from eroding: a seed may only declare a key on
the exact known-safe allowlist (never a credential, a login state, or a trust /
permission acceptance), so the screens that matter (``login_required``,
``workspace_trust_confirmation``, ``directory_trust_confirmation``) remain
operator-resolved no matter what a future profile edit tries to add.

**Non-destructive by construction.** What "already onboarded" means is the domain's
:func:`...agent_provider_onboarding_seed.evaluate_onboarding_completion` (review
j#108680 finding_completionstateaspresence, verdict j#108694): a document whose
completion FLAGS are all exactly ``True`` is complete and is not opened for writing at
all — byte-identical, formatting and mtime included. A document whose flags are not
honored (absent, ``False``, ``None``, a non-``true`` scalar) is seeded on every
unsatisfied key, while an operator's own non-empty string value (their ``theme``) is
never overwritten. That contract is what this module is built around, not an
optimisation.

**Filesystem access goes through a port.** The use case holds the decision flow and the
typed outcomes; every filesystem side effect is behind
:class:`OnboardingDocumentFilesystem` (a ``typing.Protocol``), with
:class:`LocalOnboardingDocumentFilesystem` as the injected-by-default live adapter
(review j#108680 finding_filesystemportboundary, verdict j#108694 — the
object-oriented-architecture-policy port/adapter boundary). Unit tests express the
decision flow against a fake port; the adapter's own semantics — the race-free
``os.link`` create-new and the atomic ``os.replace`` — are asserted against real temp
directories.

**Honest limit on concurrency.** A fresh document is created race-free (a temp file
linked into place, which fails rather than clobbers if another writer won). Merging into
an EXISTING document is atomic in the filesystem sense (``os.replace``, so no reader ever
sees a torn file) but is a read-modify-write, so a provider process writing the same
document in the same instant could have its update overwritten. That window is only
reachable while the document exists AND still fails the completion evaluation — i.e.
while a provider is mid-onboarding, which is precisely the state a managed launch is not
supposed to be racing. It is not closed by a lock here because the provider does not
take one this code could share; naming the limit is more useful than a lock that would
imply a mutual exclusion that does not exist.

**Never raises; the caller decides what a failure costs.** Every outcome is a typed,
value-free token. The caller is the startup wrapper, and since verdict j#108694
(finding_seedfailurenotgatingexec) a ``failed`` outcome REFUSES the provider exec there
— a broken seed path surfaces as a typed startup failure instead of a silent
first-run-UI stall — so the tokens this module returns are launch-gating and must stay
exact.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Optional, Protocol

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_onboarding_seed import (  # noqa: E501
    OnboardingSeedDeclaration,
    OnboardingSeedDocument,
    evaluate_onboarding_completion,
)

#: The provider declares no seed (Codex today), or is not a registered provider at all.
#: The byte-invariant outcome: nothing was read, nothing was written.
SEED_STATUS_NOT_DECLARED = "not_declared"
#: Every completion flag was already exactly honored. **No write was attempted** — the
#: already-onboarded config is byte-identical, mtime included.
SEED_STATUS_ALREADY_COMPLETE = "already_complete"
#: At least one declared default was unsatisfied and the document now carries it.
SEED_STATUS_SEEDED = "seeded"
#: The seed could not be applied. Since verdict j#108694 the wrapper REFUSES the
#: provider exec on this token (typed startup failure), so a broken seed path can never
#: degrade into the silent first-run-UI stall #15722 j#108276 documented.
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


@dataclass(frozen=True)
class OnboardingDocumentStat:
    """The two facts the seed needs about an existing document (value object).

    ``owner_is_caller`` is the foreign-owner boundary: writing another account's
    provider config is outside what a managed launch may do, whatever the filesystem
    permits. ``mode`` (permission bits only) is carried onto a replacement so a seed
    never widens, and never narrows, what the operator chose for the file.
    """

    owner_is_caller: bool
    mode: int


class OnboardingDocumentFilesystem(Protocol):
    """Filesystem port for the onboarding pre-seed (review j#108680
    finding_filesystemportboundary, verdict j#108694).

    Exactly the operations the use case performs, no more: existence probing for
    document selection, reading a document's text, reading its ownership + mode,
    ensuring its base directory, the race-free create-new, and the atomic replace.
    The decision flow (JSON parsing, completion evaluation, typed reason mapping)
    stays in the use case; adapters signal failure with the ``OSError`` family —
    ``FileNotFoundError`` for an absent document on read, ``FileExistsError`` for a
    lost create-new race — and the use case maps those to the typed ``SEED_REASON_*``
    tokens, so a fake port expresses a failure the same way the real filesystem does.
    """

    def document_exists(self, path: str) -> bool:
        """Whether a candidate document exists at ``path`` (document selection)."""
        ...

    def read_document_text(self, path: str) -> str:
        """``path``'s text (UTF-8). Raises ``FileNotFoundError`` when absent."""
        ...

    def stat_document(self, path: str) -> OnboardingDocumentStat:
        """Ownership + permission bits of the existing document at ``path``."""
        ...

    def ensure_base_directory(self, base: str, mode: int) -> None:
        """Make sure directory ``base`` exists (created dirs get ``mode``)."""
        ...

    def create_new_document(self, path: str, text: str, mode: int) -> None:
        """Create ``path`` with ``text`` and ``mode``, atomically and never clobbering.

        Raises ``FileExistsError`` when another writer got there first (the caller
        re-reads and merges into what actually landed).
        """
        ...

    def replace_document(self, path: str, text: str, mode: int) -> None:
        """Atomically replace ``path``'s contents with ``text``, setting ``mode``."""
        ...


class LocalOnboardingDocumentFilesystem:
    """The live :class:`OnboardingDocumentFilesystem` adapter (real local filesystem).

    Holds the two write idioms whose exactness the contract depends on:

    - **create-new** writes a temp file in the target directory and ``os.link``s it
      into place — the atomic-create idiom (equivalent to an ``O_EXCL`` create), which
      fails with ``FileExistsError`` if another writer won rather than overwriting
      whatever they wrote. A filesystem that refuses ``os.link`` surfaces as a generic
      ``OSError`` instead of being papered over with a clobbering ``os.replace``,
      because silently overwriting a document another process just created is the one
      outcome the idiom exists to prevent.
    - **replace** writes a temp file and ``os.replace``s it over the target, so a
      concurrent reader sees either the old document or the new one and never a torn
      file.
    """

    def document_exists(self, path: str) -> bool:
        return os.path.exists(path)

    def read_document_text(self, path: str) -> str:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def stat_document(self, path: str) -> OnboardingDocumentStat:
        stat = os.stat(path)
        return OnboardingDocumentStat(
            owner_is_caller=stat.st_uid == os.geteuid(),
            mode=stat.st_mode & 0o7777,
        )

    def ensure_base_directory(self, base: str, mode: int) -> None:
        if os.path.isdir(base):
            return
        os.makedirs(base, mode=mode, exist_ok=True)
        if not os.path.isdir(base):
            raise NotADirectoryError(base)

    def create_new_document(self, path: str, text: str, mode: int) -> None:
        self._write_via_temp(path, text, mode, link_new=True)

    def replace_document(self, path: str, text: str, mode: int) -> None:
        self._write_via_temp(path, text, mode, link_new=False)

    @staticmethod
    def _write_via_temp(path: str, text: str, mode: int, *, link_new: bool) -> None:
        """Write ``text`` to a same-directory temp file and land it on ``path``."""
        base = os.path.dirname(path) or "."
        handle = None
        temp_path = ""
        try:
            fd, temp_path = tempfile.mkstemp(
                dir=base, prefix=".mozyo-onboarding-seed-"
            )
            handle = os.fdopen(fd, "w", encoding="utf-8")
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
            handle.close()
            handle = None
            os.chmod(temp_path, mode)
            if link_new:
                os.link(temp_path, path)
            else:
                os.replace(temp_path, path)
                temp_path = ""
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


#: The default injected adapter: existing callers (the startup wrapper) stay
#: source-compatible and hit the real filesystem, which is the only one that exists at
#: launch time. Stateless, so one shared instance is safe.
_LOCAL_FILESYSTEM = LocalOnboardingDocumentFilesystem()


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


def _read_document(
    filesystem: OnboardingDocumentFilesystem, path: str
) -> "tuple[Optional[dict], str]":
    """Read one JSON document: ``(mapping, reason)`` with exactly one meaningful half.

    ``(None, "")`` means the document does not exist — the create path. A non-empty
    reason means it exists but must not be rewritten.
    """
    try:
        text = filesystem.read_document_text(path)
    except FileNotFoundError:
        return None, ""
    except (OSError, UnicodeDecodeError):
        return None, SEED_REASON_DOCUMENT_UNREADABLE
    try:
        parsed = json.loads(text)
    except ValueError:
        return None, SEED_REASON_DOCUMENT_UNREADABLE
    if not isinstance(parsed, dict):
        return None, SEED_REASON_DOCUMENT_NOT_MAPPING
    return parsed, ""


def _select_document(
    declaration: OnboardingSeedDeclaration,
    env: Mapping[str, str],
    home: str,
    filesystem: OnboardingDocumentFilesystem,
) -> "Optional[tuple[OnboardingSeedDocument, str]]":
    """The declared document to seed and its absolute path, or ``None`` unprobeable.

    The first candidate that already EXISTS wins, mirroring how the provider resolves
    its own config: seeding a different file than the one the provider reads would
    report success while changing nothing. When no candidate exists, the single
    ``create_when_absent`` document is the one a fresh install receives.
    """
    for document in declaration.documents:
        path = resolve_document_path(document, env, home)
        try:
            exists = filesystem.document_exists(path)
        except OSError:
            # Review j#108770 finding_filesystemportexistenceerrorescapes: an
            # unprobeable candidate (e.g. PermissionError on the parent) must
            # surface as the caller's typed failed outcome — NOT fall through to
            # the create path, which would seed a different document than the one
            # the provider may actually read.
            return None
        if exists:
            return document, path
    creatable = declaration.creatable_document
    return creatable, resolve_document_path(creatable, env, home)


def _render_document(document_body: dict) -> str:
    """The exact text a seeded document carries (the pre-port byte shape, unchanged)."""
    return json.dumps(document_body, indent=2) + "\n"


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
    filesystem: OnboardingDocumentFilesystem = _LOCAL_FILESYSTEM,
) -> OnboardingSeedOutcome:
    """Place ``provider_id``'s declared first-run defaults, idempotently. Never raises.

    Returns a typed :class:`OnboardingSeedOutcome`. The three non-failure shapes are all
    normal: ``not_declared`` (this provider has no seed — byte-invariant),
    ``already_complete`` (every completion flag already honored, and nothing written),
    and ``seeded``. A ``failed`` outcome gates the launch at the caller (verdict
    j#108694), so the tokens are exact contract, not telemetry.

    ``env`` is the environment the provider will inherit; the caller passes
    ``os.environ`` from inside the launched process, which is the only place the
    provider's real ``HOME`` / config-relocation variables are truthfully readable — the
    same reason the identity self-attestation runs there (#13637).

    ``filesystem`` is the port every side effect goes through; the default is the real
    local adapter so the wrapper's call is unchanged.
    """
    declaration = _lookup_declaration(provider_id, profile_lookup)
    if declaration is None:
        return OnboardingSeedOutcome(status=SEED_STATUS_NOT_DECLARED)

    home = _resolve_home(env)
    if not home:
        return OnboardingSeedOutcome(
            status=SEED_STATUS_FAILED, reason=SEED_REASON_HOME_UNRESOLVED
        )

    selected = _select_document(declaration, env, home, filesystem)
    if selected is None:
        # A candidate could not even be probed for existence (review j#108770):
        # fail typed instead of guessing which document the provider reads.
        return OnboardingSeedOutcome(
            status=SEED_STATUS_FAILED, reason=SEED_REASON_DOCUMENT_UNREADABLE
        )
    document, path = selected
    declared = declaration.completion_key_map

    # One retry: the create path can lose a race to another writer, and the honest
    # response is to re-read what actually landed and merge into it rather than to
    # report a failure the filesystem already resolved.
    for _attempt in (0, 1):
        body, reason = _read_document(filesystem, path)
        if reason:
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=reason,
                document_id=document.document_id,
            )
        if body is None:
            try:
                filesystem.ensure_base_directory(
                    os.path.dirname(path) or ".", _CREATED_BASE_MODE
                )
            except OSError:
                return OnboardingSeedOutcome(
                    status=SEED_STATUS_FAILED,
                    reason=SEED_REASON_BASE_UNUSABLE,
                    document_id=document.document_id,
                )
            try:
                filesystem.create_new_document(
                    path, _render_document(dict(declared)), _CREATED_DOCUMENT_MODE
                )
            except FileExistsError:
                # Lost the create race — go around and merge into what the winner wrote.
                continue
            except (OSError, ValueError, TypeError):
                return OnboardingSeedOutcome(
                    status=SEED_STATUS_FAILED,
                    reason=SEED_REASON_WRITE_FAILED,
                    document_id=document.document_id,
                )
            return OnboardingSeedOutcome(
                status=SEED_STATUS_SEEDED,
                document_id=document.document_id,
                seeded_keys=tuple(declared),
            )

        evaluation = evaluate_onboarding_completion(declared, body)
        if evaluation.complete:
            # The whole non-destructive contract: a document whose completion flags are
            # all honored is not opened for writing, so it stays byte-identical down to
            # its mtime — even when a non-flag default (theme) is absent (verdict
            # j#108694 semantics).
            return OnboardingSeedOutcome(
                status=SEED_STATUS_ALREADY_COMPLETE, document_id=document.document_id
            )
        try:
            stat = filesystem.stat_document(path)
        except OSError:
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=SEED_REASON_DOCUMENT_UNREADABLE,
                document_id=document.document_id,
            )
        if not stat.owner_is_caller:
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=SEED_REASON_FOREIGN_OWNER,
                document_id=document.document_id,
            )
        merged = dict(body)
        merged.update(dict(evaluation.unsatisfied_keys))
        try:
            filesystem.replace_document(path, _render_document(merged), stat.mode)
        except (OSError, ValueError, TypeError):
            return OnboardingSeedOutcome(
                status=SEED_STATUS_FAILED,
                reason=SEED_REASON_WRITE_FAILED,
                document_id=document.document_id,
            )
        return OnboardingSeedOutcome(
            status=SEED_STATUS_SEEDED,
            document_id=document.document_id,
            seeded_keys=tuple(sorted(key for key, _v in evaluation.unsatisfied_keys)),
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
    "LocalOnboardingDocumentFilesystem",
    "OnboardingDocumentFilesystem",
    "OnboardingDocumentStat",
    "OnboardingSeedOutcome",
    "preseed_provider_onboarding",
    "resolve_document_path",
)
