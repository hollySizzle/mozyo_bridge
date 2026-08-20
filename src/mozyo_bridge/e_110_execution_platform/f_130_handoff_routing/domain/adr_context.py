"""Repo-local ADR pointer set carried beside a handoff role profile (#15722).

ADR-0011 ``## 決定 (規約行)`` states that the ADRs are referenced by every layer
of the three-layer model, but its own ``## 正直なトレードオフ`` item 3 records
that nothing actually *injects* them into a layer's execution context, so
"reference required" stays a slogan. This module is the pure, fail-closed
pointer type that closes that gap for the existing role-profile expansion seam
(Redmine #12388 / #12952): a dispatch that already carries a role profile also
carries a resolvable pointer to the repo's ADR set.

Design boundaries (Redmine #15722 acceptance criteria):

- **Pointers, never bodies.** A pointer carries the ADR index path, each ADR's
  id / canonical path / resolvable paths, and its declared status. It never
  pastes an ADR body — the same posture as
  :mod:`...domain.workflow_contract` (no version drift, no context bloat).
- **Status is never laundered.** :func:`normalize_adr_status` maps a declared
  status onto the closed vocabulary and *only* ``active`` is binding
  (:data:`BINDING_STATUSES`). A ``proposed`` ADR is presented as ``proposed``, a
  status this module does not recognise becomes :data:`STATUS_UNKNOWN`, and
  neither is ever rendered as an active rule. There is no path from an
  unparseable status to "binding".
- **Additive.** The pointer travels as an optional companion of
  :class:`...domain.role_profile.RoleProfileResolution`; omitting it is the
  explicit fallback of "no ADR context resolved", which is what a repo without
  ``vibes/docs/adr/`` gets. A receiver that does not know the field ignores it.
- **Durable-record safe in full.** Every field is a fixed token (an ``adr-NNNN``
  id, a repo-relative path, a closed-vocabulary status token, an obligation
  token), never operator free text, so the whole pointer may be persisted
  verbatim.

The resolver that reads the repo (the only filesystem seam) lives in
:mod:`...application.adr_context_resolution`; this module stays pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.workflow_contract import (
    MOZYO_BRIDGE_PROJECT_SUBDIR,
)


class AdrContextError(ValueError):
    """An ADR context pointer is malformed (fail closed)."""


#: Closed status vocabulary. ``active`` / ``superseded`` are the two states
#: ``vibes/docs/adr/README.md`` ``## 書式`` admits for a ratified ADR file;
#: ``proposed`` is the pre-ratification state ADR-0011 currently declares.
STATUS_ACTIVE = "active"
STATUS_PROPOSED = "proposed"
STATUS_SUPERSEDED = "superseded"
#: A declared status this module does not recognise. Deliberately NOT an error:
#: dropping the ADR would hide it, and guessing would launder it. It is surfaced
#: as unknown and is not binding.
STATUS_UNKNOWN = "unknown"

KNOWN_STATUSES: tuple[str, ...] = (
    STATUS_ACTIVE,
    STATUS_PROPOSED,
    STATUS_SUPERSEDED,
    STATUS_UNKNOWN,
)

#: The only status that binds a receiver's judgement. Everything else is carried
#: for visibility and explicitly marked non-binding (Redmine #15722 AC2).
BINDING_STATUSES: frozenset[str] = frozenset({STATUS_ACTIVE})

#: Fixed obligation token stating what the receiver must do with the pointer.
#: Names the status boundary in the token itself so a receiver that only logs the
#: token still records that non-active ADRs do not bind.
ADR_READ_OBLIGATION = (
    "read_active_adrs_before_deciding_non_active_adrs_are_not_binding"
)


def normalize_adr_status(raw: object) -> str:
    """Map a declared ADR status onto the closed vocabulary, never upward.

    The declared form is the ``- status: <token>`` line of an ADR file, which may
    carry a trailing qualifier (``superseded (by ADR-0007)``, ``proposed (owner
    ratify 待ち…)``). The leading token decides; anything that is not an exact
    match for a known status becomes :data:`STATUS_UNKNOWN`. Pure and
    deterministic — and one-directional: no input other than a literal ``active``
    token yields :data:`STATUS_ACTIVE`.
    """
    if not isinstance(raw, str):
        return STATUS_UNKNOWN
    # Exact literal token only (review j#108679 finding_noncanonicalstatuspromotion):
    # case-folding promoted `Active` / `ACTIVE` to the binding `active`, which the
    # fail-closed contract forbids — a non-literal declaration is unknown, not active.
    token = raw.strip().split()[0].strip() if raw.strip() else ""
    if token in (STATUS_ACTIVE, STATUS_PROPOSED, STATUS_SUPERSEDED):
        return token
    return STATUS_UNKNOWN


def _clean_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AdrContextError(
            f"adr context {field} must be a non-empty token; got {value!r}"
        )
    return value.strip()


def resolvable_paths_for(canonical_path: str) -> tuple[str, ...]:
    """Path forms a receiver workspace may try, canonical first.

    Mirrors :func:`...domain.workflow_contract.make_ref`: a receiver whose
    workspace is the GK3500-style monorepo resolves the project-nested form but
    not the bare sender-repo-relative one (#12700 j#66929), so both travel.
    """
    canonical = _clean_token(canonical_path, field="canonical_path")
    return (canonical, f"{MOZYO_BRIDGE_PROJECT_SUBDIR}/{canonical}")


@dataclass(frozen=True)
class AdrRef:
    """One ADR pointer: stable id, resolvable paths, and its declared status.

    ``status`` is already normalized onto :data:`KNOWN_STATUSES` by
    :func:`normalize_adr_status`; construction fails closed on anything else so a
    hand-built ref cannot smuggle an unvetted status token into a payload.
    """

    adr_id: str
    canonical_path: str
    resolvable_paths: tuple[str, ...]
    status: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "adr_id", _clean_token(self.adr_id, field="adr_id"))
        object.__setattr__(
            self,
            "canonical_path",
            _clean_token(self.canonical_path, field="canonical_path"),
        )
        paths = tuple(
            _clean_token(path, field="resolvable_paths entry")
            for path in self.resolvable_paths
        )
        if not paths:
            raise AdrContextError(
                "adr context resolvable_paths must list at least one path"
            )
        object.__setattr__(self, "resolvable_paths", paths)
        status = _clean_token(self.status, field="status")
        if status not in KNOWN_STATUSES:
            raise AdrContextError(
                f"adr context status must be one of {list(KNOWN_STATUSES)}; "
                f"got {status!r}"
            )
        object.__setattr__(self, "status", status)

    @property
    def is_binding(self) -> bool:
        """Whether this ADR binds a receiver's judgement (``active`` only)."""
        return self.status in BINDING_STATUSES

    def to_structured_dict(self) -> dict[str, object]:
        return {
            "adr_id": self.adr_id,
            "canonical_path": self.canonical_path,
            "resolvable_paths": list(self.resolvable_paths),
            "status": self.status,
            "binding": self.is_binding,
        }


def make_adr_ref(adr_id: str, canonical_path: str, status: object) -> AdrRef:
    """Build a ref, deriving the resolvable paths and normalizing the status."""
    canonical = _clean_token(canonical_path, field="canonical_path")
    return AdrRef(
        adr_id=adr_id,
        canonical_path=canonical,
        resolvable_paths=resolvable_paths_for(canonical),
        status=normalize_adr_status(status),
    )


@dataclass(frozen=True)
class AdrContextPointer:
    """The repo's ADR set as a resolvable, status-faithful pointer (#15722).

    ``index_canonical_path`` is the single index doc a receiver can always start
    from; ``refs`` are the per-ADR pointers with their declared statuses. The
    receiver reads the index / the active ADRs instead of discovering them by
    luck, and sees non-active ADRs marked as such rather than absent.

    Construction fails closed on a blank index path, a duplicate ADR id, or a ref
    that is not an :class:`AdrRef`. An empty ``refs`` set is allowed: a repo whose
    ADR index exists but records no ADR file yet is a real, honest state.
    """

    index_canonical_path: str
    index_resolvable_paths: tuple[str, ...]
    refs: tuple[AdrRef, ...]
    read_obligation: str = ADR_READ_OBLIGATION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "index_canonical_path",
            _clean_token(self.index_canonical_path, field="index_canonical_path"),
        )
        paths = tuple(
            _clean_token(path, field="index_resolvable_paths entry")
            for path in self.index_resolvable_paths
        )
        if not paths:
            raise AdrContextError(
                "adr context index_resolvable_paths must list at least one path"
            )
        object.__setattr__(self, "index_resolvable_paths", paths)
        object.__setattr__(
            self,
            "read_obligation",
            _clean_token(self.read_obligation, field="read_obligation"),
        )
        seen: set[str] = set()
        for ref in self.refs:
            if not isinstance(ref, AdrRef):
                raise AdrContextError(
                    f"adr context refs must be AdrRef instances; got {ref!r}"
                )
            if ref.adr_id in seen:
                raise AdrContextError(f"duplicate adr id in pointer: {ref.adr_id!r}")
            seen.add(ref.adr_id)

    def binding_refs(self) -> tuple[AdrRef, ...]:
        """The ``active`` ADRs — the only ones presented as standing rules."""
        return tuple(ref for ref in self.refs if ref.is_binding)

    def non_binding_refs(self) -> tuple[AdrRef, ...]:
        """The ADRs carried for visibility that must NOT be applied as rules."""
        return tuple(ref for ref in self.refs if not ref.is_binding)

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free fields for the handoff payload."""
        return {
            "index_canonical_path": self.index_canonical_path,
            "index_resolvable_paths": list(self.index_resolvable_paths),
            "read_obligation": self.read_obligation,
            "binding_statuses": sorted(BINDING_STATUSES),
            "refs": [ref.to_structured_dict() for ref in self.refs],
        }

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via one
        ``tmux send-keys -l`` and the landing-marker gate greps the line, so the
        per-ADR list stays in the durable delivery record. States the counts with
        their binding meaning so the pane line alone cannot be read as "all ADRs
        are rules".
        """
        binding = len(self.binding_refs())
        non_binding = len(self.non_binding_refs())
        return (
            f"adr context: index {self.index_canonical_path}, {binding} active "
            f"(binding), {non_binding} non-active (not binding); obligation "
            f"{self.read_obligation}; per-ADR ids + statuses are in the durable "
            "delivery record"
        )

    def record_lines(self) -> list[str]:
        """Full durable-record block: the index, then every ADR by binding class.

        Fixed tokens only, so it is rendered in place. The two groups are labelled
        by what they oblige, not merely by status name, so a reader cannot take a
        ``proposed`` ADR for a standing rule.
        """
        resolvable = ", ".join(f"`{p}`" for p in self.index_resolvable_paths)
        lines = [
            f"- ADR context: index `{self.index_canonical_path}` "
            f"(resolvable: {resolvable})",
            f"  - Read obligation: `{self.read_obligation}`",
        ]
        binding = self.binding_refs()
        non_binding = self.non_binding_refs()
        lines.append(
            f"  - Binding (`{STATUS_ACTIVE}`, {len(binding)}): "
            + (
                ", ".join(f"`{ref.adr_id}` (`{ref.canonical_path}`)" for ref in binding)
                or "—"
            )
        )
        lines.append(
            f"  - NOT binding ({len(non_binding)}): "
            + (
                ", ".join(
                    f"`{ref.adr_id}` status `{ref.status}` (`{ref.canonical_path}`)"
                    for ref in non_binding
                )
                or "—"
            )
        )
        return lines

    def contract_lines(self) -> list[str]:
        """Plain-text block appended to the resolved role-profile contract.

        Kept under its own heading so it reads as send-time repo state beside the
        role-profile template body, not as part of the template the
        ``profile_version`` pins.
        """
        return [
            "",
            "# ADR context (repo-local, resolved at send time; not part of the "
            "role profile template)",
            f"- ADR index: {self.index_canonical_path}",
            f"- read obligation: {self.read_obligation}",
            f"- binding status: {STATUS_ACTIVE} only; every other status is "
            "carried for visibility and must not be applied as a standing rule.",
            *(
                f"- {ref.status}"
                + ("" if ref.is_binding else " (NOT binding)")
                + f": {ref.adr_id} — {ref.canonical_path}"
                for ref in self.refs
            ),
        ]


