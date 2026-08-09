"""Pure authority model for nested-workspace alias / launch-disable (#15190).

A single Git repository can carry two real workspace anchors: the canonical
repo-root workspace and a nested application-root workspace (the observed case
is a Rails application root under a repo that already owns the default
coordinator pair). Ordinary cwd resolution is already correct — it is
Git-root-first (#13641), so ``cd <nested>`` adopts the repo root. The gap is the
*explicit* root: ``herdr session-start --repo <nested-root>`` and ``MOZYO_REPO``
short-circuit in :func:`shared.paths.resolve_repo_root` **before** any
canonicalization, so the nested anchor resolves as an independent workspace and
a second default Codex/Claude pair can be planned for one repository.

Before this module the only adjacent rail was ``workspace retire``, which is
scoped to *missing-path* registry rows. A nested path that genuinely exists has
no supported way to be folded into its canonical parent, and the manual
alternatives — deleting the nested anchor, hand-editing the registry SQLite —
destroy managed identity provenance and the rollback boundary.

This module is the pure decision core for the declarative rail. It answers one
question from already-gathered observations: *given the declaration a nested
workspace carries, what should an explicit launch root resolve to?* The three
admissible answers are

- :data:`STATE_NO_DECLARATION` — nothing declared; the caller keeps the root it
  was given (the byte-for-byte pre-#15190 behavior);
- :data:`STATE_ALIASED` — resolve to the verified canonical root instead;
- :data:`STATE_LAUNCH_DISABLED` — a fixed typed zero-launch;

plus :data:`STATE_REFUSED`, which is also zero-launch: every verification here
fails **closed**. A declaration that cannot be proven correct never silently
degrades into "launch at the nested root anyway", because that degradation is
precisely the duplicate-pair defect the rail exists to remove.

The module performs no I/O and resolves no paths: the application layer gathers
the observations (target existence, identity, git binding, containment) and this
function decides. That split is what makes every negative branch — missing,
ambiguous, cross-repository, cycle — unit-testable without a filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


#: Declaration schema version. An unknown version is refused, never guessed at:
#: a future writer may add fields this reader would silently ignore, and a
#: silently-ignored alias field is a duplicate pair.
ALIAS_SCHEMA_VERSION = 1

#: Workspace-local declaration path, relative to the *nested* workspace root.
#: Deliberately a new file rather than a field inside the identity anchor: the
#: anchor is the identity recovery record (#11429) and must keep meaning exactly
#: one thing. This file declares launch *authority routing* and can be added or
#: removed without touching identity provenance.
ALIAS_RELATIVE = ".mozyo-bridge/workspace-alias.json"

#: Declared modes.
MODE_ALIAS = "alias"
MODE_DISABLED = "disabled"
DECLARED_MODES = (MODE_ALIAS, MODE_DISABLED)

#: Resolution states.
STATE_NO_DECLARATION = "no_declaration"
STATE_ALIASED = "aliased"
STATE_LAUNCH_DISABLED = "launch_disabled"
STATE_REFUSED = "refused"

#: Git binding between the declaring root and its declared canonical target.
GIT_BINDING_SAME = "same"
GIT_BINDING_DIFFERENT = "different"
#: Neither side is a git checkout. Containment then carries the whole binding —
#: the legitimate non-git nested workspace case (#11301 scaffolded roots).
GIT_BINDING_NOT_MEASURABLE = "not_measurable"
GIT_BINDINGS = (GIT_BINDING_SAME, GIT_BINDING_DIFFERENT, GIT_BINDING_NOT_MEASURABLE)

#: Typed refusal reasons. Each is a fixed token so an operator, a test, and a
#: durable record can all name the same branch without parsing prose.
REASON_DECLARATION_UNREADABLE = "declaration_unreadable"
REASON_DECLARATION_INVALID = "declaration_invalid"
REASON_UNSUPPORTED_SCHEMA = "declaration_unsupported_schema"
# The declaration path exists but is not a regular file (symlink, directory,
# FIFO, device). Distinct from "absent" on purpose (review j#102104 Finding 2):
# treating a non-regular node as "nothing declared" is what would let a broken
# or substituted declaration re-enable the nested launch.
REASON_NOT_REGULAR_FILE = "declaration_not_regular_file"
# Write-path refusals (review j#102104 Finding 1). Each is zero-mutation.
REASON_PARENT_UNSAFE = "declaration_parent_unsafe"
# The `.mozyo-bridge` directory visible from the workspace root stopped being the
# one this operation pinned (review j#102140 Finding 1). A pinned dirfd survives a
# rename of its directory, so without this check a write lands in a *detached*
# directory and reports success while the effective path has no declaration.
REASON_PARENT_DRIFT = "declaration_parent_drift"
REASON_MULTIPLE_LINKS = "declaration_multiple_links"
REASON_WRITE_FAILED = "declaration_write_failed"
# The existing declaration could not be captured before it would be replaced, so
# a later verification failure would have nothing to restore (review j#102230
# Finding 1). Refused ahead of the replace rather than discovered after it.
REASON_SNAPSHOT_FAILED = "declaration_snapshot_failed"
# The declaration exceeds :data:`MAX_DECLARATION_BYTES`. A declaration is a
# handful of short fields; anything larger is a repository-controlled file being
# used to force an unbounded allocation in a process that only wanted to decide
# whether to launch (review j#102230 Finding 2).
REASON_TOO_LARGE = "declaration_too_large"

#: Hard ceiling on a declaration's on-disk size. Generous next to the ~300 bytes
#: a real declaration occupies, small enough that reading one can never be a
#: memory-pressure event.
MAX_DECLARATION_BYTES = 64 * 1024
REASON_READBACK_FAILED = "declaration_readback_failed"
REASON_REMOVE_FAILED = "declaration_remove_failed"
REASON_TARGET_NOT_DECLARED = "alias_target_not_declared"
REASON_TARGET_MISSING = "alias_target_missing"
REASON_TARGET_NOT_DIRECTORY = "alias_target_not_directory"
REASON_TARGET_IS_SELF = "alias_target_is_self"
REASON_TARGET_NOT_ANCESTOR = "alias_target_not_ancestor"
REASON_TARGET_IDENTITY_UNRESOLVED = "alias_target_identity_unresolved"
REASON_TARGET_IDENTITY_MISMATCH = "alias_target_identity_mismatch"
REASON_CROSS_REPOSITORY = "alias_target_cross_repository"
REASON_ALIAS_CYCLE = "alias_target_declares_alias"


#: The exact key set each mode defines. Anything else is refused by
#: :func:`parse_declaration` (review j#102104 Finding 3).
_COMMON_KEYS = ("schema_version", "mode", "reason", "created_at", "updated_at")
_ALLOWED_KEYS = {
    MODE_ALIAS: _COMMON_KEYS + ("canonical_path", "canonical_workspace_id"),
    MODE_DISABLED: _COMMON_KEYS,
}


def _exact_token(value: object) -> bool:
    """True for a non-empty, unpadded, control-character-free string."""
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and not any(ord(char) < 0x20 or ord(char) == 0x7F for char in value)
    )


@dataclass(frozen=True)
class WorkspaceAliasDeclaration:
    """One parsed workspace-local declaration.

    ``canonical_path`` / ``canonical_workspace_id`` are meaningful only for
    :data:`MODE_ALIAS`. ``canonical_workspace_id`` is the verification binding:
    the declaration names *which identity* it folded into, so a later
    re-registration, restore, or path reuse at the same location cannot silently
    re-point the alias at a different workspace.
    """

    mode: str
    canonical_path: str = ""
    canonical_workspace_id: str = ""
    reason: str = ""
    created_at: str = ""
    updated_at: str = ""

    def as_payload(self) -> dict:
        """The on-disk form — mode-appropriate keys only.

        A ``disabled`` declaration does not carry the alias target keys at all,
        rather than carrying them empty. The written form has to round-trip
        through :func:`parse_declaration`, which rejects keys a mode does not
        define (review j#102104 Finding 3), so emitting empty target fields for
        a disabled declaration would make this writer produce files its own
        reader refuses.
        """
        payload = {
            "schema_version": ALIAS_SCHEMA_VERSION,
            "mode": self.mode,
            "reason": self.reason,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if self.mode == MODE_ALIAS:
            payload["canonical_path"] = self.canonical_path
            payload["canonical_workspace_id"] = self.canonical_workspace_id
        return payload


@dataclass(frozen=True)
class AliasTargetObservation:
    """Measured facts about a declared canonical target.

    Supplied by the application layer so this module stays pure. ``workspace_id``
    is the identity that *currently* resolves at the target (registry row or
    anchor); an empty string means the target has no resolvable identity, which
    is a refusal rather than a reason to fall back.
    """

    exists: bool
    is_dir: bool
    workspace_id: str
    git_binding: str
    is_ancestor_of_source: bool
    declares_alias: bool


@dataclass(frozen=True)
class AliasResolution:
    """The decision: which root to launch at, or a typed zero-launch."""

    state: str
    #: The root a launch should use. Set for :data:`STATE_NO_DECLARATION` (the
    #: unchanged input root) and :data:`STATE_ALIASED` (the canonical root).
    #: Empty on every zero-launch state.
    launch_root: str = ""
    reason: str = ""
    detail: str = ""
    declaration: Optional[WorkspaceAliasDeclaration] = None

    @property
    def ok(self) -> bool:
        """True when a launch may proceed at :attr:`launch_root`."""
        return self.state in {STATE_NO_DECLARATION, STATE_ALIASED}

    @property
    def redirected(self) -> bool:
        """True when the launch root differs from the requested root."""
        return self.state == STATE_ALIASED

    def as_payload(self) -> dict:
        return {
            "state": self.state,
            "ok": self.ok,
            "launch_root": self.launch_root,
            "reason": self.reason,
            "detail": self.detail,
            "declaration": (
                self.declaration.as_payload() if self.declaration is not None else None
            ),
        }


def refused(
    reason: str,
    detail: str,
    declaration: Optional[WorkspaceAliasDeclaration] = None,
) -> AliasResolution:
    return AliasResolution(
        state=STATE_REFUSED,
        reason=reason,
        detail=detail,
        declaration=declaration,
    )


def parse_declaration(raw: object) -> AliasResolution | WorkspaceAliasDeclaration:
    """Parse a raw declaration mapping, or return a typed refusal.

    Structural strictness is deliberate and mirrors the anchor contract
    (#11429): a declaration that is not exactly what this schema describes is
    refused rather than partially honored. The difference from the anchor is the
    *direction* of the degrade — a corrupt anchor falls back to derivation
    because identity has a safe fallback, whereas a corrupt alias declaration
    has none: ignoring it re-opens the duplicate-pair defect, so it fails closed.
    """
    if not isinstance(raw, Mapping):
        return refused(REASON_DECLARATION_INVALID, "declaration_is_not_a_mapping")

    # Exact TYPE checks, not just value equality (review j#102140 Finding 4).
    # Python's numeric tower makes `True == 1` and `1.0 == 1`, so a plain `!=`
    # against the schema version admits a bool and a float as if they were the
    # integer 1. `bool` is a subclass of `int`, so it has to be excluded
    # explicitly rather than by an isinstance check.
    version = raw.get("schema_version")
    if type(version) is not int:
        return refused(
            REASON_UNSUPPORTED_SCHEMA,
            f"schema_version must be an integer, got {type(version).__name__} "
            f"({version!r})",
        )
    if version != ALIAS_SCHEMA_VERSION:
        return refused(
            REASON_UNSUPPORTED_SCHEMA,
            f"declared_schema_version={version!r} expected={ALIAS_SCHEMA_VERSION}",
        )
    mode = raw.get("mode")
    if not isinstance(mode, str) or mode not in DECLARED_MODES:
        return refused(
            REASON_DECLARATION_INVALID,
            f"mode={mode!r} expected one of {list(DECLARED_MODES)}",
        )

    # Unknown / surplus keys are refused, not ignored (review j#102104 Finding 3).
    # A future schema may attach meaning to a key this reader does not know —
    # `future_semantics: launch-anyway` is the shape that motivated this — and a
    # reader that silently drops it would apply a *partial* interpretation of a
    # declaration whose whole point is to gate launching. The schema_version bump
    # is the sanctioned way to extend; an unversioned extra key is malformed.
    allowed = set(_ALLOWED_KEYS[mode])
    surplus = sorted(set(raw.keys()) - allowed)
    if surplus:
        return refused(
            REASON_DECLARATION_INVALID,
            f"unknown key(s) for mode={mode!r}: {surplus}; "
            f"schema v{ALIAS_SCHEMA_VERSION} defines {sorted(allowed)}",
        )

    # Absent optional fields default to ""; a PRESENT one must already be a
    # string. The previous `raw.get(k) or ""` normalized before checking, so
    # `false` / `0` / `None` silently became "" and passed the type check they
    # were supposed to fail (review j#102140 Finding 4).
    optional = {}
    for label in ("reason", "created_at", "updated_at"):
        if label not in raw:
            optional[label] = ""
            continue
        value = raw[label]
        if not isinstance(value, str):
            return refused(
                REASON_DECLARATION_INVALID,
                f"{label} must be a string, got {type(value).__name__} ({value!r})",
            )
        optional[label] = value
    reason = optional["reason"]
    created_at = optional["created_at"]
    updated_at = optional["updated_at"]

    if mode == MODE_DISABLED:
        return WorkspaceAliasDeclaration(
            mode=MODE_DISABLED,
            reason=reason,
            created_at=created_at,
            updated_at=updated_at,
        )

    canonical_path = raw.get("canonical_path")
    canonical_workspace_id = raw.get("canonical_workspace_id")
    if not _exact_token(canonical_path):
        return refused(REASON_TARGET_NOT_DECLARED, "canonical_path_missing_or_invalid")
    if not _exact_token(canonical_workspace_id):
        return refused(
            REASON_TARGET_NOT_DECLARED,
            "canonical_workspace_id_missing_or_invalid",
        )
    return WorkspaceAliasDeclaration(
        mode=MODE_ALIAS,
        canonical_path=str(canonical_path),
        canonical_workspace_id=str(canonical_workspace_id),
        reason=reason,
        created_at=created_at,
        updated_at=updated_at,
    )


def build_alias_resolution(
    *,
    source_root: str,
    declaration: Optional[WorkspaceAliasDeclaration],
    target: Optional[AliasTargetObservation] = None,
) -> AliasResolution:
    """Decide the effective launch root for ``source_root``.

    ``declaration`` is ``None`` when the workspace declares nothing — the
    overwhelmingly common case, which must stay byte-for-byte unchanged.

    For :data:`MODE_ALIAS` every one of the following must hold, and each failure
    is its own typed zero-launch reason:

    - the target exists and is a directory (``missing`` fails closed);
    - the target is not the declaring root itself;
    - the target is a strict **ancestor** of the declaring root — this rail folds
      a *nested* workspace into the parent that contains it, and nothing else;
    - the target resolves to an identity, and that identity equals the one the
      declaration recorded (``ambiguous`` fails closed: a re-minted or restored
      identity at the same path is a different workspace);
    - the target is in the same repository (``cross-repository`` fails closed);
    - the target does not itself declare an alias — no chains, so resolution is
      one hop and cannot cycle.
    """
    if not _exact_token(source_root):
        return refused(REASON_DECLARATION_INVALID, "source_root_invalid")

    if declaration is None:
        return AliasResolution(state=STATE_NO_DECLARATION, launch_root=source_root)

    if declaration.mode == MODE_DISABLED:
        return AliasResolution(
            state=STATE_LAUNCH_DISABLED,
            reason=MODE_DISABLED,
            detail=declaration.reason or "workspace declared launch-disabled",
            declaration=declaration,
        )

    if declaration.mode != MODE_ALIAS:
        return refused(
            REASON_DECLARATION_INVALID,
            f"mode={declaration.mode!r}",
            declaration,
        )

    if target is None:
        return refused(
            REASON_TARGET_IDENTITY_UNRESOLVED,
            "no_target_observation_supplied",
            declaration,
        )
    if not target.exists:
        return refused(
            REASON_TARGET_MISSING,
            f"declared canonical_path does not exist: {declaration.canonical_path}",
            declaration,
        )
    if not target.is_dir:
        return refused(
            REASON_TARGET_NOT_DIRECTORY,
            f"declared canonical_path is not a directory: {declaration.canonical_path}",
            declaration,
        )
    if declaration.canonical_path == source_root:
        return refused(
            REASON_TARGET_IS_SELF,
            "declared canonical_path is the declaring workspace itself",
            declaration,
        )
    if not target.is_ancestor_of_source:
        return refused(
            REASON_TARGET_NOT_ANCESTOR,
            (
                f"declared canonical_path {declaration.canonical_path} does not "
                f"contain {source_root}; this rail only folds a nested workspace "
                "into a parent that contains it"
            ),
            declaration,
        )
    if target.git_binding not in GIT_BINDINGS:
        return refused(
            REASON_CROSS_REPOSITORY,
            f"git_binding={target.git_binding!r} is not measurable",
            declaration,
        )
    if target.git_binding == GIT_BINDING_DIFFERENT:
        return refused(
            REASON_CROSS_REPOSITORY,
            (
                f"declared canonical_path {declaration.canonical_path} is a "
                "different repository (a submodule / nested checkout is not the "
                "same workspace)"
            ),
            declaration,
        )
    if not _exact_token(target.workspace_id):
        return refused(
            REASON_TARGET_IDENTITY_UNRESOLVED,
            (
                f"declared canonical_path {declaration.canonical_path} has no "
                "resolvable workspace identity; register it first"
            ),
            declaration,
        )
    if target.workspace_id != declaration.canonical_workspace_id:
        return refused(
            REASON_TARGET_IDENTITY_MISMATCH,
            (
                f"declared canonical_workspace_id "
                f"{declaration.canonical_workspace_id} but "
                f"{declaration.canonical_path} currently resolves to "
                f"{target.workspace_id}"
            ),
            declaration,
        )
    if target.declares_alias:
        return refused(
            REASON_ALIAS_CYCLE,
            (
                f"declared canonical_path {declaration.canonical_path} itself "
                "declares an alias; alias chains are refused so resolution stays "
                "one hop"
            ),
            declaration,
        )

    return AliasResolution(
        state=STATE_ALIASED,
        launch_root=declaration.canonical_path,
        declaration=declaration,
    )


__all__ = (
    "ALIAS_RELATIVE",
    "ALIAS_SCHEMA_VERSION",
    "MAX_DECLARATION_BYTES",
    "REASON_PARENT_DRIFT",
    "REASON_SNAPSHOT_FAILED",
    "REASON_TOO_LARGE",
    "AliasResolution",
    "AliasTargetObservation",
    "DECLARED_MODES",
    "GIT_BINDING_DIFFERENT",
    "GIT_BINDING_NOT_MEASURABLE",
    "GIT_BINDING_SAME",
    "MODE_ALIAS",
    "MODE_DISABLED",
    "REASON_ALIAS_CYCLE",
    "REASON_CROSS_REPOSITORY",
    "REASON_DECLARATION_INVALID",
    "REASON_DECLARATION_UNREADABLE",
    "REASON_MULTIPLE_LINKS",
    "REASON_NOT_REGULAR_FILE",
    "REASON_PARENT_UNSAFE",
    "REASON_READBACK_FAILED",
    "REASON_REMOVE_FAILED",
    "REASON_WRITE_FAILED",
    "REASON_TARGET_IDENTITY_MISMATCH",
    "REASON_TARGET_IDENTITY_UNRESOLVED",
    "REASON_TARGET_IS_SELF",
    "REASON_TARGET_MISSING",
    "REASON_TARGET_NOT_ANCESTOR",
    "REASON_TARGET_NOT_DECLARED",
    "REASON_TARGET_NOT_DIRECTORY",
    "REASON_UNSUPPORTED_SCHEMA",
    "STATE_ALIASED",
    "STATE_LAUNCH_DISABLED",
    "STATE_NO_DECLARATION",
    "STATE_REFUSED",
    "WorkspaceAliasDeclaration",
    "build_alias_resolution",
    "parse_declaration",
    "refused",
)
