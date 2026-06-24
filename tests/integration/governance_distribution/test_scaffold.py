"""Scaffold preset / rules / status / diff tests (Redmine #12140, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of the scaffold-family test classes out of the
monolithic test spine, per #12138 first-wave split and
vibes/docs/logics/refactor-split-strategy.md Priority 2. No test logic changed."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser
from mozyo_bridge.scaffold.rules import (
    package_version,
    rules_status,
    scaffold_state,
)

class ScaffoldRulesTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_rules_install_and_scaffold_asana_thin_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            asana_workflow = home / "rules" / "presets" / "asana" / "agent-workflow.md"
            self.assertTrue(asana_workflow.exists())
            installed_workflow = asana_workflow.read_text(encoding="utf-8")
            self.assertIn("User Interaction And Escalation", installed_workflow)
            self.assertIn("designated coordinator", installed_workflow)
            self.assertIn("Role Boundaries", installed_workflow)
            self.assertIn("coordinating/auditing agent must not directly implement", installed_workflow)
            # Asana-native guardrails added in this task.
            for marker in (
                "Factual Posture",
                "Prioritize factual correctness",
                "review input, not completion",
                "Handoff Startup Decision",
                "Receiver pane unavailable",
                "Notification fails",
                "mozyo-bridge init",
                "Receive method id",
                "Asana API",
                "Scope Preservation",
                "residual scope",
                "Decision Routing",
                # Ticket-ID entrypoint runtime reflection.
                "Ticket-ID Entrypoint",
                'ticket-ID only',
                "pane / chat body looks fully framed",
                "task comment / story id",
                # Audit-owned commit authority codified in this task.
                "Audit-Owned Commit Authority",
                "commit authority, not an implementation authority",
                "Refs: Asana task <task_id>",
                "Audit: Asana comment <comment_id>",
                "git diff --cached --stat",
                "git add -A",
                "commit-hash comment",
                # Chat-surface boundary added to reduce noisy chat output for
                # un-notified / pending-operator-action handoffs.
                "Chat surface boundary",
                "Chat output is a notification only",
                "Do not duplicate the comment body in chat",
            ):
                self.assertIn(marker, installed_workflow)
            # Asana central preset must NOT import Redmine journal / gate semantics.
            for forbidden in (
                "Redmine journal",
                "Review Gate",
                "Implementation Done Gate",
                "Close Gate",
                "Design Consultation Gate",
            ):
                self.assertNotIn(forbidden, installed_workflow)
            # The central preset is the scaffold for arbitrary downstream
            # projects; team-specific tools (Notion in this team's flow) must
            # not leak into the generated guidance.
            self.assertNotIn("Notion", installed_workflow)
            self.assertIn(
                "Do not ask the user directly when the task, project notes, or repository docs",
                installed_workflow,
            )
            self.assertIn(
                "Do not store credentials, tokens, personal data, or private internal URLs",
                installed_workflow,
            )

            result, output = self.run_cli(["scaffold", "apply", "asana", "--target", str(project), "--home", str(home)])

            self.assertEqual(0, result)
            self.assertIn("AGENTS.md", output)
            self.assertTrue((project / "AGENTS.md").exists())
            self.assertTrue((project / "CLAUDE.md").exists())
            self.assertFalse((project / "vibes" / "docs" / "rules" / "asana-agent-workflow.md").exists())
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                agents,
            )
            # The router must not leak the host-resolved home path or any
            # user-specific absolute home path.
            self.assertNotIn(str(home), agents)
            self.assertNotIn("/Users/", agents)
            self.assertIn("active な `Asana task / comment`", agents)
            self.assertIn("router に本文を複製しない", agents)
            # Generated routers must not name vibes/docs/* paths as runtime context.
            # vibes/docs/ is this repo's design/spec source, not an external scaffold
            # target's runtime convention.
            self.assertNotIn("vibes/docs/specs/project-map.md", agents)
            self.assertNotIn("vibes/docs/rules/agent-workflow.md", agents)
            self.assertNotIn("vibes/docs/", agents)
            # The Project-Local Context heading was folded into step 3.
            self.assertNotIn("## Project-Local Context", agents)
            self.assertIn("target project 側の任意の convention", agents)
            self.assertIn("mozyo-bridge の runtime 必須参照ではない", agents)

            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                claude,
            )
            self.assertNotIn(str(home), claude)
            self.assertNotIn("/Users/", claude)
            self.assertIn("ClaudeCode 起動時の最小 reminder", claude)
            self.assertIn("迎合せず", claude)
            self.assertIn("implementation done / implementation_done は completion ではない", claude)
            self.assertIn("Asana task / comment", claude)
            self.assertIn("受領方法", claude)
            # Chat surface stays thin: durable receive method lives in the task
            # comment, chat reports stay to a state + task-id pointer.
            self.assertIn("最小ポインタ", claude)
            self.assertIn("chat に貼り直さない", claude)
            # CLAUDE.md stays thin even with the Claude-specific reminder
            # block AND the Project-Local Additions marker block + boilerplate
            # (~9 lines) shipped from the router template.
            self.assertLess(len(claude.splitlines()), 50)
            # Asana CLAUDE.md must not import Redmine-specific vocabulary.
            for forbidden in ("Redmine journal", "Review Gate", "Implementation Done Gate"):
                self.assertNotIn(forbidden, claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual("asana", state["preset"])
            # Schema v2 + preset_hash from the previous task must still be in effect.
            self.assertEqual(2, state["schema_version"])
            self.assertEqual(
                hashlib.sha256(asana_workflow.read_bytes()).hexdigest(),
                state["preset_hash"],
            )
            self.assertEqual("2026.05.17.2", state["preset_version"])
            self.assertIn("AGENTS.md", state["files"])

            # The audit-owned commit policy belongs in the central preset only.
            # Root routers stay thin and must not duplicate the policy body.
            self.assertNotIn("Audit-Owned Commit Authority", agents)
            self.assertNotIn("Audit-Owned Commit Authority", claude)
            self.assertNotIn("Refs: Asana task", agents)
            self.assertNotIn("Refs: Asana task", claude)

            # Tool-specific router split: the rendered routers must not import
            # each other. CLAUDE.md previously imported AGENTS.md via the
            # Claude Code `@AGENTS.md` file-import directive; the split makes
            # each tool's entry standalone so the central preset path and the
            # active ticket anchor are reachable without touching the peer
            # file. This rendered-output assertion catches a future template
            # regression that template-level tests would miss if the import
            # ever leaked through substitution.
            self.assertNotIn("@AGENTS.md", claude)
            self.assertNotIn("@CLAUDE.md", agents)
            self.assertIn("tool-specific", agents)
            self.assertIn("tool-specific", claude)
            self.assertIn("import しない", agents)
            self.assertIn("import しない", claude)

    def test_rules_install_and_scaffold_redmine_thin_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            redmine_workflow = home / "rules" / "presets" / "redmine" / "agent-workflow.md"
            self.assertTrue(redmine_workflow.exists())
            installed = redmine_workflow.read_text(encoding="utf-8")
            for marker in (
                "Redmine Gate Lifecycle",
                "Start Gate",
                "Progress Log Gate",
                "Design Consultation Gate",
                "Design Consultation Answer Gate",
                "Implementation Done Gate",
                "Review Request Gate",
                "Review Gate",
                "QA Verification Gate",
                "Production Verification Gate",
                "Close Gate",
                "Pane Notification",
                "Handoff Startup Decision",
                "Review Quality Hierarchy",
                "Test / QA Role Boundary",
                "Close Gate Checklist",
                "事実姿勢",
                "実装者 / 監査者境界",
                "判断の routing",
                "Scope Integrity",
                "Verification Discipline",
                "notify-codex-review",
                "mozyo-bridge init",
                "Receiver pane unavailable",
                "Notification fails",
                "`Implementation Done` は `完了` ではない",
                "Stop hook handoff wait",
                "迎合より事実を優先する",
                # Ticket-ID entrypoint runtime reflection.
                "Ticket-ID Entrypoint",
                "入力が Redmine issue id",
                "Redmine issue record",
                "journal id と gate 順序が監査 replay の鍵",
                # Audit-owned commit authority codified in this task.
                "Audit-Owned Commit Authority",
                "commit authority であって implementation authority ではない",
                "Refs: Redmine #<issue_id>",
                "Journal: <journal_id>",
                "Review Gate journal",
                "git diff --cached --stat",
                "git add -A",
                "Close Gate journal",
                # Chat-surface boundary added to reduce noisy chat output for
                # un-notified / pending-operator-action handoffs.
                "chat には issue / journal id",
                "durable 手順を chat に再掲しない",
                "durable 手順",
                # Field-tested Redmine review payload details.
                "Review Request Gate",
                "target commit / diff",
                "changed files",
                "期待する read / ack path",
                "[事実]",
                "[仮説]",
                "是正条件",
                "仕様・設計整合",
                "bug / spec misunderstanding / unnecessary work",
                "reproduction",
                "expected",
                "actual",
                "version consistency",
                "`catalog.yaml` / docs resolver / nagger file conventions tooling の標準化は別タスク",
                # Close Approval Separation codified in 2026.05.18.3: review
                # approval and owner close approval are distinct gates; the
                # reviewer (audit role / Codex equivalent) records a separate
                # journal asking the owner about close after review approval,
                # and the implementer must not close from review approval
                # alone.
                "Close Approval Separation",
                "owner close approval",
                "Review Gate approval を owner close approval と読み替えない",
                "Review Gate とは別 journal",
                "owner close approval が未取得のまま close してはならない",
                "同一 issue の **別 journal** を作成し、owner にクローズ可否を確認する",
                "Review Gate approval だけで issue を close へ進めない",
            ):
                self.assertIn(marker, installed)
            self.assertIn(
                "`Claude Code が常に実装、Codex が常に監査` のような固定 role split",
                installed,
            )
            self.assertNotIn("python3 vibes/tools/mozyo_bridge", installed)
            self.assertNotIn(".claude-nagger/file_conventions.yaml", installed)
            self.assertNotIn("resolve_audit_docs.py", installed)
            # The central preset is the scaffold for arbitrary downstream
            # projects; team-specific tools (Notion in this team's flow) must
            # not leak into the generated guidance.
            self.assertNotIn("Notion", installed)
            self.assertNotIn("vibes/docs/catalog.yaml", installed)
            self.assertNotIn("manual_spec", installed)
            self.assertNotIn("FeatureListDsl", installed)
            self.assertNotIn("/myapp/Source/rails", installed)
            self.assertNotIn("tmux-integrated", installed)
            self.assertNotIn("VS Code", installed)
            # Implementation Done Gate is a distinct durable gate, not a Progress Log.
            # The Factual Posture wording must not downgrade it.
            self.assertNotIn("self-verification is a Progress Log", installed)
            self.assertIn("review input であり completion ではない", installed)

            result, output = self.run_cli(
                ["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            self.assertIn("AGENTS.md", output)
            self.assertTrue((project / "AGENTS.md").exists())
            self.assertTrue((project / "CLAUDE.md").exists())
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md",
                agents,
            )
            self.assertNotIn(str(home), agents)
            self.assertNotIn("/Users/", agents)
            self.assertIn("active な `Redmine issue / journal`", agents)
            self.assertIn("durable state、handoff、review、verification、close 条件", agents)
            self.assertIn("router に本文を複製しない", agents)
            # Generated routers must not name vibes/docs/* paths as runtime context.
            # vibes/docs/ is this repo's design/spec source, not an external scaffold
            # target's runtime convention.
            self.assertNotIn("vibes/docs/specs/project-map.md", agents)
            self.assertNotIn("vibes/docs/rules/agent-workflow.md", agents)
            self.assertNotIn("vibes/docs/", agents)
            self.assertNotIn("## Project-Local Context", agents)
            self.assertIn("target project 側の任意の convention", agents)
            self.assertIn("mozyo-bridge の runtime 必須参照ではない", agents)

            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md",
                claude,
            )
            self.assertNotIn(str(home), claude)
            self.assertNotIn("/Users/", claude)
            self.assertIn("ClaudeCode 起動時の最小 reminder", claude)
            self.assertIn("迎合せず", claude)
            self.assertIn("implementation done / implementation_done は completion ではない", claude)
            self.assertIn("Redmine issue / journal", claude)
            self.assertIn("handoff startup decision", claude)
            # Chat surface stays thin: durable receive method lives in Redmine,
            # chat reports stay to a state + issue/journal-id pointer.
            self.assertIn("最小ポインタ", claude)
            self.assertIn("chat に貼り直さない", claude)
            # Router stays thin: well below the central preset's depth, even
            # with the Project-Local Additions marker block + boilerplate
            # (~9 lines) shipped from the router template.
            self.assertLess(len(claude.splitlines()), 50)
            self.assertNotIn("Redmine Gate Lifecycle", claude)
            self.assertNotIn("Implementer / Auditor Role Boundary", claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual("redmine", state["preset"])
            self.assertIn("AGENTS.md", state["files"])
            self.assertEqual("2026.06.10.1", state["preset_version"])

            # The audit-owned commit policy belongs in the central preset only.
            # Root routers stay thin and must not duplicate the policy body.
            self.assertNotIn("Audit-Owned Commit Authority", agents)
            self.assertNotIn("Audit-Owned Commit Authority", claude)
            self.assertNotIn("Refs: Redmine #", agents)
            self.assertNotIn("Refs: Redmine #", claude)

    def test_rules_install_and_scaffold_redmine_rails_layered_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            rails_workflow = home / "rules" / "presets" / "redmine-rails" / "agent-workflow.md"
            self.assertTrue(rails_workflow.exists())
            installed = rails_workflow.read_text(encoding="utf-8")
            for marker in (
                "Redmine Rails Agent Workflow",
                "rules/presets/redmine/agent-workflow.md",
                "Rails Scope Posture",
                "Rails Design Consultation Triggers",
                "Rails Implementation Done Additions",
                "Rails Review Focus",
                "Data / migration safety",
                "Authorization / tenant boundary",
                "Hotwire / UI behavior",
                "Rails Verification Discipline",
                "Rails QA / Production Verification",
                # Project-Local Layer section added in 2026.05.18.3.
                # The scaffold preset must explicitly tell operators which
                # categories of project-local facts stay in the target repo
                # and must not be overwritten by `scaffold apply`.
                "Project-Local Layer",
                "Project-Local Layer Apply Discipline",
                "do not erase on scaffold apply",
                "App stack identity",
                "Rails extension conventions",
                "Read-only documentation areas",
                "Project-specific safety commands",
                "Project docs governance",
                "Local role-boundary overrides",
                "Project tooling and private convention",
                "scaffold diff redmine-rails",
                "--backup",
                # New marker-bounded preservation guidance + concrete
                # category sections added in 2026.05.18.4.
                "Project-Local Additions マーカー",
                "<!-- mozyo-bridge:project-local-additions:begin -->",
                "<!-- mozyo-bridge:project-local-additions:end -->",
                "Active-Doc Resolver Concept",
                "Dangerous DB / Test Command Category",
                "Presenter / YAML / Doc-Readonly Category",
                "Project Tooling / Local Skill / Role-Boundary Override Category",
            ):
                self.assertIn(marker, installed)
            # Regression rails: the scaffold preset must not import team-specific
            # paths or convention names, even when describing what stays in
            # project-local docs. Existing-repo examples are described in
            # generic terms only.
            self.assertNotIn("/myapp/Source/rails", installed)
            self.assertNotIn("vibes/docs/catalog.yaml", installed)
            self.assertNotIn(".claude-nagger/file_conventions.yaml", installed)
            self.assertNotIn("resolve_audit_docs.py", installed)
            self.assertNotIn("bin/recreate_db.sh", installed)
            self.assertNotIn("bin/sync-mozyo-bridge-skill", installed)
            self.assertNotIn("RAILS_ENV=test", installed)
            self.assertNotIn("app/presenters/", installed)

            result, _ = self.run_cli(
                ["scaffold", "apply", "redmine-rails", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            self.assertIn(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-rails/agent-workflow.md",
                agents,
            )
            self.assertIn("active な `Redmine issue / journal と Rails project docs`", agents)
            self.assertIn("router に本文を複製しない", agents)
            self.assertNotIn("Data / migration safety", agents)
            self.assertNotIn("Rails Review Focus", claude)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("redmine-rails", state["preset"])
            self.assertEqual("2026.05.18.4", state["preset_version"])

    def test_rules_install_and_scaffold_redmine_governed_full_package(self) -> None:
        """The non-Rails governed preset ships the governance package.

        It must extend the generic Redmine workflow, not the Rails layer, and
        its catalog skeleton must stay framework-neutral.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            governed_workflow = (
                home / "rules" / "presets" / "redmine-governed" / "agent-workflow.md"
            )
            self.assertTrue(governed_workflow.exists())
            installed = governed_workflow.read_text(encoding="utf-8")

            for marker in (
                "Redmine Governed Agent Workflow",
                "rules/presets/redmine/agent-workflow.md",
                "Scaffolded Repo-Local Artifacts",
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                "mozyo-bridge docs validate",
                "mozyo-bridge docs resolve",
                "mozyo-bridge docs generate-file-conventions",
                "mozyo-bridge docs audit-impact",
                "Gate Schema",
                "Codex Direct Edit Gate",
                "codex_direct_edit",
                "Governed Mode Prohibitions",
            ):
                self.assertIn(marker, installed)

            for forbidden in (
                "rules/presets/redmine-rails/agent-workflow.md",
                "redmine-rails-governed",
                "bundle exec",
                "rspec",
                "rubocop",
                "brakeman",
                "db/migrate",
                "app/**/*.rb",
                "spec/**/*.rb",
                "fc-rails",
                "NIPT",
                "nihonidenshi",
            ):
                self.assertNotIn(forbidden, installed)

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)
            for expected_path in (
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
            ):
                self.assertTrue((project / expected_path).exists())

            catalog_example = (
                project / ".mozyo-bridge/docs/catalog.yaml.example"
            ).read_text(encoding="utf-8")
            self.assertIn("fc-implementation-source", catalog_example)
            self.assertIn("fc-tests", catalog_example)
            for forbidden in (
                "fc-rails",
                "Rails app",
                "app/**/*.rb",
                "db/migrate",
                "spec/**/*.rb",
            ):
                self.assertNotIn(forbidden, catalog_example)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("redmine-governed", state["preset"])
            tracked_files = set(state["files"].keys())
            for expected in (
                "AGENTS.md",
                "CLAUDE.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
            ):
                self.assertIn(expected, tracked_files)

    def test_rules_install_and_scaffold_redmine_rails_governed_full_package(self) -> None:
        """The governed preset must ship a full guardrail package.

        The central preset must surface strong governance language —
        gate schema, Codex direct edit gate, docs catalog governance,
        LLM rule authoring — without leaking nihonidenshi-specific names,
        paths, or business-domain identifiers. `scaffold apply` must
        write the repo-local rules / catalog skeleton into the
        target repository so the package is usable out of the box.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            result, _ = self.run_cli(["rules", "install", "--home", str(home)])
            self.assertEqual(0, result)
            governed_workflow = (
                home / "rules" / "presets" / "redmine-rails-governed" / "agent-workflow.md"
            )
            self.assertTrue(governed_workflow.exists())
            installed = governed_workflow.read_text(encoding="utf-8")

            # Strong governance language must survive the de-domain pass.
            for marker in (
                "Redmine Rails Governed Agent Workflow",
                "rules/presets/redmine/agent-workflow.md",
                "rules/presets/redmine-rails/agent-workflow.md",
                "Scaffolded Repo-Local Artifacts",
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
                # docs catalog tooling lives in the mozyo-bridge package now,
                # not vendor-copied into the target repo. The workflow doc
                # references it by CLI name.
                "mozyo-bridge docs validate",
                "mozyo-bridge docs resolve",
                "mozyo-bridge docs generate-file-conventions",
                "mozyo-bridge docs audit-impact",
                "Gate Schema",
                "Codex Direct Edit Gate",
                "codex_direct_edit",
                "allowed_paths",
                "follow_up_review",
                "Docs Catalog Governance",
                "Active-Doc Resolver",
                "LLM Rule Authoring",
                "Required Verification",
                "Close Approval Separation",
                "Governed Mode Prohibitions",
            ):
                self.assertIn(marker, installed)

            # Regression rails: nihonidenshi-specific business domain,
            # paths, and project identifiers must not leak into the
            # generalized preset.
            for forbidden in (
                "nihonidenshi",
                "idenshi_youbou",
                "jgmlife",
                "/myapp/Source/rails",
                "/myapp/Doc",
                "NIPT",
                "検査依頼",
                "検体",
                "帳票",
                "判定",
                "集荷",
                "_機能リスト.json",
                "FeatureList",
                "vibes/docs/tools",
                "bin/recreate_db.sh",
                "bin/sync-mozyo-bridge-skill",
            ):
                self.assertNotIn(forbidden, installed)

            # `scaffold apply` writes the repo-local governance artifacts under
            # .mozyo-bridge/ in the target repo so the package is
            # immediately usable. The main gate / role contract now lives
            # in the preset agent-workflow.md itself rather than a second
            # shipped development_flow.md file.
            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)
            for expected_path in (
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
            ):
                self.assertTrue(
                    (project / expected_path).exists(),
                    msg=f"governed scaffold did not write {expected_path}",
                )
            # Vendor-copied Python tools must not ship anymore — the
            # docs catalog tooling lives inside the mozyo-bridge package
            # and is invoked through `mozyo-bridge docs ...` instead.
            self.assertFalse(
                (project / ".mozyo-bridge/tools").exists(),
                msg=(
                    "governed scaffold should no longer vendor-copy "
                    ".mozyo-bridge/tools/*.py — those tools now live "
                    "inside the mozyo-bridge package."
                ),
            )

            for marker in (
                "codex_direct_edit",
                "allowed_paths",
                "implementation_done",
                "owner_close_approval",
                "禁止_並行表現",
            ):
                self.assertIn(marker, installed)
            self.assertFalse(
                (project / ".mozyo-bridge/rules/development_flow.md").exists(),
                msg=(
                    "development_flow.md should not ship; governed agent "
                    "execution contract is merged into agent-workflow.md"
                ),
            )

            # The catalog example references the shipped rule files only,
            # never the nihonidenshi domain catalog ids.
            catalog_example = (
                project / ".mozyo-bridge/docs/catalog.yaml.example"
            ).read_text(encoding="utf-8")
            for marker in (
                "rule-llm-rule-authoring",
                "rule-docs-catalog-governance",
            ):
                self.assertIn(marker, catalog_example)
            self.assertNotIn("rule-mozyo-bridge-development-flow", catalog_example)
            self.assertNotIn(".mozyo-bridge/rules/development_flow.md", catalog_example)
            for forbidden in ("NIPT", "_機能リスト", "nihonidenshi"):
                self.assertNotIn(forbidden, catalog_example)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("redmine-rails-governed", state["preset"])
            # Every shipped extra is tracked in the manifest so `scaffold
            # status` can detect drift after operators edit the file.
            tracked_files = set(state["files"].keys())
            for expected in (
                "AGENTS.md",
                "CLAUDE.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
            ):
                self.assertIn(expected, tracked_files)
            # And the manifest must not claim any `.mozyo-bridge/tools/*`
            # entries — they moved into the mozyo-bridge package.
            self.assertFalse(
                any(p.startswith(".mozyo-bridge/tools/") for p in tracked_files),
                msg=(
                    "manifest still references .mozyo-bridge/tools/*; "
                    "the governed preset should no longer ship those."
                ),
            )

    def test_governed_scaffold_refuses_to_silently_overwrite_shipped_artifacts(self) -> None:
        """Shipped governance artifacts are protected from silent overwrite.

        Operators must opt in with `--backup` or `--force`, same as the
        router pair, because the file body may carry local edits even
        though the preset side is the source of truth.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)

            # Second apply without --backup / --force must refuse rather
            # than clobber the shipped artifacts the operator may have
            # touched between applies.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails-governed",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
            err = stderr.getvalue()
            self.assertIn("refusing to overwrite existing scaffold files", err)
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", err)

            # --backup re-runs the apply and stashes the pre-existing file.
            backup_result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )
            self.assertEqual(0, backup_result)
            self.assertTrue(
                list((project / ".mozyo-bridge/rules").glob("llm_rule_authoring.md.bak.*"))
            )

    def test_governed_scaffold_status_clean_after_fresh_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, result)
            self.assertIn("preset: redmine-rails-governed", output)
            self.assertIn("result: clean", output)
            # Manifest now tracks router + repo-local artifacts. The
            # label must reflect that scope rather than misleadingly
            # calling everything a router file.
            self.assertIn("tracked files:", output)
            self.assertNotIn("router files:", output)

    def test_governed_scaffold_distributes_local_overlay_governance(self) -> None:
        """Governed presets must ship the local-only overlay boundary.

        The package primitive (`catalog.local.yaml` overlay) is useless to
        consumers unless the governed scaffold tells them the overlay is a
        git-ignored local-only artifact and keeps it out of the public
        catalog / generated conventions. Assert the distribution surfaces —
        a `.mozyo-bridge/docs/.gitignore`, the catalog skeleton comment, and
        the docs_catalog_governance rule — for both governed presets, and
        that the overlay file itself is never a tracked, shipped artifact.
        """
        for preset in ("redmine-governed", "redmine-rails-governed"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--home", str(home)])
                    result, _ = self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
                    self.assertEqual(0, result)

                    # The overlay is git-ignored by a shipped, tracked
                    # `.mozyo-bridge/docs/.gitignore`.
                    docs_gitignore = project / ".mozyo-bridge/docs/.gitignore"
                    self.assertTrue(docs_gitignore.exists())
                    gitignore_text = docs_gitignore.read_text(encoding="utf-8")
                    self.assertIn("catalog.local.yaml", gitignore_text)

                    state = scaffold_state(project)
                    assert state is not None
                    tracked = set(state["files"].keys())
                    self.assertIn(".mozyo-bridge/docs/.gitignore", tracked)
                    # The overlay body itself is operator-owned local-only
                    # state; the scaffold never ships or tracks it.
                    self.assertNotIn(
                        ".mozyo-bridge/docs/catalog.local.yaml", tracked
                    )
                    self.assertFalse(
                        (project / ".mozyo-bridge/docs/catalog.local.yaml").exists()
                    )

                    # The catalog skeleton documents the overlay boundary.
                    example = (
                        project / ".mozyo-bridge/docs/catalog.yaml.example"
                    ).read_text(encoding="utf-8")
                    self.assertIn("catalog.local.yaml", example)
                    self.assertIn("--no-local", example)

                    # The governance rule records the public/private
                    # separation and the secret-shaped fail-closed guard.
                    governance = (
                        project / ".mozyo-bridge/rules/docs_catalog_governance.yaml"
                    ).read_text(encoding="utf-8")
                    self.assertIn("catalog.local.yaml", governance)
                    self.assertIn("separation guard", governance)
                    self.assertIn("fail-closed", governance)

    def test_docs_validate_coverage_roots_precedence(self) -> None:
        """coverage_roots: CLI overrides catalog overrides default.

        The docs catalog tooling now ships as the ``mozyo-bridge docs``
        CLI inside the package. Precedence stays as before:

        1. ``--coverage-root`` CLI flag — wins when present.
        2. ``catalog.coverage_roots`` field — used when CLI absent.
        3. Built-in Rails-flavoured default — fallback when neither.

        The validator prints which source it used as the first
        ``notice:`` so operators can see the resolution from stdout.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            catalog_path = project / ".mozyo-bridge/docs/catalog.yaml"
            base_catalog = (
                project / ".mozyo-bridge/docs/catalog.yaml.example"
            ).read_text(encoding="utf-8")

            def run_coverage(*extra: str) -> tuple[int, str]:
                return self.run_cli(
                    [
                        "docs",
                        "validate",
                        "--check-file-coverage",
                        "--repo",
                        str(project),
                        *extra,
                    ]
                )

            # (3) No catalog field, no CLI flag — default Rails roots.
            catalog_path.write_text(base_catalog, encoding="utf-8")
            default_code, default_output = run_coverage()
            self.assertEqual(0, default_code)
            self.assertIn("coverage_roots source: default", default_output)

            # (2) Catalog declares coverage_roots — used when no CLI flag.
            catalog_path.write_text(
                base_catalog + "\ncoverage_roots:\n  - .mozyo-bridge\n",
                encoding="utf-8",
            )
            catalog_code, catalog_output = run_coverage()
            self.assertEqual(0, catalog_code)
            self.assertIn("coverage_roots source: catalog", catalog_output)
            self.assertNotIn("coverage_roots source: default", catalog_output)

            # (1) CLI overrides catalog. Non-existent root → notice only.
            cli_code, cli_output = run_coverage("--coverage-root", "unknown_layer")
            self.assertEqual(0, cli_code)
            self.assertIn("coverage_roots source: cli", cli_output)
            self.assertIn("coverage root does not exist", cli_output)

            # Bad shape: catalog with a string-typed coverage_roots
            # must fail validation (not silently ignored).
            catalog_path.write_text(
                base_catalog + "\ncoverage_roots: app\n", encoding="utf-8"
            )
            bad_code, bad_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(1, bad_code)
            self.assertIn("coverage_roots must be a list", bad_output)

            # An unmatched file inside an *existing* coverage root is
            # still exit 1. This guards against the manifest-driven
            # refactor accidentally swallowing real coverage gaps.
            unmatched_root = project / "fresh_app"
            unmatched_root.mkdir()
            (unmatched_root / "orphan.rb").write_text("# orphan\n", encoding="utf-8")
            catalog_path.write_text(
                base_catalog + "\ncoverage_roots:\n  - fresh_app\n",
                encoding="utf-8",
            )
            real_gap_code, real_gap_output = run_coverage()
            self.assertEqual(1, real_gap_code)
            self.assertIn(
                "no file_convention matched: fresh_app/orphan.rb",
                real_gap_output,
            )

    def test_docs_cli_round_trips_against_shipped_catalog_example(self) -> None:
        """The packaged `docs ...` CLI must work on the catalog skeleton.

        After `scaffold apply`, copying `catalog.yaml.example` to
        `catalog.yaml` should immediately let every docs subcommand run
        cleanly. This is the operator's first-day experience after
        installing mozyo-bridge — if it fails, the governance package
        is unusable straight out of `scaffold apply`.
        """
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            example = project / ".mozyo-bridge/docs/catalog.yaml.example"
            catalog = project / ".mozyo-bridge/docs/catalog.yaml"
            _shutil.copyfile(example, catalog)

            # docs validate
            validate_code, validate_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(0, validate_code, msg=validate_output)
            self.assertIn("catalog validation passed", validate_output)

            # docs validate --check-file-coverage: missing roots ok.
            coverage_code, coverage_output = self.run_cli(
                [
                    "docs",
                    "validate",
                    "--check-file-coverage",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, coverage_code)
            self.assertIn("notice:", coverage_output)
            self.assertIn("catalog validation passed", coverage_output)

            # docs resolve — surfaces the catalog's rule docs via the
            # agent-guardrail file_convention's document_refs.
            resolve_code, resolve_output = self.run_cli(
                [
                    "docs",
                    "resolve",
                    "--repo",
                    str(project),
                    "--format",
                    "json",
                    "AGENTS.md",
                ]
            )
            self.assertEqual(0, resolve_code, msg=resolve_output)
            results = json.loads(resolve_output)
            self.assertEqual(1, len(results))
            resolved_ids = {doc["id"] for doc in results[0]["documents"]}
            self.assertIn("rule-docs-catalog-governance", resolved_ids)
            self.assertIn("rule-llm-rule-authoring", resolved_ids)

            # docs generate-file-conventions writes the output and a
            # follow-up --check confirms the round-trip is clean.
            gen_code, gen_output = self.run_cli(
                [
                    "docs",
                    "generate-file-conventions",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, gen_code, msg=gen_output)
            gen_path = project / ".mozyo-bridge/docs/file_conventions.generated.yaml"
            self.assertTrue(gen_path.exists())

            drift_code, drift_output = self.run_cli(
                [
                    "docs",
                    "generate-file-conventions",
                    "--check",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, drift_code, msg=drift_output)

    # ------------------------------------------------------------------
    # Local-only docs catalog overlay (Redmine #11819).
    # ------------------------------------------------------------------

    def _make_catalog_project(self, tmp: str) -> Path:
        """Scaffold a governed project with a ready-to-use public catalog."""
        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        project.mkdir()
        self.run_cli(["rules", "install", "--home", str(home)])
        self.run_cli(
            [
                "scaffold",
                "apply",
                "redmine-governed",
                "--target",
                str(project),
                "--home",
                str(home),
            ]
        )
        shutil.copyfile(
            project / ".mozyo-bridge/docs/catalog.yaml.example",
            project / ".mozyo-bridge/docs/catalog.yaml",
        )
        return project

    _SAMPLE_OVERLAY = (
        "schema_version: 1\n"
        "documents:\n"
        "  - id: local-only-doc\n"
        "    type: rule\n"
        "    status: active\n"
        "    canonical_path: local_only_doc.md\n"
        "    purpose: local-only rule\n"
        "    audit_role: local_only\n"
        "file_conventions:\n"
        "  - id: fc-local-only\n"
        "    name: local only\n"
        "    patterns:\n"
        "      - local_only_doc.md\n"
        "    severity: warn\n"
        "    document_refs:\n"
        "      - local-only-doc\n"
    )

    def _write_overlay(self, project: Path, body: str) -> Path:
        overlay = project / ".mozyo-bridge/docs/catalog.local.yaml"
        overlay.write_text(body, encoding="utf-8")
        return overlay

    def test_docs_overlay_absent_is_noop(self) -> None:
        """Fresh clone / CI: no overlay file → public catalog verbatim."""
        from mozyo_bridge.docs_tools import (
            CatalogContext,
            load_catalog,
            load_effective_catalog,
        )

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_catalog_project(tmp)
            context = CatalogContext.build(str(project), None)
            self.assertFalse(context.overlay_path.exists())
            catalog, overlay = load_effective_catalog(context)
            self.assertFalse(overlay.applied)
            self.assertEqual(load_catalog(context.catalog_path), catalog)

    def test_docs_overlay_present_merges_for_resolve(self) -> None:
        """Overlay present → its docs resolve; --no-local hides them again."""
        from mozyo_bridge.docs_tools import CatalogContext, resolve_paths_detailed

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_catalog_project(tmp)
            (project / "local_only_doc.md").write_text("local\n", encoding="utf-8")
            self._write_overlay(project, self._SAMPLE_OVERLAY)
            context = CatalogContext.build(str(project), None)

            results, overlay = resolve_paths_detailed(context, ["local_only_doc.md"])
            self.assertTrue(overlay.applied)
            self.assertEqual(1, overlay.document_count)
            self.assertEqual(1, overlay.file_convention_count)
            ids = {doc["id"] for doc in results[0]["documents"]}
            self.assertIn("local-only-doc", ids)

            # --no-local forces the public-only view a fresh clone / CI sees.
            public_results, public_overlay = resolve_paths_detailed(
                context, ["local_only_doc.md"], include_local=False
            )
            self.assertFalse(public_overlay.applied)
            public_ids = {doc["id"] for doc in public_results[0]["documents"]}
            self.assertNotIn("local-only-doc", public_ids)

    def test_docs_overlay_excluded_from_public_artifacts(self) -> None:
        """Separation guard: overlay never leaks into generate / public validate."""
        from mozyo_bridge.docs_tools import CatalogContext, load_catalog

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_catalog_project(tmp)
            (project / "local_only_doc.md").write_text("local\n", encoding="utf-8")
            self._write_overlay(project, self._SAMPLE_OVERLAY)

            # Public catalog load never sees the overlay.
            context = CatalogContext.build(str(project), None)
            public_ids = {d["id"] for d in load_catalog(context.catalog_path)["documents"]}
            self.assertNotIn("local-only-doc", public_ids)

            # generate-file-conventions output excludes overlay data.
            gen_code, _ = self.run_cli(
                ["docs", "generate-file-conventions", "--repo", str(project)]
            )
            self.assertEqual(0, gen_code)
            generated = (
                project / ".mozyo-bridge/docs/file_conventions.generated.yaml"
            ).read_text(encoding="utf-8")
            self.assertNotIn("fc-local-only", generated)
            self.assertNotIn("local_only_doc.md", generated)

            # The generated drift check stays clean with the overlay present.
            check_code, _ = self.run_cli(
                ["docs", "generate-file-conventions", "--check", "--repo", str(project)]
            )
            self.assertEqual(0, check_code)

            # Public validate (no --include-local) passes regardless of overlay.
            validate_code, validate_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(0, validate_code, msg=validate_output)
            self.assertIn("catalog validation passed", validate_output)

    def test_docs_overlay_secret_guard_fails_closed(self) -> None:
        """An overlay carrying a secret-shaped value stops the local resolve."""
        from mozyo_bridge.docs_tools import (
            CatalogContext,
            OverlayError,
            load_effective_catalog,
            scan_for_secret_shaped_values,
        )

        # Assemble an AWS-access-key-shaped value at runtime so this
        # tracked test source carries no secret-shaped literal — the same
        # public-private-boundary.md constraint the overlay guard enforces.
        # The result is `<prefix>` + 16 uppercase chars, matching the
        # aws-access-key-id pattern without appearing as a token in source.
        secret_value = ("A" + "KIA") + "".join(
            chr(ord("A") + offset) for offset in range(16)
        )

        # Unit: the scanner reports locations, never the offending value.
        findings = scan_for_secret_shaped_values(
            {"documents": [{"id": "x", "api_key": secret_value}]}
        )
        self.assertTrue(findings)
        self.assertTrue(all(secret_value not in finding for finding in findings))
        self.assertFalse(
            scan_for_secret_shaped_values(
                {"documents": [{"id": "x", "canonical_path": "foo/bar.md"}]}
            )
        )

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_catalog_project(tmp)
            (project / "local_only_doc.md").write_text("local\n", encoding="utf-8")
            self._write_overlay(
                project,
                "documents:\n"
                "  - id: local-only-doc\n"
                "    type: rule\n"
                "    status: active\n"
                "    canonical_path: local_only_doc.md\n"
                f"    token: {secret_value}\n",
            )
            context = CatalogContext.build(str(project), None)
            with self.assertRaises(OverlayError):
                load_effective_catalog(context)

            # CLI fails closed with a clean message (no traceback) on stderr.
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                code, _ = self.run_cli(
                    ["docs", "resolve", "--repo", str(project), "local_only_doc.md"]
                )
            self.assertEqual(1, code)
            self.assertIn("local overlay error", stderr.getvalue())
            self.assertNotIn(secret_value, stderr.getvalue())

    def test_docs_overlay_id_collision_rejected(self) -> None:
        """Overlay must add new ids, never shadow a public catalog id."""
        from mozyo_bridge.docs_tools import (
            CatalogContext,
            OverlayError,
            load_effective_catalog,
        )

        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_catalog_project(tmp)
            # `rule-docs-catalog-governance` is a public id in the skeleton.
            self._write_overlay(
                project,
                "documents:\n"
                "  - id: rule-docs-catalog-governance\n"
                "    type: rule\n"
                "    status: active\n"
                "    canonical_path: local_only_doc.md\n",
            )
            context = CatalogContext.build(str(project), None)
            with self.assertRaises(OverlayError):
                load_effective_catalog(context)

    def test_docs_validate_include_local(self) -> None:
        """`docs validate --include-local` checks the overlay when present."""
        with tempfile.TemporaryDirectory() as tmp:
            project = self._make_catalog_project(tmp)
            (project / "local_only_doc.md").write_text("local\n", encoding="utf-8")
            self._write_overlay(project, self._SAMPLE_OVERLAY)

            ok_code, ok_output = self.run_cli(
                ["docs", "validate", "--repo", str(project), "--include-local"]
            )
            self.assertEqual(0, ok_code, msg=ok_output)
            self.assertIn("local overlay validated", ok_output)

            # A missing canonical_path in the overlay is reported as an error.
            self._write_overlay(
                project,
                "documents:\n"
                "  - id: local-only-doc\n"
                "    type: rule\n"
                "    status: active\n"
                "    canonical_path: does_not_exist.md\n",
            )
            bad_code, bad_output = self.run_cli(
                ["docs", "validate", "--repo", str(project), "--include-local"]
            )
            self.assertEqual(1, bad_code)
            self.assertIn("canonical_path does not exist", bad_output)

            # Without --include-local the overlay error is not surfaced.
            quiet_code, quiet_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(0, quiet_code)
            self.assertIn("catalog validation passed", quiet_output)

    def test_docs_validate_check_file_coverage_canonical_cli_shape(self) -> None:
        """Pin `docs validate --check-file-coverage` as the coverage entrypoint.

        The governed agent workflow advertises a single canonical
        invocation for the coverage check::

            mozyo-bridge docs validate --repo <path> --check-file-coverage

        This regression test guards three CLI contracts that the workflow
        relies on:

        1. ``--check-file-coverage`` is a flag on the ``validate``
           subcommand (not a separate ``docs coverage`` subcommand).
        2. The flag's position relative to ``--repo`` does not matter —
           argparse keyword order must stay flexible so the workflow's
           advertised form keeps working alongside other phrasings.
        3. Plain ``docs validate`` (no flag) does **not** silently emit
           the coverage-source notice; the check is opt-in via the flag.
        """
        import shutil as _shutil

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            _shutil.copyfile(
                project / ".mozyo-bridge/docs/catalog.yaml.example",
                project / ".mozyo-bridge/docs/catalog.yaml",
            )

            # (1) Canonical order: --repo before --check-file-coverage.
            canonical_code, canonical_output = self.run_cli(
                [
                    "docs",
                    "validate",
                    "--repo",
                    str(project),
                    "--check-file-coverage",
                ]
            )
            self.assertEqual(0, canonical_code, msg=canonical_output)
            self.assertIn("coverage_roots source:", canonical_output)
            self.assertIn("catalog validation passed", canonical_output)

            # (2) Reversed order must produce the same outcome — argparse
            # keyword ordering is part of the public CLI contract.
            reversed_code, reversed_output = self.run_cli(
                [
                    "docs",
                    "validate",
                    "--check-file-coverage",
                    "--repo",
                    str(project),
                ]
            )
            self.assertEqual(0, reversed_code, msg=reversed_output)
            self.assertIn("coverage_roots source:", reversed_output)

            # (3) Without the flag, the validator must not emit the
            # coverage-source notice — coverage checking is opt-in.
            plain_code, plain_output = self.run_cli(
                ["docs", "validate", "--repo", str(project)]
            )
            self.assertEqual(0, plain_code, msg=plain_output)
            self.assertNotIn("coverage_roots source:", plain_output)
            self.assertIn("catalog validation passed", plain_output)

            # (4) The coverage check lives on `validate`, not as a
            # separate `coverage` subcommand. argparse must reject it.
            # Suppress argparse's usage/error text — matching the
            # parser-rejection convention used elsewhere in this file.
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                self.run_cli(["docs", "coverage", "--repo", str(project)])

    def test_preset_files_walker_skips_pycache_and_pyc(self) -> None:
        """The scaffold walker must drop pip-generated bytecode cruft.

        The governed preset no longer ships ``.py`` source files, so a
        normal wheel install will not generate ``__pycache__`` next to
        the remaining artifacts. The defence-in-depth still matters —
        a future preset that ships ``.py`` artifacts (or an operator
        who unpacks a wheel into the preset tree by accident) should
        not break the walker. We inject the cruft next to an existing
        shipped artifact and confirm the walker still surfaces the real
        files while dropping ``__pycache__`` / ``.pyc`` entries.
        """
        from mozyo_bridge.scaffold.rules import render_preset_extra_files

        rules_dir = (
            Path(__file__).resolve().parents[3]
            / "src/mozyo_bridge/scaffold/presets"
            / "redmine-rails-governed/files/.mozyo-bridge/rules"
        )
        self.assertTrue(rules_dir.exists(), msg=f"rules dir missing: {rules_dir}")
        fake_pycache = rules_dir / "__pycache__"
        fake_pyc = fake_pycache / "fake_module.cpython-314.pyc"
        fake_pycache.mkdir(exist_ok=True)
        try:
            fake_pyc.write_bytes(b"\x82\x82\x82bogus pyc bytes")
            extras = render_preset_extra_files("redmine-rails-governed")
            paths = {item.path.as_posix() for item in extras}
            # No __pycache__ entry and no .pyc entry leak through.
            self.assertFalse(
                any("__pycache__" in p for p in paths),
                msg=f"walker leaked __pycache__/* entries: {sorted(paths)}",
            )
            self.assertFalse(
                any(p.endswith(".pyc") for p in paths),
                msg=f"walker leaked .pyc entries: {sorted(paths)}",
            )
            # The legitimate rule files under the same directory still
            # surface — we only filter cache cruft, not real artifacts.
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", paths)
        finally:
            import shutil as _shutil

            _shutil.rmtree(fake_pycache, ignore_errors=True)

    def test_governed_scaffold_apply_succeeds_after_wheel_install(self) -> None:
        """End-to-end: build wheel, pip install to a venv, run scaffold apply.

        Earlier iterations passed when running from the source tree but
        crashed under a real pip install because pip wrote `__pycache__/*.pyc`
        files next to the shipped catalog tools and the scaffold walker
        tried to decode them as UTF-8. This test mirrors that exact path
        so we don't regress.
        """
        import subprocess
        import venv as _venv

        repo_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            dist = Path(tmp) / "dist"
            dist.mkdir()
            build_proc = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(dist)],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if build_proc.returncode != 0:
                self.skipTest(
                    "python -m build failed (probably missing build backend deps); "
                    f"stderr={build_proc.stderr[:500]}"
                )
            wheels = list(dist.glob("mozyo_bridge-*.whl"))
            self.assertEqual(1, len(wheels), msg=f"unexpected wheels: {wheels}")

            venv_dir = Path(tmp) / "venv"
            try:
                _venv.EnvBuilder(with_pip=True, clear=True).create(venv_dir)
            except subprocess.CalledProcessError as exc:
                # Some Python distributions (e.g. uv-managed runtimes) abort
                # in ensurepip. The integration test still has value on CI
                # where venv works; skip when it doesn't rather than mask
                # the underlying regression.
                self.skipTest(f"venv with pip could not be created: {exc}")
            venv_python = venv_dir / "bin" / "python"
            venv_bin = venv_dir / "bin" / "mozyo-bridge"
            self.assertTrue(venv_python.exists(), msg=f"venv python missing: {venv_python}")

            install_proc = subprocess.run(
                [str(venv_python), "-m", "pip", "install", "-q", str(wheels[0])],
                capture_output=True,
                text=True,
            )
            if install_proc.returncode != 0:
                self.skipTest(
                    "pip install of the built wheel failed (no network or build deps "
                    f"missing): stderr={install_proc.stderr[:500]}"
                )
            self.assertTrue(venv_bin.exists(), msg=f"mozyo-bridge entry-point missing: {venv_bin}")

            home_dir = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            rules_proc = subprocess.run(
                [str(venv_bin), "rules", "install", "--home", str(home_dir)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                rules_proc.returncode,
                msg=(
                    "rules install failed post-wheel-install:\n"
                    f"stdout={rules_proc.stdout}\nstderr={rules_proc.stderr}"
                ),
            )

            apply_proc = subprocess.run(
                [
                    str(venv_bin),
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home_dir),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                apply_proc.returncode,
                msg=(
                    "scaffold apply failed post-wheel-install (this is the path "
                    "where pip's __pycache__/*.pyc files break the walker):\n"
                    f"stdout={apply_proc.stdout}\nstderr={apply_proc.stderr}"
                ),
            )

            # Every shipped governance artifact must land in the target
            # after a real wheel install, not just under the source tree.
            # Docs catalog tooling no longer ships as `.mozyo-bridge/tools/*.py`
            # — it lives inside the mozyo-bridge package and runs via the
            # `mozyo-bridge docs ...` CLI on the installed venv. We assert
            # the target tree does not carry the legacy vendor copy.
            for expected_path in (
                ".mozyo-bridge/rules/llm_rule_authoring.md",
                ".mozyo-bridge/rules/docs_catalog_governance.yaml",
                ".mozyo-bridge/docs/catalog.yaml.example",
            ):
                self.assertTrue(
                    (project / expected_path).exists(),
                    msg=f"post-install scaffold did not write {expected_path}",
                )
            self.assertFalse(
                (project / ".mozyo-bridge/tools").exists(),
                msg=(
                    "post-install scaffold still wrote .mozyo-bridge/tools/ "
                    "(legacy vendor copy) — the tooling should live in the "
                    "mozyo-bridge package now."
                ),
            )

            # And no `.pyc` / `__pycache__` cruft should leak into the
            # target tree — the walker skips them.
            stray_pyc = list(project.rglob("*.pyc"))
            stray_cache = list(project.rglob("__pycache__"))
            self.assertEqual([], stray_pyc, msg=f"unexpected .pyc copied: {stray_pyc}")
            self.assertEqual(
                [], stray_cache, msg=f"unexpected __pycache__ copied: {stray_cache}"
            )

            # Smoke: the packaged docs CLI runs against the catalog
            # skeleton straight after install. This is what operators
            # actually use; the previous vendor-copy test verified the
            # wrong thing once the tools moved into the package.
            import shutil as _shutil

            _shutil.copyfile(
                project / ".mozyo-bridge/docs/catalog.yaml.example",
                project / ".mozyo-bridge/docs/catalog.yaml",
            )
            validate_proc = subprocess.run(
                [
                    str(venv_bin),
                    "docs",
                    "validate",
                    "--repo",
                    str(project),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                0,
                validate_proc.returncode,
                msg=(
                    "post-install `mozyo-bridge docs validate` failed:\n"
                    f"stdout={validate_proc.stdout}\nstderr={validate_proc.stderr}"
                ),
            )

    def test_governed_scaffold_ships_tmux_ui_and_nagger_artifacts_by_default(self) -> None:
        """Default `scaffold apply` writes the tmux-ui + Claude Nagger artifacts.

        Both are default-on so a fresh `redmine-rails-governed` install
        carries the agent-window status snippet and the Claude Nagger
        skeleton. The artifacts land under the standard governed paths
        and are tracked in the scaffold manifest.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(0, result)
            for expected_path in (
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
                ".claude-nagger/command_conventions.yaml.example",
                ".claude-nagger/mcp_conventions.yaml.example",
                ".claude-nagger/.gitignore",
            ):
                self.assertTrue(
                    (project / expected_path).exists(),
                    msg=f"default scaffold did not write {expected_path}",
                )

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            tracked = set(state["files"].keys())
            for tracked_path in (
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
                ".claude-nagger/command_conventions.yaml.example",
                ".claude-nagger/mcp_conventions.yaml.example",
                ".claude-nagger/.gitignore",
            ):
                self.assertIn(
                    tracked_path,
                    tracked,
                    msg=f"manifest does not track {tracked_path}",
                )

    def test_governed_nagger_warns_on_default_lane_implementation_handoff(self) -> None:
        """Redmine #12171: governed Nagger skeleton ships the dispatch warning.

        The shipped `command_conventions.yaml.example` must carry a `warn`
        (not `block`) rule that fires before an implementation-shaped
        default-lane Claude handoff, so a sender is reminded to confirm a
        Redmine dispatch decision first. This guards against the #11619
        j#60252 failure mode of dispatching guardrail / scaffold / preset /
        workflow / release implementation requests to the default-lane
        Claude without a recorded dispatch decision.
        """
        import yaml

        for preset in ("redmine-governed", "redmine-rails-governed"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--home", str(home)])
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )

                    skeleton = (
                        project
                        / ".claude-nagger/command_conventions.yaml.example"
                    )
                    data = yaml.safe_load(skeleton.read_text(encoding="utf-8"))
                    rules = {r["id"]: r for r in data["rules"]}
                    self.assertIn(
                        "default-lane-implementation-handoff",
                        rules,
                        msg="governed Nagger skeleton missing the "
                        "default-lane implementation handoff warning",
                    )
                    rule = rules["default-lane-implementation-handoff"]
                    # Warning, never a hard block (#12171 keeps this advisory).
                    self.assertEqual("warn", rule["severity"])
                    self.assertIn(
                        "mozyo-bridge handoff send --to claude * "
                        "--kind implementation_request*",
                        rule["patterns"],
                    )
                    self.assertIn("mozyo-bridge notify-claude*", rule["patterns"])
                    # The reminder names the dispatch-decision precondition.
                    self.assertIn("dispatch decision", rule["message"])

    # ------------------------------------------------------------------
    # Redmine #11955: opt-in sublane / worktree runbook scaffold category.
    # The docs distribute only when `--with-worktree-runbook` is passed;
    # scaffold must never auto-write the operator-owned catalog.yaml.
    # ------------------------------------------------------------------
    WORKTREE_RUNBOOK_PATHS = (
        "vibes/docs/logics/worktree-lifecycle-boundary.md",
        "vibes/docs/logics/worktree-runbook-catalog-registration.md",
    )

    def test_worktree_runbook_is_off_by_default(self) -> None:
        """A plain governed `scaffold apply` does NOT ship the runbook docs.

        The `worktree-runbook` category is opt-in (Redmine #11955), so a
        default apply must neither write the docs to disk nor track them
        in the manifest. Adopting projects only get them via the explicit
        `--with-worktree-runbook` flag.
        """
        for preset in ("redmine-governed", "redmine-rails-governed"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--home", str(home)])

                    result, _ = self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
                    self.assertEqual(0, result)

                    state = scaffold_state(project)
                    assert state is not None
                    tracked = set(state["files"].keys())
                    for runbook_path in self.WORKTREE_RUNBOOK_PATHS:
                        self.assertFalse(
                            (project / runbook_path).exists(),
                            msg=f"default apply wrote opt-in doc {runbook_path}",
                        )
                        self.assertNotIn(
                            runbook_path,
                            tracked,
                            msg=f"default manifest tracks opt-in doc {runbook_path}",
                        )

    def test_worktree_runbook_installs_with_flag(self) -> None:
        """`--with-worktree-runbook` ships docs + manual catalog note.

        Option-on installs the two byte-synced runbook docs plus the
        scaffold-only catalog-registration note, tracks all three in the
        manifest, and — per the governed invariant — never creates or
        mutates the operator-owned `catalog.yaml`. `scaffold status`
        stays clean afterwards.
        """
        for preset in ("redmine-governed", "redmine-rails-governed"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--home", str(home)])

                    result, _ = self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                            "--with-worktree-runbook",
                        ]
                    )
                    self.assertEqual(0, result)

                    state = scaffold_state(project)
                    assert state is not None
                    tracked = set(state["files"].keys())
                    for runbook_path in self.WORKTREE_RUNBOOK_PATHS:
                        self.assertTrue(
                            (project / runbook_path).exists(),
                            msg=f"--with-worktree-runbook did not write {runbook_path}",
                        )
                        self.assertIn(
                            runbook_path,
                            tracked,
                            msg=f"manifest does not track {runbook_path}",
                        )

                    # B1 invariant: scaffold never auto-writes catalog.yaml.
                    # Only the `.example` and the manual note are shipped.
                    self.assertFalse(
                        (project / ".mozyo-bridge/docs/catalog.yaml").exists(),
                        msg="opt-in apply must not auto-write operator catalog.yaml",
                    )
                    note = project / (
                        "vibes/docs/logics/worktree-runbook-catalog-registration.md"
                    )
                    self.assertIn(
                        "catalog.yaml",
                        note.read_text(encoding="utf-8"),
                        msg="catalog-registration note missing manual snippet guidance",
                    )

                    # Existing scaffold status check must remain clean.
                    status_result, status_out = self.run_cli(
                        [
                            "scaffold",
                            "status",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
                    self.assertEqual(0, status_result)
                    self.assertIn("clean", status_out)

    def test_worktree_runbook_packaged_docs_match_authored_sources(self) -> None:
        """Sync-check: packaged runbook copies == authored repo sources.

        The scaffold ships a byte copy of this repo's authored
        `vibes/docs/logics/worktree-lifecycle-boundary.md`. This drift
        test fails if a packaged copy diverges from the source-of-truth
        doc; regenerate the copies (re-copy from `vibes/docs/logics/`)
        when the authored doc changes. The catalog-registration note is
        scaffold-only and intentionally NOT part of this pair.

        `sublane-worktree-operating-runbook.md` was consolidated into
        `coordinator-sublane-development-flow.md` and physically removed
        in Redmine #12215; the packaged copies were retired with it
        (Redmine #12235), so it is no longer a synced doc here.
        """
        repo_root = Path(__file__).resolve().parents[3]
        synced_docs = ("worktree-lifecycle-boundary.md",)
        for preset in ("redmine-governed", "redmine-rails-governed"):
            for doc in synced_docs:
                with self.subTest(preset=preset, doc=doc):
                    authored = repo_root / "vibes/docs/logics" / doc
                    packaged = (
                        repo_root
                        / "src/mozyo_bridge/scaffold/presets"
                        / preset
                        / "files/vibes/docs/logics"
                        / doc
                    )
                    self.assertTrue(
                        authored.is_file(), msg=f"authored source missing: {authored}"
                    )
                    self.assertTrue(
                        packaged.is_file(), msg=f"packaged copy missing: {packaged}"
                    )
                    self.assertEqual(
                        authored.read_bytes(),
                        packaged.read_bytes(),
                        msg=(
                            f"packaged worktree runbook doc drifted from authored "
                            f"source: {packaged} != {authored}. Re-copy from "
                            f"vibes/docs/logics/ to resync."
                        ),
                    )

    def test_worktree_runbook_rejects_unknown_opt_in_category(self) -> None:
        """`with_categories` validation rejects non-opt-in labels.

        A library caller that bypasses the CLI must be told when it asks
        for a category that is not opt-in (or does not exist), mirroring
        the skip-category validation.
        """
        from mozyo_bridge.scaffold.rules import render_preset_extra_files

        # An opt-out / unknown label is not a valid opt-in target. `die`
        # raises SystemExit, same as the skip-category validation path.
        with self.assertRaises(SystemExit):
            render_preset_extra_files(
                "redmine-governed", with_categories={"nagger"}
            )
        with self.assertRaises(SystemExit):
            render_preset_extra_files(
                "redmine-governed", with_categories={"does-not-exist"}
            )

        # The valid opt-in label surfaces the runbook docs.
        extras = render_preset_extra_files(
            "redmine-governed", with_categories={"worktree-runbook"}
        )
        paths = {item.path.as_posix() for item in extras}
        for runbook_path in self.WORKTREE_RUNBOOK_PATHS:
            self.assertIn(runbook_path, paths)

    # ------------------------------------------------------------------
    # Redmine #12362 / #12363: opt-in sublane-flow runtime profile.
    # `--with-sublane-flow` ships a portable profile doc AND toggles a
    # thin sublane read-route in the generated routers. Default scaffold
    # must keep sublane flow out of every runtime-active entrypoint.
    # ------------------------------------------------------------------
    SUBLANE_PROFILE_PATH = "vibes/docs/profiles/sublane-flow-runtime-profile.md"
    SUBLANE_ROUTE_HEADING = "## サブレーン開発フロー (opt-in profile)"

    def test_sublane_flow_is_off_by_default(self) -> None:
        """A plain governed `scaffold apply` adds no sublane runtime route.

        Default-off (Redmine #12362): the profile doc is not written, the
        manifest does not track it, and neither generated router carries
        the sublane read-route section. Single-lane projects stay light.
        """
        for preset in ("redmine-governed", "redmine-rails-governed"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--home", str(home)])

                    result, _ = self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
                    self.assertEqual(0, result)

                    state = scaffold_state(project)
                    assert state is not None
                    tracked = set(state["files"].keys())
                    self.assertFalse(
                        (project / self.SUBLANE_PROFILE_PATH).exists(),
                        msg="default apply wrote opt-in sublane profile doc",
                    )
                    self.assertNotIn(self.SUBLANE_PROFILE_PATH, tracked)
                    for router in ("AGENTS.md", "CLAUDE.md"):
                        body = (project / router).read_text(encoding="utf-8")
                        self.assertNotIn(
                            self.SUBLANE_ROUTE_HEADING,
                            body,
                            msg=f"default {router} carries the sublane read-route",
                        )

    def test_sublane_flow_installs_with_flag(self) -> None:
        """`--with-sublane-flow` ships the profile doc + router read-route.

        Option-on installs the portable profile doc, tracks it in the
        manifest, and adds the thin read-route section to BOTH generated
        routers. Per the governed invariant the scaffold never creates or
        mutates `catalog.yaml`, and `scaffold status` stays clean.
        """
        for preset in ("redmine-governed", "redmine-rails-governed"):
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--home", str(home)])

                    result, _ = self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                            "--with-sublane-flow",
                        ]
                    )
                    self.assertEqual(0, result)

                    state = scaffold_state(project)
                    assert state is not None
                    tracked = set(state["files"].keys())
                    self.assertTrue(
                        (project / self.SUBLANE_PROFILE_PATH).exists(),
                        msg="--with-sublane-flow did not write the profile doc",
                    )
                    self.assertIn(self.SUBLANE_PROFILE_PATH, tracked)

                    # Both routers carry the thin read-route section, and
                    # the route points at the profile doc rather than
                    # inlining the long-form workflow body.
                    for router in ("AGENTS.md", "CLAUDE.md"):
                        body = (project / router).read_text(encoding="utf-8")
                        self.assertIn(self.SUBLANE_ROUTE_HEADING, body)
                        self.assertIn(self.SUBLANE_PROFILE_PATH, body)
                        self.assertIn(
                            "skills/mozyo-bridge-agent/references/workflow.md",
                            (project / self.SUBLANE_PROFILE_PATH).read_text(
                                encoding="utf-8"
                            ),
                        )

                    # Redmine #12432: the opt-in profile must give adopters a
                    # read-route to the existing-project adoption procedure
                    # (distilled into the distributed skill workflow reference),
                    # so `--with-sublane-flow` reaches it without the repo-local
                    # runbook.
                    profile_body = (project / self.SUBLANE_PROFILE_PATH).read_text(
                        encoding="utf-8"
                    )
                    self.assertIn(
                        "## Existing-Project Sublane Adoption",
                        profile_body,
                        msg="sublane profile lost the existing-project adoption read-route",
                    )

                    # B1 invariant: scaffold never auto-writes catalog.yaml.
                    self.assertFalse(
                        (project / ".mozyo-bridge/docs/catalog.yaml").exists(),
                        msg="sublane opt-in apply must not auto-write catalog.yaml",
                    )

                    status_result, status_out = self.run_cli(
                        [
                            "scaffold",
                            "status",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
                    self.assertEqual(0, status_result)
                    self.assertIn("clean", status_out)

    def test_sublane_flow_diff_matches_opt_in_behavior(self) -> None:
        """`scaffold diff` mirrors the apply opt-in gating.

        A default diff against an empty target must not mention the
        sublane route or profile doc; the `--with-sublane-flow` diff must
        surface both. This keeps preview and apply in lockstep.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            default_code, default_out = self.run_cli(
                [
                    "scaffold",
                    "diff",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            self.assertEqual(1, default_code)  # empty target → changes
            self.assertNotIn(self.SUBLANE_ROUTE_HEADING, default_out)
            self.assertNotIn(self.SUBLANE_PROFILE_PATH, default_out)

            optin_code, optin_out = self.run_cli(
                [
                    "scaffold",
                    "diff",
                    "redmine-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--with-sublane-flow",
                ]
            )
            self.assertEqual(1, optin_code)
            self.assertIn(self.SUBLANE_ROUTE_HEADING, optin_out)
            self.assertIn(self.SUBLANE_PROFILE_PATH, optin_out)

    def test_sublane_flow_library_surface_round_trips(self) -> None:
        """Library callers key both the doc and the route off one set.

        `render_preset_extra_files(with_categories={"sublane-flow"})`
        surfaces the profile doc, and `sublane_flow_enabled` + the
        `render_router_pair(sublane_flow=...)` selector add the route only
        when enabled. An unknown / opt-out label is rejected the same way
        the file-shipping path rejects it.
        """
        from mozyo_bridge.scaffold.rules import (
            installed_agent_workflow,
            render_preset_extra_files,
            render_router_pair,
            sublane_flow_enabled,
        )

        # Validation parity with the file-shipping path.
        self.assertFalse(sublane_flow_enabled(None))
        self.assertFalse(sublane_flow_enabled(set()))
        self.assertTrue(sublane_flow_enabled({"sublane-flow"}))
        with self.assertRaises(SystemExit):
            sublane_flow_enabled({"nagger"})

        extras = render_preset_extra_files(
            "redmine-governed", with_categories={"sublane-flow"}
        )
        paths = {item.path.as_posix() for item in extras}
        self.assertIn(self.SUBLANE_PROFILE_PATH, paths)

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            self.run_cli(["rules", "install", "--home", str(home)])
            workflow = installed_agent_workflow("redmine-governed", home)
            base = render_router_pair(
                "redmine-governed", Path(tmp), workflow, sublane_flow=False
            )
            opt = render_router_pair(
                "redmine-governed", Path(tmp), workflow, sublane_flow=True
            )
            for item in base:
                self.assertNotIn(self.SUBLANE_ROUTE_HEADING, item.content)
            for item in opt:
                self.assertIn(self.SUBLANE_ROUTE_HEADING, item.content)
                self.assertIn(self.SUBLANE_PROFILE_PATH, item.content)

    def test_sublane_flow_profile_doc_excludes_private_operating_policy(self) -> None:
        """The shipped profile doc stays portable (Redmine #12362).

        Private operator-specific elements (lane counts, cockpit
        composition, absolute paths, session naming) must NOT be baked
        into the distributed default. The doc routes to the portable
        skill workflow reference instead of inlining policy.
        """
        repo_root = Path(__file__).resolve().parents[3]
        copies: list[bytes] = []
        for preset in ("redmine-governed", "redmine-rails-governed"):
            packaged = (
                repo_root
                / "src/mozyo_bridge/scaffold/presets"
                / preset
                / "files"
                / self.SUBLANE_PROFILE_PATH
            )
            with self.subTest(preset=preset):
                self.assertTrue(packaged.is_file(), msg=f"missing: {packaged}")
                text = packaged.read_text(encoding="utf-8")
                # Routes to the portable distributed entrypoint.
                self.assertIn(
                    "skills/mozyo-bridge-agent/references/workflow.md", text
                )
                # No leaked operator home / absolute path.
                self.assertNotIn("/Users/", text)
                self.assertNotIn("/home/", text)
                # Explicitly carves out private operating policy.
                self.assertIn("private operating policy", text)
                copies.append(packaged.read_bytes())

        # Both governed presets ship the identical portable doc; pin them
        # so a future edit to one is mirrored to the other.
        self.assertEqual(
            copies[0],
            copies[1],
            msg="sublane profile doc drifted between governed presets",
        )

    def test_governed_doctor_reports_skipped_after_skip_with_backup(self) -> None:
        """`--skip-* --backup` opt-out: doctor must use the manifest, not disk.

        `--backup` leaves `.bak.<timestamp>` files inside the
        `.claude-nagger/` directory after the opt-out unlink. A doctor
        that checks disk state alone would see the directory still
        exists with the original example files missing and report
        `incomplete`, even though the operator deliberately opted out.
        Reading the manifest as source-of-truth keeps the diagnosis
        consistent with operator intent.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            # Default install first so the next apply has something to
            # reconcile away under --backup.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            # Opt-out under --backup. The reconcile path stashes the
            # tracked artifacts as `.bak.<timestamp>` and unlinks the
            # originals, leaving the directory in a "backups only"
            # state that confuses any disk-only check.
            apply_result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--skip-tmux-ui",
                    "--skip-nagger",
                    "--backup",
                ]
            )
            self.assertEqual(0, apply_result)

            # Backup files must still be there (--backup contract); the
            # manifest must NOT track the .claude-nagger/* / tmux/* paths.
            self.assertTrue(
                list((project / ".claude-nagger").glob("*.bak.*")),
                msg="--backup did not leave any backup files",
            )
            self.assertFalse(
                (project / ".claude-nagger/config.yaml.example").exists()
            )
            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertFalse(
                any(p.startswith(".claude-nagger/") for p in tracked)
            )
            self.assertNotIn(".mozyo-bridge/tmux/agent-ui.conf", tracked)

            # Doctor must read manifest, see no nagger / no tmux UI
            # tracked, and report `skipped` for both. Overall doctor
            # must stay `ok` — opt-out is not a failure mode.
            _, output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            payload = json.loads(output)
            self.assertEqual(
                "skipped",
                payload["sections"]["claude_nagger"]["status"],
                msg=(
                    "doctor reported nagger status based on disk debris "
                    "instead of the manifest: "
                    f"{payload['sections']['claude_nagger']}"
                ),
            )
            self.assertEqual(
                "skipped",
                payload["sections"]["tmux"]["artifact"]["status"],
            )
            # The new manifest_tracks_* booleans expose the source-of-
            # truth signal so downstream tooling can rely on it too.
            self.assertFalse(
                payload["sections"]["claude_nagger"]["manifest_tracks_nagger"]
            )
            self.assertFalse(
                payload["sections"]["tmux"]["artifact"]["manifest_tracks_tmux_ui"]
            )

    def test_governed_doctor_reports_incomplete_for_real_drift(self) -> None:
        """Manifest tracks the artifact but it was removed → `incomplete`.

        Confirms that the manifest-driven doctor still catches genuine
        drift (operator deleted a tracked file by accident), not just
        the opt-out case. Without this assertion the move to manifest
        source-of-truth could silently swallow real failures.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            # Delete one tracked nagger example and the tmux snippet.
            (project / ".claude-nagger/config.yaml.example").unlink()
            (project / ".mozyo-bridge/tmux/agent-ui.conf").unlink()

            _, output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            payload = json.loads(output)
            self.assertEqual(
                "incomplete",
                payload["sections"]["claude_nagger"]["status"],
            )
            self.assertEqual(
                "incomplete",
                payload["sections"]["tmux"]["artifact"]["status"],
            )
            self.assertFalse(payload["ok"])  # overall non-ok per BAD set

    def test_governed_scaffold_skip_after_default_apply_removes_stale_artifacts(self) -> None:
        """Re-applying with `--skip-*` must clean up previously-installed files.

        Earlier behaviour dropped the entries from the new manifest but
        left the on-disk artifacts in place, so `scaffold status`
        falsely reported `clean` while `.claude-nagger/` and
        `.mozyo-bridge/tmux/` files still existed. The reconcile path
        compares the previous manifest's tracked set with the new
        render and treats the gap as outgoing files. `--backup` /
        `--force` gates the destructive removal, same as the
        overwrite path for routers and extras.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            # Default apply lays down both governed default-on bundles.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )
            for tracked_path in (
                ".mozyo-bridge/tmux/agent-ui.conf",
                ".claude-nagger/config.yaml.example",
            ):
                self.assertTrue((project / tracked_path).exists())

            # A bare re-apply with the opt-out flags must refuse to
            # remove existing files silently. This is the same
            # contract as overwriting routers without --backup/--force.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails-governed",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                            "--skip-tmux-ui",
                            "--skip-nagger",
                        ]
                    )
            err = stderr.getvalue()
            self.assertIn("refusing to overwrite existing scaffold files", err)
            self.assertIn(".claude-nagger/config.yaml.example", err)
            self.assertIn(".mozyo-bridge/tmux/agent-ui.conf", err)

            # With --backup, the removal proceeds and previously-tracked
            # artifacts are stashed to `.bak.<timestamp>` next to the
            # original paths before deletion. The backups stay on disk
            # so the directories may still exist, but the originals
            # themselves must be gone (otherwise the opt-out is a no-op).
            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--skip-tmux-ui",
                    "--skip-nagger",
                    "--backup",
                ]
            )
            self.assertEqual(0, result)
            self.assertFalse(
                (project / ".claude-nagger/config.yaml.example").exists()
            )
            self.assertFalse(
                (project / ".mozyo-bridge/tmux/agent-ui.conf").exists()
            )
            # Backups landed next to the original paths.
            self.assertTrue(
                list((project / ".claude-nagger").glob("config.yaml.example.bak.*"))
            )
            self.assertTrue(
                list((project / ".mozyo-bridge/tmux").glob("agent-ui.conf.bak.*"))
            )

            # Manifest reflects reality: only the kept categories.
            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertNotIn(".claude-nagger/config.yaml.example", tracked)
            self.assertNotIn(".mozyo-bridge/tmux/agent-ui.conf", tracked)
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", tracked)

            # And status is genuinely clean — not just nominally clean
            # while stale files remain.
            status_result, status_output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, status_result)
            self.assertIn("result: clean", status_output)

    def test_governed_tmux_snippet_evaluates_without_duplicate_status(self) -> None:
        """Sourcing the snippet under tmux must not duplicate status output.

        The earlier `set -wga` revision left the default fallback
        (`#I:#W`) in place when the codex-conditional line appended,
        producing strings like `0:codex#[fg=colour67]0:codex#[default]`
        on codex windows. The nested-conditional form must render
        exactly one branch per window name. Skip when no tmux binary
        is available on PATH (CI environments without tmux still keep
        the unit-level expectations green via the static checks above).
        """
        import shutil as _shutil
        import subprocess

        if _shutil.which("tmux") is None:
            self.skipTest("tmux binary not on PATH")

        snippet = (
            Path(__file__).resolve().parents[3]
            / "src/mozyo_bridge/scaffold/presets/redmine-rails-governed/files/.mozyo-bridge/tmux/agent-ui.conf"
        )
        self.assertTrue(snippet.exists(), msg=f"snippet missing: {snippet}")

        # Use a dedicated tmux socket so concurrent test runs / the
        # operator's real tmux server are not touched.
        socket = f"mozyo-audit-{os.getpid()}"
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

        def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["tmux", "-L", socket, *argv],
                capture_output=True,
                text=True,
                check=check,
                env=env,
            )

        # Best-effort cleanup of any prior server on the same socket.
        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            capture_output=True,
            text=True,
            env=env,
        )
        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "audit", "-n", "codex", "sleep 60")
            tmux("source-file", str(snippet))
            fmt = tmux("show-options", "-gqv", "window-status-format").stdout.strip()
            self.assertTrue(fmt, msg="window-status-format was empty after source-file")

            def render(window_name: str) -> str:
                tmux("rename-window", "-t", "audit:0", window_name)
                return tmux("display-message", "-p", "-t", f"audit:{window_name}", fmt).stdout.strip()

            codex = render("codex")
            other = render("other")
            claude = render("claude")
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                capture_output=True,
                text=True,
                env=env,
            )

        # No duplicated output: each rendered string contains the
        # window name exactly once. The earlier bug yielded
        # "0:codex#[fg=colour67]0:codex#[default]" — count == 2.
        self.assertEqual(1, codex.count(":codex"), msg=f"codex render duplicated: {codex!r}")
        self.assertEqual(1, claude.count(":claude"), msg=f"claude render duplicated: {claude!r}")
        self.assertEqual(1, other.count(":other"), msg=f"other render duplicated: {other!r}")

        # Agent windows carry the expected colour code; non-agent
        # windows render plain (no colour escape).
        self.assertIn("colour67", codex)
        self.assertIn("colour108", claude)
        self.assertNotIn("colour", other)

    def test_governed_tmux_snippet_declares_attention_marker(self) -> None:
        """Every distributed agent-ui.conf renders the #11954 attention projection.

        Static checks (no tmux needed, so CI stays green) that both
        packaged preset copies and the repo-local snippet read the
        `@mozyo_attention_*` pane user options and map each non-healthy
        attention state to a short uppercase label, with severity-driven
        colour as an auxiliary hint. `healthy` carries no label so healthy
        panes stay quiet, and the agent-name colours are left untouched.
        """
        repo_root = Path(__file__).resolve().parents[3]
        confs = [
            repo_root
            / "src/mozyo_bridge/scaffold/presets/redmine-governed/files/.mozyo-bridge/tmux/agent-ui.conf",
            repo_root
            / "src/mozyo_bridge/scaffold/presets/redmine-rails-governed/files/.mozyo-bridge/tmux/agent-ui.conf",
            repo_root / ".mozyo-bridge/tmux/agent-ui.conf",
        ]
        for conf in confs:
            text = conf.read_text(encoding="utf-8")
            self.assertIn("@mozyo_attention_state", text, msg=str(conf))
            self.assertIn("@mozyo_attention_severity", text, msg=str(conf))
            for label in ("OWNER", "REVIEW", "BLOCKED", "STALLED", "DONE", "RETIRE", "UNKNOWN"):
                self.assertIn(label, text, msg=f"{conf} missing {label}")
            # healthy is quiet by default: no HEALTHY label is ever rendered.
            self.assertNotIn("HEALTHY", text, msg=str(conf))
            # The agent-name colour distinction stays intact.
            self.assertIn("colour108", text, msg=str(conf))
            self.assertIn("colour67", text, msg=str(conf))

    def test_repo_local_tmux_snippet_matches_governed_preset(self) -> None:
        """The repo-local snippet stays byte-identical to the governed preset.

        The preset side is the source of truth for the distributed
        artifact; the scaffolded repo-local copy must not drift from it.
        """
        repo_root = Path(__file__).resolve().parents[3]
        preset = (
            repo_root
            / "src/mozyo_bridge/scaffold/presets/redmine-governed/files/.mozyo-bridge/tmux/agent-ui.conf"
        ).read_text(encoding="utf-8")
        local = (repo_root / ".mozyo-bridge/tmux/agent-ui.conf").read_text(encoding="utf-8")
        self.assertEqual(preset, local)

    def test_governed_tmux_snippet_renders_attention_marker_under_tmux(self) -> None:
        """Under real tmux the marker reflects the `@mozyo_attention_*` options.

        Sourcing the snippet and setting the pane user options must
        surface the derived state as a label suffix in the
        window-status format while leaving the agent-name colour intact.
        An unset / healthy state renders no marker, and an unrecognised
        state fails safe to UNKNOWN rather than looking healthy. Skipped
        when tmux is not on PATH (the static checks above keep CI green).
        """
        import shutil as _shutil
        import subprocess

        if _shutil.which("tmux") is None:
            self.skipTest("tmux binary not on PATH")

        snippet = Path(__file__).resolve().parents[3] / ".mozyo-bridge/tmux/agent-ui.conf"
        self.assertTrue(snippet.exists(), msg=f"snippet missing: {snippet}")

        socket = f"mozyo-attn-{os.getpid()}"
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

        def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["tmux", "-L", socket, *argv],
                capture_output=True,
                text=True,
                check=check,
                env=env,
            )

        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            capture_output=True,
            text=True,
            env=env,
        )
        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "attn", "-n", "codex", "sleep 60")
            tmux("source-file", str(snippet))
            fmt = tmux("show-options", "-gqv", "window-status-format").stdout.strip()
            self.assertTrue(fmt, msg="window-status-format was empty after source-file")

            def render() -> str:
                return tmux("display-message", "-p", "-t", "attn:codex", fmt).stdout.strip()

            def set_attention(state: str, severity: str = "normal") -> None:
                tmux("set-option", "-p", "-t", "attn:0", "@mozyo_attention_state", state)
                tmux("set-option", "-p", "-t", "attn:0", "@mozyo_attention_severity", severity)

            # Unset attention -> no marker, agent colour intact. The quiet
            # render is exactly the agent-name segment (no label suffix).
            quiet = "#[fg=colour67]0:codex#[default]"
            self.assertEqual(quiet, render())

            # review_waiting/notice -> REVIEW label; agent colour preserved.
            set_attention("review_waiting", "notice")
            review = render()
            self.assertIn("[REVIEW]", review, msg=review)
            self.assertIn("colour67", review)

            # blocked/critical -> BLOCKED label.
            set_attention("blocked", "critical")
            self.assertIn("[BLOCKED]", render())

            # Unrecognised state fails safe to UNKNOWN, never healthy-looking.
            set_attention("weird", "normal")
            self.assertIn("[UNKNOWN]", render())

            # healthy -> marker suppressed again so healthy panes stay quiet.
            set_attention("healthy", "normal")
            self.assertEqual(quiet, render())
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                capture_output=True,
                text=True,
                env=env,
            )

    def test_governed_tmux_snippet_declares_pane_level_attention_marker(self) -> None:
        """Every distributed agent-ui.conf projects attention at pane level (#11958).

        Static checks (no tmux needed, so CI stays green) that all three
        copies render the same `@mozyo_attention_*` projection on the
        tmux-native pane border for cockpit multi-pane visibility:

        * `pane-border-format` carries the label-bearing marker (so the
          signal survives without colour, as #11957 requires) and reuses
          the same uppercase state labels as the window marker;
        * a `window-layout-changed` hook toggles `pane-border-status` so
          single-pane windows keep their full height (the restraint cue);
        * the colour-only border styles are deliberately NOT bound to
          attention, so the active/inactive focus cue is left intact.
        """
        repo_root = Path(__file__).resolve().parents[3]
        confs = [
            repo_root
            / "src/mozyo_bridge/scaffold/presets/redmine-governed/files/.mozyo-bridge/tmux/agent-ui.conf",
            repo_root
            / "src/mozyo_bridge/scaffold/presets/redmine-rails-governed/files/.mozyo-bridge/tmux/agent-ui.conf",
            repo_root / ".mozyo-bridge/tmux/agent-ui.conf",
        ]
        for conf in confs:
            text = conf.read_text(encoding="utf-8")
            # The pane border carries the projection, keyed off the same
            # per-pane user options the window marker reads.
            self.assertIn("pane-border-format", text, msg=str(conf))
            self.assertIn("@mozyo_attention_state", text, msg=str(conf))
            self.assertIn("@mozyo_attention_severity", text, msg=str(conf))
            # The hook gates the border row by pane count (restraint).
            self.assertIn("window-layout-changed", text, msg=str(conf))
            self.assertIn("pane-border-status", text, msg=str(conf))
            self.assertIn("window_panes", text, msg=str(conf))
            # The hook is registered at a stable reserved array index so a
            # re-source replaces it in place instead of appending a
            # duplicate (the non-idempotent `set-hook -ga` bug, #11958
            # j#58616). The fix lives in the actual directives, so check
            # those rather than the prose comment (which names `-ga` while
            # explaining why it is avoided).
            hook_directives = [
                line.strip()
                for line in text.splitlines()
                if line.strip().startswith("set-hook") and not line.lstrip().startswith("#")
            ]
            self.assertTrue(hook_directives, msg=f"{conf} has no set-hook directive")
            for directive in hook_directives:
                self.assertIn("window-layout-changed[9000]", directive, msg=str(conf))
                self.assertNotIn("-ga", directive, msg=f"{conf} re-introduced non-idempotent -ga")
            # Same label vocabulary as the window marker; quiet when healthy.
            for label in ("OWNER", "REVIEW", "BLOCKED", "STALLED", "DONE", "RETIRE", "UNKNOWN"):
                self.assertIn(label, text, msg=f"{conf} missing {label}")
            self.assertNotIn("HEALTHY", text, msg=str(conf))
            # Border colour stays the focus cue: attention must not be wired
            # into the (colour-only) active/inactive border styles. They may
            # be named in the explanatory comment, but never actually set.
            directives = [
                line.strip()
                for line in text.splitlines()
                if line.strip().startswith("set") and not line.lstrip().startswith("#")
            ]
            for directive in directives:
                self.assertNotIn("pane-active-border-style", directive, msg=str(conf))
                self.assertNotIn("pane-border-style", directive, msg=str(conf))

    def test_governed_tmux_snippet_renders_pane_marker_under_tmux(self) -> None:
        """Under real tmux the pane border reflects per-pane `@mozyo_attention_*`.

        Sources the snippet into a window, splits it into two panes, and
        checks that each pane's border renders its own derived state: a
        quiet `index:command` border for unset/healthy panes, a colourful
        `[LABEL]` suffix otherwise, and a fail-safe `[UNKNOWN]` for an
        unrecognised state. Also checks the restraint contract — the
        border status row only appears once a window has more than one
        pane and disappears again when it drops back to one, so ordinary
        single-pane agent windows keep their full height. Skipped when
        tmux is not on PATH (the static checks above keep CI green).
        """
        import shutil as _shutil
        import subprocess

        if _shutil.which("tmux") is None:
            self.skipTest("tmux binary not on PATH")

        snippet = Path(__file__).resolve().parents[3] / ".mozyo-bridge/tmux/agent-ui.conf"
        self.assertTrue(snippet.exists(), msg=f"snippet missing: {snippet}")

        socket = f"mozyo-pane-{os.getpid()}"
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

        def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["tmux", "-L", socket, *argv],
                capture_output=True,
                text=True,
                check=check,
                env=env,
            )

        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            capture_output=True,
            text=True,
            env=env,
        )
        try:
            tmux(
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "cp",
                "-n",
                "cockpit",
                "-x",
                "80",
                "-y",
                "24",
                "sleep 60",
            )
            tmux("source-file", str(snippet))

            # Single pane: the layout hook keeps the border row off, so the
            # pane keeps the full window height (restraint for ordinary
            # single-pane agent windows).
            self.assertEqual(
                "24",
                tmux("display-message", "-p", "-t", "cp:cockpit", "#{pane_height}").stdout.strip(),
            )

            tmux("split-window", "-t", "cp:cockpit", "-d", "-v", "sleep 60")

            # Two panes -> the hook enables the border status row.
            self.assertEqual(
                "top",
                tmux("show-options", "-wqv", "-t", "cp:cockpit", "pane-border-status").stdout.strip(),
            )

            fmt = tmux("show-options", "-gqv", "pane-border-format").stdout.strip()
            self.assertTrue(fmt, msg="pane-border-format was empty after source-file")

            def render(pane: str) -> str:
                return tmux("display-message", "-p", "-t", pane, fmt).stdout.strip()

            def set_attention(pane: str, state: str, severity: str = "normal") -> None:
                tmux("set-option", "-p", "-t", pane, "@mozyo_attention_state", state)
                tmux("set-option", "-p", "-t", pane, "@mozyo_attention_severity", severity)

            # Unset pane -> quiet identity border, no label.
            self.assertNotIn("[", render("cp:cockpit.0"))
            self.assertIn("0:", render("cp:cockpit.0"))

            # An inactive pane carrying a blocked/critical state surfaces a
            # red [BLOCKED] label so its stopped state is visible even
            # though it is not the active pane.
            set_attention("cp:cockpit.1", "blocked", "critical")
            blocked = render("cp:cockpit.1")
            self.assertIn("[BLOCKED]", blocked, msg=blocked)
            self.assertIn("colour203", blocked, msg=blocked)

            # owner_waiting/warning -> amber [OWNER]; per-pane, independent
            # of the other pane.
            set_attention("cp:cockpit.0", "owner_waiting", "warning")
            owner = render("cp:cockpit.0")
            self.assertIn("[OWNER]", owner, msg=owner)
            self.assertIn("colour179", owner, msg=owner)

            # Unrecognised state fails safe to UNKNOWN, never healthy-looking.
            set_attention("cp:cockpit.0", "weird", "normal")
            self.assertIn("[UNKNOWN]", render("cp:cockpit.0"))

            # healthy -> marker suppressed so healthy panes stay quiet.
            set_attention("cp:cockpit.0", "healthy", "normal")
            self.assertNotIn("[", render("cp:cockpit.0"))

            # Drop back to one pane: the hook turns the border row off again
            # and the surviving pane reclaims the full height.
            tmux("kill-pane", "-t", "cp:cockpit.1")
            self.assertEqual(
                "off",
                tmux("show-options", "-wqv", "-t", "cp:cockpit", "pane-border-status").stdout.strip(),
            )
            self.assertEqual(
                "24",
                tmux("display-message", "-p", "-t", "cp:cockpit", "#{pane_height}").stdout.strip(),
            )
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                capture_output=True,
                text=True,
                env=env,
            )

    def test_governed_tmux_snippet_layout_hook_is_idempotent_under_tmux(self) -> None:
        """Re-sourcing the snippet does not pile up duplicate layout hooks (#11958).

        Regression test for the j#58616 Major finding: `set-hook -ga`
        appends a fresh `window-layout-changed` entry on every source, so
        a reusable snippet that operators re-source (config reloads,
        repeated `source-file`) would accumulate duplicates. The fix
        registers the hook at a stable reserved array index, so sourcing
        the file twice leaves exactly one mozyo entry while an unrelated
        operator hook at another index survives. Skipped when tmux is not
        on PATH (the static check above keeps CI green).
        """
        import shutil as _shutil
        import subprocess

        if _shutil.which("tmux") is None:
            self.skipTest("tmux binary not on PATH")

        snippet = Path(__file__).resolve().parents[3] / ".mozyo-bridge/tmux/agent-ui.conf"
        self.assertTrue(snippet.exists(), msg=f"snippet missing: {snippet}")

        socket = f"mozyo-hook-{os.getpid()}"
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)

        def tmux(*argv: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(
                ["tmux", "-L", socket, *argv],
                capture_output=True,
                text=True,
                check=check,
                env=env,
            )

        subprocess.run(
            ["tmux", "-L", socket, "kill-server"],
            capture_output=True,
            text=True,
            env=env,
        )
        try:
            tmux("-f", "/dev/null", "new-session", "-d", "-s", "hk", "-n", "cockpit", "sleep 60")
            # An operator already has their own layout hook at the default
            # index; it must survive our (re-)source untouched.
            tmux("set-hook", "-ga", "window-layout-changed", "display-message 'operator hook'")

            # Source the snippet twice to simulate a config reload.
            tmux("source-file", str(snippet))
            tmux("source-file", str(snippet))

            entries = [
                line.strip()
                for line in tmux("show-hooks", "-gw").stdout.splitlines()
                if line.strip().startswith("window-layout-changed[")
            ]
            # Exactly one mozyo entry (no duplicate from the second source)
            # plus the preserved operator entry.
            mozyo_entries = [e for e in entries if "pane-border-status" in e]
            operator_entries = [e for e in entries if "operator hook" in e]
            self.assertEqual(1, len(mozyo_entries), msg=f"duplicate mozyo hooks: {entries}")
            self.assertIn("window-layout-changed[9000]", mozyo_entries[0], msg=mozyo_entries[0])
            self.assertEqual(1, len(operator_entries), msg=f"operator hook lost/dup: {entries}")

            # The hook still functions after the dedup: border row on with
            # >1 pane, off again at one.
            tmux("split-window", "-t", "hk:cockpit", "-d", "-v", "sleep 60")
            self.assertEqual(
                "top",
                tmux("show-options", "-wqv", "-t", "hk:cockpit", "pane-border-status").stdout.strip(),
            )
            tmux("kill-pane", "-t", "hk:cockpit.1")
            self.assertEqual(
                "off",
                tmux("show-options", "-wqv", "-t", "hk:cockpit", "pane-border-status").stdout.strip(),
            )
        finally:
            subprocess.run(
                ["tmux", "-L", socket, "kill-server"],
                capture_output=True,
                text=True,
                env=env,
            )

    def test_governed_scaffold_skip_flags_omit_artifacts_and_manifest_entries(self) -> None:
        """`--skip-tmux-ui` / `--skip-nagger` opt-outs drop the category.

        The dropped category neither lands on disk nor appears in the
        manifest, so a clean `scaffold status` afterwards confirms the
        opt-out is consistent (no drift detected from the missing files
        because the manifest never claimed them).
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--skip-tmux-ui",
                    "--skip-nagger",
                ]
            )
            self.assertEqual(0, result)

            # Skipped artifacts never landed.
            self.assertFalse(
                (project / ".mozyo-bridge/tmux/agent-ui.conf").exists()
            )
            self.assertFalse((project / ".claude-nagger").exists())

            # Non-skipped artifacts still ship — opt-outs are scoped.
            self.assertTrue(
                (project / ".mozyo-bridge/rules/llm_rule_authoring.md").exists()
            )
            self.assertTrue(
                (project / ".mozyo-bridge/rules/docs_catalog_governance.yaml").exists()
            )

            # Manifest tracks only what was written; status stays clean
            # because the manifest never claimed the skipped files.
            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertNotIn(".mozyo-bridge/tmux/agent-ui.conf", tracked)
            for nagger in (
                ".claude-nagger/config.yaml.example",
                ".claude-nagger/command_conventions.yaml.example",
                ".claude-nagger/mcp_conventions.yaml.example",
                ".claude-nagger/.gitignore",
            ):
                self.assertNotIn(nagger, tracked)
            # Non-skipped categories still tracked.
            self.assertIn(".mozyo-bridge/rules/llm_rule_authoring.md", tracked)

            status_result, status_output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, status_result)
            self.assertIn("result: clean", status_output)

    def test_governed_doctor_reports_nagger_and_tmux_ui_artifact_state(self) -> None:
        """`doctor` surfaces tmux-ui + Claude Nagger artifact state.

        After a default `scaffold apply`, both the new `claude_nagger`
        section and the artifact attachment on the `tmux` section show
        the skeleton + snippet as present.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            result, output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            payload = json.loads(output)
            sections = payload["sections"]
            self.assertIn("claude_nagger", sections)
            nagger = sections["claude_nagger"]
            # Skeleton landed but config.yaml is not yet copied — the
            # default-on apply puts the example next to the target's
            # config slot.
            self.assertEqual("skeleton-only", nagger["status"])
            for name in (
                "config.yaml.example",
                "command_conventions.yaml.example",
                "mcp_conventions.yaml.example",
            ):
                self.assertTrue(
                    nagger["examples"][name]["present"],
                    msg=f"doctor did not see {name}",
                )
            self.assertFalse(nagger["config_yaml"]["present"])

            tmux = sections["tmux"]
            self.assertIn("artifact", tmux)
            self.assertTrue(tmux["artifact"]["present"])
            self.assertEqual("ok", tmux["artifact"]["status"])

            # And after opt-out, doctor reports `skipped`.
            project_skip = Path(tmp) / "project_skip"
            project_skip.mkdir()
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project_skip),
                    "--home",
                    str(home),
                    "--skip-nagger",
                    "--skip-tmux-ui",
                ]
            )
            _, skip_output = self.run_cli(
                [
                    "doctor",
                    "--target",
                    str(project_skip),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            skip_payload = json.loads(skip_output)
            self.assertEqual(
                "skipped", skip_payload["sections"]["claude_nagger"]["status"]
            )
            self.assertEqual(
                "skipped",
                skip_payload["sections"]["tmux"]["artifact"]["status"],
            )

    def test_governed_scaffold_reconciles_legacy_governed_artifacts_on_reapply(self) -> None:
        """Re-apply with the new preset must clean up legacy governed artifacts.

        Prior governed-scaffold releases vendor-copied
        ``.mozyo-bridge/tools/*.py`` and a separate
        ``.mozyo-bridge/rules/development_flow.md`` into the target and
        recorded those paths in the scaffold manifest. The new preset
        does not ship those files, so the next `scaffold apply` must
        reconcile them as outgoing files: refuse to overwrite silently,
        then remove them when ``--backup`` (or ``--force``) is provided.
        This guards the upgrade path for existing operators.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            # First, run a real apply to lay down routers, manifest,
            # and the rule/doc files we will keep.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            # Simulate a legacy scaffold state by writing tool source
            # files and patching the manifest to claim them. This is
            # exactly what an older governed release left behind.
            tools_dir = project / ".mozyo-bridge/tools"
            tools_dir.mkdir(parents=True, exist_ok=True)
            legacy_dev_flow = project / ".mozyo-bridge/rules/development_flow.md"
            legacy_files = {
                legacy_dev_flow: "# legacy development flow\n",
                tools_dir / "docs_catalog.py": "# legacy vendor copy\n",
                tools_dir / "validate_catalog.py": "# legacy vendor copy\n",
                tools_dir / "resolve_audit_docs.py": "# legacy vendor copy\n",
                tools_dir / "generate_file_conventions.py": "# legacy vendor copy\n",
                tools_dir / "audit_doc_impact.py": "# legacy vendor copy\n",
            }
            for path, body in legacy_files.items():
                path.write_text(body, encoding="utf-8")

            manifest_path = project / ".mozyo-bridge/scaffold.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for path in legacy_files:
                rel = path.relative_to(project).as_posix()
                manifest["files"][rel] = {
                    "sha256": "0" * 64,  # arbitrary; reconcile only consults the key set
                }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )

            # Re-applying without --backup / --force must refuse: the
            # outgoing legacy files would be silently destroyed otherwise.
            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails-governed",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )
            err = stderr.getvalue()
            self.assertIn("refusing to overwrite existing scaffold files", err)
            self.assertIn(".mozyo-bridge/rules/development_flow.md", err)
            self.assertIn(".mozyo-bridge/tools/validate_catalog.py", err)

            # With --backup, the reconcile path stashes each legacy tool
            # to `.bak.<timestamp>` and removes the original. The new
            # manifest no longer tracks them.
            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails-governed",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )
            self.assertEqual(0, result)
            for path in legacy_files:
                self.assertFalse(path.exists(), msg=f"legacy tool not removed: {path}")
            # And `.bak.<timestamp>` files landed next to where the
            # originals lived.
            backups = list(tools_dir.glob("*.bak.*")) if tools_dir.exists() else []
            self.assertTrue(backups, msg="--backup did not stash any legacy tool files")

            state = scaffold_state(project)
            assert state is not None
            tracked = set(state["files"].keys())
            self.assertFalse(
                any(p.startswith(".mozyo-bridge/tools/") for p in tracked),
                msg=(
                    "post-reconcile manifest still references legacy "
                    f".mozyo-bridge/tools/ entries: "
                    f"{[p for p in tracked if p.startswith('.mozyo-bridge/tools/')]}"
                ),
            )
            self.assertNotIn(".mozyo-bridge/rules/development_flow.md", tracked)

            # scaffold status reports clean after reconcile.
            status_result, status_output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, status_result)
            self.assertIn("result: clean", status_output)

    def test_governed_preset_artifacts_ship_in_built_wheel(self) -> None:
        """The governed preset's repo-local artifacts must end up in the wheel.

        setuptools' glob for `package-data` skips hidden directories by
        default, so the package-data spec must enumerate the `.mozyo-bridge/`
        subtree explicitly. We build a real wheel via `python -m build`
        and assert every shipped artifact ends up inside the wheel; this
        guards against regressions where the source tree builds locally
        but the wheel released to PyPI is silently missing the governance
        package.
        """
        import subprocess
        import zipfile

        repo_root = Path(__file__).resolve().parents[3]
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "dist"
            out_dir.mkdir()
            build_proc = subprocess.run(
                [sys.executable, "-m", "build", "--wheel", "--outdir", str(out_dir)],
                cwd=repo_root,
                capture_output=True,
                text=True,
            )
            if build_proc.returncode != 0:
                self.skipTest(
                    "python -m build failed (probably missing build backend deps); "
                    f"stderr={build_proc.stderr[:500]}"
                )
            wheels = list(out_dir.glob("mozyo_bridge-*.whl"))
            self.assertEqual(
                1,
                len(wheels),
                msg=f"expected exactly one wheel under {out_dir}, found {wheels}",
            )

            with zipfile.ZipFile(wheels[0]) as wheel:
                names = set(wheel.namelist())

            governed_prefix = (
                "mozyo_bridge/scaffold/presets/redmine-rails-governed/"
            )
            mb_prefix = governed_prefix + "files/.mozyo-bridge/"
            nagger_prefix = governed_prefix + "files/.claude-nagger/"
            expected = [
                governed_prefix + "VERSION",
                governed_prefix + "agent-workflow.md",
                mb_prefix + "rules/llm_rule_authoring.md",
                mb_prefix + "rules/docs_catalog_governance.yaml",
                mb_prefix + "docs/catalog.yaml.example",
                mb_prefix + "tmux/agent-ui.conf",
                nagger_prefix + "config.yaml.example",
                nagger_prefix + "command_conventions.yaml.example",
                nagger_prefix + "mcp_conventions.yaml.example",
                nagger_prefix + ".gitignore",
            ]
            missing = [entry for entry in expected if entry not in names]
            self.assertEqual(
                [],
                missing,
                msg=(
                    "wheel is missing governed preset artifacts (release would ship "
                    "an empty governance package):\n  " + "\n  ".join(missing)
                ),
            )
            # Docs catalog tooling lives in mozyo_bridge.docs_tools now,
            # not in the preset's `files/` tree. The wheel must NOT ship
            # any vendor-copied tools under that prefix anymore.
            legacy_tools = [
                name for name in names if mb_prefix + "tools/" in name
            ]
            self.assertEqual(
                [],
                legacy_tools,
                msg=(
                    "wheel still carries vendor-copied .mozyo-bridge/tools/ "
                    f"entries: {legacy_tools}"
                ),
            )
            # And the docs_tools package itself must ship.
            docs_tools_prefix = "mozyo_bridge/docs_tools/"
            for expected_module in (
                "__init__.py",
                "catalog.py",
                "validate.py",
                "resolve.py",
                "generate.py",
                "impact.py",
            ):
                self.assertIn(
                    docs_tools_prefix + expected_module,
                    names,
                    msg=(
                        f"wheel is missing the docs_tools module "
                        f"`{expected_module}` — the docs CLI cannot run."
                    ),
                )

    def test_scaffold_requires_installed_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home)])

            self.assertIn("rules preset is not installed", stderr.getvalue())
            self.assertFalse((project / "AGENTS.md").exists())

    def test_scaffold_without_target_writes_to_current_working_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            parent = Path(tmp) / "parent"
            nested = parent / "nested"
            nested.mkdir(parents=True)
            (parent / "pyproject.toml").write_text("[project]\nname = \"parent\"\n", encoding="utf-8")
            self.run_cli(["rules", "install", "--home", str(home)])
            cwd = Path.cwd()
            try:
                os.chdir(nested)

                result, output = self.run_cli(["scaffold", "apply", "asana", "--home", str(home)])

                self.assertEqual(0, result)
                self.assertIn(str(nested / "AGENTS.md"), output)
                self.assertTrue((nested / "AGENTS.md").exists())
                self.assertTrue((nested / "CLAUDE.md").exists())
                self.assertFalse((parent / "AGENTS.md").exists())
            finally:
                os.chdir(cwd)

    def test_scaffold_without_target_ignores_mozyo_repo_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            env_repo = Path(tmp) / "env-repo"
            cwd_project = Path(tmp) / "cwd-project"
            env_repo.mkdir()
            cwd_project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            cwd = Path.cwd()
            try:
                os.chdir(cwd_project)
                with patch.dict(os.environ, {"MOZYO_REPO": str(env_repo)}):
                    result, _ = self.run_cli(["scaffold", "apply", "none", "--home", str(home)])

                self.assertEqual(0, result)
                self.assertTrue((cwd_project / "AGENTS.md").exists())
                self.assertFalse((env_repo / "AGENTS.md").exists())
            finally:
                os.chdir(cwd)

    def test_rules_status_reports_installed_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"

            self.run_cli(["rules", "install", "--home", str(home)])

            result, output = self.run_cli(["rules", "status", "--home", str(home)])
            rows = rules_status(home)

            self.assertEqual(0, result)
            self.assertIn("PRESET\tSTATUS\tINSTALLED\tPACKAGED\tPATH", output)
            self.assertEqual(["ok"] * len(rows), [row["status"] for row in rows])
            self.assertIn(f"asana\tok\t{package_version('asana')}\t{package_version('asana')}\t", output)
            self.assertIn(
                f"redmine-rails\tok\t{package_version('redmine-rails')}\t{package_version('redmine-rails')}\t",
                output,
            )
            self.assertIn(
                f"redmine-governed\tok\t{package_version('redmine-governed')}\t"
                f"{package_version('redmine-governed')}\t",
                output,
            )
            self.assertIn(
                f"redmine-rails-governed\tok\t{package_version('redmine-rails-governed')}\t"
                f"{package_version('redmine-rails-governed')}\t",
                output,
            )
            self.assertIn(str(home / "rules" / "presets" / "asana" / "agent-workflow.md"), output)

    def test_rules_status_reports_missing_and_outdated_presets(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"

            self.run_cli(["rules", "install", "--home", str(home)])
            (home / "rules" / "presets" / "redmine" / "agent-workflow.md").unlink()
            (home / "rules" / "presets" / "none" / "VERSION").write_text("0.0.0\n", encoding="utf-8")

            result, output = self.run_cli(["rules", "status", "--home", str(home)])
            rows = {row["preset"]: row for row in rules_status(home)}

            self.assertEqual(1, result)
            self.assertEqual("ok", rows["asana"]["status"])
            self.assertEqual("missing", rows["redmine"]["status"])
            self.assertEqual("-", rows["redmine"]["installed"])
            self.assertEqual("outdated", rows["none"]["status"])
            self.assertEqual("0.0.0", rows["none"]["installed"])
            self.assertIn(f"redmine\tmissing\t-\t{package_version('redmine')}\t", output)
            self.assertIn(f"none\toutdated\t0.0.0\t{package_version('none')}\t", output)

    def test_rules_home_default_prints_portable_expression_only(self) -> None:
        # Spoof env values via a tempdir so the fixture itself never carries
        # a literal personal-home-shaped path (the release tree scanner
        # rejects `/Users/<name>/` in tracked source). The assertions still
        # prove the default output cannot leak the env override or HOME.
        with tempfile.TemporaryDirectory() as tmp:
            spoofed_home = Path(tmp) / "fake-home"
            spoofed_home.mkdir()
            spoofed_override = Path(tmp) / "mozyo-bridge-override"
            with patch.dict(
                os.environ,
                {"HOME": str(spoofed_home), "MOZYO_BRIDGE_HOME": str(spoofed_override)},
            ):
                result, output = self.run_cli(["rules", "home"])

            self.assertEqual(0, result)
            self.assertEqual("${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}\n", output)
            self.assertNotIn(str(spoofed_home), output)
            self.assertNotIn(str(spoofed_override), output)
            self.assertNotIn(str(Path.home()), output)

    def test_rules_home_resolved_honors_env_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            override = Path(tmp) / "custom_home"
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(override)}):
                result, output = self.run_cli(["rules", "home", "--resolved"])

            self.assertEqual(0, result)
            self.assertEqual(f"{override.resolve()}\n", output)

    def test_rules_home_resolved_expands_tilde_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp) / "fake_home"
            fake_home.mkdir()
            env = {"HOME": str(fake_home)}
            env_clear = {"MOZYO_BRIDGE_HOME": ""}
            with patch.dict(os.environ, env), patch.dict(os.environ, env_clear, clear=False):
                os.environ.pop("MOZYO_BRIDGE_HOME", None)
                result, output = self.run_cli(["rules", "home", "--resolved"])

            self.assertEqual(0, result)
            self.assertEqual(f"{(fake_home / '.mozyo_bridge').resolve()}\n", output)

    def test_rules_home_help_text_distinguishes_portable_and_resolved(self) -> None:
        parser = build_parser()
        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            with self.assertRaises(SystemExit):
                parser.parse_args(["rules", "home", "--help"])
        help_text = stdout.getvalue()

        # argparse wraps long descriptions; normalize whitespace before
        # checking that the portable-vs-resolved distinction is documented.
        flat = " ".join(help_text.split())
        self.assertIn("${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}", flat)
        self.assertIn("committed docs", flat)
        self.assertIn("--resolved", flat)
        self.assertIn("local diagnostics", flat)

    def test_scaffold_refuses_overwrite_by_default_and_dry_run_writes_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(["scaffold", "apply", "none", "--target", str(project), "--home", str(home)])

            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self.run_cli(["scaffold", "apply", "none", "--target", str(project), "--home", str(home)])

            fresh = Path(tmp) / "fresh"
            fresh.mkdir()
            result, output = self.run_cli(
                ["scaffold", "apply", "none", "--target", str(fresh), "--home", str(home), "--dry-run"]
            )

            self.assertEqual(0, result)
            self.assertIn("would write", output)
            self.assertFalse((fresh / "AGENTS.md").exists())

    def test_scaffold_backup_replaces_existing_router(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            (project / "AGENTS.md").write_text("old agents\n", encoding="utf-8")
            (project / "CLAUDE.md").write_text("old claude\n", encoding="utf-8")
            self.run_cli(["rules", "install", "--home", str(home)])

            result, _ = self.run_cli(["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home), "--backup"])

            self.assertEqual(0, result)
            self.assertIn("active な `Redmine issue / journal`", (project / "AGENTS.md").read_text(encoding="utf-8"))
            self.assertTrue(list(project.glob("AGENTS.md.bak.*")))
            self.assertTrue(list(project.glob("CLAUDE.md.bak.*")))

    def test_relative_home_does_not_leak_into_router_or_manifest(self) -> None:
        # Even when --home resolves to a host-specific absolute path, the
        # generated router and the scaffold manifest must record the portable
        # ${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge} symbolic form. This guards
        # against personal-home leakage in committed artifacts.
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path.cwd()
            try:
                os.chdir(tmp)
                project = Path(tmp) / "project"
                project.mkdir()
                self.run_cli(["rules", "install", "--home", "home"])

                self.run_cli(["scaffold", "apply", "asana", "--target", str(project), "--home", "home"])

                state = scaffold_state(project)
                self.assertIsNotNone(state)
                assert state is not None
                self.assertEqual(
                    "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                    state["rule_path"],
                )

                resolved_home = (Path(tmp) / "home").resolve()
                for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                    text = (project / filename).read_text(encoding="utf-8")
                    self.assertNotIn(str(resolved_home), text)
                    self.assertIn(
                        "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                        text,
                    )
            finally:
                os.chdir(cwd)

    def test_scaffold_does_not_leak_home_path_for_any_preset(self) -> None:
        # Fresh scaffold for every supported preset must avoid leaking the
        # resolved host home path into AGENTS.md, CLAUDE.md, or the manifest,
        # and must instead reference the portable symbolic form. The MOZYO_BRIDGE_HOME
        # override semantics are preserved because consumers expand the env var
        # when they read the router, not when the router is generated.
        from mozyo_bridge.scaffold.rules import PRESETS

        for preset in PRESETS:
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    home = Path(tmp) / "home"
                    project = Path(tmp) / "project"
                    project.mkdir()

                    self.run_cli(["rules", "install", "--home", str(home)])
                    result, _ = self.run_cli(
                        ["scaffold", "apply", preset, "--target", str(project), "--home", str(home)]
                    )
                    self.assertEqual(0, result)

                    resolved_home = home.resolve()
                    expected_rule_path = (
                        f"${{MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}}/rules/presets/{preset}/agent-workflow.md"
                    )
                    for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                        text = (project / filename).read_text(encoding="utf-8")
                        self.assertNotIn("/Users/", text)
                        self.assertNotIn(str(resolved_home), text)
                        self.assertIn(expected_rule_path, text)

                    state = scaffold_state(project)
                    self.assertIsNotNone(state)
                    assert state is not None
                    self.assertEqual(expected_rule_path, state["rule_path"])

                    status_result, status_output = self.run_cli(
                        ["scaffold", "status", "--target", str(project), "--home", str(home)]
                    )
                    self.assertEqual(0, status_result)
                    self.assertIn("clean", status_output)


class ScaffoldRepoLocalModeTest(unittest.TestCase):
    """Repo-local guardrail rules mode for Dev Container / ephemeral-home workspaces.

    Asana task 1214948474095217. Covers `rules install --repo-local`,
    `rules status --repo-local`, `scaffold apply --repo-local`, `scaffold diff
    --repo-local`, the auto-detecting `scaffold status` path, the manifest
    `mode` field, the repo-local portable `rule_path`, the host-path leak
    guard for repo-local artifacts, and the `--home` / `--repo-local`
    mutual exclusion.
    """

    REPO_LOCAL_RULE_PATH_TEMPLATE = (
        ".mozyo-bridge/rules/presets/{preset}/agent-workflow.md"
    )

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_rules_install_repo_local_writes_into_target_repo(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            result, output = self.run_cli(
                ["rules", "install", "--repo-local", str(project)]
            )

            self.assertEqual(0, result)
            from mozyo_bridge.scaffold.rules import PRESETS

            for preset in PRESETS:
                workflow = (
                    project
                    / ".mozyo-bridge"
                    / "rules"
                    / "presets"
                    / preset
                    / "agent-workflow.md"
                )
                version = (
                    project / ".mozyo-bridge" / "rules" / "presets" / preset / "VERSION"
                )
                self.assertTrue(workflow.exists(), f"missing workflow for {preset}")
                self.assertTrue(version.exists(), f"missing VERSION for {preset}")
                self.assertIn(str(workflow), output)

    def test_rules_status_repo_local_reports_target_repo_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])

            result, output = self.run_cli(
                ["rules", "status", "--repo-local", str(project)]
            )

            self.assertEqual(0, result)
            self.assertIn("asana\tok", output)
            self.assertIn(
                str(project.resolve() / ".mozyo-bridge" / "rules" / "presets" / "asana"),
                output,
            )

    def test_rules_status_repo_local_flags_uninstalled_store(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()

            result, output = self.run_cli(
                ["rules", "status", "--repo-local", str(project)]
            )

            self.assertEqual(1, result)
            self.assertIn("asana\tmissing", output)

    def test_rules_install_repo_local_and_home_are_mutually_exclusive(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    ["rules", "install", "--home", "/tmp/x", "--repo-local", "/tmp/y"]
                )

    def test_scaffold_apply_repo_local_uses_relative_rule_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])

            result, output = self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )

            self.assertEqual(0, result)
            expected_rule_path = self.REPO_LOCAL_RULE_PATH_TEMPLATE.format(preset="asana")
            for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                text = (project / filename).read_text(encoding="utf-8")
                self.assertIn(expected_rule_path, text)
                # The portable repo-local form must not carry the central
                # ${MOZYO_BRIDGE_HOME:...} expansion — Dev Container users
                # have no such home to resolve against.
                self.assertNotIn("${MOZYO_BRIDGE_HOME", text)

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("repo-local", state["mode"])
            self.assertEqual(expected_rule_path, state["rule_path"])
            self.assertEqual(2, state["schema_version"])

    def test_scaffold_apply_repo_local_does_not_leak_host_paths_for_any_preset(self) -> None:
        # Repo-local artifacts must never carry an absolute host path. The
        # whole point of the Dev Container mode is portability across hosts.
        from mozyo_bridge.scaffold.rules import PRESETS

        for preset in PRESETS:
            with self.subTest(preset=preset):
                with tempfile.TemporaryDirectory() as tmp:
                    project = Path(tmp) / "project"
                    project.mkdir()
                    self.run_cli(["rules", "install", "--repo-local", str(project)])

                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            preset,
                            "--target",
                            str(project),
                            "--repo-local",
                        ]
                    )

                    expected_rule_path = self.REPO_LOCAL_RULE_PATH_TEMPLATE.format(
                        preset=preset
                    )
                    resolved_project = project.resolve()
                    for filename in ("AGENTS.md", "CLAUDE.md", ".mozyo-bridge/scaffold.json"):
                        text = (project / filename).read_text(encoding="utf-8")
                        self.assertNotIn("/Users/", text)
                        self.assertNotIn(str(resolved_project), text)
                        self.assertIn(expected_rule_path, text)

    def test_scaffold_apply_repo_local_rejects_combined_home_flag(self) -> None:
        parser = build_parser()

        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args(
                    [
                        "scaffold",
                        "apply",
                        "asana",
                        "--target",
                        "/tmp/x",
                        "--home",
                        "/tmp/y",
                        "--repo-local",
                    ]
                )

    def test_scaffold_diff_repo_local_clean_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--repo-local"]
            )

            self.assertEqual(0, result)
            self.assertIn("scaffold diff: clean", output)

    def test_scaffold_diff_repo_local_detects_router_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )
            agents = project / "AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nlocal hand edit\n",
                encoding="utf-8",
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--repo-local"]
            )

            self.assertEqual(1, result)
            self.assertIn("local hand edit", output)

    def test_scaffold_status_auto_detects_repo_local_mode(self) -> None:
        # Status takes no --repo-local flag; the manifest's `mode` field is
        # the source of truth so a single status command works for either
        # mode without operator bookkeeping.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "redmine", "--target", str(project), "--repo-local"]
            )

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project)]
            )

            self.assertEqual(0, result)
            self.assertIn("mode: repo-local", output)
            self.assertIn("result: clean", output)

    def test_scaffold_status_repo_local_manifest_with_home_flag_is_invalid(self) -> None:
        # Passing --home against a repo-local manifest is operator error;
        # status surfaces it as an invalid manifest rather than silently
        # comparing against the wrong store.
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp) / "project"
            project.mkdir()
            unused_home = Path(tmp) / "unused-home"
            self.run_cli(["rules", "install", "--repo-local", str(project)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--repo-local"]
            )

            result, output = self.run_cli(
                [
                    "scaffold",
                    "status",
                    "--target",
                    str(project),
                    "--home",
                    str(unused_home),
                ]
            )

            self.assertEqual(1, result)
            self.assertIn("repo-local mode; --home is unused", output)

    def test_scaffold_apply_repo_local_requires_repo_local_rules_install(self) -> None:
        # The repo-local store is read from <target>/.mozyo-bridge, so a
        # central-mode `rules install` does NOT satisfy `scaffold apply
        # --repo-local`. The error must point operators at the repo-local
        # install command, not the central one.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "asana",
                            "--target",
                            str(project),
                            "--repo-local",
                        ]
                    )

            err_text = stderr.getvalue()
            self.assertIn("rules preset is not installed", err_text)
            self.assertIn("--repo-local", err_text)

    def test_scaffold_apply_central_mode_default_remains_unchanged(self) -> None:
        # Backward compatibility: without --repo-local, scaffold apply must
        # still emit the central ${MOZYO_BRIDGE_HOME:...} portable form and
        # manifest mode "central". Default behavior is the load-bearing
        # contract for existing users.
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--home", str(home)]
            )

            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual("central", state["mode"])
            self.assertEqual(
                "${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/asana/agent-workflow.md",
                state["rule_path"],
            )
            for filename in ("AGENTS.md", "CLAUDE.md"):
                text = (project / filename).read_text(encoding="utf-8")
                self.assertIn("${MOZYO_BRIDGE_HOME", text)


class ScaffoldDiffTest(unittest.TestCase):
    """Coverage for the new `scaffold diff <preset>` breaking-change entrypoint."""

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def test_diff_detects_unapplied_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(1, result)
            self.assertIn("+++ b/AGENTS.md", output)
            self.assertIn("+++ b/CLAUDE.md", output)
            self.assertIn("+++ b/.mozyo-bridge/scaffold.json", output)
            self.assertFalse((project / "AGENTS.md").exists())
            self.assertFalse((project / "CLAUDE.md").exists())
            self.assertFalse((project / ".mozyo-bridge" / "scaffold.json").exists())

    def test_diff_is_clean_after_apply(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "redmine", "--target", str(project), "--home", str(home)]
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "redmine", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            self.assertIn("scaffold diff: clean", output)
            self.assertNotIn("--- a/", output)

    def test_diff_detects_local_edit_against_rendered_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "asana", "--target", str(project), "--home", str(home)]
            )

            agents = project / "AGENTS.md"
            agents.write_text(
                agents.read_text(encoding="utf-8") + "\nlocal hand edit\n",
                encoding="utf-8",
            )

            result, output = self.run_cli(
                ["scaffold", "diff", "asana", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(1, result)
            self.assertIn("--- a/AGENTS.md", output)
            self.assertIn("+++ b/AGENTS.md", output)
            self.assertIn("local hand edit", output)

    def test_diff_requires_installed_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        ["scaffold", "diff", "asana", "--target", str(project), "--home", str(home)]
                    )

            self.assertIn("rules preset is not installed", stderr.getvalue())


class ScaffoldProjectLocalAdditionsPreservationTest(unittest.TestCase):
    """Marker-bounded preservation contract for project-local additions.

    Operators put project-local layer body (Rails / Ruby version, dangerous
    DB / test commands, Presenter / YAML conventions, docs catalog
    governance, role-boundary overrides, etc.) between the marker pair shipped
    inside scaffold-generated AGENTS.md / CLAUDE.md. `scaffold apply` and
    `scaffold diff` must mechanically preserve that body across re-sync so
    mature target repos do not lose project-local guardrails when a new
    preset version lands.
    """

    BEGIN = "<!-- mozyo-bridge:project-local-additions:begin -->"
    END = "<!-- mozyo-bridge:project-local-additions:end -->"

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _setup(self, tmp: Path, preset: str = "redmine-rails") -> tuple[Path, Path]:
        home = tmp / "home"
        project = tmp / "project"
        project.mkdir()
        self.run_cli(["rules", "install", "--home", str(home)])
        self.run_cli(
            ["scaffold", "apply", preset, "--target", str(project), "--home", str(home)]
        )
        return home, project

    def _insert_project_local_body(self, file_path: Path, body: str) -> str:
        """Replace the marker-bounded block in `file_path` with `body`.

        Returns the raw text written to disk so callers can assert against it.
        """
        text = file_path.read_text(encoding="utf-8")
        begin_idx = text.find(self.BEGIN)
        end_idx = text.find(self.END, begin_idx)
        assert begin_idx >= 0 and end_idx >= 0, "marker pair must be present"
        new_text = (
            text[: begin_idx + len(self.BEGIN)]
            + "\n"
            + body
            + "\n"
            + text[end_idx:]
        )
        file_path.write_text(new_text, encoding="utf-8")
        return new_text

    def test_router_templates_carry_marker_pair(self) -> None:
        """Both AGENTS.md and CLAUDE.md ship with the marker pair on fresh apply."""
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            self.run_cli(
                ["scaffold", "apply", "redmine-rails", "--target", str(project), "--home", str(home)]
            )

            agents = (project / "AGENTS.md").read_text(encoding="utf-8")
            claude = (project / "CLAUDE.md").read_text(encoding="utf-8")
            for text in (agents, claude):
                self.assertIn(self.BEGIN, text)
                self.assertIn(self.END, text)

    def test_backup_apply_preserves_project_local_body(self) -> None:
        """--backup re-apply preserves project-local body inside the markers."""
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            project_local_body = (
                "## Project-Local Layer\n\n"
                "- Ruby 3.1.5 / Rails 7.0.8.1.\n"
                "- DB safety: TEST_DB_ENV=test must be set when running rspec.\n"
                "- Read-only docs directory: Doc/ (edit forbidden).\n"
            )
            agents_path = project / "AGENTS.md"
            claude_path = project / "CLAUDE.md"
            self._insert_project_local_body(agents_path, project_local_body)
            self._insert_project_local_body(
                claude_path, "## Project-Local Reminder\n\nRAILS_ENV=test is mandatory.\n"
            )

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )

            self.assertEqual(0, result)
            agents_after = agents_path.read_text(encoding="utf-8")
            claude_after = claude_path.read_text(encoding="utf-8")
            # Project-local body must survive re-apply, byte-for-byte.
            self.assertIn("Ruby 3.1.5 / Rails 7.0.8.1.", agents_after)
            self.assertIn("TEST_DB_ENV=test", agents_after)
            self.assertIn("Read-only docs directory: Doc/ (edit forbidden).", agents_after)
            self.assertIn("RAILS_ENV=test is mandatory.", claude_after)
            # Markers still present after re-apply.
            self.assertIn(self.BEGIN, agents_after)
            self.assertIn(self.END, agents_after)
            self.assertIn(self.BEGIN, claude_after)
            self.assertIn(self.END, claude_after)
            # .bak.<timestamp> files retain the pre-apply state as safety net.
            self.assertTrue(list(project.glob("AGENTS.md.bak.*")))
            self.assertTrue(list(project.glob("CLAUDE.md.bak.*")))

    def test_force_apply_preserves_project_local_body(self) -> None:
        """--force re-apply also preserves project-local body inside the markers."""
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            agents_path = project / "AGENTS.md"
            self._insert_project_local_body(
                agents_path, "- project-local addition that must survive --force.\n"
            )

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--force",
                ]
            )

            self.assertEqual(0, result)
            agents_after = agents_path.read_text(encoding="utf-8")
            self.assertIn("project-local addition that must survive --force.", agents_after)
            self.assertIn(self.BEGIN, agents_after)
            self.assertIn(self.END, agents_after)
            # --force does not produce a .bak.* file (this is the documented
            # difference between --force and --backup; preservation does not
            # change that).
            self.assertFalse(list(project.glob("AGENTS.md.bak.*")))

    def test_diff_is_clean_after_preserving_project_local_body(self) -> None:
        """scaffold diff returns clean (exit 0) once project-local body is inside markers.

        Once the operator has put their additions between the markers AND
        re-applied so the manifest records the post-substitution hash, a
        subsequent `scaffold diff` against the same preset version must not
        report any pending changes — the rendered router (with substituted
        project-local body) matches the on-disk router byte-for-byte.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            agents_path = project / "AGENTS.md"
            self._insert_project_local_body(
                agents_path, "- project-local fact preserved across re-sync.\n"
            )
            # Re-apply so the manifest records the post-substitution hash.
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )

            result, output = self.run_cli(
                [
                    "scaffold",
                    "diff",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                ]
            )

            self.assertEqual(0, result)
            self.assertIn("scaffold diff: clean", output)

    def test_preservation_skipped_when_markers_absent_on_disk(self) -> None:
        """Legacy on-disk routers without markers fall through unchanged.

        When the operator's AGENTS.md does NOT contain the marker pair (legacy
        scaffold or hand-edited content with markers removed), preservation
        does not fire — re-apply with `--force` overwrites the file with the
        fresh scaffold base, exactly as the existing safety contract intended.
        """
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            agents_path = project / "AGENTS.md"
            # Replace the file entirely with a legacy-style router that has
            # no marker pair.
            legacy_body = "# AGENTS (legacy)\n\n- no marker pair here.\n"
            agents_path.write_text(legacy_body, encoding="utf-8")

            result, _ = self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--force",
                ]
            )

            self.assertEqual(0, result)
            agents_after = agents_path.read_text(encoding="utf-8")
            # Marker pair restored (fresh template).
            self.assertIn(self.BEGIN, agents_after)
            self.assertIn(self.END, agents_after)
            # Legacy body without markers is overwritten — no preservation
            # fallback for that case (operator must move content into markers
            # first, as documented in the preset's Apply Discipline section).
            self.assertNotIn("no marker pair here.", agents_after)

    def test_status_clean_after_preserving_body_and_reapplying(self) -> None:
        """scaffold status reports clean after preservation + re-apply cycle."""
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup(Path(tmp))
            self._insert_project_local_body(
                project / "AGENTS.md", "- project fact A\n- project fact B\n"
            )
            self._insert_project_local_body(
                project / "CLAUDE.md", "- claude reminder X\n"
            )
            self.run_cli(
                [
                    "scaffold",
                    "apply",
                    "redmine-rails",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--backup",
                ]
            )

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(0, result)
            self.assertIn("result: clean", output)

    def test_extract_and_substitute_helpers(self) -> None:
        """Unit-level coverage for the extract/substitute primitives."""
        from mozyo_bridge.scaffold.rules import (
            extract_project_local_block,
            substitute_project_local_block,
        )

        on_disk = (
            "header\n"
            + self.BEGIN
            + "\nproject additions\n"
            + self.END
            + "\ntrailer\n"
        )
        rendered = (
            "header\n"
            + self.BEGIN
            + "\nboilerplate\n"
            + self.END
            + "\ntrailer\n"
        )
        block = extract_project_local_block(on_disk)
        self.assertEqual("\nproject additions\n", block)
        new_rendered = substitute_project_local_block(rendered, block)
        self.assertIn("\nproject additions\n", new_rendered)
        self.assertNotIn("boilerplate", new_rendered)

        # Missing markers on either side returns/keeps the input unchanged.
        self.assertIsNone(extract_project_local_block("no markers here"))
        self.assertEqual(
            "no markers here",
            substitute_project_local_block("no markers here", "ignored"),
        )


