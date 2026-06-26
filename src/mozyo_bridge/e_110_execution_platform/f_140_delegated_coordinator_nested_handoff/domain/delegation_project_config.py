"""External-parent delegation child-candidate config + resolver (Redmine #12549).

#12548 (j#64716) found the external-parent delegation path has no shipped way to
turn a *parent* project's config into a concrete, public-safe child project
candidate without the operator hand-injecting a route / pane / worktree hint.
This module is that missing seam — the typed ``delegation:`` child-candidate
*schema boundary* plus a pure :func:`resolve_child_candidate` resolver. It is the
follow-up #2 (``delegation policy resolver``) named in
``vibes/docs/specs/delegation-policy-project-config.md`` and conforms to the
classical acceptance oracle pinned in
``tests/test_delegated_coordinator_acceptance_oracle.py`` (#12547): a missing
candidate yields :data:`CHILD_CANDIDATE_MISSING` and an ambiguous one yields
:data:`CHILD_CANDIDATE_AMBIGUOUS`.

What this surface is, kept enforced in code:

- **Schema only — no IO, no parsing, no actuation.**
  :meth:`DelegationConfig.from_record` normalizes an already-parsed mapping (the
  in-memory shape ``yaml.safe_load`` of a ``.mozyo-bridge/config.yaml``
  ``delegation:`` block would yield). It reads no file, opens no tmux, performs
  no Redmine write and sends no handoff. :func:`resolve_child_candidate` is a
  pure function over the parsed config; its output is *decision-support /
  executable-handoff input* for the #12550 planner only — never a PASS, never a
  side effect.
- **Public-safe child candidate.** A :class:`ChildCandidate` may carry only a
  portable child-project identifier and a set of public capability tokens. It may
  never carry a private pane id, host / absolute path, cockpit composition,
  credential, owner approval, close authority, route / routing override, target
  pane, role, or direct-send authority. Such a key — or a value shaped like one,
  or like a private filesystem path — fails closed through
  :class:`DelegationConfigError`.
- **Behavior-preserving by default.** ``None`` / an empty mapping / a
  ``delegation:`` block with no ``child_candidates`` all resolve to
  :meth:`DelegationConfig.default` (no candidates). A repo with no
  ``delegation:`` block therefore exposes no delegated child candidate — the
  pre-existing single-coordinator + sublane behavior is preserved.
- **Fail-closed, closed schema.** Unknown top-level / candidate keys, an
  unsupported version, a non-mapping record, a duplicate capability, a missing /
  non-string project id, and any boundary- or private-path-shaped token are
  rejected with a :class:`DelegationConfigError` — never a raw parser exception
  and never a silent normalization.

Missing / ambiguous resolution is *not* a schema error: those are expected,
structured runtime outcomes the #12550 planner must branch on, so they are
returned as a :class:`ChildCandidateResolution` status (mirroring the oracle's
``resolved`` / ``missing`` / ``ambiguous`` verdict vocabulary) rather than
raised. Only malformed / unsafe *config* raises.

The module is pure (dataclasses + small validation helpers) and imports nothing
from the rest of the package, so :mod:`mozyo_bridge.domain.repo_local_config`
can compose it as the ``delegation`` top-level surface without a cycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Optional

#: The supported ``delegation`` config record version. Optional in a record and
#: defaults to this; any other value is rejected so a future, not-yet-understood
#: schema never reads as version 1 (mirrors the repo-local config version rule).
DELEGATION_CONFIG_VERSION: int = 1

#: The closed set of recognized keys in the ``delegation:`` block. The policy
#: knobs (``enable_delegated_coordinator`` / ``max_delegation_depth`` / …) named
#: in ``spec-delegation-policy-project-config`` are a *separate* follow-up loader
#: (that spec's follow-up #1); #12549 ships only the child-candidate surface, so
#: those knobs are deliberately not yet accepted here and fail closed as unknown
#: keys until that loader lands.
DELEGATION_CONFIG_KEYS: frozenset[str] = frozenset({"version", "child_candidates"})

#: The closed set of recognized keys inside one ``child_candidates`` entry.
CHILD_CANDIDATE_KEYS: frozenset[str] = frozenset({"child_project", "capabilities"})

#: Resolution status / diagnostic vocabulary. The status tokens mirror the
#: #12547 acceptance oracle's ``child_candidate`` field (``resolved`` / ``missing``
#: / ``ambiguous``); the ``CHILD_CANDIDATE_*`` diagnostics are the exact reason
#: strings that oracle fails closed with, so the resolver and the oracle cannot
#: drift apart.
STATUS_RESOLVED: str = "resolved"
STATUS_MISSING: str = "missing"
STATUS_AMBIGUOUS: str = "ambiguous"

CHILD_CANDIDATE_RESOLVED: str = "child_candidate_resolved"
CHILD_CANDIDATE_MISSING: str = "child_candidate_missing"
CHILD_CANDIDATE_AMBIGUOUS: str = "child_candidate_ambiguous"

#: Substrings that, appearing in a ``delegation`` *key*, signal an attempt to
#: cross a boundary this surface does not own: address a route / target / pane /
#: role, grant or alter authority / approval / close / ownership, drive a send,
#: name a host / path / worktree / cockpit window, or carry a credential. A
#: candidate's schema is closed to :data:`CHILD_CANDIDATE_KEYS`, so a key like
#: ``close_authority`` / ``target_pane`` / ``owner_approval`` is rejected here
#: with a boundary-specific message rather than the generic unknown-key error,
#: making the rejection read as deliberate in an audit.
_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    "route",
    "routing",
    "target",
    "pane",
    "role",
    "send",
    "dispatch",
    "authority",
    "authorities",
    "approval",
    "approve",
    "grant",
    "owner",
    "review",
    "close",
    "import",
    "module",
    "callable",
    "entry",
    "plugin",
    "exec",
    "eval",
    "script",
    "load",
    "host",
    "path",
    "worktree",
    "cockpit",
    "window",
    "lane",
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "auth",
    "billing",
)

#: Substrings forbidden in a candidate *value* (the ``child_project`` id or a
#: capability token). Narrower than the key set on one deliberate point: the bare
#: role-chain capability ``review`` is legitimate (``implementation`` / ``review``
#: are the shipped capabilities), so ``review`` is the one authority-shaped word
#: NOT screened out — every other authority / leakage token still is. This keeps
#: two protections: a value must never *leak* private topology or a credential
#: (host / path / pane / route / target / send / cockpit-window / secret), and a
#: value must never be an authority-grant phrase (``owner_approval`` /
#: ``close_authority`` are rejected via ``owner`` / ``approval`` / ``authority`` /
#: ``close``). Path-shape itself is caught separately by
#: :func:`_reject_private_path_shaped`.
_FORBIDDEN_VALUE_PARTS: tuple[str, ...] = (
    "route",
    "routing",
    "target",
    "pane",
    "send",
    "dispatch",
    "host",
    "worktree",
    "cockpit",
    "window",
    "lane",
    "secret",
    "token",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "credential",
    "billing",
    "authority",
    "authorities",
    "approval",
    "approve",
    "owner",
    "grant",
    "close",
    "auth",
)


class DelegationConfigError(ValueError):
    """The ``delegation`` config record violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors. The composing
    :mod:`mozyo_bridge.domain.repo_local_config` re-raises this as its own
    ``RepoLocalConfigError`` so the loader keeps a single fail-closed boundary.
    """


def _reject_boundary_token(
    token: object, *, role: str, parts: "tuple[str, ...]"
) -> None:
    """Fail closed on a delegation string that crosses a boundary it cannot own.

    A non-string token is left for the caller's type check; a string whose name
    contains one of ``parts`` (use :data:`_FORBIDDEN_KEY_PARTS` for a structural
    key, :data:`_FORBIDDEN_VALUE_PARTS` for an opaque identifier value) is
    rejected here so the rejection reads as deliberate in an audit.
    """
    if not isinstance(token, str):
        return
    lowered = token.lower()
    for part in parts:
        if part in lowered:
            raise DelegationConfigError(
                f"delegation {role} {token!r} may not carry a boundary token: a "
                f"child candidate is a public-safe identifier and may never "
                f"address a route / target / pane / role, grant authority, drive "
                f"a send, name a host / path / worktree / cockpit, or carry a "
                f"credential (matched forbidden token {part!r})."
            )


def _reject_private_path_shaped(value: str, *, role: str) -> None:
    """Fail closed on a value shaped like a private filesystem / host path.

    A public-safe child-project identifier or capability token never contains a
    path separator, a home-dir prefix, a URL scheme, or a Windows drive — those
    shapes are how a private host topology leaks into a tracked config. Reject
    them explicitly (``## Public / Private boundary`` of
    ``spec-delegation-policy-project-config``), separately from the boundary-token
    screen, so the diagnostic names the leak.
    """
    leaked = (
        "/" in value
        or "\\" in value
        or value.startswith("~")
        or "://" in value
        or (len(value) >= 2 and value[1] == ":")  # Windows drive, e.g. C:
    )
    if leaked:
        raise DelegationConfigError(
            f"delegation {role} {value!r} is shaped like a private host / "
            f"filesystem path; a child candidate must carry only a portable, "
            f"public-safe identifier (no path separator, home prefix, URL "
            f"scheme, or drive letter)."
        )


def _checked_version(record: "Mapping[object, object]") -> int:
    """Return the supported version, failing closed on anything else.

    ``version`` is optional and defaults to :data:`DELEGATION_CONFIG_VERSION`.
    ``bool`` is rejected even though it is an ``int`` subclass so ``version:
    true`` does not silently read as version ``1``.
    """
    version = record.get("version", DELEGATION_CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise DelegationConfigError(
            f"delegation config 'version' must be an integer, got {version!r}"
        )
    if version != DELEGATION_CONFIG_VERSION:
        raise DelegationConfigError(
            f"unsupported delegation config version {version!r}; this build "
            f"understands version {DELEGATION_CONFIG_VERSION}"
        )
    return version


def _reject_unknown_keys(
    record: "Mapping[object, object]", *, allowed: "frozenset[str]", source: str
) -> None:
    """Fail closed on a non-string / boundary-crossing / unknown record key."""
    for key in record:
        if not isinstance(key, str) or not key:
            raise DelegationConfigError(
                f"{source} record keys must be non-empty strings; got {key!r}"
            )
        _reject_boundary_token(key, role=f"{source} key", parts=_FORBIDDEN_KEY_PARTS)
        if key not in allowed:
            raise DelegationConfigError(
                f"{source} record has unknown key {key!r}; allowed keys: "
                f"{sorted(allowed)}"
            )


def _checked_capabilities(raw: object) -> "frozenset[str]":
    """Normalize a candidate's ``capabilities`` into a closed token set.

    Absent / ``None`` yields the empty set (a project-level candidate that only
    matches a capability-agnostic request). Otherwise it must be a list/tuple of
    non-empty, public-safe, boundary-clean strings with no duplicates — a
    duplicate is treated as malformed config, not silently collapsed.
    """
    if raw is None:
        return frozenset()
    if isinstance(raw, str) or not isinstance(raw, Sequence):
        raise DelegationConfigError(
            "delegation child candidate 'capabilities' must be a list of strings, "
            f"got {type(raw).__name__}"
        )
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item:
            raise DelegationConfigError(
                "delegation child candidate capability must be a non-empty "
                f"string, got {item!r}"
            )
        _reject_boundary_token(
            item, role="child candidate capability", parts=_FORBIDDEN_VALUE_PARTS
        )
        _reject_private_path_shaped(item, role="child candidate capability")
        if item in seen:
            raise DelegationConfigError(
                f"delegation child candidate has a duplicate capability {item!r}"
            )
        seen.add(item)
    return frozenset(seen)


@dataclass(frozen=True)
class ChildCandidate:
    """A public-safe candidate child project resolvable from parent config.

    :attr:`child_project` is a portable, public-safe project identifier (e.g.
    ``mozyo_bridge``) — never a private Redmine project name, host path, or lane
    name. :attr:`capabilities` is the set of public capability tokens (e.g.
    ``implementation``) the candidate can serve; an empty set means the candidate
    is only matched by a capability-agnostic request.

    The record deliberately cannot express a route, target pane, role, send /
    approval / close authority, credential, or any host topology: it is the input
    a downstream planner (#12550) turns into an *executable* handoff, never the
    handoff itself.
    """

    child_project: str
    capabilities: "frozenset[str]" = field(default_factory=frozenset)

    @classmethod
    def from_record(cls, record: "Mapping[str, object]") -> "ChildCandidate":
        """Normalize one ``child_candidates`` entry, failing closed on any leak."""
        if not isinstance(record, Mapping):
            raise DelegationConfigError(
                "delegation child candidate must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_unknown_keys(
            record, allowed=CHILD_CANDIDATE_KEYS, source="delegation child candidate"
        )
        project = record.get("child_project")
        if not isinstance(project, str) or not project:
            raise DelegationConfigError(
                "delegation child candidate 'child_project' must be a non-empty "
                f"string, got {project!r}"
            )
        _reject_boundary_token(
            project, role="child candidate project", parts=_FORBIDDEN_VALUE_PARTS
        )
        _reject_private_path_shaped(project, role="child candidate project")
        capabilities = _checked_capabilities(record.get("capabilities"))
        return cls(child_project=project, capabilities=capabilities)


@dataclass(frozen=True)
class DelegationConfig:
    """The closed ``delegation:`` block (child-candidate surface only, schema-only).

    Holds the parsed, public-safe :attr:`child_candidates`. The default (no
    candidates) is behavior-preserving: a repo with no ``delegation:`` block — or
    one with no ``child_candidates`` — exposes no delegated child candidate, so
    the pre-existing single-coordinator + sublane behavior is unchanged.
    """

    child_candidates: tuple[ChildCandidate, ...] = ()

    @classmethod
    def default(cls) -> "DelegationConfig":
        """The behavior-preserving default: no delegated child candidates."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "DelegationConfig":
        """Normalize a parsed ``delegation:`` mapping into a typed config.

        ``None`` / an empty mapping / a block with no ``child_candidates`` yields
        the no-candidate default. A non-mapping record, an unknown / boundary
        key, an unsupported version, a non-list ``child_candidates``, or any
        candidate that leaks a boundary or private-path token fails closed.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise DelegationConfigError(
                "delegation config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_unknown_keys(
            record, allowed=DELEGATION_CONFIG_KEYS, source="delegation config"
        )
        _checked_version(record)
        raw_candidates = record.get("child_candidates")
        if raw_candidates is None:
            return cls.default()
        if isinstance(raw_candidates, (str, bytes, Mapping)) or not isinstance(
            raw_candidates, Sequence
        ):
            raise DelegationConfigError(
                "delegation config 'child_candidates' must be a list of mappings, "
                f"got {type(raw_candidates).__name__}"
            )
        candidates = tuple(
            ChildCandidate.from_record(entry) for entry in raw_candidates
        )
        return cls(child_candidates=candidates)


@dataclass(frozen=True)
class ChildCandidateResolution:
    """Decision-support output of :func:`resolve_child_candidate`.

    This is *executable-handoff input* for the #12550 planner, never a PASS and
    never an action. :attr:`status` mirrors the #12547 oracle's ``resolved`` /
    ``missing`` / ``ambiguous`` vocabulary; :attr:`diagnostic` is the matching
    ``child_candidate_*`` reason string the oracle fails closed with.
    :attr:`candidate` is the single resolved candidate iff :attr:`is_resolved`,
    else ``None``. The requested project / capability are echoed back so the
    planner has the full resolution context without re-deriving it.
    """

    status: str
    diagnostic: str
    requested_child_project: str
    requested_capability: Optional[str]
    candidate: Optional[ChildCandidate] = None

    @property
    def is_resolved(self) -> bool:
        """True only for an unambiguous single match — never for a partial result."""
        return self.status == STATUS_RESOLVED


def resolve_child_candidate(
    config: DelegationConfig,
    *,
    child_project: str,
    capability: Optional[str] = None,
) -> ChildCandidateResolution:
    """Resolve exactly one public-safe child candidate from parent config.

    Pure decision-support. Given a requested ``child_project`` (and optionally a
    ``capability``), match candidates declared in ``config``:

    - a candidate matches when its :attr:`~ChildCandidate.child_project` equals
      the request and, when a ``capability`` is requested, that capability is in
      the candidate's declared set (a capability-agnostic request ignores the
      capability dimension, so it matches every candidate for that project);
    - **no match** fails closed with status :data:`STATUS_MISSING` /
      diagnostic :data:`CHILD_CANDIDATE_MISSING`;
    - **more than one match** fails closed with status :data:`STATUS_AMBIGUOUS` /
      diagnostic :data:`CHILD_CANDIDATE_AMBIGUOUS` (never silently fabricated
      into one PASS);
    - **exactly one match** returns status :data:`STATUS_RESOLVED` with that
      candidate.

    The result performs no cockpit lane creation, tmux mutation, Redmine write,
    or handoff send — it only prepares input for the #12550 planner / actuator.
    A bad request (non-string / empty project or capability) is a programming
    error and fails closed via :class:`DelegationConfigError`.
    """
    if not isinstance(child_project, str) or not child_project:
        raise DelegationConfigError(
            f"resolve_child_candidate requires a non-empty child_project, got "
            f"{child_project!r}"
        )
    if capability is not None and (not isinstance(capability, str) or not capability):
        raise DelegationConfigError(
            f"resolve_child_candidate capability, when given, must be a non-empty "
            f"string, got {capability!r}"
        )

    matches = [
        candidate
        for candidate in config.child_candidates
        if candidate.child_project == child_project
        and (capability is None or capability in candidate.capabilities)
    ]

    if not matches:
        return ChildCandidateResolution(
            status=STATUS_MISSING,
            diagnostic=CHILD_CANDIDATE_MISSING,
            requested_child_project=child_project,
            requested_capability=capability,
        )
    if len(matches) > 1:
        return ChildCandidateResolution(
            status=STATUS_AMBIGUOUS,
            diagnostic=CHILD_CANDIDATE_AMBIGUOUS,
            requested_child_project=child_project,
            requested_capability=capability,
        )
    return ChildCandidateResolution(
        status=STATUS_RESOLVED,
        diagnostic=CHILD_CANDIDATE_RESOLVED,
        requested_child_project=child_project,
        requested_capability=capability,
        candidate=matches[0],
    )


__all__ = (
    "DELEGATION_CONFIG_VERSION",
    "DELEGATION_CONFIG_KEYS",
    "CHILD_CANDIDATE_KEYS",
    "STATUS_RESOLVED",
    "STATUS_MISSING",
    "STATUS_AMBIGUOUS",
    "CHILD_CANDIDATE_RESOLVED",
    "CHILD_CANDIDATE_MISSING",
    "CHILD_CANDIDATE_AMBIGUOUS",
    "DelegationConfigError",
    "ChildCandidate",
    "DelegationConfig",
    "ChildCandidateResolution",
    "resolve_child_candidate",
)
