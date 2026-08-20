"""Provider onboarding pre-seed declaration schema (Redmine #15744).

A provider's *first-run onboarding UI* is a startup screen that renders INSTEAD OF a
composer. :mod:`.agent_provider_startup_blocker` declares how to RECOGNISE such a screen
after the fact (#13760); this module declares what a managed launch may legitimately put
in place BEFORE the provider starts so the screen never renders at all.

The two are deliberately different boundaries, and this one is the narrower:

- ``startup_blockers`` is a *classifier* — it observes a live pane and refuses to send.
  Declaring a screen there never authorises answering it, and auto-accepting a trust
  prompt is explicitly out of scope (#13760 境界).
- ``onboarding_seed`` is a *pre-launch config default* — it writes the provider's own
  documented onboarding-completion flag / UI default into the provider's own config
  document while no provider process is showing anything. It never types into a pane,
  never answers a rendered dialog, and never accepts a trust or permission boundary on
  the operator's behalf. That is why a seed is admissible where an auto-answer is not:
  a default written before the question is asked is configuration; a keystroke sent
  after it is asked impersonates the operator.

WHY THIS IS DATA AND NOT CODE (the #13441 j#76725 ruling, restated). The launch layer
must not learn that "Claude" has an onboarding flag. It asks the profile whether the
provider declares a seed and applies whatever the profile declares — so a provider that
renames its config file or its completion key is a data edit, and a provider that
declares nothing (Codex today) leaves the managed launch byte-invariant.

WHAT THIS BLOCK MAY NEVER CARRY (fail-closed in the schema, not merely by convention):

- **A credential, token, API key, or account identity.** A seed exists to skip a
  cosmetic first-run question, never to install an authentication state the operator
  did not establish. ``login_required`` stays an operator-resolved blocker.
- **A trust / permission acceptance.** ``workspace_trust_confirmation`` and
  ``directory_trust_confirmation`` are the boundary deciding whether the provider may
  read, edit, and execute files in a workspace. Pre-answering that from committed data
  is exactly the auto-accept #13760 refused, so a key naming it is rejected here rather
  than left to reviewer vigilance.
- **A nested structure or a host path.** Values are scalars only, so a seed can never
  reach into a per-project sub-document (which is where a provider keeps its trust
  acceptances) and can never smuggle a filesystem location through a value.

The document location is likewise declared as *components*, never as a path: an env
variable NAME, a home-relative base, and a single filename. The absolute path is
composed at launch from the trusted environment, so committed data can no more name a
host path here than it can in ``executable`` (#13245 hostile-checkout boundary).

Kept in its own module (out of the near-threshold ``agent_provider_profile_config`` —
the module-health gate) for the reason ``agent_provider_startup_blocker`` was: it is a
cohesive, self-contained schema. Dependency direction matches that precedent too — this
is a **leaf** that borrows the shared :class:`AgentProviderProfileError` lazily, inside
the single factory that raises, so the config module can import it at top level without
a cycle.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence, Union

#: The value types a seed may carry. Scalars only — a mapping / list value would let a
#: seed reach a provider's nested per-project sub-document, which is where trust
#: acceptances live, and a float has no place in a UI default.
OnboardingSeedValue = Union[bool, str, int]

#: Upper bound on declared documents. A provider resolves its config from a small,
#: ordered candidate list (a relocation env var plus a default); a longer list is a sign
#: the declaration is being used to search the filesystem rather than name a contract.
MAX_SEED_DOCUMENTS = 4

#: Upper bound on seeded keys. A first-run seed sets a completion flag and, at most, the
#: few UI defaults the question would have asked for. A long list means the block is
#: being used to configure the provider, which is the operator's job.
MAX_SEED_KEYS = 8

#: Maximum length of a declared string value. Long enough for a theme / mode token,
#: short enough that the block cannot carry a payload.
MAX_SEED_VALUE_LEN = 64

#: Schema versions whose shape predates ``onboarding_seed`` (the v4 addition). An
#: explicit set, not a ``<`` comparison, for the reason the startup_blockers and
#: ghost_composer_signals gates use one: a future ``"10"`` must not be mis-ordered
#: against ``"4"``.
VERSIONS_WITHOUT_ONBOARDING_SEED: frozenset[str] = frozenset({"1", "2", "3"})

#: The version a profile is nominally on when it may carry ``onboarding_seed``.
ONBOARDING_SEED_MIN_VERSION = "4"

#: A POSIX-shaped environment variable NAME (never its value). The posture
#: ``TrustedExecutable.env_override`` takes: committed data names the variable, the
#: trusted environment supplies what it points at.
_ENV_NAME_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

#: A declared key must be a plain configuration identifier. Excluding separators and
#: dots keeps a key from addressing a nested path (``projects.foo.trust``) through a
#: provider that resolves dotted keys.
_SEED_KEY_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_-]*\Z")

#: Document ids are tokens a typed outcome may name, so they follow the same shape.
_DOCUMENT_ID_RE = re.compile(r"\A[a-z_][a-z0-9_]*\Z")

#: The exact, exhaustive set of keys a seed may install — the fence that DECIDES
#: (Redmine #15744 review j#108680 finding_semantickeyfencebypassable, verdict j#108694).
#: The first shape of this fence was the substring blacklist below, and the review
#: demonstrated it admits authorisation-shaped keys that carry none of the substrings
#: (``loggedIn``, ``termsAccepted``, ``accountIdentity``). A blacklist can only refuse
#: the vocabularies its author foresaw; the reviewable claim a profile makes by
#: declaring a key is "this is a known-safe first-run UI default", and only an
#: enumeration of the known-safe keys can hold that claim. So the fence is now an
#: EXACT allowlist and the default is deny: a key absent from this set is refused,
#: whatever it looks like. Growing it is deliberately a reviewed source diff, never a
#: data-only profile edit. Matched case-sensitively — the provider's config keys are
#: case-sensitive, and admitting a respelling would admit a key the provider does not
#: actually read.
ALLOWED_SEED_KEYS: frozenset[str] = frozenset({"hasCompletedOnboarding", "theme"})

#: Substrings that make a key an authentication or authorisation state rather than a UI
#: default. Matched case-insensitively against the whole key, so ``oauthAccount``,
#: ``primaryApiKey``, ``hasTrustDialogAccepted``, and their spellings in a future
#: provider are all refused without this schema enumerating provider vocabularies.
#:
#: Since verdict j#108694 this set is an *additional guard*, kept for its specific
#: error messages and as a tripwire on future allowlist growth (an allowlisted key that
#: ever matched one of these families would be refused here first, in a reviewed diff's
#: face); :data:`ALLOWED_SEED_KEYS` is what decides admission. It deliberately
#: over-refuses (``key`` also catches ``keychain`` / ``keyboardShortcuts``), which is
#: harmless under a default-deny allowlist.
FORBIDDEN_SEED_KEY_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "apikey",
        "auth",
        "credential",
        "key",
        "oauth",
        "password",
        "permission",
        "secret",
        "token",
        "trust",
    }
)

#: Keys refused outright regardless of substring: a provider's per-project sub-document
#: holds trust acceptances and workspace grants, so the container itself is never
#: seedable even though its name carries none of the substrings above.
FORBIDDEN_SEED_KEYS: frozenset[str] = frozenset({"projects"})

_DOCUMENT_KEYS: frozenset[str] = frozenset(
    {"id", "base_env", "base_home_relative", "filename", "create_when_absent"}
)

_SEED_KEYS: frozenset[str] = frozenset({"documents", "completion_keys"})


def _seed_error(message: str) -> Exception:
    """Build the shared profile error (lazy import; see the module docstring).

    Every rejection in this module is a profile-load failure, so it must be the same
    exception type every other block raises — a caller that catches
    :class:`AgentProviderProfileError` around a profile parse would otherwise miss a
    malformed seed and let a partially-understood provider contract load.
    """
    from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (  # noqa: E501
        AgentProviderProfileError,
    )

    return AgentProviderProfileError(message)


def _reject_traversal(relative: str, document_id: str) -> None:
    """Fail closed on a base that escapes, or ignores, the resolved home directory."""
    if not relative:
        return
    if relative.startswith("/") or relative.startswith("\\") or ":" in relative:
        raise _seed_error(
            f"onboarding_seed document {document_id!r} base_home_relative "
            f"{relative!r} must be relative to the resolved home directory; an absolute "
            f"base would let committed data name a host path"
        )
    parts = [part for part in relative.replace("\\", "/").split("/") if part]
    if any(part == ".." for part in parts):
        raise _seed_error(
            f"onboarding_seed document {document_id!r} base_home_relative "
            f"{relative!r} traverses above the resolved home directory"
        )


@dataclass(frozen=True)
class OnboardingSeedDocument:
    """One candidate config document, declared as components rather than a path.

    The absolute location is ``(<base_env> from the environment, else
    <home>/<base_home_relative>) / <filename>``. Two documents may legitimately share a
    ``base_env`` and still resolve to different bases: a provider that lets one variable
    relocate its config often anchors different documents at different default depths
    under the home directory, and reproducing that faithfully is the difference between
    a seed that lands and one that silently writes beside the file the provider reads.

    ``create_when_absent`` marks the document to CREATE when no candidate exists.
    Exactly one document carries it, so "which file does a fresh install get" is a
    declared fact rather than an ordering accident.
    """

    document_id: str
    base_env: str
    base_home_relative: str
    filename: str
    create_when_absent: bool

    def __post_init__(self) -> None:
        if not _DOCUMENT_ID_RE.match(self.document_id or ""):
            raise _seed_error(
                f"onboarding_seed document id {self.document_id!r} must be a lowercase "
                f"identifier token; it is reported verbatim by a typed launch outcome"
            )
        if self.base_env and not _ENV_NAME_RE.match(self.base_env):
            raise _seed_error(
                f"onboarding_seed document {self.document_id!r} base_env "
                f"{self.base_env!r} must be an environment variable NAME; committed data "
                f"names the variable and the trusted environment supplies its value (the "
                f"#13245 boundary `executable.env_override` holds to)"
            )
        _reject_traversal(self.base_home_relative, self.document_id)
        if not self.filename or "/" in self.filename or "\\" in self.filename:
            raise _seed_error(
                f"onboarding_seed document {self.document_id!r} filename "
                f"{self.filename!r} must be a single path component; a declaration names "
                f"a file inside a resolved base, never a path of its own"
            )
        if self.filename in (".", ".."):
            raise _seed_error(
                f"onboarding_seed document {self.document_id!r} filename "
                f"{self.filename!r} is a directory reference, not a config document"
            )
        if not isinstance(self.create_when_absent, bool):
            raise _seed_error(
                f"onboarding_seed document {self.document_id!r} 'create_when_absent' "
                f"must be a boolean, got {type(self.create_when_absent).__name__}"
            )

    @classmethod
    def from_record(
        cls, record: object, *, provider_id: str
    ) -> "OnboardingSeedDocument":
        """Validate one already-parsed document entry, rejecting unknown keys."""
        if not isinstance(record, Mapping):
            raise _seed_error(
                f"agent provider profile {provider_id!r} onboarding_seed document must "
                f"be a mapping, got {type(record).__name__}"
            )
        unknown = set(record) - _DOCUMENT_KEYS
        if unknown:
            raise _seed_error(
                f"unknown key(s) {sorted(map(repr, unknown))} in agent provider profile "
                f"{provider_id!r} onboarding_seed document; allowed: "
                f"{sorted(_DOCUMENT_KEYS)}"
            )
        missing = {"id", "filename"} - set(record)
        if missing:
            raise _seed_error(
                f"agent provider profile {provider_id!r} onboarding_seed document is "
                f"missing required key(s) {sorted(missing)}"
            )
        for field_name in ("id", "base_env", "base_home_relative", "filename"):
            value = record.get(field_name, "")
            if value is not None and not isinstance(value, str):
                raise _seed_error(
                    f"agent provider profile {provider_id!r} onboarding_seed document "
                    f"{field_name!r} must be a string, got {type(value).__name__}"
                )
        # `create_when_absent` is handed through UNCOERCED (review j#108680
        # finding_createflagtypecoercion, verdict j#108694): the earlier
        # ``bool(record.get(...))`` turned the YAML/JSON string ``"false"`` into True —
        # truthiness inverting the declared meaning — and made ``__post_init__``'s own
        # strict isinstance check unreachable. The raw value now reaches that check, so
        # anything but an actual bool (or absence, defaulting to False) fails closed
        # with the typed profile error.
        return cls(
            document_id=record["id"],
            base_env=record.get("base_env") or "",
            base_home_relative=record.get("base_home_relative") or "",
            filename=record["filename"],
            create_when_absent=record.get("create_when_absent", False),
        )


def _validate_seed_key(key: object, *, provider_id: str) -> str:
    """Return ``key`` when it is a seedable UI-default identifier; else fail closed."""
    if not isinstance(key, str) or not key:
        raise _seed_error(
            f"agent provider profile {provider_id!r} onboarding_seed completion_keys key "
            f"must be a non-empty string, got {key!r}"
        )
    if not _SEED_KEY_RE.match(key):
        raise _seed_error(
            f"agent provider profile {provider_id!r} onboarding_seed completion_keys key "
            f"{key!r} must be a plain configuration identifier; separators and dots are "
            f"refused so a key can never address a nested sub-document"
        )
    folded = key.casefold()
    if folded in FORBIDDEN_SEED_KEYS:
        raise _seed_error(
            f"agent provider profile {provider_id!r} may not seed {key!r}: a provider's "
            f"per-project sub-document holds workspace trust acceptances and grants, and "
            f"a managed launch never establishes those on the operator's behalf "
            f"(Redmine #13760 境界, restated by #15744)"
        )
    for forbidden in sorted(FORBIDDEN_SEED_KEY_SUBSTRINGS):
        if forbidden in folded:
            raise _seed_error(
                f"agent provider profile {provider_id!r} may not seed {key!r}: the key "
                f"names an authentication or authorisation state ({forbidden!r}), and an "
                f"onboarding seed installs a first-run UI default only. A credential or a "
                f"trust acceptance stays an operator-resolved startup blocker "
                f"(Redmine #13760 境界, restated by #15744)"
            )
    if key not in ALLOWED_SEED_KEYS:
        # The deciding fence (review j#108680 finding_semantickeyfencebypassable,
        # verdict j#108694): default deny. The guards above give the credential / trust
        # families their specific refusals; everything else — including an
        # authorisation-shaped key that dodges every substring (``loggedIn``,
        # ``termsAccepted``, ``accountIdentity``) — stops here.
        raise _seed_error(
            f"agent provider profile {provider_id!r} may not seed {key!r}: only the "
            f"known-safe first-run UI defaults {sorted(ALLOWED_SEED_KEYS)} are seedable. "
            f"Admission is an exact allowlist (default deny); a new UI default must be "
            f"added to ALLOWED_SEED_KEYS in a reviewed source diff, never smuggled in "
            f"through a data-only profile edit (Redmine #15744 verdict j#108694)"
        )
    return key


def _validate_seed_value(
    key: str, value: object, *, provider_id: str
) -> OnboardingSeedValue:
    """Return ``value`` when it is an admissible scalar; else fail closed.

    ``bool`` is checked before ``int`` because it is an ``int`` subclass in Python and
    the distinction is observable in the JSON the seed writes (``true`` vs ``1``): a
    provider comparing its completion flag with a strict equality would not accept the
    latter, so a declaration that means ``true`` must round-trip as ``true``.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if len(value) > MAX_SEED_VALUE_LEN:
            raise _seed_error(
                f"agent provider profile {provider_id!r} onboarding_seed value for "
                f"{key!r} is {len(value)} characters; the bound is {MAX_SEED_VALUE_LEN}. "
                f"A UI default is a short token, and a long value is a payload"
            )
        return value
    raise _seed_error(
        f"agent provider profile {provider_id!r} onboarding_seed value for {key!r} must "
        f"be a boolean, integer, or string, got {type(value).__name__}. A nested value "
        f"would let a seed reach a per-project sub-document"
    )


