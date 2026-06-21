"""CLI parser registration for the rules / scaffold / docs command families.

Split out of ``application/cli.py`` (Redmine #12141) so the top-level
``build_parser`` delegates these low-risk families instead of inlining their
registration. Behavior-preserving: the parser wiring, help text, choices,
defaults, and ``func`` bindings are unchanged.
"""
from __future__ import annotations

import argparse

from mozyo_bridge.application.cli_common import (
    add_repo_option,
    add_scaffold_target_option,
)
from mozyo_bridge.application.commands import (
    cmd_docs_audit_impact,
    cmd_docs_generate,
    cmd_docs_resolve,
    cmd_docs_validate,
    cmd_rules_home,
    cmd_rules_install,
    cmd_rules_status,
    cmd_scaffold_apply,
    cmd_scaffold_canonical,
    cmd_scaffold_diff,
    cmd_scaffold_status,
)


def register(sub) -> None:
    """Register the `rules`, `scaffold`, and `docs` subcommands onto ``sub``."""
    rules = sub.add_parser("rules")
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_install = rules_sub.add_parser("install")
    rules_install_store = rules_install.add_mutually_exclusive_group()
    rules_install_store.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    rules_install_store.add_argument(
        "--repo-local",
        dest="repo_local",
        metavar="REPO",
        help=(
            "Install central preset rules into REPO/.mozyo-bridge/rules/presets/ "
            "instead of the user home. Use this for Dev Container / "
            "ephemeral-home workspaces where ~/.mozyo_bridge is not persisted. "
            "Mutually exclusive with --home."
        ),
    )
    rules_install.set_defaults(func=cmd_rules_install)
    rules_status = rules_sub.add_parser("status")
    rules_status_store = rules_status.add_mutually_exclusive_group()
    rules_status_store.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    rules_status_store.add_argument(
        "--repo-local",
        dest="repo_local",
        metavar="REPO",
        help=(
            "Read the rules store from REPO/.mozyo-bridge instead of the user "
            "home. Mutually exclusive with --home."
        ),
    )
    rules_status.set_defaults(func=cmd_rules_status)
    rules_home_help = (
        "Print the mozyo-bridge home root. Default output is the portable "
        "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}` expression safe to paste "
        "into committed docs. Use --resolved to expand the env override "
        "and `~` for local diagnostics; that output may contain the "
        "operator's $HOME and must not be committed."
    )
    rules_home = rules_sub.add_parser(
        "home",
        help=rules_home_help,
        description=rules_home_help,
    )
    rules_home.add_argument(
        "--resolved",
        action="store_true",
        help=(
            "Print the resolved absolute path honoring MOZYO_BRIDGE_HOME "
            "and expanding `~`. Intended for local debugging only; do not "
            "paste the output into committed documents."
        ),
    )
    rules_home.set_defaults(func=cmd_rules_home)

    scaffold = sub.add_parser(
        "scaffold",
        help=(
            "Generate, inspect, and audit the project routers + manifest for "
            "a ticket-system preset. Use `apply` to write, `diff` to preview, "
            "and `status` to detect drift."
        ),
    )
    scaffold_sub = scaffold.add_subparsers(dest="scaffold_command", required=True)
    from mozyo_bridge.scaffold.rules import PRESETS

    scaffold_apply = scaffold_sub.add_parser(
        "apply",
        help=(
            "Write `AGENTS.md`, `CLAUDE.md`, and the scaffold manifest for "
            "the chosen preset into the target workspace. Use `scaffold diff "
            "<preset>` first to preview the change."
        ),
    )
    scaffold_apply.add_argument("preset", choices=PRESETS)
    add_scaffold_target_option(scaffold_apply)
    apply_store_group = scaffold_apply.add_mutually_exclusive_group()
    apply_store_group.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    apply_store_group.add_argument(
        "--repo-local",
        dest="repo_local",
        action="store_true",
        help=(
            "Read the rules store from the target repo's `.mozyo-bridge/` "
            "directory and embed a repo-local `rule_path` in the generated "
            "routers and manifest. Use this for Dev Container / "
            "ephemeral-home workspaces. Run `mozyo-bridge rules install "
            "--repo-local <target>` first to populate that store. Mutually "
            "exclusive with --home."
        ),
    )
    scaffold_apply.add_argument("--dry-run", action="store_true")
    apply_replace_group = scaffold_apply.add_mutually_exclusive_group()
    apply_replace_group.add_argument("--backup", action="store_true", help="Back up existing scaffold files before replacing them")
    apply_replace_group.add_argument("--force", action="store_true", help="Replace existing scaffold files without backup")
    scaffold_apply.add_argument(
        "--skip-tmux-ui",
        dest="skip_tmux_ui",
        action="store_true",
        help=(
            "Omit the governed preset's `.mozyo-bridge/tmux/` artifacts "
            "(agent-window status colouring snippet). The artifacts are "
            "default-on; pass this flag when the project does not want "
            "the tmux UI helper installed."
        ),
    )
    scaffold_apply.add_argument(
        "--skip-nagger",
        dest="skip_nagger",
        action="store_true",
        help=(
            "Omit the governed preset's `.claude-nagger/` artifacts "
            "(Claude Nagger config / convention skeletons). The artifacts "
            "are default-on; pass this flag when the project does not "
            "use Claude Nagger."
        ),
    )
    scaffold_apply.add_argument(
        "--with-worktree-runbook",
        dest="with_worktree_runbook",
        action="store_true",
        help=(
            "Install the governed preset's sublane / git worktree runbook "
            "docs under `vibes/docs/logics/` plus a manual catalog-"
            "registration note. These artifacts are opt-in (default-off); "
            "without this flag they are not written. The scaffold never "
            "mutates the operator-owned `catalog.yaml`; use the shipped note "
            "to register the docs by hand."
        ),
    )
    scaffold_apply.add_argument(
        "--with-sublane-flow",
        dest="with_sublane_flow",
        action="store_true",
        help=(
            "Activate the sublane development flow as a runtime-active "
            "reference. Installs the portable profile doc under "
            "`vibes/docs/profiles/` AND adds a thin sublane read-route "
            "section to the generated `AGENTS.md` / `CLAUDE.md`. Opt-in "
            "(default-off): without this flag the routers carry no sublane "
            "route and the doc is not written. Private operator policy "
            "(lane count, cockpit composition, paths, session naming) is "
            "never shipped; the scaffold never mutates `catalog.yaml`."
        ),
    )
    scaffold_apply.set_defaults(func=cmd_scaffold_apply)

    scaffold_diff = scaffold_sub.add_parser(
        "diff",
        help=(
            "Print a unified diff of what `scaffold apply <preset>` would "
            "change in the target workspace. Exit 0 when clean, exit 1 when "
            "the workspace would change."
        ),
    )
    scaffold_diff.add_argument("preset", choices=PRESETS)
    add_scaffold_target_option(scaffold_diff)
    diff_store_group = scaffold_diff.add_mutually_exclusive_group()
    diff_store_group.add_argument(
        "--home",
        help="mozyo-bridge home. Defaults to MOZYO_BRIDGE_HOME or ~/.mozyo_bridge",
    )
    diff_store_group.add_argument(
        "--repo-local",
        dest="repo_local",
        action="store_true",
        help=(
            "Preview against the target repo's `.mozyo-bridge/` rules store "
            "and embed a repo-local `rule_path`. Mutually exclusive with --home."
        ),
    )
    scaffold_diff.add_argument(
        "--skip-tmux-ui",
        dest="skip_tmux_ui",
        action="store_true",
        help="Preview the diff as if `scaffold apply --skip-tmux-ui` were run.",
    )
    scaffold_diff.add_argument(
        "--skip-nagger",
        dest="skip_nagger",
        action="store_true",
        help="Preview the diff as if `scaffold apply --skip-nagger` were run.",
    )
    scaffold_diff.add_argument(
        "--with-worktree-runbook",
        dest="with_worktree_runbook",
        action="store_true",
        help=(
            "Preview the diff as if `scaffold apply --with-worktree-runbook` "
            "were run (include the opt-in worktree/sublane runbook docs)."
        ),
    )
    scaffold_diff.add_argument(
        "--with-sublane-flow",
        dest="with_sublane_flow",
        action="store_true",
        help=(
            "Preview the diff as if `scaffold apply --with-sublane-flow` were "
            "run (include the opt-in sublane profile doc and the router "
            "sublane read-route section)."
        ),
    )
    scaffold_diff.set_defaults(func=cmd_scaffold_diff)

    scaffold_canonical = scaffold_sub.add_parser(
        "canonical",
        help=(
            "Render or drift-check the canonical-sourced router templates. "
            "Operates on the mozyo-bridge source tree (`--repo`, default cwd); "
            "use `render` to regenerate `_router/AGENTS.md` and `_router/CLAUDE.md` "
            "from `scaffold/canonical_sources/router.yaml`, or `--check` to "
            "verify the committed outputs match (exit 1 on drift)."
        ),
    )
    add_repo_option(scaffold_canonical)
    scaffold_canonical.add_argument(
        "--check",
        action="store_true",
        help=(
            "Re-render every canonical source in memory and compare against "
            "the committed output. Exit 1 on drift; writes nothing."
        ),
    )
    scaffold_canonical.set_defaults(func=cmd_scaffold_canonical)

    scaffold_status = scaffold_sub.add_parser("status")
    add_scaffold_target_option(scaffold_status)
    scaffold_status.add_argument(
        "--home",
        help=(
            "mozyo-bridge home for central-mode manifests. Defaults to "
            "MOZYO_BRIDGE_HOME or ~/.mozyo_bridge. Rejected against "
            "repo-local manifests (the rules store is the target repo's "
            ".mozyo-bridge); rerun without --home."
        ),
    )
    scaffold_status.add_argument("--json", action="store_true", help="Emit structured JSON output instead of human-readable text")
    scaffold_status.set_defaults(func=cmd_scaffold_status)

    docs = sub.add_parser(
        "docs",
        help=(
            "Docs catalog tooling for governed scaffolds. Replaces the "
            "Python source previously vendor-copied to the target repo "
            "under `.mozyo-bridge/tools/`; the same logic now ships in "
            "the mozyo-bridge package so upgrades follow the CLI."
        ),
    )
    docs_sub = docs.add_subparsers(dest="docs_command", required=True)

    def _add_docs_common(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--repo",
            help=(
                "Target project root. Defaults to the cwd. The catalog is "
                "resolved relative to this root."
            ),
        )
        parser.add_argument(
            "--catalog",
            help=(
                "Catalog YAML path. Defaults to "
                "`<repo>/.mozyo-bridge/docs/catalog.yaml`."
            ),
        )
        parser.add_argument(
            "--overlay",
            help=(
                "Local-only overlay YAML path (Redmine #11819). Defaults to a "
                "git-ignored `catalog.local.yaml` next to the catalog. Present "
                "only in checkouts that keep local-only docs; absent on fresh "
                "clone / CI / PyPI consumer."
            ),
        )

    docs_validate = docs_sub.add_parser(
        "validate",
        help="Validate the docs catalog (structure, refs, canonical paths, coverage roots).",
    )
    _add_docs_common(docs_validate)
    docs_validate.add_argument(
        "--strict-metadata",
        action="store_true",
        help="Require purpose / audit_role / related_document_refs on active rule/spec/task documents.",
    )
    docs_validate.add_argument(
        "--check-file-coverage",
        action="store_true",
        help="Require source files under coverage roots to match at least one file_convention.",
    )
    docs_validate.add_argument(
        "--coverage-root",
        action="append",
        default=None,
        help=(
            "Override the catalog / default coverage roots. Repeatable. "
            "CLI takes precedence over the catalog's `coverage_roots`."
        ),
    )
    docs_validate.add_argument(
        "--include-local",
        dest="include_local",
        action="store_true",
        help=(
            "Also validate the local-only overlay (catalog.local.yaml) when "
            "present: structure, secret-shaped-value guard, and id collisions. "
            "No-op on a fresh clone / CI where the overlay is absent."
        ),
    )
    docs_validate.set_defaults(func=cmd_docs_validate)

    docs_resolve = docs_sub.add_parser(
        "resolve",
        help="Resolve active docs for one or more changed paths.",
    )
    _add_docs_common(docs_resolve)
    docs_resolve.add_argument(
        "paths",
        nargs="+",
        help="Repository-relative or absolute file paths to resolve.",
    )
    docs_resolve.add_argument(
        "--format",
        choices=("text", "markdown", "json"),
        default="text",
        help="Output format (default: text).",
    )
    docs_resolve.add_argument(
        "--no-local",
        dest="no_local",
        action="store_true",
        help=(
            "Ignore the local-only overlay (catalog.local.yaml) and resolve "
            "against the public catalog only — the view a fresh clone / CI "
            "sees. By default the overlay is merged when present."
        ),
    )
    docs_resolve.set_defaults(func=cmd_docs_resolve)

    docs_generate = docs_sub.add_parser(
        "generate-file-conventions",
        help="Render the catalog's file_conventions to a generated YAML.",
    )
    _add_docs_common(docs_generate)
    docs_generate.add_argument(
        "--output",
        help=(
            "Generated YAML path. Defaults to "
            "`<repo>/.mozyo-bridge/docs/file_conventions.generated.yaml`."
        ),
    )
    docs_generate.add_argument(
        "--check",
        action="store_true",
        help="Verify the recorded output matches the catalog; exit 1 on drift.",
    )
    docs_generate.set_defaults(func=cmd_docs_generate)

    docs_impact = docs_sub.add_parser(
        "audit-impact",
        help="Resolve docs for git-changed paths and optionally drift-check the generated file.",
    )
    _add_docs_common(docs_impact)
    impact_scope = docs_impact.add_mutually_exclusive_group()
    impact_scope.add_argument("--staged", action="store_true", help="Use staged changes only.")
    impact_scope.add_argument(
        "--all-changed",
        dest="all_changed",
        action="store_true",
        help="Use staged + unstaged + untracked changes.",
    )
    docs_impact.add_argument(
        "--check-generated",
        dest="check_generated",
        action="store_true",
        help="Also run the generate-file-conventions drift check.",
    )
    docs_impact.add_argument(
        "--generated-output",
        dest="generated_output",
        help=(
            "Override the generated file path for --check-generated. "
            "Defaults to the same path as `docs generate-file-conventions`."
        ),
    )
    docs_impact.add_argument(
        "--no-local",
        dest="no_local",
        action="store_true",
        help=(
            "Ignore the local-only overlay (catalog.local.yaml) and resolve "
            "against the public catalog only. By default the overlay is merged "
            "when present."
        ),
    )
    docs_impact.set_defaults(func=cmd_docs_audit_impact)
