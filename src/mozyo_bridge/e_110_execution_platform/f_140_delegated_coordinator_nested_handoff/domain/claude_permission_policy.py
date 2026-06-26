"""Reproducible Claude permission-mode launch policy (Redmine #11925).

A managed Claude pane should launch with ``claude --permission-mode auto``
reproducibly when mozyo creates it for a cockpit / sublane lane — without
depending on the operator remembering to export an env var, and without
mozyo writing repo-local ``.claude/settings.json`` (Claude Code v2.1.142+
ignores a repo-local ``defaultMode: "auto"`` by design, see #11924 j#58207).

The launch command is the only responsibility mozyo takes here: it appends
``--permission-mode <mode>`` to the launch command, which is a per-session
override and never reads or writes any user / project settings file.

Resolution precedence (kept deliberately small and pure so both the
launch chokepoint and ``doctor`` introspect the *same* logic):

1. **Codex panes** never get a flag — Claude-only, always.
2. ``MOZYO_CLAUDE_PERMISSION_MODE`` env var (the #11857 primitive) is the
   compatibility / explicit override rail. When set to a valid mode it
   wins, so an operator can still force any mode (including turning auto
   *off* with ``default``) for one shell / cockpit session.
3. Otherwise the **launch-context policy default** applies. Cockpit /
   sublane managed-pane creation passes ``COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT``
   (``auto``); the standalone ``mozyo`` window path passes ``None`` so its
   historical bare-``claude`` launch is never changed silently.
4. Otherwise no flag.

The policy is non-retroactive: a CLI flag only affects the pane mozyo
launches, so already-running panes keep whatever mode they started with.
"""

from __future__ import annotations

from typing import Mapping

# The #11857 opt-in / override env var. Kept as a compatibility + explicit
# override rail, NOT the only source of truth (#11924 owner decision j#58208).
CLAUDE_PERMISSION_MODE_ENV = "MOZYO_CLAUDE_PERMISSION_MODE"

# Choices confirmed from local `claude --help` (#11857).
CLAUDE_PERMISSION_MODES = frozenset(
    {"acceptEdits", "auto", "bypassPermissions", "default", "dontAsk", "plan"}
)

# The launch-context default for cockpit / sublane managed Claude panes
# (#11925). This is what makes auto reproducible without an env var.
COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT = "auto"

# Source labels for the resolved mode, surfaced by `describe_launch_policy`
# so diagnostics can explain *why* a future pane will / won't be auto.
SOURCE_ENV_OVERRIDE = "env-override"
SOURCE_ENV_INVALID = "env-invalid"
SOURCE_POLICY_DEFAULT = "policy-default"
SOURCE_NONE = "none"


class InvalidPermissionMode(ValueError):
    """Raised when a requested Claude permission mode is not recognized.

    A typo must fail loudly rather than silently launch a default-permission
    pane the operator did not intend (#11857). Callers at the launch
    chokepoint translate this into a hard CLI error; ``doctor`` catches the
    equivalent state without raising so a misconfigured env var surfaces as
    a warning instead of crashing the diagnostic.
    """


def _normalized_env_mode(env: Mapping[str, str]) -> str:
    return (env.get(CLAUDE_PERMISSION_MODE_ENV) or "").strip()


def resolve_claude_permission_mode(
    agent: str,
    *,
    policy_default: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Resolve the effective Claude permission mode for a launch, or ``None``.

    ``None`` means "append no ``--permission-mode`` flag" (historical bare
    ``claude``). See the module docstring for the precedence contract.

    Raises :class:`InvalidPermissionMode` when the env override or the
    supplied ``policy_default`` is a non-empty, unrecognized value.
    """
    if agent != "claude":
        return None

    if env is None:
        import os

        env = os.environ

    env_mode = _normalized_env_mode(env)
    if env_mode:
        if env_mode not in CLAUDE_PERMISSION_MODES:
            raise InvalidPermissionMode(
                f"{CLAUDE_PERMISSION_MODE_ENV}={env_mode!r} is not a valid "
                f"Claude permission mode (choices: "
                f"{', '.join(sorted(CLAUDE_PERMISSION_MODES))})"
            )
        return env_mode

    if policy_default is None:
        return None
    if policy_default not in CLAUDE_PERMISSION_MODES:
        raise InvalidPermissionMode(
            f"policy default {policy_default!r} is not a valid Claude "
            f"permission mode (choices: "
            f"{', '.join(sorted(CLAUDE_PERMISSION_MODES))})"
        )
    return policy_default


def permission_mode_flag(
    agent: str,
    *,
    policy_default: str | None = None,
    env: Mapping[str, str] | None = None,
) -> str:
    """`` --permission-mode <mode>`` suffix for a managed pane, or ``""``.

    Thin wrapper over :func:`resolve_claude_permission_mode` that renders the
    flag (with a leading space) so the launch chokepoint can concatenate it
    directly onto the agent command.
    """
    mode = resolve_claude_permission_mode(
        agent, policy_default=policy_default, env=env
    )
    if not mode:
        return ""
    return f" --permission-mode {mode}"


def describe_launch_policy(
    *,
    policy_default: str | None = COCKPIT_CLAUDE_PERMISSION_MODE_DEFAULT,
    env: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Introspect the cockpit / sublane Claude launch policy for diagnostics.

    Read-only and never raises: ``doctor`` uses this to tell an operator
    whether *future* cockpit / sublane Claude panes will launch in ``auto``
    mode, and why. An invalid env override is reported as ``env-invalid``
    (which would hard-error at actual launch) rather than crashing here.

    Returns a dict with ``effective_mode`` (the mode a future cockpit pane
    would launch with, or ``None`` for bare ``claude``), ``source`` (one of
    the ``SOURCE_*`` constants), ``reproducible_auto`` (bool), and the env
    var observation so the override rail is visible.
    """
    if env is None:
        import os

        env = os.environ

    env_mode = _normalized_env_mode(env)
    env_present = bool(env_mode)
    env_valid = (not env_present) or env_mode in CLAUDE_PERMISSION_MODES

    if env_present and not env_valid:
        # Would `die()` at launch — surface it instead of silently stalling.
        effective: str | None = None
        source = SOURCE_ENV_INVALID
    elif env_present:
        effective = env_mode
        source = SOURCE_ENV_OVERRIDE
    elif policy_default is not None:
        effective = policy_default
        source = SOURCE_POLICY_DEFAULT
    else:
        effective = None
        source = SOURCE_NONE

    return {
        "env_var": CLAUDE_PERMISSION_MODE_ENV,
        "env_present": env_present,
        "env_value": env_mode,
        "env_valid": env_valid,
        "policy_default": policy_default,
        "effective_mode": effective,
        "source": source,
        "reproducible_auto": effective == "auto",
    }