@dataclass(frozen=True)
class OnboardingSeedEvaluation:
    """One document's completion verdict against a declaration (pure value; no I/O).

    ``complete`` is the *do-not-open-for-writing* verdict: the non-destructive contract
    ("an already-onboarded config is byte-identical, mtime included") hangs off exactly
    this bit, so what "complete" means lives here in the domain, not in the writer.

    ``unsatisfied_keys`` are the declared ``(key, value)`` pairs the seed must place,
    in declaration order. Non-empty exactly when ``complete`` is False.
    """

    complete: bool
    unsatisfied_keys: tuple[tuple[str, OnboardingSeedValue], ...]


def _seed_key_satisfied(
    declared_value: OnboardingSeedValue, current_value: object, *, present: bool
) -> bool:
    """Whether one declared default is already honored by the document's current value.

    The per-key semantics fixed by verdict j#108694 (review j#108680
    finding_completionstateaspresence):

    - a **boolean-declared** key is satisfied only by the exact value ``True`` — the
      provider tests its completion flag strictly, so a present-but-``False`` / ``None``
      / ``1`` flag leaves the onboarding screen rendering exactly as an absent one does;
    - a **string-declared** key is satisfied by ANY existing non-empty string — the
      operator's own choice (a ``theme`` they picked) is respected and NEVER overwritten;
      only absence / ``None`` / ``""`` takes the declared default;
    - any other declared scalar (an int) is satisfied by presence, the pre-verdict
      semantics, because no stricter contract has been ruled for it.
    """
    if isinstance(declared_value, bool):
        return current_value is True
    if isinstance(declared_value, str):
        return isinstance(current_value, str) and current_value != ""
    return present


