"""Managed-lane policy for **herdr community plugins** (Redmine #14619).

herdr 0.7.5 can install and enable third-party plugins. The #14613 / #14614
characterization (result journal ``#14614 j#91226``) measured two facts that make
an explicit policy necessary rather than optional:

- **A plugin's enabled state is user-global, not session- or workspace-scoped.**
  "Enable it only for this workspace" cannot be expressed. The only isolation
  boundary is a different ``HOME`` / ``XDG_CONFIG_HOME``. So enabling a plugin for
  one experiment enables it for every managed lane the operator runs.
- **A plugin can write straight into an agent's input.** ``herdr-reviewr`` binds a
  key that sends review comments to the workspace's agent, which bypasses the
  exact-once handoff rail and the durable Redmine anchor entirely — it is a
  *delivery* conflict, not merely a taste question.

This module is the pure, fail-closed core that decides what a herdr plugin is
allowed to be in a managed lane. It answers two **independent** questions, and
keeping them independent is deliberate:

``enable``
    May this plugin be *enabled* while managed lanes exist? This is the
    lane-authority axis, decided by the plugin's capability class.
``install``
    May ``herdr plugin install`` be *run* for this plugin? This is the
    supply-chain axis, decided by what the install executes.

They are separate because they fail for different reasons and at different
times. ``herdr-file-viewer`` is the case that proves it: as a capability it is a
read-only viewer that touches no authority surface (enable: admitted, the
status quo #14614 recorded), yet its ``[[build]]`` step downloads a prebuilt
binary from a GitHub release keyed by the *declared version* — not by the pinned
commit — and verifies it against a checksum file served from that same origin
(measured for this issue against the installed plugin's ``scripts/fetch-or-build.sh``).
Re-running its install is therefore an unpinned remote execution even though the
plugin itself is harmless. Folding the two axes together would have to either
deny a benign plugin's enable or bless an unpinned fetch; reporting both keeps
each fact true.

Boundary (enforced in code, not merely asserted here):

- **Pure.** No file IO, no subprocess, no network. :func:`observe_plugin`
  normalizes an *already-parsed* ``herdr plugin list --json`` plugin record; the
  application ops layer owns reading it.
- **No path ever enters a record.** herdr's payload carries three absolute
  operator-home paths (``manifest_path`` / ``plugin_root`` / ``source.managed_path``).
  :class:`PluginObservation` has no field to hold them, so a report cannot leak
  one by forgetting to redact — the value-non-disclosure requirement is
  structural rather than a formatting rule.
- **Authority stays core-owned.** A plugin is never granted identity, delivery,
  Redmine, review, or retire authority. :data:`FORBIDDEN_PLUGIN_AUTHORITIES`
  reuses the existing core-owned sets verbatim so this module and the provider /
  CLI registries cannot drift apart on what "authority" means.
- **This module decides; it never acts.** Nothing here (or in its ops layer)
  installs, enables, disables, or uninstalls anything.

## Why an allow is pinned to a commit but a deny is not

The registry is asymmetric on purpose, because the two directions have opposite
failure modes.

An **allow** is keyed on the exact ``(kind, owner, repo, commit)`` pin. A review
that concluded "this code is safe" is a statement about the bytes that were read.
An allow keyed on the repository alone would silently extend to every future
upstream commit, which is precisely the supply-chain hole the policy exists to
close. A plugin observed at any other commit resolves to
:data:`CLASS_UNKNOWN` and is denied.

A **deny** is keyed on the repository ``(kind, owner, repo)``, with no commit. The
reason ``herdr-reviewr`` is inadmissible is that the *project* writes into agent
input; a newer commit does not stop doing so. A commit-pinned deny would be
bypassed by installing a commit no one had reviewed — which would fall through to
``unknown`` and be denied anyway, so a repository-scoped deny costs nothing and
says the true thing. It is also what lets a project be classified from a durable
characterization that recorded an abbreviated commit, without inventing a
precision the record does not have.

The invariant that makes the asymmetry safe is checked at construction: a
repository-scoped entry may only carry a *deny* class, so this shape can never be
used to widen an allow.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import (
    FORBIDDEN_PROVIDER_AUTHORITIES,
)
from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry import (
    CORE_OWNED_AUTHORITIES,
)

# --- authority boundary (core-owned, reused verbatim) ------------------------
#: Authorities a herdr plugin is never granted. The two existing core-owned sets
#: are reused rather than re-listed so this module cannot drift from the provider
#: registry (#12035) or the CLI module registry (#12155) on what core never
#: delegates; the herdr-specific additions are the lane concerns those registries
#: have no reason to name.
FORBIDDEN_PLUGIN_AUTHORITIES: frozenset[str] = (
    FORBIDDEN_PROVIDER_AUTHORITIES
    | CORE_OWNED_AUTHORITIES
    | frozenset(
        {
            "delivery_authority",
            "durable_anchor_authority",
            "lane_identity",
            "retire_authority",
        }
    )
)

# --- capability classes (closed vocabulary) ----------------------------------
#: A read-only UX surface. It renders, it does not write to agent input, lane
#: state, or any durable record. The only class admissible for enable while
#: managed lanes exist.
CLASS_UX_ONLY = "ux_only"
#: Useful as a reference schema / expected-layout oracle in tests, but carrying no
#: lane identity, generation, occupancy, or retire concept. Recognized, and *not*
#: admissible for enable — a test oracle has no authority over a live lane.
CLASS_TEST_ORACLE = "test_oracle"
#: Writes into an agent's input, bypassing the exact-once handoff rail and the
#: durable anchor. Inadmissible.
CLASS_AGENT_INPUT_WRITER = "agent_input_writer"
#: Not reviewed at this identity. The fail-closed default: absence of a review is
#: never read as absence of risk.
CLASS_UNKNOWN = "unknown"

PLUGIN_CLASSES: frozenset[str] = frozenset(
    {CLASS_UX_ONLY, CLASS_TEST_ORACLE, CLASS_AGENT_INPUT_WRITER, CLASS_UNKNOWN}
)

#: Classes that may be carried by a repository-scoped (commit-less) entry. An
#: allow may never be repository-scoped; see the module docstring.
DENY_CLASSES: frozenset[str] = frozenset({CLASS_TEST_ORACLE, CLASS_AGENT_INPUT_WRITER})

# --- build provenance (closed vocabulary) ------------------------------------
#: The manifest declares no ``[[build]]``: installing executes nothing.
BUILD_NONE = "no_build"
#: A ``[[build]]`` that runs only code from the exactly-pinned source tree and
#: fetches no remote artifact.
BUILD_SOURCE_ONLY = "source_only_build"
#: A ``[[build]]`` that downloads a remote artifact whose only integrity proof is
#: served from the same origin as the artifact. A compromised origin serves both,
#: so this is an unpinned remote execution regardless of the checksum step.
BUILD_REMOTE_ARTIFACT = "remote_artifact_same_origin_checksum"
#: Build provenance has not been reviewed for this project. Fail-closed default.
BUILD_UNREVIEWED = "unreviewed_build_provenance"

BUILD_PROVENANCES: frozenset[str] = frozenset(
    {BUILD_NONE, BUILD_SOURCE_ONLY, BUILD_REMOTE_ARTIFACT, BUILD_UNREVIEWED}
)

#: Provenances under which running the install is admissible.
ADMISSIBLE_BUILD_PROVENANCES: frozenset[str] = frozenset({BUILD_NONE, BUILD_SOURCE_ONLY})

# --- deny reasons (closed vocabulary) ----------------------------------------
#: The source is not an exact immutable pin (a locally linked plugin, a
#: non-``github`` source kind, or a missing / malformed commit).
REASON_UNPINNED_SOURCE = "unpinned_source"
#: The source is pinned, but nothing has reviewed *this* identity.
REASON_UNREVIEWED_PIN = "unreviewed_pin"
#: The pin resolves to a reviewed entry, but the observed plugin id disagrees with
#: what was reviewed — the local manifest says it is something else.
REASON_IDENTITY_MISMATCH = "identity_mismatch"
#: The observed manifest surface contradicts the reviewed provenance (a reviewed
#: build-less plugin that now declares a build, or the reverse). The commit pin
#: fixes what upstream published; it does not fix the bytes sitting in the
#: operator's plugin directory after install.
REASON_MANIFEST_DRIFT = "manifest_drift"
#: The plugin writes into agent input.
REASON_AGENT_INPUT_WRITER = "agent_input_writer"
#: Recognized as a test oracle / reference schema, which carries no lane authority.
REASON_NO_LANE_AUTHORITY = "no_lane_authority"
#: Installing would execute a build that fetches a remote artifact not pinned by
#: content.
REASON_UNPINNED_REMOTE_BUILD = "unpinned_remote_build"
#: Installing would execute a build whose provenance has not been reviewed.
REASON_UNREVIEWED_BUILD = "unreviewed_build"
#: The record could not be read as a plugin at all.
REASON_MALFORMED_RECORD = "malformed_record"

DENY_REASONS: frozenset[str] = frozenset(
    {
        REASON_UNPINNED_SOURCE,
        REASON_UNREVIEWED_PIN,
        REASON_IDENTITY_MISMATCH,
        REASON_MANIFEST_DRIFT,
        REASON_AGENT_INPUT_WRITER,
        REASON_NO_LANE_AUTHORITY,
        REASON_UNPINNED_REMOTE_BUILD,
        REASON_UNREVIEWED_BUILD,
        REASON_MALFORMED_RECORD,
    }
)

# --- scope truthfulness ------------------------------------------------------
#: What a herdr plugin's enabled state is actually scoped to (0.7.5 breaking
#: change, measured in #14614). Reports state this literally; they never describe
#: an enable as workspace-, session-, or lane-local.
ENABLE_SCOPE = "user_global"
#: The one-line truth a report must carry alongside any enabled state.
ENABLE_SCOPE_STATEMENT = (
    "herdr plugin install/enable state is user-global: it applies to every herdr "
    "session and every managed lane run under the same config root, not to one "
    "workspace. There is no workspace-local enable."
)
#: What the affected config root is resolved *from*. Reports name these
#: determinants rather than the operator's resolved absolute path, which keeps the
#: blast radius explicit without disclosing a private path.
SCOPE_ROOT_DETERMINANTS: "tuple[str, ...]" = (
    "HERDR_CONFIG_PATH",
    "XDG_CONFIG_HOME",
    "HOME",
)
#: The only isolation mechanism that actually separates plugin state (#14614).
SCOPE_ISOLATION_MECHANISM = (
    "a separate HOME / XDG_CONFIG_HOME config root; splitting herdr sessions does not "
    "isolate plugin state"
)

#: The one source kind herdr resolves to an immutable commit. ``link`` (a local
#: directory) carries no upstream identity at all.
SOURCE_KIND_GITHUB = "github"

_COMMIT_RE = re.compile(r"\A[0-9a-f]{40}\Z")
_SEGMENT_RE = re.compile(r"\A[A-Za-z0-9._-]+\Z")


class HerdrPluginPolicyError(ValueError):
    """A plugin record, review entry, or registry is unreadable / self-contradictory.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    adapter-boundary errors (``HerdrPinPostureError`` / ``ProviderRegistryError``).
    """


def _require_segment(value: object, field: str) -> str:
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


def read_source_ref(source: object) -> Optional[PluginSourceRef]:
    """Read a pinned :class:`PluginSourceRef` out of a herdr ``source`` record.

    Returns ``None`` — never a partial or guessed identity — whenever the record
    does not carry a complete, exactly-pinned upstream identity: a missing or
    non-mapping ``source``, a source kind other than ``github`` (a linked local
    directory has no upstream identity), a missing / malformed owner or repo, or a
    ``resolved_commit`` that is not a full lowercase hex commit. The caller turns
    ``None`` into :data:`REASON_UNPINNED_SOURCE`; it is a denial, not an error,
    because an unpinnable source is a legitimate thing to observe and report.
    """
    if not isinstance(source, Mapping):
        return None
    if source.get("kind") != SOURCE_KIND_GITHUB:
        return None
    try:
        return PluginSourceRef.pinned(
            kind=SOURCE_KIND_GITHUB,
            owner=source.get("owner"),
            repo=source.get("repo"),
            commit=source.get("resolved_commit"),
        )
    except HerdrPluginPolicyError:
        return None


@dataclass(frozen=True)
class ReviewedPlugin:
    """A recorded review decision about a herdr plugin project or exact build.

    ``ref.commit is None`` means the decision is about the *project* and may only
    be a deny (:data:`DENY_CLASSES`); a commit-pinned entry may carry any class.
    ``review_anchor`` is the durable record the decision replays from — a
    classification with no anchor is an opinion, not a review, so it is rejected.
    """

    ref: PluginSourceRef
    plugin_id: str
    plugin_class: str
    build_provenance: str
    review_anchor: str
    rationale: str

    def __post_init__(self) -> None:
        if self.plugin_class not in PLUGIN_CLASSES:
            raise HerdrPluginPolicyError(
                f"plugin class {self.plugin_class!r} is not one of {sorted(PLUGIN_CLASSES)}"
            )
        if self.plugin_class == CLASS_UNKNOWN:
            raise HerdrPluginPolicyError(
                "a reviewed entry may not carry the unknown class; unknown is the "
                "absence of an entry, not a recordable verdict"
            )
        if self.build_provenance not in BUILD_PROVENANCES:
            raise HerdrPluginPolicyError(
                f"build provenance {self.build_provenance!r} is not one of "
                f"{sorted(BUILD_PROVENANCES)}"
            )
        _require_segment(self.plugin_id, "reviewed plugin_id")
        if not self.review_anchor.strip():
            raise HerdrPluginPolicyError(
                f"reviewed plugin {self.plugin_id!r} needs a durable review anchor"
            )
        if not self.rationale.strip():
            raise HerdrPluginPolicyError(
                f"reviewed plugin {self.plugin_id!r} needs a rationale"
            )
        if not self.ref.is_pinned and self.plugin_class not in DENY_CLASSES:
            raise HerdrPluginPolicyError(
                f"a repository-scoped entry may only carry a deny class "
                f"{sorted(DENY_CLASSES)}; {self.plugin_class!r} would extend an allow "
                f"to every future commit of {self.ref.describe()}"
            )

    @property
    def declares_build(self) -> Optional[bool]:
        """Whether the reviewed manifest declared a ``[[build]]`` step.

        ``None`` for :data:`BUILD_UNREVIEWED`, which is the point of the tri-state:
        "no review has established what the build does" asserts nothing about
        whether a build *exists*, so it must not be folded into either boolean.
        Collapsing it to ``True`` made every unreviewed project report
        ``manifest_drift`` — a claim about a comparison that was never made — and
        hid the real reason it is inadmissible.
        """
        if self.build_provenance == BUILD_UNREVIEWED:
            return None
        return self.build_provenance != BUILD_NONE


def build_review_registry(
    entries: "Sequence[ReviewedPlugin]",
) -> "dict[PluginSourceRef, ReviewedPlugin]":
    """Index reviewed entries by their reference, rejecting an ambiguous registry.

    Two failures are programming errors rather than runtime conditions, so both
    raise: a duplicated reference (which of the two decisions applies would be
    unanswerable), and a repository that carries both a repository-scoped deny and
    a commit-pinned allow (resolution order would silently decide a security
    question).
    """
    indexed: "dict[PluginSourceRef, ReviewedPlugin]" = {}
    for entry in entries:
        if entry.ref in indexed:
            raise HerdrPluginPolicyError(
                f"duplicate review entry for {entry.ref.describe()}"
            )
        indexed[entry.ref] = entry
    denied_repos = {ref.repo_key for ref in indexed if not ref.is_pinned}
    for ref in indexed:
        if ref.is_pinned and ref.repo_key in denied_repos:
            raise HerdrPluginPolicyError(
                f"{ref.describe()} is pinned-allowed while its repository is "
                f"deny-classified; a repository cannot be both"
            )
    return indexed


#: The reviewed herdr plugins. Each entry replays from a durable record; nothing
#: is classified from a plugin's own self-description, because a manifest cannot
#: be trusted to declare that it writes into agent input.
REVIEWED_PLUGINS: "dict[PluginSourceRef, ReviewedPlugin]" = build_review_registry(
    (
        ReviewedPlugin(
            ref=PluginSourceRef.pinned(
                SOURCE_KIND_GITHUB,
                "smarzban",
                "herdr-file-viewer",
                "96fcc0a2bdd2727ec88c38f8c8806f97b7ca0ea0",
            ),
            plugin_id="herdr-file-viewer",
            plugin_class=CLASS_UX_ONLY,
            build_provenance=BUILD_REMOTE_ARTIFACT,
            review_anchor="#14614 j#91226 (classification); #14619 (build provenance)",
            rationale=(
                "A git-aware read-only file viewer in a herdr split pane. It writes to "
                "no agent input, no lane state, and no durable record, so it touches no "
                "authority surface — UX-only, enable admitted (the status quo #14614 "
                "recorded). Its [[build]] step, however, downloads a prebuilt binary "
                "from a GitHub release keyed by the version this source declares — "
                "explicitly not by the pinned commit — and verifies it against a "
                "SHA256SUMS file served from that same origin, falling back to a cargo "
                "build on any miss. Running that install is therefore an unpinned "
                "remote execution; the already-installed plugin is unaffected."
            ),
        ),
        ReviewedPlugin(
            ref=PluginSourceRef.repository(
                SOURCE_KIND_GITHUB, "yuk1ty", "herdr-spreader"
            ),
            plugin_id="herdr-spreader",
            plugin_class=CLASS_TEST_ORACLE,
            build_provenance=BUILD_UNREVIEWED,
            review_anchor="#14614 j#91226",
            rationale=(
                "Declares workspaces > tabs > panes in YAML with split / ratio / cwd / "
                "env / focus / wait_for, which expresses the same three axes as the "
                "layout work (#14567 / #14568 / #14569) and is genuinely useful as a "
                "reference schema and expected-layout oracle in tests. It is a one-shot "
                "workspace applier with no concept of lane identity, generation, "
                "occupancy, retire, or durable anchor, so it holds no authority over a "
                "live lane: recognized, never enabled alongside managed lanes. Its "
                "build provenance has not been reviewed."
            ),
        ),
        ReviewedPlugin(
            ref=PluginSourceRef.repository(
                SOURCE_KIND_GITHUB, "persiyanov", "herdr-reviewr"
            ),
            plugin_id="herdr-reviewr",
            plugin_class=CLASS_AGENT_INPUT_WRITER,
            build_provenance=BUILD_REMOTE_ARTIFACT,
            review_anchor="#14614 j#91226",
            rationale=(
                "Binds a key that sends every review comment to the workspace's agent, "
                "selecting the agent itself when more than one is present. That writes "
                "into a managed lane's agent input while bypassing the exact-once "
                "handoff rail and the durable Redmine anchor, so it conflicts on "
                "delivery before any question of review verdict or approval arises. Its "
                "[[build]] additionally downloads a prebuilt binary from a GitHub "
                "release verified only by a same-origin sha256 sidecar."
            ),
        ),
    )
)


def resolve_review(ref: Optional[PluginSourceRef]) -> Optional[ReviewedPlugin]:
    """Resolve the review decision for ``ref``, or ``None`` when nothing reviewed it.

    A repository-scoped deny is consulted **first**, so a deny-classified project
    is denied at every commit — including one nobody has looked at. Only then is
    the exact pin consulted, and only an exact pin can produce an allow. An
    unpinned reference resolves to the repository entry if one exists and to
    ``None`` otherwise.
    """
    if ref is None:
        return None
    repository_entry = REVIEWED_PLUGINS.get(ref.repo_key)
    if repository_entry is not None:
        return repository_entry
    if not ref.is_pinned:
        return None
    return REVIEWED_PLUGINS.get(ref)


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
    plugin_id = _require_segment(record.get("plugin_id"), "plugin_id")
    version = record.get("version", "")
    if not isinstance(version, str):
        raise HerdrPluginPolicyError(
            f"plugin record field 'version' must be a string, got "
            f"{type(version).__name__}"
        )
    source = record.get("source")
    source_kind = ""
    if isinstance(source, Mapping):
        raw_kind = source.get("kind")
        source_kind = raw_kind if isinstance(raw_kind, str) else ""
    return PluginObservation(
        plugin_id=plugin_id,
        version=version,
        enabled=_require_bool(record, "enabled"),
        source_kind=source_kind,
        ref=read_source_ref(source),
        declares_build=_declares(record, "build"),
        declares_panes=_declares(record, "panes"),
        declares_actions=_declares(record, "actions"),
    )


@dataclass(frozen=True)
class PolicyDecision:
    """One admissibility decision, with a reason from the closed vocabulary.

    An admitted decision carries no reason and a denied decision must carry one:
    the two are kept mutually exclusive at construction so a consumer can branch on
    ``admitted`` and on ``reason`` without them ever disagreeing.
    """

    admitted: bool
    reason: Optional[str] = None
    detail: str = ""

    def __post_init__(self) -> None:
        if self.admitted:
            if self.reason is not None:
                raise HerdrPluginPolicyError(
                    "an admitted decision may not carry a deny reason"
                )
        elif self.reason not in DENY_REASONS:
            raise HerdrPluginPolicyError(
                f"a denied decision must carry a reason from {sorted(DENY_REASONS)}, "
                f"got {self.reason!r}"
            )

    @classmethod
    def admit(cls, detail: str = "") -> "PolicyDecision":
        return cls(admitted=True, detail=detail)

    @classmethod
    def deny(cls, reason: str, detail: str = "") -> "PolicyDecision":
        return cls(admitted=False, reason=reason, detail=detail)


def _identity_decision(
    observation: PluginObservation, review: Optional[ReviewedPlugin]
) -> Optional[PolicyDecision]:
    """The denial shared by both axes, or ``None`` when identity is settled.

    Both the enable and the install question are unanswerable for the same three
    identity failures, so they are decided once here rather than duplicated — a
    second copy is where the two axes would drift apart on what "unknown" means.
    """
    if observation.ref is None:
        return PolicyDecision.deny(
            REASON_UNPINNED_SOURCE,
            f"source kind {observation.source_kind or '(absent)'!r} carries no exact "
            f"upstream commit, so what this code is cannot be established",
        )
    if review is None:
        return PolicyDecision.deny(
            REASON_UNREVIEWED_PIN,
            f"{observation.ref.describe()} has no reviewed classification",
        )
    if observation.plugin_id != review.plugin_id:
        return PolicyDecision.deny(
            REASON_IDENTITY_MISMATCH,
            f"{observation.ref.describe()} was reviewed as {review.plugin_id!r} but "
            f"the local manifest declares {observation.plugin_id!r}",
        )
    reviewed_build = review.declares_build
    # ``None`` means the review established nothing about the build surface, so
    # there is no recorded shape to have drifted from. Comparing against it would
    # manufacture a contradiction out of an absence.
    if reviewed_build is not None and observation.declares_build != reviewed_build:
        declared = "declares" if observation.declares_build else "declares no"
        reviewed = "a build" if reviewed_build else "no build"
        return PolicyDecision.deny(
            REASON_MANIFEST_DRIFT,
            f"the local manifest {declared} [[build]] step but the reviewed "
            f"{review.build_provenance} recorded {reviewed}; the commit pin fixes what "
            f"upstream published, not the bytes on disk after install",
        )
    return None


def decide_enable(
    observation: PluginObservation, review: Optional[ReviewedPlugin]
) -> PolicyDecision:
    """May this plugin be enabled while managed lanes exist? (lane-authority axis)"""
    identity = _identity_decision(observation, review)
    if identity is not None:
        return identity
    assert review is not None  # settled by _identity_decision
    if review.plugin_class == CLASS_AGENT_INPUT_WRITER:
        return PolicyDecision.deny(
            REASON_AGENT_INPUT_WRITER,
            "writes into agent input, bypassing the exact-once handoff rail and the "
            "durable anchor",
        )
    if review.plugin_class == CLASS_TEST_ORACLE:
        return PolicyDecision.deny(
            REASON_NO_LANE_AUTHORITY,
            "recognized as a test oracle / reference schema; it carries no lane "
            "identity, generation, occupancy, or retire concept, so it holds no "
            "authority over a live lane",
        )
    return PolicyDecision.admit(
        "read-only UX surface; writes to no agent input, lane state, or durable record"
    )


def _decide_install_from_review(review: ReviewedPlugin) -> PolicyDecision:
    """The supply-chain half, once identity is settled.

    The supply-chain question is asked before the capability question because it is
    the install-specific axis: what running ``herdr plugin install`` *executes* does
    not depend on what the plugin is for. A capability-denied project also fails
    here whenever its build is unpinned, and the more specific reason is the useful
    one to report.
    """
    if review.build_provenance == BUILD_REMOTE_ARTIFACT:
        return PolicyDecision.deny(
            REASON_UNPINNED_REMOTE_BUILD,
            "the [[build]] step downloads a remote artifact whose only integrity proof "
            "is served from the same origin, so the commit pin does not pin what runs",
        )
    if review.build_provenance == BUILD_UNREVIEWED:
        return PolicyDecision.deny(
            REASON_UNREVIEWED_BUILD,
            "no review has established what the [[build]] step executes",
        )
    if review.plugin_class == CLASS_AGENT_INPUT_WRITER:
        return PolicyDecision.deny(
            REASON_AGENT_INPUT_WRITER,
            "installing a plugin that may never be enabled here serves no purpose and "
            "leaves an inadmissible capability one user-global toggle away",
        )
    if review.plugin_class == CLASS_TEST_ORACLE:
        return PolicyDecision.deny(
            REASON_NO_LANE_AUTHORITY,
            "recognized as a test oracle; its schema is used as a reference, which "
            "needs no install into the operator config root",
        )
    return PolicyDecision.admit(
        f"{review.build_provenance}: the install executes nothing outside the pinned "
        f"source"
    )


def decide_install(
    observation: PluginObservation, review: Optional[ReviewedPlugin]
) -> PolicyDecision:
    """May ``herdr plugin install`` be run for this plugin? (supply-chain axis)"""
    identity = _identity_decision(observation, review)
    if identity is not None:
        return identity
    assert review is not None  # settled by _identity_decision
    return _decide_install_from_review(review)


def plan_install(ref: Optional[PluginSourceRef]) -> PolicyDecision:
    """Decide a *candidate* install, before anything exists locally to observe.

    Identity here is only the reference the operator names, so the two observation
    checks (``identity_mismatch`` / ``manifest_drift``) have nothing to compare
    against and are not applied — there is no local manifest yet.

    A resolved review is answered **before** the missing-commit check, so a
    deny-classified project reports why the *project* is inadmissible rather than
    "you did not name a commit" — a reason that would invite the operator to
    supply one and be denied again. That ordering is only safe because a
    repository-scoped entry can never carry an allow (:class:`ReviewedPlugin`
    rejects one at construction); the class is re-checked here so the guarantee
    does not live solely in another module's constructor.
    """
    if ref is None:
        return PolicyDecision.deny(
            REASON_UNPINNED_SOURCE,
            "no exact upstream commit was named, so what would be executed cannot be "
            "established",
        )
    review = resolve_review(ref)
    if review is not None and (ref.is_pinned or review.plugin_class in DENY_CLASSES):
        return _decide_install_from_review(review)
    if not ref.is_pinned:
        return PolicyDecision.deny(
            REASON_UNPINNED_SOURCE,
            f"{ref.describe()} names no commit; an install must name the exact commit "
            f"it executes",
        )
    return PolicyDecision.deny(
        REASON_UNREVIEWED_PIN, f"{ref.describe()} has no reviewed classification"
    )


@dataclass(frozen=True)
class PluginVerdict:
    """Both decisions for one observed plugin, plus whether it is a live breach."""

    observation: PluginObservation
    plugin_class: str
    build_provenance: str
    review_anchor: str
    enable: PolicyDecision
    install: PolicyDecision

    @property
    def breach(self) -> bool:
        """Enabled right now despite not being admissible — an active violation.

        Distinct from a mere denial: a denied plugin that is *not* enabled is the
        policy working. Only the conjunction needs operator action.
        """
        return self.observation.enabled and not self.enable.admitted


def classify_plugin(observation: PluginObservation) -> PluginVerdict:
    """Decide both axes for one observed plugin (pure; fail-closed on both)."""
    review = resolve_review(observation.ref)
    return PluginVerdict(
        observation=observation,
        plugin_class=review.plugin_class if review else CLASS_UNKNOWN,
        build_provenance=review.build_provenance if review else BUILD_UNREVIEWED,
        review_anchor=review.review_anchor if review else "",
        enable=decide_enable(observation, review),
        install=decide_install(observation, review),
    )


__all__ = (
    "ADMISSIBLE_BUILD_PROVENANCES",
    "BUILD_NONE",
    "BUILD_PROVENANCES",
    "BUILD_REMOTE_ARTIFACT",
    "BUILD_SOURCE_ONLY",
    "BUILD_UNREVIEWED",
    "CLASS_AGENT_INPUT_WRITER",
    "CLASS_TEST_ORACLE",
    "CLASS_UNKNOWN",
    "CLASS_UX_ONLY",
    "DENY_CLASSES",
    "DENY_REASONS",
    "ENABLE_SCOPE",
    "ENABLE_SCOPE_STATEMENT",
    "FORBIDDEN_PLUGIN_AUTHORITIES",
    "PLUGIN_CLASSES",
    "REASON_AGENT_INPUT_WRITER",
    "REASON_IDENTITY_MISMATCH",
    "REASON_MALFORMED_RECORD",
    "REASON_MANIFEST_DRIFT",
    "REASON_NO_LANE_AUTHORITY",
    "REASON_UNPINNED_REMOTE_BUILD",
    "REASON_UNPINNED_SOURCE",
    "REASON_UNREVIEWED_BUILD",
    "REASON_UNREVIEWED_PIN",
    "REVIEWED_PLUGINS",
    "SCOPE_ISOLATION_MECHANISM",
    "SCOPE_ROOT_DETERMINANTS",
    "SOURCE_KIND_GITHUB",
    "HerdrPluginPolicyError",
    "PluginObservation",
    "PluginSourceRef",
    "PluginVerdict",
    "PolicyDecision",
    "ReviewedPlugin",
    "build_review_registry",
    "classify_plugin",
    "decide_enable",
    "decide_install",
    "observe_plugin",
    "plan_install",
    "read_source_ref",
    "resolve_review",
)
