from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Optional

from mozyo_bridge import __version__
from mozyo_bridge.application.commands import (
    cmd_doctor,
    cmd_doctor_instruction,
    cmd_id,
    cmd_init,
    cmd_keys,
    cmd_list,
    cmd_mozyo,
    cmd_read,
    cmd_resolve,
    cmd_status,
    cmd_type,
)
from mozyo_bridge.application.instruction_doctor import (
    KNOWN_PROFILES,
    PROFILE_REDMINE_CODEX,
)
from mozyo_bridge.application import (
    cli_core,
    cli_modules,
    cli_runtime_config,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.application import (
    cli_session,
    cli_workspace,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.application import cli_agents
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application import cli_handoff
from mozyo_bridge.e_110_execution_platform.f_150_runtime_observation_event_timeline.application import cli_observability
from mozyo_bridge.e_120_operations_cockpit.f_120_cockpit_web_ui.application import cli_cockpit
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application import cli_docs_scaffold
from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application import cli_release
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_diagnostics import (
    cmd_sublane_callback_recovery,
    cmd_sublane_readiness,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.sublane_callback import (
    CALLBACK_ABSENT,
    CALLBACK_CHOICES,
)
from mozyo_bridge.application.cli_common import add_repo_option
from mozyo_bridge.e_140_adapter_provider.f_140_presentation_provider.application.presentation_runtime import (
    PresentationRuntimeError,
    resolve_presentation_provider,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.provider_runtime import resolve_builtin_providers
from mozyo_bridge.application.repo_local_config_loader import (
    CONFIG_FILE_RELPATH,
    load_repo_local_config,
)
from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry import ModuleRegistryError
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.provider_registry import ProviderRegistryError
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.shared.paths import (
    default_queue_path,
    default_tmux_conf,
    find_repo_root,
    resolve_repo_root,
    workspace_adoption_marker,
)

# --- Backward-compatible import surface (Redmine #12138 / #12141 / #12153). ---
# Before the parser split, handler / helper / constant symbols were importable
# as ``mozyo_bridge.application.cli.<name>`` because ``cli.py`` imported them
# directly for the monolithic ``build_parser()``. The parser *registration* now
# lives in the family modules (``cli_agents`` / ``cli_cockpit`` / ``cli_handoff``
# / ``cli_observability`` / ``cli_runtime_config`` / ``cli_session`` plus the
# earlier ``cli_release`` / ``cli_docs_scaffold`` / ``cli_workspace``), but the
# module-level import path is preserved here so downstream imports / monkeypatch
# targets that referenced them through ``application.cli`` keep working. This is
# the #12138 scope guard "do not retire legacy import paths" applied to
# ``cli.py``; it does not affect parser behavior.
from mozyo_bridge.application.cli_common import add_scaffold_target_option  # noqa: F401,E402
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.application.cli_handoff import (  # noqa: F401,E402
    add_legacy_notify_options,
    add_notify_delivery_options,
    add_notify_options,
)
from mozyo_bridge.application.cli_runtime_config import (  # noqa: F401,E402
    _add_runtime_config_check_parser,
    _add_runtime_config_install_parser,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import AGENT_KINDS  # noqa: F401,E402
from mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.domain.handoff import (  # noqa: F401,E402
    KIND_LABELS,
    MODE_QUEUE_ENTER,
    MODES,
    RECORD_FORMAT_BOTH,
    RECORD_FORMATS,
    SOURCES,
)
from mozyo_bridge.e_110_execution_platform.f_110_workspace_session_identity.domain.session_boundary import SESSION_BOUNDARY_SIGNALS  # noqa: F401,E402
from mozyo_bridge.application.commands import (  # noqa: F401,E402
    cmd_agents_attention_project,
    cmd_agents_list,
    cmd_agents_targets,
    cmd_cockpit,
    cmd_config,
    cmd_docs_audit_impact,
    cmd_docs_generate,
    cmd_docs_resolve,
    cmd_docs_validate,
    cmd_events_query,
    cmd_events_tail,
    cmd_handoff_cross_workspace_consult,
    cmd_handoff_reply,
    cmd_handoff_send,
    cmd_instruction_doctor,
    cmd_instruction_install,
    cmd_layout_apply,
    cmd_message,
    cmd_notify_claude,
    cmd_notify_claude_legacy_task,
    cmd_notify_claude_review_result,
    cmd_notify_codex,
    cmd_notify_codex_legacy_task,
    cmd_notify_codex_review,
    cmd_otel_activity,
    cmd_otel_events,
    cmd_otel_launchd,
    cmd_otel_serve,
    cmd_otel_status,
    cmd_rules_home,
    cmd_rules_install,
    cmd_rules_status,
    cmd_scaffold_apply,
    cmd_scaffold_canonical,
    cmd_scaffold_diff,
    cmd_scaffold_status,
    cmd_session_boundary_prompt,
    cmd_session_list,
    cmd_session_name,
    cmd_session_pane_decision,
    cmd_session_vscode_settings,
    cmd_tmux_ui_install,
    cmd_tmux_ui_status,
    cmd_tmux_ui_uninstall,
    cmd_workspace_defaults,
    cmd_workspace_inspect,
    cmd_workspace_list,
    cmd_workspace_register,
)
from mozyo_bridge.e_130_governance_distribution.f_160_release_version_governance.application.release import (  # noqa: F401,E402
    cmd_release_bump,
    cmd_release_check_artifact,
    cmd_release_check_drift,
    cmd_release_check_scaffold,
    cmd_release_check_tree,
    cmd_release_check_workflow,
    cmd_release_publish,
    cmd_release_workflow_runs,
    cmd_release_workflow_wait,
)


def repo_root_from_args(args: argparse.Namespace):
    return resolve_repo_root(getattr(args, "repo", None))


def normalize_paths(args: argparse.Namespace) -> argparse.Namespace:
    repo_root = repo_root_from_args(args)
    if hasattr(args, "cwd") and args.cwd is None:
        args.cwd = str(repo_root)
    if hasattr(args, "config_path"):
        args.config_path_was_default = args.config_path is None
        if args.config_path is None:
            args.config_path = str(default_tmux_conf(repo_root))
    if hasattr(args, "queue") and args.queue is None:
        args.queue = str(default_queue_path(repo_root))
    return args


# `_add_doctor_diagnostic_options` moved into ``cli_core`` with the doctor /
# sublane lifecycle block (Redmine #12155); re-exported here so the legacy
# ``application.cli._add_doctor_diagnostic_options`` import / monkeypatch path
# keeps working (same #12138 scope guard as the symbols above).
from mozyo_bridge.application.cli_core import (  # noqa: F401,E402
    _add_doctor_diagnostic_options,
)


def build_parser(config: Optional[RepoLocalConfig] = None) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mozyo-bridge",
        description=(
            "Repo-aware tmux session bootstrap plus Asana/Redmine-gated pane "
            "notification bridge for ClaudeCode/Codex terminals. "
            "Run with no subcommand to ensure a repo-scoped session with "
            "claude/codex windows and attach."
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument(
        "--no-attach",
        action="store_true",
        default=False,
        dest="no_attach",
        help="Bare `mozyo`: ensure the repo session and agent windows but do not attach. Ignored when a subcommand is given.",
    )
    parser.add_argument(
        "--cc",
        action="store_true",
        default=False,
        dest="cc",
        help=(
            "Bare `mozyo`: attach via iTerm2 control mode (`tmux -CC attach`) "
            "instead of a plain `tmux attach`, so iTerm2 manages tmux windows "
            "as native windows/panes. Ensure behavior is unchanged. "
            "`--no-attach` and `--json` both win: they ensure only and never "
            "exec, so the printed/JSON attach command just reflects the `-CC` "
            "variant. Ignored when a subcommand is given."
        ),
    )
    parser.add_argument(
        "--repo",
        default=None,
        help=(
            "Bare `mozyo`: override the repo root resolution (otherwise MOZYO_REPO env "
            "or a `.git` / `.tmux.conf` / `pyproject.toml` parent of the cwd). "
            "Subcommands accept their own `--repo` after the subcommand name."
        ),
    )
    parser.add_argument(
        "--session",
        default=None,
        help=(
            "Bare `mozyo`: override the tmux session name. Defaults to the "
            "derived collision-safe name (`mozyo-bridge session name`): the "
            "workspace-defaults Redmine identifier when present, else a "
            "hash-suffixed repo-path name. Pass an explicit name to override."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        default=False,
        dest="json_output",
        help=(
            "Bare `mozyo`: emit machine-readable JSON describing the resolved "
            "session, current windows, and a `ready` flag (claude/codex windows "
            "present) instead of the human table. Implies no attach so a launcher "
            "capturing stdout is never replaced by `tmux attach`. Ignored when a "
            "subcommand is given."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=False)

    # Compose the top-level subparsers from the internal built-in CLI module
    # registry (Redmine #12155), honoring the repo-local YAML config's CLI
    # family selection when one is supplied (Redmine #12191). ``config is None``
    # — the default, and every existing direct ``build_parser()`` caller — keeps
    # the full composition, so a missing/absent config never changes the default
    # ``mozyo-bridge`` help/subcommand tree. A supplied config may disable only
    # non-mandatory families; the registry forbids disabling a core /
    # authority-bearing family, failing closed in ``main`` with actionable text.
    cli_modules.compose_parser(sub, config.cli if config is not None else None)
    return parser


def _warn_deprecated_alias(args: argparse.Namespace) -> None:
    """Emit a stderr migration warning when a deprecated command alias is used.

    The warning goes to stderr only, so JSON output on stdout stays additive /
    unbroken for existing `jq` consumers (Redmine #11051 / #53306).
    """
    alias = getattr(args, "deprecated_alias", None)
    if not alias:
        return
    canonical = getattr(args, "canonical_command", None) or "the renamed command"
    print(
        f"deprecated: `{alias}` is a deprecated alias; use `{canonical}` instead "
        "(the alias is a removal candidate next minor).",
        file=sys.stderr,
    )


def _root_repo_override(argv: Optional[list[str]] = None) -> Optional[str]:
    """Extract the root-level ``--repo`` value before full parser composition.

    Composition (which CLI families exist) must be decided before argparse can
    parse the real arguments, but the repo-local config that drives composition
    lives under the repo root that the documented root-level ``--repo`` may
    override (see the ``--repo`` help on the top-level parser). So the config
    source has to honor the same ``--repo`` override, which means reading it
    *before* the real parse.

    A tiny ``add_help=False`` pre-parser mirrors the root-level options and
    slurps everything from the first positional (the subcommand) onward into an
    ``argparse.REMAINDER`` tail. That makes it read only the *root-level*
    ``--repo`` — a subcommand-local ``--repo`` (which applies to that command,
    not to which families compose) lands in the tail and is ignored here,
    matching exactly how the real parser binds the root ``--repo`` before the
    subparsers. The mirrored root options exist only so their values are not
    mistaken for the first positional; this list must track the root options on
    :func:`build_parser`. An absent ``--repo`` yields ``None`` -> the cwd /
    ``MOZYO_REPO`` default, so config-absent default behavior is unchanged.
    """
    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--repo", default=None)
    pre.add_argument("--session", default=None)
    pre.add_argument("--no-attach", action="store_true")
    pre.add_argument("--cc", action="store_true")
    pre.add_argument("--json", action="store_true", dest="json_output")
    pre.add_argument("rest", nargs=argparse.REMAINDER)
    known, _ = pre.parse_known_args(argv)
    return known.repo


def _exit_on_repo_local_config_error(exc: Exception) -> int:
    """Fail closed on a broken repo-local config with actionable error text.

    A present-but-invalid ``.mozyo-bridge/config.yaml`` — a malformed YAML
    document, an unreadable present file, a schema violation, or a CLI family
    selection the registry rejects (unknown or mandatory family) — must never be
    silently ignored: that would let a misconfigured repo run a different CLI
    than its config asks for. Instead the whole invocation fails closed with one
    actionable line (what went wrong, where the file is, and that removing it
    restores the default), and no raw parser / registry traceback ever reaches
    the user. Returns the conventional ``2`` CLI usage/error exit code, matching
    argparse's own error exit.
    """
    print(
        f"mozyo-bridge: invalid repo-local config ({CONFIG_FILE_RELPATH}): {exc}\n"
        f"Fix the file or remove it to use the default CLI "
        f"(a missing config is the behavior-preserving default).",
        file=sys.stderr,
    )
    return 2


def _select_bare_target_root(argv: Optional[list[str]]) -> Path:
    """Select the bare-`mozyo` target root (Redmine #13497 j#74936).

    Honors, in order: an explicit root-level `--repo`; the trusted `MOZYO_REPO`
    environment override; without an override, an **adopted** ancestor of the cwd
    (so launching from a subdirectory of an adopted project still launches it);
    otherwise the canonical current directory itself as the fresh onboarding
    target — never silently adopting an incidental *unadopted* ancestor (e.g. a
    plain Git root or a stray marker in ``$HOME``). The same root is used for
    config load/reload, adoption classification, inspect/plan/apply, and launch.
    """
    override = _root_repo_override(argv)
    if override:
        return Path(override).expanduser().resolve()
    env_repo = os.environ.get("MOZYO_REPO")
    if env_repo:
        return Path(env_repo).expanduser().resolve()
    candidate = find_repo_root()
    if workspace_adoption_marker(candidate) is not None:
        return candidate
    return Path.cwd().resolve()


def _backend_aware_launch(args: argparse.Namespace, target_root: Path) -> int:
    """Reload ``target_root``'s config *now* and take its backend-aware launch.

    The bare-`mozyo` backend selection (Redmine #13324) reads only the resolved
    repo's `terminal_transport.backend`: `herdr` runs the single-command herdr
    session-start + UI attach, otherwise the byte-invariant tmux cockpit path.
    This re-resolves the config **at invocation** from the selected root rather
    than closing over the config loaded at `main()` start: after a fresh
    onboarding writes the typed `terminal_transport.backend: herdr`, a stale
    pre-adoption closure would wrongly take the tmux path (Redmine #13497
    j#74934). A broken config fails closed with the same actionable text.
    """
    try:
        fresh = load_repo_local_config(str(target_root))
    except (
        RepoLocalConfigError,
        ModuleRegistryError,
        ProviderRegistryError,
        PresentationRuntimeError,
    ) as exc:
        return _exit_on_repo_local_config_error(exc)
    if fresh.terminal_transport.herdr_enabled:
        from mozyo_bridge.application.herdr_launch_command import cmd_mozyo_herdr

        return cmd_mozyo_herdr(args)
    return cmd_mozyo(args)


def _bare_mozyo_entry(args: argparse.Namespace, argv: Optional[list[str]]) -> int:
    """Bare `mozyo` (no subcommand): route through the onboarding entry gate.

    An already-adopted project launches unchanged; an in-progress adoption
    resumes; a fresh root runs the provider-neutral conversation → visible plan →
    human confirmation → deterministic apply, then reaches the same backend-aware
    launch. All authority-sensitive mutation stays in the #13498 tool surface
    (Redmine #13497). The launch is the shared reload-at-invocation callback so
    the newly written herdr config is honored, bound to the same selected root.
    """
    from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.bare_entry import (
        run_bare_entry,
    )
    from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.commands_onboarding import (
        GATE_SECRET_ENV,
    )
    from mozyo_bridge.e_110_execution_platform.f_170_conversational_onboarding.application.onboarding_providers import (
        SafeClaudeCliProvider,
    )

    target_root = _select_bare_target_root(argv)
    return run_bare_entry(
        target_root=target_root,
        launch_adopted=lambda: _backend_aware_launch(args, target_root),
        provider=SafeClaudeCliProvider(),
        gate_secret=os.environ.get(GATE_SECRET_ENV),
        json_output=bool(getattr(args, "json_output", False)),
    )


def main(argv: Optional[list[str]] = None) -> int:
    # Read the repo-local YAML config (Redmine #12190 loader) and compose the
    # parser from it (Redmine #12191). The config is loaded from the same repo
    # root the root-level ``--repo`` selects (resolved before composition by
    # ``_root_repo_override``), so an explicit ``--repo <target>`` reads
    # ``<target>/.mozyo-bridge/config.yaml``, preserving the documented
    # ``--repo`` override contract (review j#60857). A missing/empty config
    # resolves to the behavior-preserving default, so the default CLI is
    # unchanged; a present but broken config (parse / schema / family-resolution
    # failure) fails closed here with actionable text instead of a traceback.
    try:
        config = load_repo_local_config(_root_repo_override(argv))
        parser = build_parser(config)
        # Connect the repo-local provider selection to runtime resolution
        # (Redmine #12249): resolve ``config.providers`` against the live
        # built-in provider registry so a present-but-invalid selection — an
        # unknown provider id, an unknown category, or a category/provider
        # mismatch — fails closed at the entrypoint, exactly as the CLI family
        # selection is resolved during ``build_parser``. The default (no
        # selection) resolves every populated category to its current built-in
        # default, so a missing/default config is behavior-preserving; no
        # provider dispatch path consumes the resolved mapping yet, so this is
        # the fail-closed validation seam and adds no dynamic import or ABI.
        resolve_builtin_providers(config.providers)
        # Connect the repo-local presentation-surface selection to runtime
        # resolution (Redmine #12251): resolve ``config.presentation`` to the
        # built-in projection provider that owns the selected surface, so a
        # present-but-unrealizable selection — a core-recognized surface with no
        # built-in provider — fails closed here, exactly as the provider
        # selection does above. The default surface (``tmux_user_option``)
        # resolves to the tmux provider, so a missing/default config is
        # behavior-preserving; no projection dispatch path consumes the resolved
        # provider yet, so this is the fail-closed validation seam and adds no
        # dynamic import, public ABI, or projection authority. Unknown / target /
        # credential-shaped surfaces already fail at PresentationSelectionConfig
        # construction (surfaced as RepoLocalConfigError).
        resolve_presentation_provider(config.presentation)
    except (
        RepoLocalConfigError,
        ModuleRegistryError,
        ProviderRegistryError,
        PresentationRuntimeError,
    ) as exc:
        return _exit_on_repo_local_config_error(exc)
    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        return _bare_mozyo_entry(args, argv)
    args = normalize_paths(args)
    _warn_deprecated_alias(args)
    return args.func(args)