def evaluate_onboarding_completion(
    declared: Mapping[str, OnboardingSeedValue],
    body: Mapping[str, object],
) -> OnboardingSeedEvaluation:
    """Judge one parsed config document against the declared defaults (pure; no I/O).

    Redmine #15744 review j#108680 finding_completionstateaspresence, fixed per verdict
    j#108694. The first implementation asked only whether every declared KEY was
    PRESENT, which mistook ``{"hasCompletedOnboarding": false}`` — a provider state in
    which the onboarding screen absolutely renders — for "already onboarded" and
    returned the byte-invariant outcome while the symptom survived.

    Completion is decided by the **completion flags**: every declared key whose declared
    value is boolean ``True``. The document is complete iff ALL of them are already
    exactly ``True`` — and then it is complete *even when other declared keys (theme)
    are absent*, because a provider that has recorded onboarding as done never re-asks
    the theme question, so writing the remaining defaults would touch an operator's
    settled config for nothing.

    A declaration with NO boolean-``True`` key has no completion flag to read, so it
    falls back to the previous presence semantics: complete iff every declared key is
    present, and only the absent ones are unsatisfied.
    """
    flags = [key for key, value in declared.items() if value is True]
    if flags:
        if all(body.get(key) is True for key in flags):
            return OnboardingSeedEvaluation(complete=True, unsatisfied_keys=())
        unsatisfied = tuple(
            (key, value)
            for key, value in declared.items()
            if not _seed_key_satisfied(value, body.get(key), present=key in body)
        )
        return OnboardingSeedEvaluation(complete=False, unsatisfied_keys=unsatisfied)
    missing = tuple(
        (key, value) for key, value in declared.items() if key not in body
    )
    return OnboardingSeedEvaluation(
        complete=not missing, unsatisfied_keys=missing
    )


