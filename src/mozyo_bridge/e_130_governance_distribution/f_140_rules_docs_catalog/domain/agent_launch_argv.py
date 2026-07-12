"""Launch-argv override vocabulary + validation for ``agent_launch`` (Redmine #13425).

The self-contained sibling of the repo-local config schema — it mirrors
:mod:`...domain.role_provider_binding_config`: the provider / lane-class vocabulary, the
reserved managed-flag set, and the per-token / structural / conflict validators of the
``agent_launch.launch_argv`` table live here, so
:class:`~...domain.repo_local_config.AgentLaunchConfig` stays a thin field contract and the
governance-config module stays within the module-health budget while the launch-argv rules
are one cohesive unit.

It raises its own :class:`AgentLaunchArgvError` and imports **nothing** from
``repo_local_config`` (one-way dependency, exactly like ``role_provider_binding_config``);
the composing ``AgentLaunchConfig`` re-raises it as ``RepoLocalConfigError`` so the public
config-failure boundary stays uniform.

``launch_argv`` is the provider-agnostic generalization of the #13155 single-model knob:
a ``provider -> lane_class -> [argv tokens]`` table whose tokens mozyo appends verbatim
after ``-- {provider}`` at the launch chokepoint (mozyo never hardcodes any provider's flag
spec — it pass-throughs the operator's tokens). The provider key is a launch provider label
(never an executable / argv[0] — that stays mozyo-controlled, #13245 posture); ``lane_class``
is ``default`` (the main coordinator / auditor pair) or ``sublane`` (a lane worker /
gateway). The keying axis is deliberately ``provider x lane_class`` — NOT the workflow-role
vocabulary of ``provider_binding`` (#13157), a different (role -> provider) axis (design
consultation answer j#73949 Q1).
"""

from __future__ import annotations

from collections.abc import Mapping

#: The closed provider + lane-class vocabulary of ``agent_launch.launch_argv`` (#13425).
LAUNCH_ARGV_PROVIDERS: frozenset[str] = frozenset({"claude", "codex"})
LAUNCH_ARGV_LANE_CLASSES: frozenset[str] = frozenset({"default", "sublane"})

#: mozyo-owned managed launch flags a config ``launch_argv`` may NOT specify (Redmine
#: #13425 design consultation answer j#73949 Q4). The managed Claude permission-mode
#: posture (#13360) is mozyo policy applied at the launch chokepoint; a config token that
#: re-specifies it fails closed rather than silently overriding the managed launch (config
#: argv is otherwise rendered *after* the managed flag, so CLI last-wins would let it win).
RESERVED_MANAGED_FLAGS: "dict[str, tuple[str, ...]]" = {
    "claude": ("--permission-mode",),
}


class AgentLaunchArgvError(ValueError):
    """An ``agent_launch.launch_argv`` override violates the closed schema (fail-closed).

    Inherits :class:`ValueError` for fail-closed semantics, matching the sibling
    repo-local domain errors. The composing
    :class:`~...domain.repo_local_config.AgentLaunchConfig` re-raises this as its own
    ``RepoLocalConfigError`` so the repo-local config loader keeps a single fail-closed
    boundary.
    """


def _validate_launch_argv_token(token: object, *, source: str) -> None:
    """Fail closed on a launch-argv token that is not a safe single argv element.

    Redmine #13425 (design consultation answer j#73949 Q2/Q3): a token is a single argv
    element mozyo appends verbatim after ``-- {provider}``. It is validated as **B3** —
    reject a non-string, an empty string, or a token carrying NUL / newline / any other
    control character; *allow* ``=`` / ``/`` / ``:`` / ``.`` / ``_`` / ``-`` / spaces and
    path-like values (a flag *value* that is a path — e.g. ``--add-dir /x`` — is
    legitimate; the executable / argv[0] stays mozyo-controlled, so a path in a value is
    not a boundary breach). The shell-string launch surface (tmux) ``shlex.quote``s each
    token; the argv-list surface (herdr) extends the list verbatim.
    """
    if not isinstance(token, str):
        raise AgentLaunchArgvError(
            f"{source} launch argv token must be a string, got "
            f"{type(token).__name__}"
        )
    if token == "":
        raise AgentLaunchArgvError(f"{source} launch argv token must not be empty")
    if token == "--":
        raise AgentLaunchArgvError(
            f"{source} launch argv token '--' is not allowed: managed launch options "
            "must remain in the provider option region"
        )
    for ch in token:
        if ord(ch) < 0x20 or ord(ch) == 0x7F:
            raise AgentLaunchArgvError(
                f"{source} launch argv token {token!r} must not contain NUL, "
                "newline, tab, or other control characters"
            )


def _reject_reserved_managed_flags(
    provider: str, tokens: "tuple[str, ...]", *, source: str
) -> None:
    """Fail closed when config launch argv re-specifies a mozyo-managed flag.

    Redmine #13425 (answer j#73949 Q4): config argv is rendered *after* the managed flag,
    so CLI last-wins semantics would let a config ``--permission-mode`` override the
    managed Claude permission posture (#13360). The managed flag is mozyo policy, so a
    config token that names it (bare or ``--flag=value``) fails closed rather than silently
    winning.
    """
    for flag in RESERVED_MANAGED_FLAGS.get(provider, ()):
        for token in tokens:
            if token == flag or token.startswith(flag + "="):
                raise AgentLaunchArgvError(
                    f"{source} launch argv for provider {provider!r} may not specify "
                    f"the mozyo-managed flag {flag!r}: it is set by the managed launch "
                    "posture (Redmine #13360) and cannot be overridden from config"
                )


