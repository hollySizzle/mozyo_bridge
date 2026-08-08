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
being third-party data. :class:`PluginObservation` is a **closed representation**:
every field is a core-owned value — a projected vocabulary token, a validated
reference, a strict boolean — except the plugin id, which is a *bounded*
identifier and is echoed only because it is the operand an operator types.
``__post_init__`` checks every field against a validator table and refuses to
construct a record whose field has no validator, so the guarantee does not depend
on anyone remembering to extend a hand-written check.

Two review rounds shaped that definition, and each correction is recorded because
each was a case of the claim outrunning the code:

- j#92053 F1 — "no field is *meant* to hold a path" was not a boundary. ``version``
  and ``source_kind`` were only checked for *being* strings and carried a private
  path straight into a report.
- j#92092 F1 — the replacement ``__post_init__`` hand-listed three checks while
  this docstring claimed all eight fields were closed. The five unchecked fields
  accepted arbitrary text (a path in ``declares_build`` reached the report) or
  raised a raw ``TypeError``.
- j#92092 F3 — narrowing ``version`` to a version-shaped alphabet still was not
  closed. **Closed means the value is one core owns, not one whose shape we
  constrained.** ``version`` is gone: identity is the commit pin, so nothing
  needed it.

Pure: no file IO, no subprocess, no network.
"""

from __future__ import annotations

import dataclasses
import re
from types import MappingProxyType

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.absolute_path_rule import (
    ABSOLUTE_ROOT_RE,
    RELATIVE_CONTINUATION_RE,
    contains_absolute_path,
    keeps_absolute_root,
)
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
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

#: What an operand we will not echo is rendered as. A closed token, never the value.
REDACTED_TOKEN = "<withheld>"

#: Upper bound on any identifier segment this module will echo. Review j#92092
#: found ``version`` echoed third-party text; the same class applied to
#: ``plugin_id``, which has no upper bound in its alphabet alone — a 5,000-character
#: id was accepted and rendered. An id *must* be echoed (it is the operand an
#: operator types at ``herdr plugin enable``), so the remedy here is a bound rather
#: than suppression.
MAX_SEGMENT_LENGTH = 64

#: A plugin may live below a repository root, but the identity remains a bounded
#: sequence of ordinary GitHub-style segments rather than an arbitrary path.  The
#: depth bound prevents a syntactically valid third-party value from becoming an
#: unbounded report channel.
MAX_SUBDIR_SEGMENTS = 16

#: Upper bound on any single rendered text field (a diagnostic sentence, not an
#: essay). Bounds are part of the boundary: an unbounded field is a channel.
MAX_RENDERED_FIELD_LENGTH = 2000

_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")

#: The absolute-path rule is NOT defined here. It is shared with the #14258
#: launcher-probe redaction, so it lives in a module belonging to neither consumer
#: (``absolute_path_rule``). Coordinator ruling j#92243 requires one *neutral*
#: authority; the previous arrangement had the generic redaction importing this
#: plugin-specific module, which is the dependency pointing the wrong way. These
#: names are re-exported for callers that already use them.
_ABS_ROOT_RE = ABSOLUTE_ROOT_RE
_RELATIVE_CONTINUATION_RE = RELATIVE_CONTINUATION_RE

#: Control characters. ``\n`` matters most: this surface's text is written to be
#: pasted into a durable record, and a newline inside a *field* lets that field
#: forge a line of the record (review j#92092 F2 measured a forged ``BREACH:``
#: line). A field never legitimately contains one; only the assembled artifact does.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def require_renderable_field(value: object, field: str) -> str:
    """Return ``value`` if it may be rendered into a report, else fail closed.

    The single predicate every renderable field shares. Used where the content is
    **ours** — a decision's diagnostic sentence, a review anchor — because a
    violation there is a bug in text we wrote, and the honest response is to refuse
    rather than to quietly rewrite it.

    Where the content is derived from a third party (a parser's message about a
    hostile record, a subprocess's stderr) the boundary *sanitizes* instead; see
    the ops layer's ``sanitize_renderable``. Both end with a record that cannot
    hold an unrenderable value; they differ only in who is at fault when one shows
    up.
    """
    if not isinstance(value, str):
        raise HerdrPluginPolicyError(
            f"{field} must be a string, got {type(value).__name__}"
        )
    if len(value) > MAX_RENDERED_FIELD_LENGTH:
        raise HerdrPluginPolicyError(
            f"{field} exceeds {MAX_RENDERED_FIELD_LENGTH} characters"
        )
    if _CONTROL_CHAR_RE.search(value):
        raise HerdrPluginPolicyError(
            f"{field} carries a control character; a field that can hold one can "
            f"forge a line of the record it is pasted into"
        )
    if contains_absolute_path(value):
        raise HerdrPluginPolicyError(f"{field} carries an absolute filesystem path")
    return value


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
            f"{field} is not a bare identifier segment "
            f"(letters, digits, '.', '_', '-')"
        )
    if len(value) > MAX_SEGMENT_LENGTH:
        raise HerdrPluginPolicyError(
            f"{field} exceeds {MAX_SEGMENT_LENGTH} characters; an identifier this "
            f"long is third-party text, not an identifier"
        )
    return value


#: In-module alias; the public spelling is ``require_segment``.
_require_segment = require_segment


def _require_subdir_segments(value: object) -> tuple[str, ...]:
    """Validate the immutable relative segments of a plugin source subdirectory."""
    if not isinstance(value, tuple):
        raise HerdrPluginPolicyError(
            f"source subdir must be a tuple, got {type(value).__name__}"
        )
    if len(value) > MAX_SUBDIR_SEGMENTS:
        raise HerdrPluginPolicyError(
            f"source subdir exceeds {MAX_SUBDIR_SEGMENTS} segments"
        )
    for index, segment in enumerate(value):
        if segment in {"", ".", ".."}:
            raise HerdrPluginPolicyError(
                f"source subdir segment {index} is empty or navigational"
            )
        _require_segment(segment, f"source subdir segment {index}")
    return value


def _parse_source_subdir(value: object) -> tuple[str, ...]:
    """Parse Herdr's optional slash-joined ``source.subdir`` field strictly."""
    if value is None:
        return ()
    if not isinstance(value, str):
        raise HerdrPluginPolicyError(
            f"source subdir must be a string or null, got {type(value).__name__}"
        )
    return _require_subdir_segments(tuple(value.split("/")))


@dataclass(frozen=True)
class PluginSourceRef:
    """Where a plugin's code came from: repository, subdirectory, and commit.

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
    subdir: tuple[str, ...] = field(default=(), kw_only=True)

    def __post_init__(self) -> None:
        if self.kind != SOURCE_KIND_GITHUB:
            raise HerdrPluginPolicyError(
                f"source kind {self.kind!r} is not a pinnable upstream identity; "
                f"only {SOURCE_KIND_GITHUB!r} resolves to an immutable commit"
            )
        _require_segment(self.owner, "source owner")
        _require_segment(self.repo, "source repo")
        if self.owner in {".", ".."} or self.repo in {".", ".."}:
            raise HerdrPluginPolicyError(
                "source owner and repo must not be navigational segments"
            )
        _require_subdir_segments(self.subdir)
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
    def pinned(
        cls,
        kind: str,
        owner: str,
        repo: str,
        commit: str,
        *,
        subdir: tuple[str, ...] = (),
    ) -> "PluginSourceRef":
        """A reference pinned to an exact immutable commit."""
        return cls(
            kind=kind,
            owner=owner,
            repo=repo,
            commit=commit,
            subdir=subdir,
        )

    @property
    def repo_key(self) -> "PluginSourceRef":
        """This reference with both commit and subdirectory dropped."""
        if self.commit is None and not self.subdir:
            return self
        return PluginSourceRef(kind=self.kind, owner=self.owner, repo=self.repo)

    @property
    def is_pinned(self) -> bool:
        return self.commit is not None

    @property
    def install_spec(self) -> str:
        """The bounded Herdr ``owner/repo[/subdir...]`` install operand."""
        base = f"{self.owner}/{self.repo}"
        return f"{base}/{'/'.join(self.subdir)}" if self.subdir else base

    def describe(self) -> str:
        """A short, bounded description including the exact plugin subdirectory."""
        base = f"{self.kind}:{self.install_spec}"
        return f"{base}@{self.commit}" if self.commit else base


def source_ref_from_parts(
    kind: object,
    owner: object,
    repo: object,
    commit: object,
    subdir: object = None,
) -> Optional[PluginSourceRef]:
    """Build the strongest reference these parts support (review j#92053 F3).

    **Repository identity and exact plugin identity are separate facts, so a bad
    commit or subdirectory may not discard the repository.** The original version collapsed them: any
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
    reference; subdirectory and commit together decide whether that reference is
    *also* an exact pin. A malformed subdirectory falls back only to the repository
    identity, never to a root-plugin allow. A valid subdirectory remains on an
    unpinned reference when only the commit is malformed.
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
        subdir_segments = _parse_source_subdir(subdir)
    except HerdrPluginPolicyError:
        # Subdirectory validity cannot erase an independently valid repository
        # identity.  Keeping only the repository preserves repository-wide deny
        # classifications while remaining unpinned, so it can never fall through
        # to a root-plugin allow.
        return repository
    unpinned = PluginSourceRef(
        kind=SOURCE_KIND_GITHUB,
        owner=repository.owner,
        repo=repository.repo,
        subdir=subdir_segments,
    )
    try:
        return PluginSourceRef.pinned(
            SOURCE_KIND_GITHUB,
            repository.owner,
            repository.repo,
            commit,
            subdir=subdir_segments,
        )
    except HerdrPluginPolicyError:
        return unpinned


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
        source.get("subdir"),
    )


def _check_plugin_id(value: object, name: str) -> None:
    require_segment(value, name)


def _check_source_kind(value: object, name: str) -> None:
    # Type before membership: an unhashable value would raise a raw ``TypeError``
    # out of the frozenset test rather than a policy error (review j#92092 F1).
    if not isinstance(value, str):
        raise HerdrPluginPolicyError(
            f"{name} must be a string, got {type(value).__name__}"
        )
    if value not in OBSERVED_SOURCE_KINDS:
        raise HerdrPluginPolicyError(
            f"{name} is outside the observed vocabulary "
            f"{sorted(OBSERVED_SOURCE_KINDS)}"
        )


def _check_strict_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise HerdrPluginPolicyError(
            f"{name} must be a boolean, got {type(value).__name__}"
        )


def _check_optional_ref(value: object, name: str) -> None:
    if value is not None and not isinstance(value, PluginSourceRef):
        raise HerdrPluginPolicyError(
            f"{name} must be a PluginSourceRef or None, got {type(value).__name__}"
        )


#: One validator per :class:`PluginObservation` field. ``__post_init__`` walks the
#: dataclass's own field list against this table and refuses to construct anything
#: when a field has no entry, so a field added later cannot slip through
#: unvalidated. Review j#92092 finding 1 is exactly that failure: the previous
#: ``__post_init__`` hand-listed three checks while the docstring claimed all
#: eight fields were closed, and the five unchecked ones accepted arbitrary text
#: (a path in ``declares_build`` reached the report) or raised raw ``TypeError``.
#: The table is still an enumeration — but a *detected* one.
_OBSERVATION_FIELD_CHECKS = MappingProxyType(
    {
        "plugin_id": _check_plugin_id,
        "enabled": _check_strict_bool,
        "source_kind": _check_source_kind,
        "ref": _check_optional_ref,
        "declares_build": _check_strict_bool,
        "declares_panes": _check_strict_bool,
        "declares_actions": _check_strict_bool,
    }
)


@dataclass(frozen=True)
class PluginObservation:
    """A herdr plugin as observed, reduced to core-owned facts.

    Holds no ``manifest_path`` / ``plugin_root`` / ``managed_path`` (the three
    absolute operator-home paths in herdr's payload) and — since review j#92092
    finding 3 — no ``version`` either. A narrow alphabet is not a closed
    representation: "closed" means the value is one core owns, not one whose
    *shape* we constrained, and a version-shaped marker was still third-party text
    reaching the report. Identity is the commit pin, so nothing needed the version.

    What remains is a plugin id (a bounded identifier, which must be echoed because
    it is the operand an operator types), a projected source kind, a validated
    reference, and three booleans recording only *whether* the local manifest
    declares each surface — never the commands, which are third-party strings.
    """

    plugin_id: str
    enabled: bool
    source_kind: str
    ref: Optional[PluginSourceRef]
    declares_build: bool
    declares_panes: bool
    declares_actions: bool

    def __post_init__(self) -> None:
        missing = {
            field.name for field in dataclasses.fields(self)
        } - _OBSERVATION_FIELD_CHECKS.keys()
        if missing:
            raise HerdrPluginPolicyError(
                f"no closed-representation validator for field(s) {sorted(missing)}; "
                f"every field must be checked or the record is not closed"
            )
        for name, check in _OBSERVATION_FIELD_CHECKS.items():
            check(getattr(self, name), name)
        # Relational invariant, not a field one (review j#92194 F2). Field-level
        # checks let `source_kind="unrecognized"` sit beside a *reviewed* pin, and
        # the classifier reads the ref — so an observation that says "I could not
        # recognize this source" still classified as ux_only and admitted.
        if self.ref is not None and self.source_kind != SOURCE_KIND_GITHUB:
            raise HerdrPluginPolicyError(
                f"a resolved source reference requires source_kind "
                f"{SOURCE_KIND_GITHUB!r}, not {self.source_kind!r}"
            )


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
    "MAX_RENDERED_FIELD_LENGTH",
    "MAX_SEGMENT_LENGTH",
    "MAX_SUBDIR_SEGMENTS",
    "REDACTED_TOKEN",
    "HerdrPluginPolicyError",
    "PluginObservation",
    "PluginSourceRef",
    "contains_absolute_path",
    "normalize_source_kind",
    "observe_plugin",
    "read_source_ref",
    "require_renderable_field",
    "require_segment",
    "source_ref_from_parts",
)
