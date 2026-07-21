"""Repo-local config shared helpers + launch / integration sub-records (Redmine #12189/#12604/#13155/#13425).

Extracted from :mod:`repo_local_config` so that module stays within the module-health line
budget (Redmine #14148 item 10 leaf split). This holds the fail-closed schema *helpers* every
repo-local sub-record shares (:class:`RepoLocalConfigError`, the boundary-token / unknown-key
screens, the strict version / bool checks) plus the two self-contained sub-record schemas
(:class:`SublaneIntegrationConfig`, :class:`AgentLaunchConfig`). It imports only the launch-argv
validator sibling, so the dependency points one way (records -> agent_launch_argv); the composing
:mod:`repo_local_config` imports back from here and re-exports for a stable public surface.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Optional

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (
    AgentLaunchArgvError,
    parse_launch_argv_record,
    validate_launch_argv,
)


#: The legacy repo-local config record version. ``version`` is optional in a
#: record and defaults to this, so a config with no ``version`` key reads as the
#: historical v1 shape.
REPO_LOCAL_CONFIG_VERSION: int = 1
#: The closed set of recognized keys inside the ``sublane_integration`` sub-record
#: (Redmine #12604): the sublane Git worktree / retire-merge policy knob. ``version``
#: is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`; the three policy
#: keys carry *operational intent only* (see :class:`SublaneIntegrationConfig`) and
#: never any owner-approval / close / callback / routing / send authority — those stay
#: core-owned and the runtime preflight remains the final authority over this config.
SUBLANE_INTEGRATION_KEYS: frozenset[str] = frozenset(
    {"version", "manage_worktree", "integration_branch", "merge_on_retire"}
)
#: The behavior-preserving sublane-integration policy defaults (Redmine #12604).
#: They are the documented *default path* of the (opt-in, explicitly invoked) sublane
#: integration flow — create a worktree / branch at launch, and attempt a merge to the
#: integration branch before retirement. They do **not** change any existing command:
#: no current ``mozyo-bridge`` surface reads this block, and a repo that never invokes
#: the sublane integration flow is unaffected by the defaults.
DEFAULT_MANAGE_WORKTREE: bool = True
DEFAULT_MERGE_ON_RETIRE: bool = True
#: The closed set of recognized keys inside the ``agent_launch`` sub-record
#: (Redmine #13155): the per-role / lane managed-pane launch model knob. ``version``
#: is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`; ``sublane_claude_model``
#: names a single Claude model token appended as ``--model <token>`` at the sublane
#: managed-pane launch chokepoint. It carries *launch-model intent only* — never a shell
#: string, and never any routing / owner-approval / close / send authority — and an unset
#: value is byte-for-byte the historical launch command.
AGENT_LAUNCH_KEYS: frozenset[str] = frozenset(
    {"version", "sublane_claude_model", "launch_argv"}
)
#: The permitted shape of a launch model token (Redmine #13155). A single opaque token —
#: a leading alphanumeric then alphanumerics / ``.`` / ``_`` / ``-`` — so a config value
#: can never smuggle a space, empty value, shell metacharacter, flag, or path into the
#: launch command. Kept intentionally identical to the flag-policy module's regex
#: (:data:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_model_policy.MODEL_TOKEN_PATTERN`);
#: the two are small and deliberately duplicated so neither layer depends on the other.
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
#: Substrings in a config key that signal an attempt to cross a boundary this
#: surface owns: load / execute code, name a module / callable / entry point,
#: grant or alter authority / approval / routing / send safety, address a
#: target / pane / route, or carry a credential / secret. Such a key is rejected
#: with a boundary-specific message rather than the generic unknown-key error, so
#: the rejection reads as deliberate in an audit. This is the union of the CLI
#: composition record's forbidden parts (Redmine #12155) plus the
#: presentation-only additions (``target`` / ``pane`` / ``route`` / ``send``) the
#: projection-only invariant requires.
_FORBIDDEN_KEY_PARTS: tuple[str, ...] = (
    "import",
    "module",
    "path",
    "registrar",
    "callable",
    "entry",
    "plugin",
    "exec",
    "eval",
    "script",
    "load",
    "authority",
    "authorities",
    "approval",
    "approve",
    "grant",
    "owner",
    "review",
    "close",
    "routing",
    "route",
    "send",
    "send_safety",
    "target",
    "pane",
    "role",
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
class RepoLocalConfigError(ValueError):
    """The repo-local YAML config record violates the closed schema.

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    domain errors (:class:`~mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry.ModuleRegistryError`,
    :class:`~mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry.ProviderRegistryError`,
    :class:`~mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.domain.presentation_adapter.PresentationRecordError`).
    """
def _reject_boundary_token(token: object, *, source: str, role: str) -> None:
    """Fail closed on a repo-local config string that crosses an owned boundary.

    ``token`` is something the YAML names — a top-level key, a provider-selection
    category, or a selected provider id. A non-string token is left for the
    downstream record to type-check; a string whose name contains a
    :data:`_FORBIDDEN_KEY_PARTS` token (code loading, module / callable / entry
    point, authority / approval / routing, target / pane / route / send, or a
    credential) is rejected here with a boundary-specific message so the
    rejection reads as deliberate in an audit.
    """
    if not isinstance(token, str):
        return
    lowered = token.lower()
    for part in _FORBIDDEN_KEY_PARTS:
        if part in lowered:
            raise RepoLocalConfigError(
                f"{source} {role} {token!r} may not carry a boundary token: this "
                f"surface is config-only and may never load code, name a module / "
                f"callable / entry point, grant authority, address a target / pane "
                f"/ route, or carry a credential (matched forbidden token {part!r})."
            )
def _reject_boundary_and_unknown_keys(
    record: "Mapping[object, object]",
    *,
    allowed: "frozenset[str]",
    source: str,
) -> None:
    """Fail closed on a non-string / boundary-crossing / unknown record key.

    Runs the same ordered checks the CLI composition record uses: keys must be
    non-empty strings; a key whose name contains a :data:`_FORBIDDEN_KEY_PARTS`
    token is rejected with a boundary-specific message (code loading, authority,
    routing, target/pane, or credential); any remaining key outside ``allowed``
    is rejected as an unknown key (closed schema / typo protection).
    """
    for key in record:
        if not isinstance(key, str) or not key:
            raise RepoLocalConfigError(
                f"{source} record keys must be non-empty strings; got {key!r}"
            )
        _reject_boundary_token(key, source=source, role="record key")
        if key not in allowed:
            raise RepoLocalConfigError(
                f"{source} record has unknown key {key!r}; allowed keys: "
                f"{sorted(allowed)}"
            )
def _checked_version(record: "Mapping[object, object]", *, source: str) -> int:
    """Return the supported version, failing closed on anything else.

    ``version`` is optional and defaults to :data:`REPO_LOCAL_CONFIG_VERSION`.
    ``bool`` is rejected even though it is an ``int`` subclass so ``version:
    true`` does not silently read as version ``1``.
    """
    version = record.get("version", REPO_LOCAL_CONFIG_VERSION)
    if isinstance(version, bool) or not isinstance(version, int):
        raise RepoLocalConfigError(
            f"{source} record 'version' must be an integer, got {version!r}"
        )
    if version != REPO_LOCAL_CONFIG_VERSION:
        raise RepoLocalConfigError(
            f"unsupported {source} record version {version!r}; this build "
            f"understands version {REPO_LOCAL_CONFIG_VERSION}"
        )
    return version
def _checked_bool(
    record: "Mapping[object, object]", key: str, default: bool, *, source: str
) -> bool:
    """Return a strict boolean record value, failing closed on anything else.

    The key is optional and defaults to ``default``. ``int`` (including ``0`` /
    ``1``) and any non-``bool`` value are rejected so ``merge_on_retire: 0`` cannot
    silently read as ``False`` and a typo'd value never becomes a quiet policy
    change — the fail-closed boundary the rest of this schema enforces.
    """
    if key not in record:
        return default
    value = record[key]
    if not isinstance(value, bool):
        raise RepoLocalConfigError(
            f"{source} record {key!r} must be a boolean, got {value!r}"
        )
    return value
@dataclass(frozen=True)
class SublaneIntegrationConfig:
    """The sublane Git worktree / retire-merge policy knob (Redmine #12604).

    This is the typed *field contract* for the ``sublane_integration`` block of
    ``.mozyo-bridge/config.yaml``. It carries **operational intent only** for the
    config-driven sublane lifecycle the parent (#12603) re-evaluates: whether to
    create a Git worktree / branch at sublane launch, which integration branch a
    retire-time merge targets, and whether that merge is attempted at all.

    Three policy fields:

    - :attr:`manage_worktree` — in a Git workspace, the sublane launch default path
      creates a worktree / branch (the documented #12604 default). ``False`` opts a
      lane out, e.g. for a non-Git directory scaffold the runtime preflight also
      skips worktree creation regardless of this flag.
    - :attr:`integration_branch` — the configured target branch a retire-time merge
      integrates into. ``None`` (the default) leaves the branch to runtime
      resolution (e.g. the repo default branch); a runtime that cannot resolve a
      target fails closed (``integration_blocked``) rather than guessing.
    - :attr:`merge_on_retire` — attempt a merge to :attr:`integration_branch` before
      retiring the lane (the documented #12604 default). ``merge_on_retire: false``
      is the opt-out: the merge attempt is skipped, but every other retirement gate
      (clean worktree, verification, durable record, and the owner-approval / close
      / callback / durable-anchor invariants) still applies.

    Boundary, kept enforced in code (this is *policy intent*, not authority):

    - **The runtime preflight is the final authority, never this config.** This
      record can opt *out* of a merge attempt; it can never opt out of a safety
      gate or mark a blocked retirement as ``ok``. The decision authority is the
      pure :mod:`...domain.sublane_integration_policy` preflight, which fails closed
      on a dirty worktree, a merge conflict, an unresolved target branch, a
      verification failure, or a missing invariant — *whatever this config says*.
    - **The owner-approval / close / callback / durable-anchor invariants cannot be
      disabled here.** There is deliberately no key for them: the closed
      :data:`SUBLANE_INTEGRATION_KEYS` set admits only the three operational fields,
      and a boundary-shaped key (``owner`` / ``approval`` / ``close`` / ``route`` /
      ``send`` / credential, …) is rejected by the same closed-schema check the rest
      of this module enforces.
    - **Behavior-preserving default.** The default (manage worktree, merge on
      retire, runtime-resolved branch) is the documented default *path of the opt-in
      sublane integration flow*; no existing command reads this block, so a missing
      ``sublane_integration`` block never changes default ``mozyo-bridge`` behavior.
    """

    manage_worktree: bool = DEFAULT_MANAGE_WORKTREE
    integration_branch: Optional[str] = None
    merge_on_retire: bool = DEFAULT_MERGE_ON_RETIRE

    @classmethod
    def default(cls) -> "SublaneIntegrationConfig":
        """The behavior-preserving default sublane-integration policy."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "SublaneIntegrationConfig":
        """Normalize a ``sublane_integration`` sub-record into a typed policy.

        ``None`` or an empty mapping yields the behavior-preserving default. A
        non-mapping record, a boundary-crossing / unknown key, an unsupported
        version, a non-boolean ``manage_worktree`` / ``merge_on_retire``, or an
        ``integration_branch`` that is neither ``None`` nor a non-empty string fails
        closed with :class:`RepoLocalConfigError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RepoLocalConfigError(
                "sublane integration config record must be a mapping (a YAML "
                f"table), got {type(record).__name__}"
            )
        _reject_boundary_and_unknown_keys(
            record,
            allowed=SUBLANE_INTEGRATION_KEYS,
            source="sublane integration config",
        )
        _checked_version(record, source="sublane integration config")
        manage_worktree = _checked_bool(
            record,
            "manage_worktree",
            DEFAULT_MANAGE_WORKTREE,
            source="sublane integration config",
        )
        merge_on_retire = _checked_bool(
            record,
            "merge_on_retire",
            DEFAULT_MERGE_ON_RETIRE,
            source="sublane integration config",
        )
        integration_branch = record.get("integration_branch")
        if integration_branch is not None:
            if not isinstance(integration_branch, str) or not integration_branch.strip():
                raise RepoLocalConfigError(
                    "sublane integration config 'integration_branch' must be a "
                    "non-empty string naming the target branch, or omitted for "
                    f"runtime resolution; got {integration_branch!r}"
                )
        return cls(
            manage_worktree=manage_worktree,
            integration_branch=integration_branch,
            merge_on_retire=merge_on_retire,
        )
@dataclass(frozen=True)
class AgentLaunchConfig:
    """The provider-agnostic per-agent x lane-class launch-argv override (#13155/#13425).

    This is the typed *field contract* for the ``agent_launch`` block of
    ``.mozyo-bridge/config.yaml``. It lets a repo pin the extra launch argv (model,
    reasoning-effort flag, …) each managed agent is started with, keyed by
    ``provider x lane_class``, without touching the launch command's byte shape when
    unset. mozyo does **not** hardcode any provider's flag spec — it appends the
    operator's tokens verbatim after ``-- {provider}`` (Redmine #13425).

    Value fields:

    - :attr:`launch_argv` — the generalized override: a frozen, sorted tuple of
      ``(provider, lane_class, tokens)`` triples parsed from the
      ``launch_argv: {provider: {lane_class: [tokens]}}`` mapping. ``provider`` is a
      :data:`LAUNCH_ARGV_PROVIDERS` launch label (never an executable / argv[0], which
      stays mozyo-controlled — #13245 posture); ``lane_class`` is ``default`` (the main
      coordinator / auditor pair) or ``sublane`` (a lane worker / gateway); each token is
      validated by :func:`_validate_launch_argv_token`. The keying axis is
      ``provider x lane_class``, deliberately NOT the ``provider_binding`` workflow-role
      axis (design consultation answer j#73949 Q1).
    - :attr:`sublane_claude_model` — the #13155 predecessor: a single Claude model *token*
      (e.g. ``claude-opus-4-8``) matching :data:`_MODEL_TOKEN_RE`. It is **folded** into the
      generalized mechanism: :meth:`resolve_launch_argv` treats it as the
      ``claude x sublane`` argv ``["--model", <token>]`` (answer j#73949 Q5). Kept as a
      distinct field for byte-for-byte backward compatibility.

    Boundary, kept enforced in code (this is *launch intent*, not authority):

    - **Config-only, never an executable.** Tokens are argv *elements* appended after the
      mozyo-controlled provider command; config can never select argv[0] / the executable
      (#13245). A path in a flag *value* is allowed; the shell-string launch surface
      ``shlex.quote``s every token.
    - **No mozyo-managed flag override.** A token that re-specifies a
      :data:`RESERVED_MANAGED_FLAGS` flag (currently Claude ``--permission-mode``) fails
      closed — the managed posture (#13360) is authoritative (answer j#73949 Q4).
    - **Old / new conflict fails closed.** Setting both ``sublane_claude_model`` and an
      explicit ``launch_argv.claude.sublane`` is a fail-closed conflict — the operator's
      intended source is ambiguous (answer j#73949 Q5).
    - **Non-retroactive.** The tokens only affect a managed pane mozyo *launches*; an
      already-running pane is untouched (same posture as the #11925 permission policy).
    - **Behavior-preserving default.** No block ⇒ empty ⇒ no extra argv, so a repo with no
      ``agent_launch`` block launches exactly as before.
    """

    version: int = REPO_LOCAL_CONFIG_VERSION
    sublane_claude_model: Optional[str] = None
    launch_argv: "tuple[tuple[str, str, tuple[str, ...]], ...]" = ()

    def __post_init__(self) -> None:
        model = self.sublane_claude_model
        if model is not None and (
            not isinstance(model, str) or not _MODEL_TOKEN_RE.match(model)
        ):
            raise RepoLocalConfigError(
                "agent launch config 'sublane_claude_model' must be a single model "
                f"token matching {_MODEL_TOKEN_RE.pattern} (no spaces, empty value, or "
                "shell metacharacters), or omitted for the historical launch command; "
                f"got {model!r}"
            )
        # Validate the generalized launch_argv (covers direct construction too — existing
        # tests build AgentLaunchConfig(...) directly, and from_record re-uses this path).
        # The sibling raises AgentLaunchArgvError; re-raise as RepoLocalConfigError so the
        # public config-failure boundary is uniform (the role_provider_binding pattern).
        try:
            validate_launch_argv(
                self.launch_argv,
                sublane_claude_model_set=model is not None,
                source="agent launch config",
            )
        except AgentLaunchArgvError as exc:
            raise RepoLocalConfigError(str(exc)) from exc

    def resolve_launch_argv(self, provider: str, lane_class: str) -> "list[str]":
        """The extra launch argv tokens for ``(provider, lane_class)`` (the single source).

        Both launch backends resolve through this method (Redmine #13425 answer j#73949
        Q6): herdr extends its ``agent start ... -- {provider}`` argv list with the
        returned tokens verbatim; the tmux shell-string surface ``shlex.quote``s them. An
        explicit :attr:`launch_argv` entry wins; otherwise the #13155
        :attr:`sublane_claude_model` is folded in for the ``claude x sublane`` slot only
        (the conflict guard in :meth:`__post_init__` means at most one source is set).
        Returns a fresh list (``[]`` when nothing is configured — byte-for-byte historical).
        """
        for entry_provider, entry_lane_class, tokens in self.launch_argv:
            if entry_provider == provider and entry_lane_class == lane_class:
                return list(tokens)
        if (
            provider == "claude"
            and lane_class == "sublane"
            and self.sublane_claude_model is not None
        ):
            return ["--model", self.sublane_claude_model]
        return []

    @classmethod
    def default(cls) -> "AgentLaunchConfig":
        """The behavior-preserving default: no launch-argv override."""
        return cls()

    @classmethod
    def from_record(
        cls, record: "Optional[Mapping[str, object]]" = None
    ) -> "AgentLaunchConfig":
        """Normalize an ``agent_launch`` sub-record into a typed policy.

        ``None`` or an empty mapping yields the behavior-preserving default. A
        non-mapping record, a boundary-crossing / unknown key, an unsupported
        version, a ``sublane_claude_model`` that is neither ``None`` nor a valid
        single model token, or a ``launch_argv`` that violates the provider /
        lane_class / token / reserved-flag / old-new-conflict rules fails closed
        with :class:`RepoLocalConfigError`.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise RepoLocalConfigError(
                "agent launch config record must be a mapping (a YAML table), got "
                f"{type(record).__name__}"
            )
        _reject_boundary_and_unknown_keys(
            record, allowed=AGENT_LAUNCH_KEYS, source="agent launch config"
        )
        version = _checked_version(record, source="agent launch config")
        try:
            launch_argv = parse_launch_argv_record(
                record.get("launch_argv"), source="agent launch config"
            )
        except AgentLaunchArgvError as exc:
            raise RepoLocalConfigError(str(exc)) from exc
        return cls(
            version=version,
            sublane_claude_model=record.get("sublane_claude_model"),
            launch_argv=launch_argv,
        )


__all__ = (
    "REPO_LOCAL_CONFIG_VERSION",
    "SUBLANE_INTEGRATION_KEYS",
    "DEFAULT_MANAGE_WORKTREE",
    "DEFAULT_MERGE_ON_RETIRE",
    "AGENT_LAUNCH_KEYS",
    "RepoLocalConfigError",
    "_reject_boundary_token",
    "_reject_boundary_and_unknown_keys",
    "_checked_version",
    "_checked_bool",
    "SublaneIntegrationConfig",
    "AgentLaunchConfig",
)