def parse_launch_argv_record(
    record: object, *, source: str
) -> "tuple[tuple[str, str, tuple[str, ...]], ...]":
    """Normalize an ``agent_launch.launch_argv`` mapping into sorted, frozen triples.

    Shape: ``provider -> lane_class -> [argv tokens]`` where ``provider`` is a
    :data:`LAUNCH_ARGV_PROVIDERS` label and ``lane_class`` a :data:`LAUNCH_ARGV_LANE_CLASSES`
    value. Returns a sorted tuple of ``(provider, lane_class, tokens)`` triples (hashable,
    so the composing :class:`RepoLocalConfig` stays hashable). Structural validation only —
    the per-token / reserved-flag / old-new-conflict checks run in
    :func:`validate_launch_argv` (invoked from ``AgentLaunchConfig.__post_init__`` so a
    directly-constructed config is validated too). ``None`` yields ``()`` (no override,
    byte-for-byte historical).
    """
    if record is None:
        return ()
    if not isinstance(record, Mapping):
        raise AgentLaunchArgvError(
            f"{source} 'launch_argv' must be a mapping of provider -> lane_class -> "
            f"argv list, got {type(record).__name__}"
        )
    triples: list = []
    for provider, lane_map in record.items():
        if not isinstance(provider, str) or provider not in LAUNCH_ARGV_PROVIDERS:
            raise AgentLaunchArgvError(
                f"{source} 'launch_argv' provider key must be one of "
                f"{sorted(LAUNCH_ARGV_PROVIDERS)}, got {provider!r}"
            )
        if not isinstance(lane_map, Mapping):
            raise AgentLaunchArgvError(
                f"{source} 'launch_argv.{provider}' must be a mapping of lane_class -> "
                f"argv list, got {type(lane_map).__name__}"
            )
        for lane_class, argv in lane_map.items():
            if (
                not isinstance(lane_class, str)
                or lane_class not in LAUNCH_ARGV_LANE_CLASSES
            ):
                raise AgentLaunchArgvError(
                    f"{source} 'launch_argv.{provider}' lane-class key must be one of "
                    f"{sorted(LAUNCH_ARGV_LANE_CLASSES)}, got {lane_class!r}"
                )
            if not isinstance(argv, (list, tuple)):
                raise AgentLaunchArgvError(
                    f"{source} 'launch_argv.{provider}.{lane_class}' must be a list of "
                    f"argv tokens, got {type(argv).__name__}"
                )
            triples.append((provider, lane_class, tuple(argv)))
    return tuple(sorted(triples))


def validate_launch_argv(
    launch_argv: "tuple[tuple[str, str, tuple[str, ...]], ...]",
    *,
    sublane_claude_model_set: bool,
    source: str,
) -> None:
    """Fail closed on an invalid ``launch_argv`` (provider / lane_class / token / conflict).

    Runs the full (non-structural) validation so a directly-constructed
    ``AgentLaunchConfig(launch_argv=...)`` is checked as thoroughly as one parsed from a
    record: every provider / lane_class is in vocabulary, every token passes
    :func:`_validate_launch_argv_token`, no token re-specifies a
    :data:`RESERVED_MANAGED_FLAGS` flag, and an explicit ``claude x sublane`` entry does not
    collide with a set ``sublane_claude_model`` (the old key folds into that same slot —
    answer j#73949 Q5).
    """
    for provider, lane_class, tokens in launch_argv:
        if provider not in LAUNCH_ARGV_PROVIDERS:
            raise AgentLaunchArgvError(
                f"{source} 'launch_argv' provider must be one of "
                f"{sorted(LAUNCH_ARGV_PROVIDERS)}, got {provider!r}"
            )
        if lane_class not in LAUNCH_ARGV_LANE_CLASSES:
            raise AgentLaunchArgvError(
                f"{source} 'launch_argv' lane_class must be one of "
                f"{sorted(LAUNCH_ARGV_LANE_CLASSES)}, got {lane_class!r}"
            )
        for token in tokens:
            _validate_launch_argv_token(token, source=source)
        _reject_reserved_managed_flags(provider, tokens, source=source)
    if sublane_claude_model_set and any(
        provider == "claude" and lane_class == "sublane"
        for provider, lane_class, _ in launch_argv
    ):
        raise AgentLaunchArgvError(
            f"{source} sets both 'sublane_claude_model' and "
            "'launch_argv.claude.sublane'; they are the same slot — set only one "
            "(the old key folds into launch_argv.claude.sublane)"
        )


__all__ = (
    "LAUNCH_ARGV_PROVIDERS",
    "LAUNCH_ARGV_LANE_CLASSES",
    "RESERVED_MANAGED_FLAGS",
    "AgentLaunchArgvError",
    "parse_launch_argv_record",
    "validate_launch_argv",
)
