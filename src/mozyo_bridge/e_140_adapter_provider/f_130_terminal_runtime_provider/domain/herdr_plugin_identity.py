"""What a herdr plugin **is**: source identity + a normalized observation (Redmine #14619).

The lower half of the managed-lane plugin policy, carved out of
:mod:`...herdr_plugin_policy` when that module crossed the module-health ceiling
(a cohesive split, not an allowlist entry). The cut is along a real seam rather
than a line count:

- **this module** answers *what a plugin is* — where its code came from
  (:class:`PluginSourceRef`), and what a ``herdr plugin list --json`` record says
  about it once every third-party value has been normalized
  (:class:`PluginObservation`). It makes no admission judgement and knows nothing
  about capability classes, reviews, or reasons;
- **:mod:`...herdr_plugin_policy`** answers *what is admitted*, and imports this
  module. The dependency arrow points one way only.

The disclosure boundary lives here, because this is where third-party data stops
being third-party data. herdr's payload carries three absolute operator-home
paths (``manifest_path`` / ``plugin_root`` / ``source.managed_path``) and two
free-text fields, so :class:`PluginObservation` is a **closed representation**:
every field is either core-owned vocabulary or a validated segment, and
``__post_init__`` checks that rather than trusting it. Review j#92053 finding 1
measured what the weaker version was worth — "no field is *meant* to hold a path"
let ``version`` and ``source_kind`` carry one straight into a report.

Pure: no file IO, no subprocess, no network.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

#: The one source kind herdr resolves to an immutable commit.
SOURCE_KIND_GITHUB = "github"
#: A locally linked directory (``herdr plugin link``): no upstream identity at all.
SOURCE_KIND_LINK = "link"
#: The record carried no source at all.
SOURCE_KIND_ABSENT = "absent"
#: The record named a source kind outside this vocabulary. The **raw value is not
#: kept** — see :func:`normalize_source_kind`.
SOURCE_KIND_UNRECOGNIZED = "unrecognized"

#: The closed set an observation may report. A source kind is third-party text, so
#: it is *projected* onto this vocabulary rather than echoed.
OBSERVED_SOURCE_KINDS: frozenset[str] = frozenset(
    {
        SOURCE_KIND_GITHUB,
        SOURCE_KIND_LINK,
        SOURCE_KIND_ABSENT,
        SOURCE_KIND_UNRECOGNIZED,
    }
)

#: What an unusable version string is reported as. Not the raw value.
VERSION_UNRECOGNIZED = "unrecognized"

_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")
#: A version we are willing to echo: the ordinary version alphabet, bounded. It
#: admits no ``/``, ``\``, ``:``, or whitespace, so no value that passes can be a
#: filesystem path or a URL.
_VERSION_RE = re.compile(r"\A[0-9A-Za-z.+_-]{1,64}\Z")


def normalize_source_kind(raw: object) -> str:
    """Project a record's source kind onto :data:`OBSERVED_SOURCE_KINDS` (never echo it).

    The kind is a string a third party wrote. Review j#92053 finding 1 measured the
    consequence of passing it through: a record naming its kind
    ``"<an absolute path>"`` put that path straight into the report, because the
    field was only checked for *being* a string. Projecting instead of echoing
    removes the whole class — an unrecognized kind reports as
    :data:`SOURCE_KIND_UNRECOGNIZED`, and the raw value is discarded here rather
    than redacted later.

    The diagnostic cost is stated plainly: a report cannot say *which*
    unrecognized kind was seen. Only ``github`` is ever acted on, so the lost
    detail decides nothing.
    """
    if raw is None:
        return SOURCE_KIND_ABSENT
    if raw == SOURCE_KIND_GITHUB:
        return SOURCE_KIND_GITHUB
    if raw == SOURCE_KIND_LINK:
        return SOURCE_KIND_LINK
    return SOURCE_KIND_UNRECOGNIZED


def normalize_version(raw: object) -> str:
    """Return a version safe to echo, or :data:`VERSION_UNRECOGNIZED` (review j#92053 F1).

    A version is display-only — identity is the commit pin — but it is authored by
    the plugin, so an arbitrary string could reach the report. It is *not* rejected
    as a malformed record: an odd version is cosmetic, and (since finding 2's fix
    makes a malformed record block enable planning) treating it as malformed would
    let a cosmetic oddity block an operator's admin question. Instead the value is
    kept only when it looks like a version, and replaced by a closed token when it
    does not.
    """
    if isinstance(raw, str) and _VERSION_RE.match(raw):
        return raw
    return VERSION_UNRECOGNIZED


class HerdrPluginPolicyError(ValueError):
    """A plugin record, review entry, or registry is unreadable / self-contradictory.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    adapter-boundary errors (``HerdrPinPostureError`` / ``ProviderRegistryError``).
    """


def require_segment(value: object, field: str) -> str:
    """Return ``value`` as a path-free identifier segment, or fail closed.

    Type is checked before shape: ``isinstance(value, str)`` first, so a non-string
    can never reach a regex that would coerce it. The character class excludes ``/``
    and whitespace, so an owner / repo segment can never smuggle a path or a second
    path component into an identity.
    """
    if not isinstance(value, str):
        raise HerdrPluginPolicyError(
            f"{field} must be a string, got {type(value).__name__}"
        )
    if not _SEGMENT_RE.match(value):
        raise HerdrPluginPolicyError(
            f"{field} {value!r} is not a bare identifier segment "
            f"(letters, digits, '.', '_', '-')"
        )
    return value


#: In-module alias; the public spelling is ``require_segment``.
_require_segment = require_segment


@dataclass(frozen=True)
class PluginSourceRef:
    """Where a plugin's code came from: a repository, optionally at an exact commit.

    ``commit`` is ``None`` for a *repository-scoped* reference — the shape a
    deny-classification uses, and the shape an allow may never use (see the module
    docstring). When present it must be a full 40-character lowercase hex commit:
    an abbreviated commit is not an identity, because a prefix can match more than
    one object and cannot be compared for equality against a full one.
    """

    kind: str
    owner: str
    repo: str
    commit: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind != SOURCE_KIND_GITHUB:
            raise HerdrPluginPolicyError(
                f"source kind {self.kind!r} is not a pinnable upstream identity; "
                f"only {SOURCE_KIND_GITHUB!r} resolves to an immutable commit"
            )
        _require_segment(self.owner, "source owner")
        _require_segment(self.repo, "source repo")
        if self.commit is not None:
            if not isinstance(self.commit, str):
                raise HerdrPluginPolicyError(
                    f"source commit must be a string, got {type(self.commit).__name__}"
                )
            if not _COMMIT_RE.match(self.commit):
                raise HerdrPluginPolicyError(
                    "source commit must be a full 40-character lowercase hex commit; "
                    "an abbreviated commit is not an identity"
                )

    @classmethod
    def repository(cls, kind: str, owner: str, repo: str) -> "PluginSourceRef":
        """A repository-scoped reference (no commit) — deny-classifications only."""
        return cls(kind=kind, owner=owner, repo=repo)

    @classmethod
    def pinned(cls, kind: str, owner: str, repo: str, commit: str) -> "PluginSourceRef":
        """A reference pinned to an exact immutable commit."""
        return cls(kind=kind, owner=owner, repo=repo, commit=commit)

    @property
    def repo_key(self) -> "PluginSourceRef":
        """This reference with the commit dropped — the repository-scoped key."""
        if self.commit is None:
            return self
        return PluginSourceRef(kind=self.kind, owner=self.owner, repo=self.repo)

    @property
    def is_pinned(self) -> bool:
        return self.commit is not None

    def describe(self) -> str:
        """A short, path-free description (``github:owner/repo@commit``)."""
        base = f"{self.kind}:{self.owner}/{self.repo}"
        return f"{base}@{self.commit}" if self.commit else base


def source_ref_from_parts(
    kind: object, owner: object, repo: object, commit: object
) -> Optional[PluginSourceRef]:
    """Build the strongest reference these parts support (review j#92053 F3).

    **Repository identity and pin validity are separate facts, so a bad commit may
    not discard the repository.** The original version collapsed them: any
    :class:`HerdrPluginPolicyError` — including one raised solely by a malformed
    commit — returned ``None``, throwing away a perfectly good ``owner/repo``.
    Measured consequence: ``persiyanov/herdr-reviewr`` observed with an
    *abbreviated* commit classified as ``unknown`` / ``unpinned_source`` instead of
    ``agent_input_writer``. The final answer was still a denial, but the class and
    the reason were both untrue — and defeating exactly the property the
    repository-scoped deny exists to provide (the module docstring's "a newer
    commit does not stop doing so"). A deny whose reason is wrong is a deny that
    stops explaining itself.

    So: a valid ``github`` owner/repo yields at least a repository-scoped
    reference; the commit only decides whether that reference is *also* pinned.
    ``None`` is returned only when there is no usable repository identity at all —
    a non-``github`` kind, or an owner / repo that is not a bare segment.

    This is the single builder both entry points use — the observed inventory
    (:func:`read_source_ref`) and an operator-named candidate — so the two can
    never disagree about what a reference is. They already had: the candidate path
    fell back to a repository reference while the observed path did not.
    """
    if kind != SOURCE_KIND_GITHUB:
        return None
    try:
        repository = PluginSourceRef.repository(SOURCE_KIND_GITHUB, owner, repo)
    except HerdrPluginPolicyError:
        return None
    try:
        return PluginSourceRef.pinned(SOURCE_KIND_GITHUB, owner, repo, commit)
    except HerdrPluginPolicyError:
        return repository


def read_source_ref(source: object) -> Optional[PluginSourceRef]:
    """Read the strongest :class:`PluginSourceRef` a herdr ``source`` record supports.

    Returns ``None`` only when there is no usable repository identity (a missing or
    non-mapping ``source``, a non-``github`` kind, or a malformed owner / repo). A
    record with a good repository but an unusable commit yields a *repository-scoped*
    reference, not ``None`` — see :func:`source_ref_from_parts`. The caller turns a
    missing or unpinned reference into :data:`REASON_UNPINNED_SOURCE`; it is a
    denial, not an error, because an unpinnable source is a legitimate thing to
    observe and report.
    """
    if not isinstance(source, Mapping):
        return None
    return source_ref_from_parts(
        source.get("kind"),
        source.get("owner"),
        source.get("repo"),
        source.get("resolved_commit"),
    )


@dataclass(frozen=True)
class PluginObservation:
    """A herdr plugin as observed, normalized to path-free facts.

    Deliberately holds no ``manifest_path`` / ``plugin_root`` / ``managed_path``:
    the three absolute operator-home paths in herdr's payload have nowhere to go,
    so no formatter can leak one. ``declares_build`` / ``declares_panes`` /
    ``declares_actions`` record only *whether* the local manifest declares each
    surface — never the commands, which are third-party strings that can embed a
    private path.
    """

    plugin_id: str
    version: str
    enabled: bool
    source_kind: str
    ref: Optional[PluginSourceRef]
    declares_build: bool
    declares_panes: bool
    declares_actions: bool

    def __post_init__(self) -> None:
        # The closed-representation invariant, checked rather than trusted: every
        # field here is either core-owned vocabulary or a validated segment, so no
        # third-party free text survives into a report. Review j#92053 finding 1
        # measured what "no field is *meant* to hold a path" was worth without this
        # check — `version` and `source_kind` held whatever the plugin wrote.
        if self.source_kind not in OBSERVED_SOURCE_KINDS:
            raise HerdrPluginPolicyError(
                f"source_kind {self.source_kind!r} is outside the observed "
                f"vocabulary {sorted(OBSERVED_SOURCE_KINDS)}"
            )
        if self.version != VERSION_UNRECOGNIZED and not _VERSION_RE.match(self.version):
            raise HerdrPluginPolicyError(
                "version must be a bounded version-shaped token or "
                f"{VERSION_UNRECOGNIZED!r}"
            )
        _require_segment(self.plugin_id, "plugin_id")


def _require_bool(record: "Mapping[object, object]", key: str) -> bool:
    """Read a strict boolean, refusing ``bool``-adjacent values.

    ``bool`` is a subclass of ``int``, so an ``isinstance(value, int)`` check would
    accept ``1`` here and, worse, treat it as ``True``. The enabled flag decides
    whether a denied plugin is a live breach, so it is type-checked exactly.
    """
    value = record.get(key)
    if not isinstance(value, bool):
        raise HerdrPluginPolicyError(
            f"plugin record field {key!r} must be a boolean, got "
            f"{type(value).__name__}"
        )
    return value


def _declares(record: "Mapping[object, object]", key: str) -> bool:
    """Whether the manifest declares a non-empty list under ``key`` (fail-closed).

    An absent key is "does not declare". A present key that is not a list is a
    malformed record rather than an absence — refusing to read it is not the same
    as reading it as empty.
    """
    if key not in record:
        return False
    value = record[key]
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise HerdrPluginPolicyError(
            f"plugin record field {key!r} must be a list, got {type(value).__name__}"
        )
    return len(value) > 0


def observe_plugin(record: object) -> PluginObservation:
    """Normalize one ``herdr plugin list --json`` plugin record (fail-closed).

    Raises :class:`HerdrPluginPolicyError` when the record cannot be read as a
    plugin at all. A record that reads fine but carries no pinned source is *not*
    an error: it yields ``ref=None`` and is denied downstream as
    :data:`REASON_UNPINNED_SOURCE`.
    """
    if not isinstance(record, Mapping):
        raise HerdrPluginPolicyError(
            f"plugin record must be a mapping, got {type(record).__name__}"
        )
    source = record.get("source")
    return PluginObservation(
        plugin_id=_require_segment(record.get("plugin_id"), "plugin_id"),
        version=normalize_version(record.get("version")),
        enabled=_require_bool(record, "enabled"),
        source_kind=normalize_source_kind(
            source.get("kind") if isinstance(source, Mapping) else None
        ),
        ref=read_source_ref(source),
        declares_build=_declares(record, "build"),
        declares_panes=_declares(record, "panes"),
        declares_actions=_declares(record, "actions"),
    )


__all__ = (
    "OBSERVED_SOURCE_KINDS",
    "SOURCE_KIND_ABSENT",
    "SOURCE_KIND_GITHUB",
    "SOURCE_KIND_LINK",
    "SOURCE_KIND_UNRECOGNIZED",
    "VERSION_UNRECOGNIZED",
    "HerdrPluginPolicyError",
    "PluginObservation",
    "PluginSourceRef",
    "normalize_source_kind",
    "normalize_version",
    "observe_plugin",
    "read_source_ref",
    "require_segment",
    "source_ref_from_parts",
)
