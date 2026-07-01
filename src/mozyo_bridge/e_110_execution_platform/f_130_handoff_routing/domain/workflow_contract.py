"""Ticketless / delegated transition workflow-contract reference payload (#12700).

This module addresses the third ticketless blocker: a ticketless prompt — kept
Redmine-anchor-free on purpose to avoid route-oracle contamination — carries **no
pointer to the workflow contract docs**, so the receiver can classify the request
onto a project but has no way to know the parent/child/grandchild lane contract,
the Redmine work-item boundary, the blocked-callback obligation, or the
child-dispatch boundary as a normal-operation contract. It could only act if it
*happened* to discover the docs.

The #12700 j#66929 follow-up sharpened the requirement: passing raw mozyo_bridge
repo-relative paths is not enough when the receiver workspace is the GK3500
monorepo, where mozyo_bridge is checked out under
``projects/giken-3800-mozyo-bridge/``. There the receiver could resolve
``projects/giken-3800-mozyo-bridge/vibes/docs/logics/coordinator-sublane-development-flow.md``
but not the bare ``vibes/docs/logics/...`` form. The fix must therefore carry a
**resolvable** contract reference for the receiver workspace — a stable catalog
contract id plus every path form the receiver can resolve — not only a
sender-repo-relative path.

This module is the pure, fail-closed source of truth for that workflow-contract
reference bundle. Design boundaries (Redmine #12700 description / j#66929):

- A bundle carries *pointers*, never doc bodies. Each :class:`WorkflowContractRef`
  is a stable catalog ``contract_id`` (resolvable via the docs catalog regardless
  of where the repo sits — the layout-independent, version-stable identity), the
  sender ``canonical_path``, and the ordered ``resolvable_paths`` the receiver may
  try (the canonical path AND the GK3500 project-nested form). The bundle never
  pastes a doc body, so it cannot cause version drift / context bloat (the issue's
  explicit prohibition).
- Every field is a fixed token (catalog id, repo-relative path, a fixed obligation
  token, an int set-version) with no operator free text, so the whole bundle —
  including the full ref list — is durable-record safe and may be persisted
  verbatim, like the :mod:`...domain.transition_role` boundary it travels beside.
- The bundle carries no route-oracle input — no Redmine issue id, prior journal,
  ``%pane``, or expected child candidate; those are the contaminating inputs the
  contract forbids. A bundle is a normal-operation contract pointer only.
- Construction fails closed: an unknown role token raises
  :class:`WorkflowContractError`, and a malformed bundle (blank role, empty ref
  set, blank id/path, empty resolvable paths, blank obligation token) cannot be
  built. Omitting the bundle is the explicit fallback of no contract binding.

The two bundles mirror the two transition roles in
:mod:`...domain.transition_role`: the ``grandparent_coordinator`` bundle equips
the project gateway it hands off to with the four ticketless workflow contracts
the project gateway needs; the ``project_gateway`` bundle equips a delegated child
lane with
the sublane-development-flow spine and the delegated-coordinator acceptance
contracts (the parent -> child delegated transition the issue also calls out).

Redmine #12953 moved the bundle *composition* — the per-role refs, obligations,
and ``contract_set_version`` — out of builtin module constants and into the
packaged config file :data:`WORKFLOW_CONTRACT_CONFIG_FILENAME`, so the config is
the single source of truth the runtime resolver reads at import time. The values
stay fixed tokens (catalog ids, repo-relative paths, snake_case obligation
tokens, an int version), so the durable-record-safe property is unchanged; only
their *home* moved from Python literals to a validated config. The named
obligation / set-version constants and :data:`WORKFLOW_CONTRACT_BUNDLES` are now
*derived* from the loaded config rather than hand-maintained beside it.
:func:`validate_bundles_against_catalog` adds the catalog schema check the issue
asks for: every ref's ``contract_id`` must resolve in the docs catalog with a
matching ``canonical_path``, and a missing catalog ref fails closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from typing import Iterable, Mapping, Sequence

import yaml

from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.transition_role import (
    ROLE_GRANDPARENT_COORDINATOR,
    ROLE_PROJECT_GATEWAY,
)


class WorkflowContractError(ValueError):
    """A workflow-contract bundle could not be resolved or is malformed."""


# The mozyo_bridge repo is checked out under this subdir inside the GK3500 (and
# other governed monorepo) receiver workspaces. #12700 j#66929 observed that the
# GK3500 receiver could resolve the project-nested form but not the bare
# sender-repo-relative path, so every ref carries both. This is the project's own
# canonical checkout location (its redmine_project id), not a route oracle.
MOZYO_BRIDGE_PROJECT_SUBDIR = "projects/giken-3800-mozyo-bridge"

# The packaged config file that is the source of truth for the bundle
# composition (per-role refs / obligations / set version). Read at import time by
# :func:`_load_bundles_from_config`; travels with the wheel via ``package-data``.
WORKFLOW_CONTRACT_CONFIG_PACKAGE = (
    "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain"
)
WORKFLOW_CONTRACT_CONFIG_FILENAME = "workflow_contract_config.yaml"


def _clean_token(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkflowContractError(
            f"workflow contract {field} must be a non-empty token; got {value!r}"
        )
    return value.strip()


def _clean_paths(paths: Iterable[object], *, field: str) -> tuple[str, ...]:
    cleaned: list[str] = []
    for path in paths:
        token = _clean_token(path, field=f"{field} entry")
        if token not in cleaned:
            cleaned.append(token)
    if not cleaned:
        raise WorkflowContractError(
            f"workflow contract {field} must list at least one resolvable path"
        )
    return tuple(cleaned)


@dataclass(frozen=True)
class WorkflowContractRef:
    """One resolvable workflow-contract pointer carried on a transition payload.

    ``contract_id`` is the stable docs-catalog id (the layout-independent,
    version-stable identity a receiver can resolve via ``mozyo-bridge docs
    resolve`` / the catalog regardless of where the repo is checked out).
    ``canonical_path`` is the sender-repo-relative path; ``resolvable_paths`` are
    the ordered candidates the receiver may try — the canonical path plus the
    project-nested form for a monorepo receiver workspace (#12700 j#66929).

    All fields are fixed tokens (no operator free text), so a ref is
    durable-record safe in full. Construction fails closed on a blank id / blank
    canonical path / empty resolvable paths.
    """

    contract_id: str
    canonical_path: str
    resolvable_paths: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "contract_id", _clean_token(self.contract_id, field="contract_id")
        )
        object.__setattr__(
            self,
            "canonical_path",
            _clean_token(self.canonical_path, field="canonical_path"),
        )
        object.__setattr__(
            self,
            "resolvable_paths",
            _clean_paths(self.resolvable_paths, field="resolvable_paths"),
        )

    def to_structured_dict(self) -> dict[str, object]:
        return {
            "contract_id": self.contract_id,
            "canonical_path": self.canonical_path,
            "resolvable_paths": list(self.resolvable_paths),
        }


def make_ref(contract_id: str, canonical_path: str) -> WorkflowContractRef:
    """Build a ref whose resolvable paths cover the canonical + monorepo forms.

    The receiver gets the sender-repo-relative ``canonical_path`` AND the
    ``projects/giken-3800-mozyo-bridge/<canonical_path>`` form so a GK3500-style
    monorepo workspace resolves it without guessing (#12700 j#66929). Pure and
    deterministic over its inputs.
    """
    canonical = _clean_token(canonical_path, field="canonical_path")
    nested = f"{MOZYO_BRIDGE_PROJECT_SUBDIR}/{canonical}"
    return WorkflowContractRef(
        contract_id=contract_id,
        canonical_path=canonical,
        resolvable_paths=(canonical, nested),
    )


@dataclass(frozen=True)
class WorkflowContractBundle:
    """Explicit workflow-contract reference bundle for a transition (#12700).

    ``current_role`` names the receiver lane role this bundle equips;
    ``read_obligation`` / ``callback_obligation`` are fixed tokens stating what
    the receiver must do with the contracts; ``refs`` is the resolvable contract
    pointer set; ``contract_set_version`` lets a receiver detect bundle-composition
    drift. The receiver reads this instead of having to discover the docs by luck.

    All fields are fixed tokens / ints, so a bundle is durable-record safe in
    full. Construction fails closed on a blank role, an empty ref set, or a blank
    obligation token.
    """

    current_role: str
    contract_set_version: int
    read_obligation: str
    callback_obligation: str
    refs: tuple[WorkflowContractRef, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "current_role", _clean_token(self.current_role, field="current_role")
        )
        object.__setattr__(
            self,
            "read_obligation",
            _clean_token(self.read_obligation, field="read_obligation"),
        )
        object.__setattr__(
            self,
            "callback_obligation",
            _clean_token(self.callback_obligation, field="callback_obligation"),
        )
        if not isinstance(self.contract_set_version, int) or isinstance(
            self.contract_set_version, bool
        ):
            raise WorkflowContractError(
                "workflow contract contract_set_version must be an int; got "
                f"{self.contract_set_version!r}"
            )
        if not self.refs:
            raise WorkflowContractError(
                "workflow contract bundle must list at least one contract ref"
            )
        seen: set[str] = set()
        for ref in self.refs:
            if not isinstance(ref, WorkflowContractRef):
                raise WorkflowContractError(
                    "workflow contract bundle refs must be WorkflowContractRef "
                    f"instances; got {ref!r}"
                )
            if ref.contract_id in seen:
                raise WorkflowContractError(
                    f"duplicate workflow contract id in bundle: {ref.contract_id!r}"
                )
            seen.add(ref.contract_id)

    def to_structured_dict(self) -> dict[str, object]:
        """Structured, free-text-free fields for the handoff transition payload."""
        return {
            "current_role": self.current_role,
            "contract_set_version": self.contract_set_version,
            "read_obligation": self.read_obligation,
            "callback_obligation": self.callback_obligation,
            "refs": [ref.to_structured_dict() for ref in self.refs],
        }

    def pointer_clause(self) -> str:
        """Compact single-line clause for the pane notification body.

        Single line by construction (no newlines): the body is delivered via a
        single ``tmux send-keys -l`` and the landing-marker gate greps the line,
        so the full resolvable ref list and obligations stay in the durable
        delivery record. Names the role, the ref count + set version, and the two
        obligations, and points at the durable record for the resolvable refs.
        """
        return (
            f"workflow contracts for {self.current_role}: {len(self.refs)} required "
            f"doc(s) (set v{self.contract_set_version}); obligations "
            f"{self.read_obligation} + {self.callback_obligation}; the resolvable "
            "contract refs are in the durable delivery record"
        )

    def record_lines(self) -> list[str]:
        """Full durable-record block: obligations + every resolvable ref.

        Fixed tokens only (catalog ids, repo-relative paths, obligation tokens),
        so it is rendered in place and the receiver reads the contract set it must
        obey without re-reading the pane or discovering the docs by luck.
        """
        lines = [
            f"- Workflow contracts: `{self.current_role}` "
            f"(set v{self.contract_set_version}, {len(self.refs)} required)",
            f"  - Read obligation: `{self.read_obligation}`",
            f"  - Callback obligation: `{self.callback_obligation}`",
        ]
        for ref in self.refs:
            resolvable = ", ".join(f"`{p}`" for p in ref.resolvable_paths)
            lines.append(
                f"  - `{ref.contract_id}` — canonical `{ref.canonical_path}` "
                f"(resolvable: {resolvable})"
            )
        return lines


def _config_text() -> str:
    """Read the packaged workflow-contract config file text.

    Fails closed (:class:`WorkflowContractError`) when the packaged config cannot
    be read, so an install that dropped the file never silently degrades to "no
    contracts".
    """
    try:
        return (
            resources.files(WORKFLOW_CONTRACT_CONFIG_PACKAGE)
            .joinpath(WORKFLOW_CONTRACT_CONFIG_FILENAME)
            .read_text(encoding="utf-8")
        )
    except (FileNotFoundError, OSError, ModuleNotFoundError) as exc:
        raise WorkflowContractError(
            f"workflow contract config {WORKFLOW_CONTRACT_CONFIG_FILENAME!r} "
            f"could not be read: {exc}"
        ) from exc


def _bundle_from_config(role: str, spec: object, *, version: int) -> WorkflowContractBundle:
    """Build one bundle from a config ``roles`` entry, failing closed.

    Malformed / missing fields raise :class:`WorkflowContractError`; the ref
    mappings are handed to :func:`make_ref` (which derives the resolvable paths)
    and :class:`WorkflowContractBundle` (which rejects blank fields and duplicate
    contract ids), so the config never yields a partial bundle.
    """
    if not isinstance(spec, Mapping):
        raise WorkflowContractError(
            f"workflow contract config role {role!r} must map to a bundle mapping"
        )
    try:
        read_obligation = spec["read_obligation"]
        callback_obligation = spec["callback_obligation"]
        raw_refs = spec["refs"]
    except KeyError as exc:
        raise WorkflowContractError(
            f"workflow contract config role {role!r} missing required field: "
            f"{exc.args[0]!r}"
        ) from exc
    if not isinstance(raw_refs, Sequence) or isinstance(raw_refs, (str, bytes)):
        raise WorkflowContractError(
            f"workflow contract config role {role!r} refs must be a sequence"
        )
    refs: list[WorkflowContractRef] = []
    for entry in raw_refs:
        if not isinstance(entry, Mapping):
            raise WorkflowContractError(
                f"workflow contract config role {role!r} ref entries must be mappings"
            )
        try:
            contract_id = entry["contract_id"]
            canonical_path = entry["canonical_path"]
        except KeyError as exc:
            raise WorkflowContractError(
                f"workflow contract config role {role!r} ref missing required "
                f"field: {exc.args[0]!r}"
            ) from exc
        refs.append(make_ref(contract_id, canonical_path))  # type: ignore[arg-type]
    return WorkflowContractBundle(
        current_role=role,
        contract_set_version=version,
        read_obligation=read_obligation,  # type: ignore[arg-type]
        callback_obligation=callback_obligation,  # type: ignore[arg-type]
        refs=tuple(refs),
    )


def _load_bundles_from_config() -> tuple[int, dict[str, WorkflowContractBundle]]:
    """Parse the packaged config into ``(set_version, bundles-by-role)``.

    The config is the source of truth for the bundle composition (Redmine #12953);
    this loader fails closed on a malformed top-level shape (non-mapping,
    missing / non-int ``contract_set_version``, missing / non-mapping ``roles``,
    a blank role token) and delegates per-bundle validation to
    :func:`_bundle_from_config`.
    """
    raw = yaml.safe_load(_config_text())
    if not isinstance(raw, Mapping):
        raise WorkflowContractError(
            "workflow contract config must be a mapping at the top level"
        )
    version = raw.get("contract_set_version")
    if not isinstance(version, int) or isinstance(version, bool):
        raise WorkflowContractError(
            "workflow contract config contract_set_version must be an int; got "
            f"{version!r}"
        )
    roles = raw.get("roles")
    if not isinstance(roles, Mapping) or not roles:
        raise WorkflowContractError(
            "workflow contract config must contain a non-empty `roles` mapping"
        )
    bundles: dict[str, WorkflowContractBundle] = {}
    for role, spec in roles.items():
        role_token = _clean_token(role, field="config role")
        bundles[role_token] = _bundle_from_config(role_token, spec, version=version)
    return version, bundles


# The config is the single source of truth; the version, obligation constants, and
# bundle dict are DERIVED from it (Redmine #12953). The named constants are kept as
# stable, backward-compatible aliases (durable-record-safe fixed tokens) so
# existing importers and durable records keep resolving them, but they now trace
# to the config rather than duplicating literals beside it.
WORKFLOW_CONTRACT_SET_VERSION, WORKFLOW_CONTRACT_BUNDLES = _load_bundles_from_config()

READ_OBLIGATION_ALL_BEFORE_ACTING = WORKFLOW_CONTRACT_BUNDLES[
    ROLE_GRANDPARENT_COORDINATOR
].read_obligation
# #12737: the ticketless callback obligation names the product *return path*
# (``ticketless-callback`` / ``q-enter consultation_callback``), not just "callback
# the result"; a v1-pinning receiver must re-read it (set version 2).
CALLBACK_OBLIGATION_TICKETLESS = WORKFLOW_CONTRACT_BUNDLES[
    ROLE_GRANDPARENT_COORDINATOR
].callback_obligation
CALLBACK_OBLIGATION_DELEGATED_CHILD = WORKFLOW_CONTRACT_BUNDLES[
    ROLE_PROJECT_GATEWAY
].callback_obligation

WORKFLOW_CONTRACT_TOKENS: tuple[str, ...] = tuple(WORKFLOW_CONTRACT_BUNDLES.keys())


def resolve_workflow_contract(role: str) -> WorkflowContractBundle:
    """Resolve a builtin workflow-contract bundle by transition-role token.

    Fails closed with :class:`WorkflowContractError` when ``role`` has no builtin
    bundle, so a caller never silently treats an unknown role as "no contracts".
    Pure and deterministic over its input.
    """
    bundle = WORKFLOW_CONTRACT_BUNDLES.get(role)
    if bundle is None:
        raise WorkflowContractError(
            f"unknown workflow contract role: {role!r}; expected one of "
            f"{list(WORKFLOW_CONTRACT_TOKENS)}"
        )
    return bundle


def workflow_contract_from_payload(
    payload: Mapping[str, object],
) -> WorkflowContractBundle:
    """Rebuild a bundle from a structured payload (round-trips to_structured_dict).

    Fails closed (:class:`WorkflowContractError`) on a missing / malformed field,
    so a receiver that parses the transition payload cannot silently accept a
    partial bundle. ``refs`` must be a sequence of ``contract_id`` /
    ``canonical_path`` / ``resolvable_paths`` mappings.
    """
    try:
        current = payload["current_role"]
        version = payload["contract_set_version"]
        read_obligation = payload["read_obligation"]
        callback_obligation = payload["callback_obligation"]
        refs = payload["refs"]
    except KeyError as exc:
        raise WorkflowContractError(
            f"workflow contract payload missing required field: {exc.args[0]!r}"
        ) from exc
    if not isinstance(refs, Sequence) or isinstance(refs, (str, bytes)):
        raise WorkflowContractError(
            "workflow contract payload refs must be a sequence of ref mappings"
        )
    rebuilt: list[WorkflowContractRef] = []
    for entry in refs:
        if not isinstance(entry, Mapping):
            raise WorkflowContractError(
                "workflow contract payload ref entries must be mappings"
            )
        try:
            contract_id = entry["contract_id"]
            canonical_path = entry["canonical_path"]
            resolvable_paths = entry["resolvable_paths"]
        except KeyError as exc:
            raise WorkflowContractError(
                f"workflow contract ref missing required field: {exc.args[0]!r}"
            ) from exc
        if not isinstance(resolvable_paths, (list, tuple)):
            raise WorkflowContractError(
                "workflow contract ref resolvable_paths must be a sequence of paths"
            )
        rebuilt.append(
            WorkflowContractRef(
                contract_id=contract_id,  # type: ignore[arg-type]
                canonical_path=canonical_path,  # type: ignore[arg-type]
                resolvable_paths=tuple(resolvable_paths),
            )
        )
    return WorkflowContractBundle(
        current_role=current,  # type: ignore[arg-type]
        contract_set_version=version,  # type: ignore[arg-type]
        read_obligation=read_obligation,  # type: ignore[arg-type]
        callback_obligation=callback_obligation,  # type: ignore[arg-type]
        refs=tuple(rebuilt),
    )


def validate_bundles_against_catalog(
    catalog_canonical_paths: Mapping[str, str],
    bundles: Mapping[str, WorkflowContractBundle] | None = None,
) -> None:
    """Fail closed unless every ref resolves in the docs catalog (Redmine #12953).

    ``catalog_canonical_paths`` maps a docs-catalog ``contract_id`` to its
    canonical path (build it from the catalog ``documents`` list). Each ref in
    each bundle must (a) have a ``contract_id`` present in the catalog — a
    **missing catalog ref** fails closed — and (b) carry the same
    ``canonical_path`` the catalog records for that id — a drifted path fails
    closed. ``bundles`` defaults to the loaded :data:`WORKFLOW_CONTRACT_BUNDLES`.

    Pure over its inputs (no catalog I/O here), so the caller owns where the
    catalog is read from — a verification-time check that does not make the
    runtime resolver depend on the repo-local catalog being present.
    """
    if bundles is None:
        bundles = WORKFLOW_CONTRACT_BUNDLES
    for role, bundle in bundles.items():
        for ref in bundle.refs:
            if ref.contract_id not in catalog_canonical_paths:
                raise WorkflowContractError(
                    f"workflow contract ref {ref.contract_id!r} (role {role!r}) has "
                    "no matching docs-catalog id"
                )
            catalog_path = catalog_canonical_paths[ref.contract_id]
            if catalog_path != ref.canonical_path:
                raise WorkflowContractError(
                    f"workflow contract ref {ref.contract_id!r} (role {role!r}) "
                    f"canonical_path {ref.canonical_path!r} does not match the "
                    f"docs-catalog path {catalog_path!r}"
                )


__all__: Iterable[str] = (
    "WorkflowContractError",
    "MOZYO_BRIDGE_PROJECT_SUBDIR",
    "WORKFLOW_CONTRACT_CONFIG_PACKAGE",
    "WORKFLOW_CONTRACT_CONFIG_FILENAME",
    "WORKFLOW_CONTRACT_SET_VERSION",
    "READ_OBLIGATION_ALL_BEFORE_ACTING",
    "CALLBACK_OBLIGATION_TICKETLESS",
    "CALLBACK_OBLIGATION_DELEGATED_CHILD",
    "WorkflowContractRef",
    "WorkflowContractBundle",
    "WORKFLOW_CONTRACT_BUNDLES",
    "WORKFLOW_CONTRACT_TOKENS",
    "make_ref",
    "resolve_workflow_contract",
    "workflow_contract_from_payload",
    "validate_bundles_against_catalog",
)