class ScaffoldPresetHandoffPrimitiveDocsTest(unittest.TestCase):
    """Regression rails for Asana 1214760806178471: the scaffold presets must
    document the high-level handoff primitive as the standard handoff/reply
    path. If a future refactor accidentally restores the older "Standard
    notification command: `mozyo-bridge notify-* --issue --journal`" wording
    in either Asana or Redmine, or drops the explicit operator/debug boundary
    around `read` / `message` / `type` / `keys`, these tests catch it before
    operators install the drifted preset."""

    def setUp(self) -> None:
        presets_root = ROOT / "src" / "mozyo_bridge" / "scaffold" / "presets"
        self.asana_workflow = (presets_root / "asana" / "agent-workflow.md").read_text(
            encoding="utf-8"
        )
        self.redmine_workflow = (
            presets_root / "redmine" / "agent-workflow.md"
        ).read_text(encoding="utf-8")
        self.router_claude = (presets_root / "_router" / "CLAUDE.md").read_text(
            encoding="utf-8"
        )
        self.router_agents = (presets_root / "_router" / "AGENTS.md").read_text(
            encoding="utf-8"
        )
        self.redmine_rails_workflow = (
            presets_root / "redmine-rails" / "agent-workflow.md"
        ).read_text(encoding="utf-8")

    def test_asana_preset_standard_path_anchors_at_primitive(self) -> None:
        # Standard path bullet must name the high-level primitive.
        self.assertIn("**Standard path (required default)**", self.asana_workflow)
        self.assertIn("`mozyo-bridge handoff send", self.asana_workflow)
        self.assertIn("`mozyo-bridge handoff reply", self.asana_workflow)
        self.assertIn("`mozyo-bridge reply", self.asana_workflow)
        # `notify-*` are compatibility wrappers, not standard-path peers.
        self.assertIn("compatibility", self.asana_workflow)
        # `read` / `message` / `type` / `keys` are explicitly operator/debug.
        self.assertIn("operator/debug primitives", self.asana_workflow)
        # The retired-queue legacy notify wrapper must be cleanup-only.
        self.assertIn("`notify-*-legacy-task`", self.asana_workflow)

    def test_asana_preset_receiver_forbids_status_doctor_scrollback_inference(
        self,
    ) -> None:
        self.assertIn("`mozyo-bridge status`", self.asana_workflow)
        self.assertIn("`mozyo-bridge doctor`", self.asana_workflow)
        self.assertIn("pane scrollback", self.asana_workflow)
        self.assertIn("operator/debug aids", self.asana_workflow)

    def test_redmine_preset_pane_notification_anchors_at_primitive(self) -> None:
        # The Pane Notification section must name the primitive as the
        # standard command and the `notify-*` wrappers as compatibility.
        section_start = self.redmine_workflow.index("## Pane Notification")
        section_end = self.redmine_workflow.index(
            "## Handoff Startup Decision", section_start
        )
        section = self.redmine_workflow[section_start:section_end]
        self.assertIn("`mozyo-bridge handoff send", section)
        self.assertIn("`mozyo-bridge handoff reply", section)
        self.assertIn("`mozyo-bridge reply", section)
        self.assertIn("互換 entrypoint", section)
        self.assertIn("operator/debug primitives", section)
        self.assertNotIn(
            "Standard notification command: `mozyo-bridge notify-* --issue",
            section,
            msg="redmine preset still recommends the old notify-* shell as the standard",
        )
        # The retired-queue wrapper must be tagged cleanup-only here too.
        self.assertIn("retired-queue cleanup wrapper", section)

    def test_redmine_preset_handoff_startup_anchors_at_primitive(self) -> None:
        # The "Standard path" entry of the Handoff Startup Decision must
        # name the primitive, not the legacy notify-* shell.
        section_start = self.redmine_workflow.index("## Handoff Startup Decision")
        section_end = self.redmine_workflow.index(
            "## 実装者 / 監査者境界", section_start
        )
        section = self.redmine_workflow[section_start:section_end]
        # The Standard path bullet must lead with the primitive.
        self.assertIn("**Standard path**", section)
        self.assertIn("`mozyo-bridge handoff send", section)
        # `notify-*` must be described as compatibility, not the standard.
        self.assertIn("compatibility", section)

    def test_redmine_preset_receiver_forbids_status_doctor_scrollback_inference(
        self,
    ) -> None:
        # The Pane Notification section's recipient bullet must explicitly
        # forbid inferring receiver / issue state from status / doctor /
        # scrollback when a durable Redmine anchor exists.
        section_start = self.redmine_workflow.index("## Pane Notification")
        section_end = self.redmine_workflow.index(
            "## Handoff Startup Decision", section_start
        )
        section = self.redmine_workflow[section_start:section_end]
        self.assertIn("`mozyo-bridge status`", section)
        self.assertIn("`mozyo-bridge doctor`", section)
        self.assertIn("pane scrollback", section)
        self.assertIn("operator/debug aids", section)

    def test_shared_router_reminder_stays_thin_and_points_to_preset(self) -> None:
        self.assertIn("${rule_path}", self.router_claude)
        self.assertIn("${ticket_anchor_label}", self.router_claude)
        self.assertIn("handoff startup decision", self.router_claude)
        self.assertIn("operator/debug", self.router_claude)
        self.assertIn("`mozyo-bridge status`", self.router_claude)
        self.assertIn("`mozyo-bridge doctor`", self.router_claude)
        self.assertNotIn("`mozyo-bridge handoff send --to", self.router_claude)

    def test_shared_agents_router_stays_thin(self) -> None:
        self.assertIn("${rule_path}", self.router_agents)
        self.assertIn("${ticket_anchor_label}", self.router_agents)
        self.assertIn("router に本文を複製しない", self.router_agents)
        self.assertIn("operator/debug", self.router_agents)
        self.assertNotIn("Redmine Gate Lifecycle", self.router_agents)
        self.assertNotIn("Audit-Owned Commit Authority", self.router_agents)

    def test_router_templates_are_tool_specific_and_independent(self) -> None:
        """Generated AGENTS.md and CLAUDE.md must be independent tool-specific
        thin routers. CLAUDE.md must not import AGENTS.md (and vice versa) so
        each tool can read its own router as the standalone entry, and so a
        future refactor cannot accidentally restore the old shared-import
        layout where CLAUDE.md depended on AGENTS.md for session-start framing.
        """
        # No cross-import in either direction. The `@AGENTS.md` form is the
        # Claude Code-style file-import directive that previously made
        # CLAUDE.md depend on AGENTS.md for its session-start content.
        self.assertNotIn("@AGENTS.md", self.router_claude)
        self.assertNotIn("@CLAUDE.md", self.router_agents)
        # Each router announces its tool identity in the body so a reader (and
        # an auditor reading the rendered file) can see it stands alone.
        self.assertIn("Codex", self.router_agents)
        self.assertIn("tool-specific", self.router_agents)
        self.assertIn("import しない", self.router_agents)
        self.assertIn("Claude Code", self.router_claude)
        self.assertIn("tool-specific", self.router_claude)
        self.assertIn("import しない", self.router_claude)
        # Each router must independently reach the central preset and the
        # active ticket anchor without referencing the other file.
        for router in (self.router_agents, self.router_claude):
            self.assertIn("${rule_path}", router)
            self.assertIn("${ticket_anchor_label}", router)
        # Marker-bounded preservation must remain on both sides so project-
        # local additions survive re-sync after the tool-specific split.
        for router in (self.router_agents, self.router_claude):
            self.assertIn(
                "<!-- mozyo-bridge:project-local-additions:begin -->", router
            )
            self.assertIn(
                "<!-- mozyo-bridge:project-local-additions:end -->", router
            )

    def test_redmine_rails_preset_layers_on_redmine(self) -> None:
        self.assertIn("rules/presets/redmine/agent-workflow.md", self.redmine_rails_workflow)
        self.assertIn("Rails Design Consultation Triggers", self.redmine_rails_workflow)
        self.assertIn("Data / migration safety", self.redmine_rails_workflow)
        self.assertIn("Hotwire / UI behavior", self.redmine_rails_workflow)
        self.assertNotIn("/myapp/Source/rails", self.redmine_rails_workflow)

    def test_redmine_preset_separates_review_and_owner_close_approval(self) -> None:
        """Review Gate approval and owner close approval are distinct durable
        gates on Redmine projects, ported from Rails commit 8645c4d19.

        The reviewer (audit role / Codex equivalent) records `指摘事項なし` or
        re-review approval on the Review Gate journal, then must record a
        separate journal asking the owner whether close is permitted. The
        implementer must NOT close from review approval alone; it must wait
        for the owner close approval journal. The shared Redmine preset has
        to make all three responsibilities explicit so a future doc refactor
        cannot collapse them back into a single review-and-close gate.
        """
        # The dedicated section must exist.
        self.assertIn("## Close Approval Separation", self.redmine_workflow)

        # Section body must name the three responsibilities distinctly.
        section_start = self.redmine_workflow.index("## Close Approval Separation")
        section_end = self.redmine_workflow.index("## Close Gate Checklist", section_start)
        section = self.redmine_workflow[section_start:section_end]
        # Reviewer side: review approval is not close approval, and reviewer
        # has the post-approval owner-confirmation responsibility.
        self.assertIn("これだけで issue を close してはならない", section)
        self.assertIn("**別 journal**", section)
        self.assertIn("owner にクローズ可否を確認する", section)
        self.assertIn("レビュー結果と owner close approval を 1 journal にまとめない", section)
        # Implementer side: do not advance from review approval alone.
        self.assertIn("Review Gate approval だけで issue を close へ進めない", section)
        self.assertIn("owner の close approval journal を読み", section)
        # Collapsed-roles caveat preserves the record discipline.
        self.assertIn(
            "reviewer と owner を同一人物に collapse している場合でも", section
        )

        # Review Gate bullet must explicitly route the reviewer to the
        # separation section after a no-blockers verdict.
        review_gate = self.redmine_workflow[
            self.redmine_workflow.index("7. **Review Gate**") :
            self.redmine_workflow.index("8. **QA Verification Gate**")
        ]
        self.assertIn("これは close approval ではない", review_gate)
        self.assertIn("owner にクローズ可否を確認する責務", review_gate)
        self.assertIn("Close Approval Separation", review_gate)

        # Close Gate bullet must name owner close approval as separate from
        # Review Gate, not just "owner approval".
        close_gate = self.redmine_workflow[
            self.redmine_workflow.index("10. **Close Gate**") :
            self.redmine_workflow.index("\n\nproject 固有 status / tracker")
        ]
        self.assertIn("owner close approval", close_gate)
        self.assertIn("Review Gate とは別 journal", close_gate)
        self.assertIn(
            "passing Review Gate、owner close approval、commit hash record の三つが揃うまで",
            close_gate,
        )

        # Close Gate Checklist must add a dedicated bullet for the owner
        # close approval journal so a future Close Gate cannot be "passed"
        # against only the Review Gate.
        checklist_start = self.redmine_workflow.index("## Close Gate Checklist")
        checklist_end = self.redmine_workflow.index("## Pane Notification", checklist_start)
        checklist = self.redmine_workflow[checklist_start:checklist_end]
        self.assertIn(
            "**owner close approval** が Review Gate とは別 journal として記録されている",
            checklist,
        )
        self.assertIn("Review Gate approval だけで checklist を満たさない", checklist)

        # Completion section must tell the implementer to wait for the owner
        # close approval journal — review approval alone does not advance to
        # close.
        completion_start = self.redmine_workflow.index("## Completion")
        completion_end = self.redmine_workflow.index("## Audit-Owned Commit Authority", completion_start)
        completion = self.redmine_workflow[completion_start:completion_end]
        self.assertIn(
            "Review Gate approval を owner close approval と読み替えない",
            completion,
        )
        self.assertIn("owner close approval journal が記録されてから close へ進む", completion)

    def test_asana_completion_section_has_no_duplicate_numbered_steps(self) -> None:
        """Asana preset Completion section must be a contiguous 1..N numbered
        list with no duplicate numbers and no duplicate body text. A prior
        generated/rendered output exhibited a duplicated completion
        requirement line, and a regression here would re-introduce the same
        ambiguity for any downstream Asana project.
        """
        section_start = self.asana_workflow.index("## Completion")
        section_end = self.asana_workflow.index("## Audit-Owned Commit Authority", section_start)
        section = self.asana_workflow[section_start:section_end]

        numbered = re.findall(r"^(\d+)\.\s+(.+)$", section, flags=re.MULTILINE)
        self.assertGreaterEqual(
            len(numbered),
            3,
            msg=f"Completion section should contain a numbered list; got {numbered!r}",
        )
        # Contiguous 1..N, no duplicate numeric prefix.
        numbers = [int(num) for num, _ in numbered]
        self.assertEqual(
            numbers,
            list(range(1, len(numbers) + 1)),
            msg=(
                "Completion numbered list must be contiguous 1..N with no "
                f"duplicate numeric prefix; got {numbers!r}"
            ),
        )
        # No body appears twice (catches a duplicated completion requirement
        # line even if it were renumbered to keep the list "contiguous").
        bodies = [body.strip() for _, body in numbered]
        duplicates = [body for body in bodies if bodies.count(body) > 1]
        self.assertEqual(
            duplicates,
            [],
            msg=(
                "Completion section contains duplicate body text: "
                f"{duplicates!r}"
            ),
        )
        # Defensive: no two consecutive bodies are identical (cheaper to
        # catch if a future edit accidentally pastes the same line twice
        # in adjacent steps).
        for i in range(1, len(bodies)):
            self.assertNotEqual(
                bodies[i],
                bodies[i - 1],
                msg=(
                    f"Completion step {i + 1} duplicates the previous step "
                    f"body verbatim: {bodies[i]!r}"
                ),
            )