@dataclass(frozen=True)
class OnboardingSeedDeclaration:
    """A provider's complete pre-launch onboarding-seed contract (frozen, behavior-free).

    Holds no filesystem knowledge and performs no I/O: it says WHICH documents a provider
    resolves its config from and WHICH first-run defaults a managed launch may place
    there. Composing an absolute path and writing it belongs to the application layer
    (``...application.agent_provider_onboarding_preseed``), which is what keeps this
    record safe to evaluate at profile-load time.
    """

    documents: tuple[OnboardingSeedDocument, ...]
    completion_keys: tuple[tuple[str, OnboardingSeedValue], ...]

    def __post_init__(self) -> None:
        if not self.documents:
            raise _seed_error(
                "onboarding_seed must declare at least one document; a seed with no "
                "document to write is a contract that silently does nothing"
            )
        if len(self.documents) > MAX_SEED_DOCUMENTS:
            raise _seed_error(
                f"onboarding_seed declares {len(self.documents)} documents; the bound is "
                f"{MAX_SEED_DOCUMENTS}. A candidate list is a provider's declared "
                f"resolution order, not a filesystem search"
            )
        ids = [document.document_id for document in self.documents]
        if len(set(ids)) != len(ids):
            raise _seed_error(
                f"onboarding_seed declares duplicate document id(s) {sorted(ids)}; an id "
                f"is the token a typed outcome reports, so it must name exactly one "
                f"document"
            )
        creatable = [d for d in self.documents if d.create_when_absent]
        if len(creatable) != 1:
            raise _seed_error(
                f"onboarding_seed must mark exactly one document 'create_when_absent' "
                f"(got {len(creatable)}); which file a fresh install receives is a "
                f"declared fact, never an ordering accident"
            )
        if not self.completion_keys:
            raise _seed_error(
                "onboarding_seed must declare at least one completion key; a seed that "
                "writes nothing cannot close a first-run screen"
            )
        if len(self.completion_keys) > MAX_SEED_KEYS:
            raise _seed_error(
                f"onboarding_seed declares {len(self.completion_keys)} completion keys; "
                f"the bound is {MAX_SEED_KEYS}. Beyond a completion flag and the defaults "
                f"the first-run question would have asked for, configuring the provider "
                f"is the operator's job"
            )
        seen: set[str] = set()
        for key, _value in self.completion_keys:
            if key in seen:
                raise _seed_error(
                    f"onboarding_seed declares duplicate completion key {key!r}"
                )
            seen.add(key)

    @property
    def creatable_document(self) -> OnboardingSeedDocument:
        """The document to create when no declared candidate exists on disk."""
        return next(d for d in self.documents if d.create_when_absent)

    @property
    def completion_key_map(self) -> "dict[str, OnboardingSeedValue]":
        """The declared defaults as a plain ``{key: value}`` dict (a copy)."""
        return {key: value for key, value in self.completion_keys}

    @classmethod
    def from_record(
        cls, record: object, *, provider_id: str
    ) -> "OnboardingSeedDeclaration":
        """Validate an already-parsed ``onboarding_seed`` block, failing closed."""
        if not isinstance(record, Mapping):
            raise _seed_error(
                f"agent provider profile {provider_id!r} 'onboarding_seed' must be a "
                f"mapping of {{documents, completion_keys}}, got {type(record).__name__}"
            )
        unknown = set(record) - _SEED_KEYS
        if unknown:
            raise _seed_error(
                f"unknown key(s) {sorted(map(repr, unknown))} in agent provider profile "
                f"{provider_id!r} onboarding_seed; allowed: {sorted(_SEED_KEYS)}"
            )
        missing = _SEED_KEYS - set(record)
        if missing:
            raise _seed_error(
                f"agent provider profile {provider_id!r} onboarding_seed is missing "
                f"required key(s) {sorted(missing)}"
            )
        raw_documents = record["documents"]
        if isinstance(raw_documents, (str, bytes)) or not isinstance(
            raw_documents, Sequence
        ):
            raise _seed_error(
                f"agent provider profile {provider_id!r} onboarding_seed 'documents' "
                f"must be a list of document records, got {type(raw_documents).__name__}"
            )
        documents = tuple(
            OnboardingSeedDocument.from_record(entry, provider_id=provider_id)
            for entry in raw_documents
        )
        raw_keys = record["completion_keys"]
        if not isinstance(raw_keys, Mapping):
            raise _seed_error(
                f"agent provider profile {provider_id!r} onboarding_seed "
                f"'completion_keys' must be a mapping of key -> scalar default, got "
                f"{type(raw_keys).__name__}"
            )
        completion_keys = tuple(
            (
                _validate_seed_key(key, provider_id=provider_id),
                _validate_seed_value(str(key), value, provider_id=provider_id),
            )
            for key, value in raw_keys.items()
        )
        return cls(documents=documents, completion_keys=completion_keys)


