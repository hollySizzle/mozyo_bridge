"""Concrete onboarding conversation bindings (Redmine #13497).

The provider-neutral port lives in ``domain.conversation_port``; the concrete
bindings live here so a provider's name / transport never leaks into the domain
contract (j#74919 R2). The production first binding is the safe Claude local-CLI
adapter; the deterministic fixed provider is a **test double only** and is never
admitted as a production fallback.
"""

from .claude_cli_provider import (
    DEFAULT_CLAUDE_MODEL,
    RunResult,
    SafeClaudeCliProvider,
    build_safe_argv,
)
from .provider_binary import CLAUDE_BINARY_ENV, resolve_claude_binary

__all__ = (
    "CLAUDE_BINARY_ENV",
    "DEFAULT_CLAUDE_MODEL",
    "RunResult",
    "SafeClaudeCliProvider",
    "build_safe_argv",
    "resolve_claude_binary",
)