class ScaffoldStatusTest(unittest.TestCase):
    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _setup_scaffold(self, tmp: Path, preset: str = "redmine") -> tuple[Path, Path]:
        home = tmp / "home"
        project = tmp / "project"
        project.mkdir()
        self.run_cli(["rules", "install", "--home", str(home)])
        self.run_cli(["scaffold", "apply", preset, "--target", str(project), "--home", str(home)])
        return home, project

    def test_manifest_records_preset_hash_and_schema_v2(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp), "redmine")
            state = scaffold_state(project)
            self.assertIsNotNone(state)
            assert state is not None
            self.assertEqual(2, state["schema_version"])
            workflow = home / "rules" / "presets" / "redmine" / "agent-workflow.md"
            expected_hash = hashlib.sha256(workflow.read_bytes()).hexdigest()
            self.assertEqual(expected_hash, state["preset_hash"])
            self.assertIn("AGENTS.md", state["files"])
            self.assertIn("sha256", state["files"]["AGENTS.md"])

    def test_scaffold_status_reports_clean_after_fresh_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, result)
            self.assertIn("manifest: present", output)
            self.assertIn("central status: ok", output)
            self.assertIn("result: clean", output)

    def test_scaffold_status_reports_clean_after_fresh_asana_scaffold(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp), preset="asana")
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(0, result)
            self.assertIn("manifest: present", output)
            self.assertIn("central status: ok", output)
            self.assertIn("result: clean", output)

    def test_scaffold_status_detects_central_preset_content_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            workflow = home / "rules" / "presets" / "redmine" / "agent-workflow.md"
            workflow.write_text(
                workflow.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8"
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("central status: drifted-content", output)
            self.assertIn("result: drift detected", output)
            self.assertIn("central preset content has changed", output)

    def test_scaffold_status_detects_router_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            agents_path = project / "AGENTS.md"
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\nlocal edit\n", encoding="utf-8"
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md: drifted", output)
            self.assertIn("router AGENTS.md was modified locally", output)

    def test_scaffold_status_reports_missing_central_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            preset_dir = home / "rules" / "presets" / "redmine"
            shutil.rmtree(preset_dir)
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("central status: missing", output)
            self.assertIn("`mozyo-bridge rules install`", output)

    def test_scaffold_status_reports_missing_extended_base_preset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp), preset="redmine-rails")
            shutil.rmtree(home / "rules" / "presets" / "redmine")

            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )

            self.assertEqual(1, result)
            self.assertIn("preset: redmine-rails", output)
            self.assertIn("central status: missing", output)
            self.assertIn("`mozyo-bridge rules install`", output)

    def test_scaffold_refuses_when_extended_base_preset_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            project = Path(tmp) / "project"
            project.mkdir()
            self.run_cli(["rules", "install", "--home", str(home)])
            shutil.rmtree(home / "rules" / "presets" / "redmine")

            with contextlib.redirect_stderr(io.StringIO()) as stderr:
                with self.assertRaises(SystemExit):
                    self.run_cli(
                        [
                            "scaffold",
                            "apply",
                            "redmine-rails",
                            "--target",
                            str(project),
                            "--home",
                            str(home),
                        ]
                    )

            self.assertIn("rules preset is not installed: redmine", stderr.getvalue())

    def test_scaffold_status_reports_missing_manifest_for_empty_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "empty"
            empty.mkdir()
            home = Path(tmp) / "home"
            self.run_cli(["rules", "install", "--home", str(home)])
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(empty), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: missing", output)
            self.assertIn("no scaffold manifest", output)

    def test_scaffold_status_handles_schema_v1_manifest_gracefully(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            # Simulate a manifest written by a pre-hash version of the scaffolder.
            data["schema_version"] = 1
            data.pop("preset_hash", None)
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            # Same version + no hash means we can't prove content drift, but the
            # known router hashes still verify; treat as drift so the user upgrades.
            self.assertEqual(1, result)
            self.assertIn("central status: ok-version-only", output)
            self.assertIn("schema v1 (no preset_hash)", output)

    def test_scaffold_status_reports_invalid_manifest_on_bad_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            manifest_path.write_text("{bad json", encoding="utf-8")
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: invalid", output)
            self.assertIn("manifest is not valid JSON", output)

    def test_scaffold_status_json_output_for_invalid_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            manifest_path.write_text("{bad json", encoding="utf-8")
            result, output = self.run_cli(
                [
                    "scaffold",
                    "status",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            self.assertEqual(1, result)
            payload = json.loads(output)
            self.assertEqual("invalid", payload["manifest"])
            self.assertFalse(payload["clean"])
            self.assertIn("error", payload)

    def test_scaffold_status_rejects_schema_v2_manifest_with_missing_router_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["files"] = {}
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: invalid", output)
            self.assertIn("schema v2 manifest is missing router hash entries", output)
            self.assertIn("AGENTS.md", output)
            self.assertIn("CLAUDE.md", output)

    def test_scaffold_status_rejects_schema_v2_manifest_with_partial_router_entries(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            manifest_path = project / ".mozyo-bridge" / "scaffold.json"
            data = json.loads(manifest_path.read_text(encoding="utf-8"))
            data["files"].pop("CLAUDE.md", None)
            manifest_path.write_text(
                json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result, output = self.run_cli(
                ["scaffold", "status", "--target", str(project), "--home", str(home)]
            )
            self.assertEqual(1, result)
            self.assertIn("manifest: invalid", output)
            self.assertIn("CLAUDE.md", output)
            self.assertNotIn("result: clean", output)

    def test_scaffold_status_json_output(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home, project = self._setup_scaffold(Path(tmp))
            result, output = self.run_cli(
                [
                    "scaffold",
                    "status",
                    "--target",
                    str(project),
                    "--home",
                    str(home),
                    "--json",
                ]
            )
            self.assertEqual(0, result)
            payload = json.loads(output)
            self.assertTrue(payload["clean"])
            self.assertEqual("redmine", payload["preset"])
            self.assertEqual(2, payload["schema_version"])
            self.assertEqual("ok", payload["central_status"])
            self.assertEqual(
                payload["manifest_preset_hash"], payload["installed_preset_hash"]
            )