def parse_onboarding_seed(
    record: Mapping[str, object],
    *,
    provider_id: str,
    schema_version: str,
) -> Optional[OnboardingSeedDeclaration]:
    """The declared seed for one profile entry, or ``None`` when it declares none.

    Also enforces the version lock-step the sibling optional blocks enforce: a field is
    honored only by an artifact whose own declared version says it has that field, so the
    documented "v4 adds onboarding_seed" contract and the shape the loader actually reads
    can never drift apart.

    ``None`` is the byte-invariant answer, not a degraded one: a provider that declares
    no seed (Codex today) leaves the managed launch exactly as it was before this field
    existed.
    """
    if "onboarding_seed" not in record:
        return None
    if schema_version in VERSIONS_WITHOUT_ONBOARDING_SEED:
        raise _seed_error(
            f"agent provider profile {provider_id!r} declares 'onboarding_seed' but the "
            f"config schema version is {schema_version!r}; that field was added in "
            f"version {ONBOARDING_SEED_MIN_VERSION!r}. Bump the artifact to version "
            f"{ONBOARDING_SEED_MIN_VERSION!r} to use onboarding_seed (Redmine #15744)."
        )
    return OnboardingSeedDeclaration.from_record(
        record["onboarding_seed"], provider_id=provider_id
    )


__all__ = (
    "ALLOWED_SEED_KEYS",
    "FORBIDDEN_SEED_KEYS",
    "FORBIDDEN_SEED_KEY_SUBSTRINGS",
    "MAX_SEED_DOCUMENTS",
    "MAX_SEED_KEYS",
    "MAX_SEED_VALUE_LEN",
    "ONBOARDING_SEED_MIN_VERSION",
    "VERSIONS_WITHOUT_ONBOARDING_SEED",
    "OnboardingSeedDeclaration",
    "OnboardingSeedDocument",
    "OnboardingSeedEvaluation",
    "OnboardingSeedValue",
    "evaluate_onboarding_completion",
    "parse_onboarding_seed",
)
