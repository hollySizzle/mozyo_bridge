"""Managed Claude launch-model flag policy (Redmine #13155).

A managed Claude pane can be launched with a repo-configured ``--model
<token>`` so a role / lane pins which Claude model its worker runs, without
depending on an operator remembering a flag and without changing the launch
command when no model is configured.

The launch command is the only responsibility this module takes: it renders
a `` --model <token>`` suffix (leading space) the launch chokepoint
concatenates directly onto the agent command — the same concat convention as
:func:`mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy.permission_mode_flag`.
A CLI ``--model`` flag is a per-session selection and never reads or writes any
settings file.

Contract, kept deliberately small and pure:

1. **Codex panes never get a flag** — Claude-only, always.
2. **``model=None`` renders nothing** — the historical launch command,
   byte-for-byte, so an unconfigured repo is unaffected.
3. **A configured value is a single opaque model token, not a shell string.**
   It must match :data:`MODEL_TOKEN_PATTERN` (a leading alphanumeric then
   alphanumerics / ``.`` / ``_`` / ``-``); a space, empty value, flag, path, or
   shell metacharacter raises :class:`InvalidClaudeModel` so a typo / injection
   cannot reach a launch command. Callers at the launch chokepoint translate
   this into a hard CLI error.

The regex is kept intentionally identical to the config schema's
``_MODEL_TOKEN_RE``
(:mod:`mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config`);
the two are small and deliberately duplicated so neither layer depends on the
other (the config schema validates at parse time, this validates at launch
time — defense in depth).
"""

from __future__ import annotations

import re

#: The permitted shape of a launch model token (Redmine #13155). See the module
#: docstring: a single opaque token, never a shell string. Kept byte-identical to
#: ``repo_local_config._MODEL_TOKEN_RE``.
MODEL_TOKEN_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]+$"
_MODEL_TOKEN_RE = re.compile(MODEL_TOKEN_PATTERN)


class InvalidClaudeModel(ValueError):
    """Raised when a requested Claude launch model token is not a valid token.

    A malformed value must fail loudly rather than silently reach — or corrupt —
    the launch command (#13155). Callers at the launch chokepoint translate this
    into a hard CLI error.
    """


def claude_model_flag(agent: str, model: str | None) -> str:
    """`` --model <token>`` suffix for a managed Claude pane, or ``""``.

    Returns ``""`` for a Codex pane or when ``model`` is ``None`` (the
    historical launch command). For a Claude pane with a non-``None`` model,
    renders `` --model <token>`` with a leading space so the launch chokepoint
    can concatenate it directly, or raises :class:`InvalidClaudeModel` when the
    value is not a single valid model token.
    """
    if agent != "claude" or model is None:
        return ""
    if not isinstance(model, str) or not _MODEL_TOKEN_RE.match(model):
        raise InvalidClaudeModel(
            f"{model!r} is not a valid Claude model token: expected a single "
            f"token matching {MODEL_TOKEN_PATTERN} (no spaces, empty value, or "
            "shell metacharacters)"
        )
    return f" --model {model}"
