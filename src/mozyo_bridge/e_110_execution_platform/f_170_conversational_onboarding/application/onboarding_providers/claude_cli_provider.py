"""Safe Claude local-CLI conversation binding (Redmine #13497 j#74915 / j#74919 R2).

The production first binding for the provider-neutral conversation port. It
shells out to a local Claude CLI locked down to a **zero-authority, closed-output**
posture so the model can only translate natural language into a closed
``OnboardingIntent`` — never run a shell / file / network / MCP tool, read
project or user customization, or persist a session. The safety-bearing argv is
fixed per the pre-commit safety audit (Redmine #13497 j#74928):

- ``--print`` one-shot, ``--output-format json`` structured envelope;
- ``--tools ""`` — the CLI's explicit **zero built-in tool** selector (NOT
  ``--allowed-tools``, which only controls prompting for otherwise-registered
  tools);
- ``--safe-mode`` — disables all customizations (CLAUDE.md, skills, plugins,
  hooks, MCP servers, custom commands / agents) — the primary isolation boundary;
- ``--disable-slash-commands`` — disables skills / slash commands explicitly;
- ``--strict-mcp-config`` with an **explicit empty** ``--mcp-config
  {"mcpServers":{}}`` (zero MCP by construction, not by absence semantics);
- ``--json-schema`` = the closed conversation-turn schema, so the model cannot
  *generate* a key / enum outside the closed shape (post-hoc parsing is a second
  gate, not the only one);
- ``--no-session-persistence`` (nothing is written to a resumable session);
- ``--setting-sources ""`` + ``--exclude-dynamic-system-prompt-sections`` and a
  full ``--system-prompt`` replacement (defense-in-depth over safe-mode);
- ``--permission-mode plan`` — defense-in-depth only (never the sole guard);
- the runner runs in a neutral temp cwd so no on-disk ``CLAUDE.md`` is auto-read.

The inherited environment is *not* trusted to add authority: ``--safe-mode`` plus
this fixed argv is the boundary. ``stderr`` and the raw provider payload are never
persisted or rendered — only generic fail-closed codes surface.

Input is sanitized (only :class:`~...domain.conversation_port.SanitizedFacts`,
the closed schema, and the in-memory transcript reach the prompt — never the
canonical path, file hashes, herdr realpath, or the gate secret). Any inability
to operate safely — missing binary, timeout, non-zero exit, an error envelope,
or output that is not a single closed turn — fails closed as a
:class:`ConversationProviderError` (``conversation_provider_unavailable``); the
binding never fabricates an intent, and the transcript is never written anywhere.

The subprocess call is behind an injectable :data:`ProviderRunner` seam so the
argv construction, prompt sanitization, envelope parsing, and timeout / exit /
malformed handling are all testable without a live CLI (the live acceptance is
deferred to #13490 per the spec).
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import dataclass
from typing import Callable, Mapping

from ...domain.conversation_port import (
    PROVIDER_UNAVAILABLE,
    ConversationContext,
    ConversationProviderError,
    ConversationTurn,
    Explain,
    IntentCandidate,
    build_turn_json_schema,
)
from .provider_binary import resolve_claude_binary

__all__ = (
    "DEFAULT_CLAUDE_MODEL",
    "DEFAULT_TIMEOUT_S",
    "EMPTY_MCP_CONFIG",
    "RunResult",
    "ProviderRunner",
    "SafeClaudeCliProvider",
    "build_safe_argv",
)

#: Pinned to the latest capable model (Claude Opus 4.8) via its stable alias so
#: the adapter is not tied to a dated build. Overridable per instance.
DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"
DEFAULT_TIMEOUT_S = 60.0

#: The explicit empty strict-MCP config — zero MCP servers by construction, so
#: strictness never relies on the absence of the flag (Redmine #13497 j#74928).
EMPTY_MCP_CONFIG = '{"mcpServers":{}}'

_SYSTEM_PROMPT = (
    "You are the onboarding conversation UI for the `mozyo` CLI. Your ONLY job "
    "is to turn the human's words into a single closed OnboardingIntent JSON "
    "object. You have NO shell, file, network, YAML, or MCP tool authority and "
    "must not attempt to use any tool. Never invent preflight facts; use only "
    "the facts given. Reply with EXACTLY ONE JSON object and nothing else, in "
    'one of two closed shapes: {"turn":"explain","text":"..."} to ask a '
    'question or explain, or {"turn":"intent","intent":{<OnboardingIntent>}} '
    "to propose the intent. Emit no key outside the intent schema; unknown "
    "keys, unknown enum values, or any shell/file/credential-shaped value are "
    "rejected and returned to you as a structured error to correct."
)


@dataclass(frozen=True)
class RunResult:
    """The closed result of one provider subprocess call (test-injectable)."""

    returncode: int
    stdout: str
    stderr: str = ""


#: The injectable subprocess seam: ``(argv, stdin_text, timeout_s) -> RunResult``.
#: It may raise :class:`subprocess.TimeoutExpired` or :class:`FileNotFoundError`,
#: both of which the binding converts to a fail-closed provider error.
ProviderRunner = Callable[[list[str], str, float], RunResult]


def build_safe_argv(
    binary: str, model: str, system_prompt: str, turn_schema: str
) -> list[str]:
    """Construct the fixed, locked-down Claude CLI argv (pure; Redmine #13497 j#74928).

    Every safety-bearing flag is fixed here; the only variables are the binary
    path, the pinned model, the replacement system prompt, and the closed
    turn JSON Schema. No flag that could grant authority
    (``--dangerously-skip-permissions``, ``--allow-dangerously-skip-permissions``,
    ``--add-dir``, ``--allowed-tools``, ``--tools default``, a non-empty
    ``--mcp-config``) is ever emitted.
    """
    return [
        binary,
        "--print",
        "--output-format",
        "json",
        "--model",
        model,
        # Zero built-in tools — the CLI's explicit disable-all selector (NOT
        # --allowed-tools, which only governs prompting for registered tools).
        "--tools",
        "",
        # Disable all customizations: CLAUDE.md, skills, plugins, hooks, MCP,
        # custom commands / agents. The primary isolation boundary.
        "--safe-mode",
        # Disable skills / slash commands explicitly.
        "--disable-slash-commands",
        # Zero MCP by construction: strict + an explicit empty config.
        "--strict-mcp-config",
        "--mcp-config",
        EMPTY_MCP_CONFIG,
        # Constrain generation to the closed conversation-turn schema.
        "--json-schema",
        turn_schema,
        # No session is written anywhere (non-persistence).
        "--no-session-persistence",
        # Defense-in-depth over safe-mode: zero setting sources, no dynamic
        # system-prompt sections; the system prompt is fully replaced below.
        "--setting-sources",
        "",
        "--exclude-dynamic-system-prompt-sections",
        # Defense-in-depth only (never the sole guard): never execute a tool.
        "--permission-mode",
        "plan",
        "--system-prompt",
        system_prompt,
    ]


def _default_runner(argv: list[str], stdin_text: str, timeout_s: float) -> RunResult:
    # Run in a neutral temp cwd so no on-disk CLAUDE.md is auto-loaded; capture
    # output; never inherit a TTY. Kept tiny so the seam is the only live edge.
    with tempfile.TemporaryDirectory(prefix="mozyo-onboarding-conv-") as neutral_cwd:
        completed = subprocess.run(  # noqa: S603 - fixed safe argv, no shell
            argv,
            input=stdin_text,
            capture_output=True,
            text=True,
            timeout=timeout_s,
            cwd=neutral_cwd,
            check=False,
        )
    return RunResult(
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )


class SafeClaudeCliProvider:
    """A locked-down Claude local-CLI :class:`ConversationProvider` binding."""

    def __init__(
        self,
        *,
        binary: str | None = None,
        model: str = DEFAULT_CLAUDE_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        runner: ProviderRunner | None = None,
        system_prompt: str = _SYSTEM_PROMPT,
        env: Mapping[str, str] | None = None,
        resolver=resolve_claude_binary,
    ) -> None:
        # ``binary=None`` (production) resolves to a verified absolute executable
        # via ``resolver`` before any subprocess (j#74942); an explicit ``binary``
        # (a pre-resolved / test value) is used verbatim.
        self._explicit_binary = binary
        self._resolver = resolver
        self._env = env
        self._resolved_binary: str | None = None
        self._model = model
        self._timeout_s = timeout_s
        self._runner = runner or _default_runner
        self._system_prompt = system_prompt
        self._turn_schema = json.dumps(
            build_turn_json_schema(), ensure_ascii=False, sort_keys=True
        )

    def _binary(self) -> str:
        """The verified executable for ``argv[0]`` (resolved once, fail-closed)."""
        if self._explicit_binary is not None:
            return self._explicit_binary
        if self._resolved_binary is None:
            # Raises ConversationProviderError on missing/non-executable/unsafe —
            # the caller then spawns no subprocess and mutates nothing.
            self._resolved_binary = self._resolver(self._env)
        return self._resolved_binary

    def argv(self) -> list[str]:
        """The exact fixed argv this binding will invoke (for evidence / tests).

        Resolves the provider executable to a verified absolute path first, so
        ``argv[0]`` is never a bare ambient-PATH name (j#74942).
        """
        return build_safe_argv(
            self._binary(), self._model, self._system_prompt, self._turn_schema
        )

    def build_prompt(self, context: ConversationContext) -> str:
        """Build the sanitized user prompt (only redacted facts + transcript)."""
        payload = {
            "facts": context.facts.as_prompt_facts(),
            "intent_schema": dict(context.intent_schema),
            "tool_schema": [dict(t) for t in context.tool_schema],
            "transcript": [dict(m) for m in context.messages],
            "prior_errors": [dict(e) for e in context.errors],
        }
        return json.dumps(payload, ensure_ascii=False, sort_keys=True)

    def converse(self, context: ConversationContext) -> ConversationTurn:
        argv = self.argv()
        prompt = self.build_prompt(context)
        try:
            result = self._runner(argv, prompt, self._timeout_s)
        except subprocess.TimeoutExpired as exc:
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE,
                f"conversation provider timed out after {self._timeout_s}s",
            ) from exc
        except FileNotFoundError as exc:
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE,
                "conversation provider binary not found at the resolved path",
            ) from exc
        except OSError as exc:
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE,
                f"conversation provider could not be launched: {exc}",
            ) from exc

        if result.returncode != 0:
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE,
                f"conversation provider exited non-zero ({result.returncode})",
            )
        return _parse_turn(_extract_result(result.stdout))


def _extract_result(stdout: str) -> object:
    """Extract the ``result`` value from the ``--output-format json`` envelope.

    With ``--json-schema`` the CLI may place the schema-conforming turn either as
    a JSON *string* or as an already-parsed *object* in ``result``; both are
    returned as-is for :func:`_parse_turn` to coerce. Fails closed on a missing /
    non-object envelope, an error envelope, or an empty result — never guesses.
    """
    try:
        envelope = json.loads(stdout)
    except (ValueError, TypeError) as exc:
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE, "conversation provider returned non-JSON output"
        ) from exc
    if not isinstance(envelope, Mapping):
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE, "conversation provider envelope is not an object"
        )
    if envelope.get("is_error"):
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE, "conversation provider reported an error result"
        )
    if "result" not in envelope:
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE, "conversation provider envelope has no result"
        )
    return envelope["result"]


def _parse_turn(result: object) -> ConversationTurn:
    """Coerce the envelope ``result`` into exactly one closed :class:`ConversationTurn`.

    ``result`` is either a JSON string or an already-parsed object (both shapes
    the CLI may emit under ``--json-schema``). The turn's ``turn`` must be
    ``explain`` or ``intent``; anything else fails closed. The raw ``intent``
    mapping is passed through untouched so the loop's ``validate_onboarding_intent``
    remains the single fail-closed authority over the intent contents.
    """
    if isinstance(result, str):
        try:
            obj: object = json.loads(result)
        except (ValueError, TypeError) as exc:
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE, "conversation turn is not valid JSON"
            ) from exc
    else:
        obj = result
    if not isinstance(obj, Mapping):
        raise ConversationProviderError(
            PROVIDER_UNAVAILABLE, "conversation turn is not a JSON object"
        )
    kind = obj.get("turn")
    if kind == "explain":
        explain_text = obj.get("text")
        if not isinstance(explain_text, str):
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE, "explain turn has no text"
            )
        return Explain(text=explain_text)
    if kind == "intent":
        intent = obj.get("intent")
        if not isinstance(intent, Mapping):
            raise ConversationProviderError(
                PROVIDER_UNAVAILABLE, "intent turn has no intent object"
            )
        return IntentCandidate(intent=dict(intent))
    raise ConversationProviderError(
        PROVIDER_UNAVAILABLE, f"unknown conversation turn kind {kind!r}"
    )