def adr_context_from_payload(payload: Mapping[str, object]) -> AdrContextPointer:
    """Rebuild a pointer from a structured payload (round-trips the dict form).

    Fails closed (:class:`AdrContextError`) on a missing / malformed field so a
    receiver parsing the payload cannot silently accept a partial pointer. The
    ``binding`` flag in a ref payload is derived output, not input: it is
    recomputed from ``status`` here, so a payload claiming ``binding: true`` on a
    ``proposed`` ADR cannot promote it.
    """
    try:
        index_path = payload["index_canonical_path"]
        index_paths = payload["index_resolvable_paths"]
        refs = payload["refs"]
    except KeyError as exc:
        raise AdrContextError(
            f"adr context payload missing required field: {exc.args[0]!r}"
        ) from exc
    if not isinstance(index_paths, Sequence) or isinstance(index_paths, (str, bytes)):
        raise AdrContextError(
            "adr context payload index_resolvable_paths must be a sequence of paths"
        )
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise AdrContextError(
            "adr context payload refs must be a sequence of ref mappings"
        )
    rebuilt: list[AdrRef] = []
    for entry in refs:
        if not isinstance(entry, Mapping):
            raise AdrContextError("adr context payload ref entries must be mappings")
        try:
            adr_id = entry["adr_id"]
            canonical_path = entry["canonical_path"]
            status = entry["status"]
        except KeyError as exc:
            raise AdrContextError(
                f"adr context ref missing required field: {exc.args[0]!r}"
            ) from exc
        rebuilt.append(make_adr_ref(adr_id, canonical_path, status))  # type: ignore[arg-type]
    obligation = payload.get("read_obligation", ADR_READ_OBLIGATION)
    return AdrContextPointer(
        index_canonical_path=index_path,  # type: ignore[arg-type]
        index_resolvable_paths=tuple(index_paths),
        refs=tuple(rebuilt),
        read_obligation=obligation,  # type: ignore[arg-type]
    )


__all__: Iterable[str] = (
    "AdrContextError",
    "STATUS_ACTIVE",
    "STATUS_PROPOSED",
    "STATUS_SUPERSEDED",
    "STATUS_UNKNOWN",
    "KNOWN_STATUSES",
    "BINDING_STATUSES",
    "ADR_READ_OBLIGATION",
    "normalize_adr_status",
    "resolvable_paths_for",
    "AdrRef",
    "make_adr_ref",
    "AdrContextPointer",
    "adr_context_from_payload",
)
