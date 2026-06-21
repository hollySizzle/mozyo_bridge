"""Role profile template resolution for handoff prompt expansion (Redmine #12388).

US #12388 / Task #12396 implements the send-side resolution of the fixed role
profile templates defined by US #12387
(``vibes/docs/specs/delegated-coordinator-role-profile.md`` ``## 固定 role
profile template``). That spec is the human-facing source of truth for the
template *bodies*; this module is the runtime resolver that

- pins the builtin templates as code constants so resolution is self-contained
  and fail-closed (no filesystem path guessing at send time),
- substitutes the ``<...>`` placeholders from handoff structured fields,
- carries the structured ``role_profile`` / ``profile_source`` /
  ``profile_version`` fields so the receiver never has to discover the template
  path itself, and
- fails closed when an unknown role profile is requested (template missing).

Per the #12387 design the role profile is the receiver's *custom instruction*
and stays separate from the handoff *structured fields*: this module never
mutates the routing landing marker. The resolved contract is carried in the
durable delivery record and a compact single-line pointer in the pane body; the
durable anchor remains the source of truth.

When a template body changes, bump :data:`ROLE_PROFILE_VERSION` so the persisted
``profile_version`` stays a faithful pointer to the resolved contract text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Mapping, Optional


class RoleProfileError(ValueError):
    """A role profile could not be resolved (e.g. unknown / missing template)."""


# Stable identifier for the builtin template set. Date-stamped to the #12387
# spec body it mirrors; bump on any template-body edit so a persisted
# ``profile_version`` always points at the contract text that was sent.
ROLE_PROFILE_VERSION = "2026-06-21"

# Repo-relative pointer to the human-facing source of truth for the template
# bodies. Persisted as ``profile_source`` so the receiver reads the role
# contract without guessing a path.
ROLE_PROFILE_SOURCE = "vibes/docs/specs/delegated-coordinator-role-profile.md"

ROLE_COORDINATOR = "coordinator"
ROLE_DELEGATED_COORDINATOR = "delegated_coordinator"
ROLE_IMPLEMENTATION_GATEWAY = "implementation_gateway"
ROLE_IMPLEMENTATION_WORKER = "implementation_worker"

# Insertion-ordered so CLI ``choices`` and help text list the roles top-down by
# authority, matching the spec's ``## role 語彙`` ordering.
ROLE_PROFILE_TEMPLATES: dict[str, str] = {
    ROLE_COORDINATOR: (
        "# role profile: coordinator\n"
        "- あなたは <project> の最上位 coordinator (管制塔 Codex) である。\n"
        "- owner-facing 判断、owner approval 回収、親 issue / US の Review Gate と close を担う。\n"
        "- 実装 diff は自分で作らず、子 lane / sublane へ委譲する。\n"
        "- owner-approval-waiting はすべてあなたに集約される単一 aggregation point である。\n"
        "- durable record: <redmine_project> の issue / journal。"
    ),
    ROLE_DELEGATED_COORDINATOR: (
        "# role profile: delegated_coordinator\n"
        "- あなたは <parent_project> から委譲された <child_project> の delegated_coordinator である。\n"
        "- 委譲元 (parent coordinator route): <parent_callback_target>。\n"
        "- 子 project 内の dispatch / audit / 子 issue close を担うが、親 issue (<parent_issue>) は close しない。\n"
        "- owner approval は自 lane で回収せず、parent coordinator route へ callback して戻す。\n"
        "- downstream (孫) dispatch は shallow delegation のみ。主目的は context window 圧迫回避。\n"
        "- handoff-worthy state は parent coordinator route へ callback する。\n"
        "- durable record: <redmine_project> の issue / journal。"
    ),
    ROLE_IMPLEMENTATION_GATEWAY: (
        "# role profile: implementation_gateway\n"
        "- あなたは <lane> の implementation_gateway (target-lane Codex) である。\n"
        "- cross-lane handoff を受け、durable anchor <durable_anchor> を読み、自 lane の request か確認する。\n"
        "- same-lane の implementation_worker へ submit 完結で route する。\n"
        "- blocked / review-ready / owner-action-needed を上位 (<upstream_coordinator>) へ callback する。\n"
        "- 実装 diff は作らない。owner approval / parent close は扱わない。"
    ),
    ROLE_IMPLEMENTATION_WORKER: (
        "# role profile: implementation_worker\n"
        "- あなたは <lane> の implementation_worker (sublane Claude) である。\n"
        "- durable anchor <durable_anchor> から実装し、implementation_done / review_request / verification / residual risk を記録する。\n"
        "- owner approval 回収・issue close・coordinator-owned 仕様決定の自己確定はしない。\n"
        "- 仕様矛盾・scope 不足・invariant 衝突に当たったら停止し、design consultation / blocked を記録して same-lane gateway へ callback する。\n"
        "- callback 先 (same-lane gateway): <gateway_callback_target>。"
    ),
}

ROLE_PROFILE_TOKENS: tuple[str, ...] = tuple(ROLE_PROFILE_TEMPLATES.keys())

# Placeholder token shape used in the templates above: ``<lower_snake_case>``.
_PLACEHOLDER_RE = re.compile(r"<([a-z_]+)>")


def template_placeholders(role: str) -> tuple[str, ...]:
    """Return the placeholder field names a role template expects, in order.

    Fails closed (:class:`RoleProfileError`) when the role has no builtin
    template, so callers never silently treat an unknown role as "no fields".
    """
    template = ROLE_PROFILE_TEMPLATES.get(role)
    if template is None:
        raise RoleProfileError(
            f"unknown role profile: {role!r}; expected one of {list(ROLE_PROFILE_TOKENS)}"
        )
    seen: list[str] = []
    for match in _PLACEHOLDER_RE.finditer(template):
        name = match.group(1)
        if name not in seen:
            seen.append(name)
    return tuple(seen)


@dataclass(frozen=True)
class RoleProfileResolution:
    """Resolved role profile carried by a handoff (Redmine #12388).

    ``resolved_text`` is the template body with every supplied placeholder
    substituted; any placeholder without a value is left as its literal
    ``<name>`` token and reported in ``unresolved_placeholders`` so a partial
    resolution is explicit rather than silently dropped (the explicit-fallback
    posture for missing *fields*; a missing *template* fails closed earlier).

    The structured pointer fields (:attr:`role_profile`, :attr:`profile_source`,
    :attr:`profile_version`, :attr:`unresolved_placeholders`) carry no
    operator-supplied free text and are always durable-record safe.
    :attr:`resolved_text` may embed supplied field values, so callers keep it to
    the stdout / pasteable record and out of any unvetted auto-persist body,
    mirroring the ``--record-command`` precedent.
    """

    role_profile: str
    profile_source: str
    profile_version: str
    resolved_text: str
    unresolved_placeholders: tuple[str, ...]

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free pointer fields for the handoff payload."""
        return {
            "role_profile": self.role_profile,
            "profile_source": self.profile_source,
            "profile_version": self.profile_version,
            "unresolved_placeholders": list(self.unresolved_placeholders),
        }

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full multi-line contract stays in the durable delivery record,
        not the pane. Names the role, the source path (so the receiver does not
        guess a template path), and the version, and points at the durable
        record for the fully resolved contract.
        """
        clause = (
            f"role profile: {self.role_profile} "
            f"(source: {self.profile_source}, version: {self.profile_version}; "
            "fully resolved contract is in the durable delivery record)"
        )
        if self.unresolved_placeholders:
            clause += (
                " [unresolved fields: "
                + ", ".join(self.unresolved_placeholders)
                + "]"
            )
        return clause


def resolve_role_profile(
    role: str,
    fields: Optional[Mapping[str, str]] = None,
) -> RoleProfileResolution:
    """Resolve a builtin role profile template, substituting structured fields.

    Fails closed with :class:`RoleProfileError` when ``role`` has no builtin
    template (the "template missing" contract from US #12388 / #12387). Supplied
    ``fields`` substitute the matching ``<name>`` placeholders; unsupplied
    placeholders are left as literal ``<name>`` tokens and reported in
    :attr:`RoleProfileResolution.unresolved_placeholders`.

    The function is pure and deterministic over its inputs.
    """
    template = ROLE_PROFILE_TEMPLATES.get(role)
    if template is None:
        raise RoleProfileError(
            f"unknown role profile: {role!r}; expected one of {list(ROLE_PROFILE_TOKENS)}"
        )

    supplied = dict(fields or {})
    placeholders = template_placeholders(role)

    resolved = template
    unresolved: list[str] = []
    for name in placeholders:
        value = supplied.get(name)
        if value is None or value == "":
            unresolved.append(name)
            continue
        resolved = resolved.replace(f"<{name}>", value)

    return RoleProfileResolution(
        role_profile=role,
        profile_source=ROLE_PROFILE_SOURCE,
        profile_version=ROLE_PROFILE_VERSION,
        resolved_text=resolved,
        unresolved_placeholders=tuple(unresolved),
    )


def parse_profile_fields(pairs: Optional[Iterable[str]]) -> dict[str, str]:
    """Parse ``KEY=VALUE`` CLI pairs into a profile-field mapping.

    Fails closed (:class:`RoleProfileError`) on a pair without ``=`` or with an
    empty key so a malformed ``--profile-field`` never silently drops a value.
    The first ``=`` splits the pair, so values may themselves contain ``=``.
    """
    result: dict[str, str] = {}
    for raw in pairs or ():
        if "=" not in raw:
            raise RoleProfileError(
                f"--profile-field must be KEY=VALUE; got {raw!r}"
            )
        key, value = raw.split("=", 1)
        key = key.strip()
        if not key:
            raise RoleProfileError(
                f"--profile-field key must be non-empty; got {raw!r}"
            )
        result[key] = value
    return result


__all__: Iterable[str] = (
    "RoleProfileError",
    "ROLE_PROFILE_VERSION",
    "ROLE_PROFILE_SOURCE",
    "ROLE_COORDINATOR",
    "ROLE_DELEGATED_COORDINATOR",
    "ROLE_IMPLEMENTATION_GATEWAY",
    "ROLE_IMPLEMENTATION_WORKER",
    "ROLE_PROFILE_TEMPLATES",
    "ROLE_PROFILE_TOKENS",
    "RoleProfileResolution",
    "template_placeholders",
    "resolve_role_profile",
    "parse_profile_fields",
)
