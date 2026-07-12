"""Durable herdr workflow-role authority for the default-lane coordinator pair (Redmine #13583).

Problem: the herdr default lane pair (a coordinator's Codex + its Main-unit assistant Claude)
carries no step-time durable authority to tell a ``grandparent_coordinator`` (department root)
apart from a ``project_gateway``, so ``workflow step`` fail-closes
``ambiguous_default_coordinator_role`` even after the topology is configured (Redmine #13581
j#75707). The mzb1 ``role`` segment is a runtime **provider** token (``claude`` / ``codex``),
not a workflow role, and the herdr identity carries no project scope — so provider, pane, and
default placement must never be promoted to a role authority (design principle 4,
``vibes/docs/logics/workflow-step-command-design.md`` §#13489; identity model
``vibes/docs/specs/herdr-native-identity.md`` §1).

This module is the **pure, fail-closed** durable role authority the Design Consultation
(j#75780) / Answer (j#75782) put in that gap. It owns:

- the **closed** vocabulary of durable workflow roles a binding may declare
  (``grandparent_coordinator`` / ``project_gateway``, canonical; ``root_coordinator`` accepted
  only as a compat *input* alias for the grandparent, never re-emitted — j#75782 vocabulary);
- the **versioned, deterministic** ``project_scope -> lane_id`` resolver
  (:func:`project_gateway_lane_id`) so the project-gateway lane id is *derived*, never hand
  copied into several places (Design Answer Q2-A);
- parse + **fail-closed validation** of a repo-local static binding declaration
  (:func:`parse_role_bindings`) — a portable *topology*, NOT runtime state: it stores no
  ``workspace_id`` literal, no liveness / delivery / approval / current-status
  (``vibes/docs/logics/managed-state-model.md`` repo-local static-artifact boundary; the
  ``workspace_id`` stays registry-authoritative and is attested independently at read time);
- resolution of the current lane to a durable role (:func:`resolve_role_for_lane`) from the
  attested ``lane_id`` + the current provider, cross-checked against the **expected** provider
  resolved out of band from ``provider_binding`` — missing / ambiguous / invalid /
  provider-mismatch each fail closed with a fixed reason so a caller never routes on a guess.

It is **pure**: value objects + total functions over plain strings / plain records. It opens no
file, reads no env, scans no inventory (the application loader supplies the parsed record, the
attested workspace, and the expected-provider resolver). Increment 1 (Design Answer j#75782) is
resolution-only — this authority *names* the role; the herdr-native forward consult /
child-intake **send** wiring is a later increment.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Optional, Sequence

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)

# ---------------------------------------------------------------------------
# Static binding-file schema (machine-readable; kept literal regardless of UI language).
# ---------------------------------------------------------------------------
#: The tracked static artifact name (repo-local, portable topology declaration).
BINDINGS_FILENAME = "workflow-role-bindings.json"
#: The schema discriminator the file must carry.
SCHEMA_NAME = "mozyo.workflow-role-bindings"
#: The schema version. Bumped when the on-disk shape changes incompatibly.
SCHEMA_VERSION = 1

#: Closed top-level / per-entry key sets (Redmine #13583 R2): a present-but-malformed declaration
#: with an unknown key fails closed rather than being silently accepted with the key ignored.
_ALLOWED_TOP_KEYS = frozenset({"schema", "version", "bindings"})
_ALLOWED_ENTRY_KEYS = frozenset({"role", "project_scope", "source_pointer"})

# ---------------------------------------------------------------------------
# Role vocabulary. The canonical durable workflow roles a binding may declare; the
# provider space (claude / codex / …) is deliberately NOT part of this vocabulary — a
# binding names a workflow role, and the runtime provider is cross-checked separately.
# ---------------------------------------------------------------------------
#: Compat input alias only: an older ``root_coordinator`` token normalizes to the grandparent.
#: Never written to the canonical vocabulary (j#75782).
ROLE_ROOT_COORDINATOR_ALIAS = "root_coordinator"

#: The CLOSED set of canonical roles a binding resolves to.
CANONICAL_ROLES: frozenset = frozenset(
    {ROLE_GRANDPARENT_COORDINATOR, ROLE_PROJECT_GATEWAY}
)

_ROLE_ALIASES: Mapping[str, str] = {
    ROLE_ROOT_COORDINATOR_ALIAS: ROLE_GRANDPARENT_COORDINATOR,
}

#: The normalized stand-in for an unset lane (mirrors ``herdr_identity.DEFAULT_LANE`` / the
#: pure ``workflow_step_herdr.HERDR_DEFAULT_LANE``); the grandparent coordinator sits here.
DEFAULT_LANE = "default"

# ---------------------------------------------------------------------------
# Versioned project-gateway lane-id derivation. The scheme tag is embedded so a future
# derivation change is observable and cannot silently collide with a lane minted under an
# older scheme; the readable slug keeps the id human-auditable, the digest keeps distinct
# scopes distinct (project-scope lane uniqueness, mid-review audit item).
# ---------------------------------------------------------------------------
LANE_SCHEME = "pgwv1"
_SLUG_RE = re.compile(r"[^a-z0-9]+")
#: Readable-slug budget for the derived lane id (Redmine #13583 R3). Bounded so the lane id,
#: once encoded into an mzb1 assigned name (``mzb1_<ws>_<role>_<lane>`` — each non-``[A-Za-z0-9]``
#: byte escapes to 3 chars) with a 32-hex workspace id + a provider token, stays within the herdr
#: ``NAME_MAX_LENGTH`` (128) so the project-gateway lane can actually be launched / adopted. The
#: full digest below carries injectivity, so truncating the readable slug never risks a collision.
_SLUG_BUDGET = 16


def _norm(value: object) -> str:
    """Trim a raw field to a comparable token (``None`` -> ``""``)."""
    return str(value).strip() if value is not None else ""


def _norm_lane(value: object) -> str:
    """Normalize a lane id, mapping an empty lane to :data:`DEFAULT_LANE` (mirrors herdr)."""
    return _norm(value) or DEFAULT_LANE


def normalize_role(role: object) -> str:
    """Normalize a raw role token to a canonical role, or ``""`` if unknown (pure).

    Trims, maps the ``root_coordinator`` compat alias to ``grandparent_coordinator``, and
    returns a canonical role only when it is in :data:`CANONICAL_ROLES`. An unknown / empty
    token yields ``""`` so the caller fails closed rather than binding an out-of-vocabulary role.
    """
    token = _norm(role)
    token = _ROLE_ALIASES.get(token, token)
    return token if token in CANONICAL_ROLES else ""


class WorkflowRoleAuthorityError(ValueError):
    """A project scope has no valid project-gateway lane (empty / underivable)."""


def project_gateway_lane_id(project_scope: object) -> str:
    """Derive the deterministic, versioned project-gateway lane id for ``project_scope`` (pure).

    The lane id is ``<scheme>_<slug>-<digest>`` — a readable slug of the scope (bounded to
    :data:`_SLUG_BUDGET` chars so the lane fits the mzb1 assigned-name length limit, Redmine
    #13583 R3) plus a short stable digest of the *exact* raw scope so two distinct scopes never
    collide onto one lane (and a slug that reduces to empty still derives a stable id from the
    digest alone). The result can never equal :data:`DEFAULT_LANE`, so a project gateway lane can
    never collide with the grandparent's default lane. Fails closed
    (:class:`WorkflowRoleAuthorityError`) on an empty scope — a grandparent has no project scope
    and is not derived here; a project gateway must name one.
    """
    scope = _norm(project_scope)
    if not scope:
        raise WorkflowRoleAuthorityError(
            "a project gateway lane requires a non-empty project scope; the grandparent "
            "coordinator (no scope) sits in the default lane and is not derived here"
        )
    slug = _SLUG_RE.sub("-", scope.lower()).strip("-")[:_SLUG_BUDGET].strip("-")
    digest = hashlib.sha256(scope.encode("utf-8")).hexdigest()[:12]
    core = f"{slug}-{digest}" if slug else digest
    return f"{LANE_SCHEME}_{core}"


# ---------------------------------------------------------------------------
# Value objects.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WorkflowRoleBinding:
    """One durable role binding: a canonical role at its derived lane (value object).

    ``lane_id`` is **derived** (never stored in the file): :data:`DEFAULT_LANE` for the
    grandparent, :func:`project_gateway_lane_id` for a project gateway. ``project_scope`` is
    ``""`` for the grandparent and the declared scope for a gateway. ``source_pointer`` is an
    advisory durable pointer (e.g. ``redmine:#13583``) for review only — never a route authority.
    """

    role: str
    project_scope: str
    lane_id: str
    source_pointer: str = ""


# Parse-time status: whether the whole declaration is well-formed.
@dataclass(frozen=True)
class ParsedRoleBindings:
    """The parsed + validated binding declaration, or a fail-closed reason (value object)."""

    ok: bool
    bindings: tuple = ()
    reason: str = ""
    detail: str = ""

    @classmethod
    def valid(cls, bindings: Sequence[WorkflowRoleBinding]) -> "ParsedRoleBindings":
        return cls(ok=True, bindings=tuple(bindings))

    @classmethod
    def invalid(cls, detail: str) -> "ParsedRoleBindings":
        return cls(ok=False, reason=REASON_ROLE_BINDING_INVALID, detail=detail)

    @classmethod
    def empty(cls) -> "ParsedRoleBindings":
        """A well-formed declaration with no bindings (an absent file resolves to this)."""
        return cls(ok=True, bindings=())


# Resolution status tokens (machine-readable).
STATUS_RESOLVED = "resolved"
STATUS_MISSING = "missing"  # no explicit binding for this lane -> caller keeps existing class
STATUS_AMBIGUOUS = "ambiguous"
STATUS_INVALID = "invalid"
STATUS_PROVIDER_MISMATCH = "provider_mismatch"

# Fixed reason tokens surfaced by the herdr step resolver for the blocking statuses.
REASON_ROLE_BINDING_INVALID = "herdr_role_binding_invalid"
REASON_ROLE_BINDING_AMBIGUOUS = "herdr_role_binding_ambiguous"
REASON_ROLE_PROVIDER_MISMATCH = "herdr_role_provider_mismatch"

_STATUS_REASON: Mapping[str, str] = {
    STATUS_INVALID: REASON_ROLE_BINDING_INVALID,
    STATUS_AMBIGUOUS: REASON_ROLE_BINDING_AMBIGUOUS,
    STATUS_PROVIDER_MISMATCH: REASON_ROLE_PROVIDER_MISMATCH,
}


@dataclass(frozen=True)
class WorkflowRoleResolution:
    """The resolved durable role for the current lane, or a fail-closed status (value object)."""

    status: str
    role: str = ""
    project_scope: str = ""
    lane_id: str = ""
    source_pointer: str = ""
    detail: str = ""

    @property
    def resolved(self) -> bool:
        return self.status == STATUS_RESOLVED

    @property
    def missing(self) -> bool:
        """No explicit binding for this lane: the caller keeps its existing classification."""
        return self.status == STATUS_MISSING

    @property
    def blocked(self) -> bool:
        """A present-but-unusable authority (invalid / ambiguous / provider mismatch)."""
        return self.status in _STATUS_REASON

    @property
    def reason(self) -> str:
        """The fixed reason token for a blocking status (``""`` for resolved / missing)."""
        return _STATUS_REASON.get(self.status, "")


# ---------------------------------------------------------------------------
# Parsing + validation.
# ---------------------------------------------------------------------------


def _entry_type_error(index: int, entry: Mapping) -> str:
    """Return a fixed error message if an entry's fields are the wrong type, else ``""`` (R2).

    ``role`` must be a non-empty string; ``project_scope`` / ``source_pointer``, when present, must
    be strings. A non-string value is rejected rather than silently ``str()``-coerced (a
    ``project_scope: 123`` must not become the scope ``"123"``).
    """
    role = entry.get("role")
    if not isinstance(role, str) or not role.strip():
        return f"binding #{index} role must be a non-empty string; got {role!r}"
    for field in ("project_scope", "source_pointer"):
        value = entry.get(field)
        if value is not None and not isinstance(value, str):
            return f"binding #{index} {field} must be a string when present; got {value!r}"
    return ""


def parse_role_bindings(record: object) -> ParsedRoleBindings:
    """Parse + fail-closed validate a static binding declaration into :class:`ParsedRoleBindings`.

    ``record`` is the decoded JSON object (or ``None`` for an absent file -> :meth:`empty`). The
    schema is **closed** (Redmine #13583 R2): the declaration must carry exactly ``schema`` ==
    :data:`SCHEMA_NAME`, ``version`` == :data:`SCHEMA_VERSION`, and a ``bindings`` **list**
    (present and a list — a missing / null ``bindings`` is *not* an empty authority; only an
    explicit ``[]`` is). Each entry has only ``{role, project_scope?, source_pointer?}`` with
    string values; the ``lane_id`` is derived, never read from the file. A **present-but-malformed
    declaration never falls through like an absent file** — it fails closed (an ``invalid`` result
    with a fixed reason) on:

    - a wrong schema / version, an unknown top-level key, or a missing / non-list ``bindings``;
    - an entry that is not an object, an unknown entry key, a non-string / unknown / out-of-vocabulary
      role, or a non-string project scope / source pointer;
    - a grandparent with a non-empty project scope, or a project gateway with an empty scope;
    - two grandparent entries, or two entries that derive the same lane id (slot collision).
    """
    if record is None:
        return ParsedRoleBindings.empty()
    if not isinstance(record, Mapping):
        return ParsedRoleBindings.invalid(
            f"binding declaration must be a JSON object, got {type(record).__name__}"
        )
    unknown_top = set(record.keys()) - _ALLOWED_TOP_KEYS
    if unknown_top:
        return ParsedRoleBindings.invalid(
            f"unknown top-level key(s) {sorted(unknown_top)}; allowed: {sorted(_ALLOWED_TOP_KEYS)}"
        )
    if _norm(record.get("schema")) != SCHEMA_NAME:
        return ParsedRoleBindings.invalid(
            f"unexpected schema {record.get('schema')!r}; expected {SCHEMA_NAME!r}"
        )
    if record.get("version") != SCHEMA_VERSION:
        return ParsedRoleBindings.invalid(
            f"unexpected schema version {record.get('version')!r}; expected {SCHEMA_VERSION}"
        )
    raw_bindings = record.get("bindings")
    if not isinstance(raw_bindings, list):
        return ParsedRoleBindings.invalid(
            "`bindings` must be present and a list (an empty topology is an explicit `[]`, "
            f"never a missing / null key); got {type(raw_bindings).__name__}"
        )

    bindings: list = []
    lane_ids: dict = {}
    seen_grandparent = False
    for index, entry in enumerate(raw_bindings):
        if not isinstance(entry, Mapping):
            return ParsedRoleBindings.invalid(
                f"binding #{index} must be an object, got {type(entry).__name__}"
            )
        unknown_entry = set(entry.keys()) - _ALLOWED_ENTRY_KEYS
        if unknown_entry:
            return ParsedRoleBindings.invalid(
                f"binding #{index} has unknown key(s) {sorted(unknown_entry)}; "
                f"allowed: {sorted(_ALLOWED_ENTRY_KEYS)}"
            )
        type_error = _entry_type_error(index, entry)
        if type_error:
            return ParsedRoleBindings.invalid(type_error)
        role = normalize_role(entry.get("role"))
        if not role:
            return ParsedRoleBindings.invalid(
                f"binding #{index} has an unknown / empty role {entry.get('role')!r}; "
                f"expected one of {sorted(CANONICAL_ROLES)} (or the {ROLE_ROOT_COORDINATOR_ALIAS!r} alias)"
            )
        scope = _norm(entry.get("project_scope"))
        source_pointer = _norm(entry.get("source_pointer"))

        if role == ROLE_GRANDPARENT_COORDINATOR:
            if scope:
                return ParsedRoleBindings.invalid(
                    f"binding #{index} (grandparent_coordinator) must not declare a project "
                    f"scope; the department-root coordinator has none (got {scope!r})"
                )
            if seen_grandparent:
                return ParsedRoleBindings.invalid(
                    "more than one grandparent_coordinator binding; the default lane holds "
                    "exactly one department-root coordinator"
                )
            seen_grandparent = True
            lane_id = DEFAULT_LANE
        else:  # ROLE_PROJECT_GATEWAY
            if not scope:
                return ParsedRoleBindings.invalid(
                    f"binding #{index} (project_gateway) must declare a non-empty project scope"
                )
            try:
                lane_id = project_gateway_lane_id(scope)
            except WorkflowRoleAuthorityError as exc:  # defensive; scope is non-empty here
                return ParsedRoleBindings.invalid(f"binding #{index}: {exc}")

        if lane_id in lane_ids:
            return ParsedRoleBindings.invalid(
                f"binding #{index} derives lane id {lane_id!r} already used by binding "
                f"#{lane_ids[lane_id]} (slot collision); a lane holds one role"
            )
        lane_ids[lane_id] = index
        bindings.append(
            WorkflowRoleBinding(
                role=role,
                project_scope=scope,
                lane_id=lane_id,
                source_pointer=source_pointer,
            )
        )

    return ParsedRoleBindings.valid(bindings)


# ---------------------------------------------------------------------------
# Resolution.
# ---------------------------------------------------------------------------


def resolve_role_for_lane(
    parsed: ParsedRoleBindings,
    *,
    lane_id: object,
    provider: object,
    expected_provider: Callable[[str], Optional[str]],
) -> WorkflowRoleResolution:
    """Resolve the current lane's durable role from a parsed declaration (pure, fail-closed).

    - a malformed declaration -> :data:`STATUS_INVALID` (the whole authority is untrustworthy);
    - no binding whose derived lane matches the current ``lane_id`` -> :data:`STATUS_MISSING`
      (this lane is not grandparent / gateway — the caller keeps its existing classification);
    - two bindings matching the same lane -> :data:`STATUS_AMBIGUOUS` (defensive; validation
      already rejects a slot collision, but resolution never guesses);
    - the current ``provider`` disagrees with the ``expected_provider`` for the bound role, or the
      expected provider cannot be resolved -> :data:`STATUS_PROVIDER_MISMATCH` (fail closed rather
      than accept a default-lane pair whose Codex/Claude surface is not the configured coordinator);
    - otherwise -> :data:`STATUS_RESOLVED` with the canonical role + declared project scope.

    ``expected_provider`` maps a canonical role to the provider ``provider_binding`` expects for
    it (resolved out of band by the caller); a ``None`` return means the binding names no provider
    for the role and the lane cannot be trusted as that coordinator surface.
    """
    if not parsed.ok:
        return WorkflowRoleResolution(status=STATUS_INVALID, detail=parsed.detail)

    lane = _norm_lane(lane_id)
    matches = [b for b in parsed.bindings if b.lane_id == lane]
    if not matches:
        return WorkflowRoleResolution(
            status=STATUS_MISSING,
            lane_id=lane,
            detail="no durable workflow-role binding for this lane",
        )
    if len(matches) > 1:
        return WorkflowRoleResolution(
            status=STATUS_AMBIGUOUS,
            lane_id=lane,
            detail=f"{len(matches)} bindings match lane {lane!r}",
        )

    binding = matches[0]
    want = expected_provider(binding.role)
    have = _norm(provider)
    if not _norm(want) or have != _norm(want):
        return WorkflowRoleResolution(
            status=STATUS_PROVIDER_MISMATCH,
            role=binding.role,
            project_scope=binding.project_scope,
            lane_id=binding.lane_id,
            source_pointer=binding.source_pointer,
            detail=(
                f"lane provider {have!r} does not match the expected provider "
                f"{_norm(want)!r} for role {binding.role!r} (from provider_binding)"
            ),
        )

    return WorkflowRoleResolution(
        status=STATUS_RESOLVED,
        role=binding.role,
        project_scope=binding.project_scope,
        lane_id=binding.lane_id,
        source_pointer=binding.source_pointer,
        detail=f"durable workflow-role binding: {binding.role} (lane {binding.lane_id})",
    )


__all__ = (
    "BINDINGS_FILENAME",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "ROLE_ROOT_COORDINATOR_ALIAS",
    "CANONICAL_ROLES",
    "DEFAULT_LANE",
    "LANE_SCHEME",
    "WorkflowRoleAuthorityError",
    "normalize_role",
    "project_gateway_lane_id",
    "WorkflowRoleBinding",
    "ParsedRoleBindings",
    "WorkflowRoleResolution",
    "STATUS_RESOLVED",
    "STATUS_MISSING",
    "STATUS_AMBIGUOUS",
    "STATUS_INVALID",
    "STATUS_PROVIDER_MISMATCH",
    "REASON_ROLE_BINDING_INVALID",
    "REASON_ROLE_BINDING_AMBIGUOUS",
    "REASON_ROLE_PROVIDER_MISMATCH",
    "parse_role_bindings",
    "resolve_role_for_lane",
)
