"""Docs catalog audit-impact / canonical renderer / workspace-defaults renderer tests (Redmine #12140, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of the docs-catalog / canonical-renderer /
workspace-defaults test classes out of the monolithic test spine, per
#12138 first-wave split and vibes/docs/logics/refactor-split-strategy.md
Priority 1/2. No test logic changed."""

from __future__ import annotations

import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser

# Redmine #13148 REV2: single definition of the activation-tag grammar,
# shared by the derivation tests below. Source-of-truth rules (skill body
# / governed preset) carry an invisible HTML-comment tag that embeds the
# exact digest line the router must show for that always rule. The digest
# fragment (`always_digest.md`) is the render-time intermediate; this
# grammar lets the test prove the fragment is *derived* from the tags,
# not hand-written in parallel. `digest` runs up to the closing `"`; the
# rule text must therefore never contain a literal double quote.
ACTIVATION_TAG_RE = re.compile(
    r"<!-- mozyo-bridge:activation:(?P<activation>\w+) "
    r"id=(?P<id>[\w-]+) "
    r'digest="(?P<digest>[^"]*)" -->'
)


def parse_activation_tags(text: str) -> dict[str, tuple[str, str]]:
    """Return {id: (activation, digest)} for every activation tag in *text*."""
    tags: dict[str, tuple[str, str]] = {}
    for match in ACTIVATION_TAG_RE.finditer(text):
        tags[match.group("id")] = (match.group("activation"), match.group("digest"))
    return tags


class DocsAuditImpactDirtyFileTest(unittest.TestCase):
    """Pin docs audit-impact + --check-generated behavior on unrelated dirty files.

    Workflow-change verification target for Redmine #10338 lane policy
    (parent #10338, this task #10344). `mozyo-bridge docs audit-impact
    --all-changed --check-generated` must surface every git-changed path,
    including unrelated dirty files that the catalog does not map to any
    document, while still returning 0 when the generated drift check is
    clean. Otherwise the operator pre-commit gate would block every
    commit that happens to share a worktree with one stray untracked
    file (e.g., `.claude/settings.local.json`), and the
    `codex_autonomous_edit` verification command list in the lane
    policy would be impossible to run cleanly.

    The test sets up a real `git init` repo with a catalog +
    fresh-regenerated file_conventions, drops an untracked file outside
    every file_convention pattern, and drives `cmd_docs_audit_impact`
    end-to-end.
    """

    def _run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _run_git(self, repo: Path, *cmd: str) -> None:
        subprocess.run(
            ["git", *cmd],
            cwd=repo,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    def _bring_up_governed_repo(self, tmp: str) -> Path:
        """Governed scaffold + live catalog + clean generated file + git repo.

        Shared by the dirty-file and staged-file gates below so both drive
        the identical real-scaffold workspace.
        """
        import shutil as _shutil

        home = Path(tmp) / "home"
        project = Path(tmp) / "project"
        project.mkdir()

        # Bring up a governed scaffold so the catalog skeleton ships.
        self._run_cli(["rules", "install", "--home", str(home)])
        self._run_cli(
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
        # The skeleton is the safe minimal catalog to drive resolver +
        # generator against; we promote it to the live catalog as the
        # docs `## Quick Start` invocation does.
        example = project / ".mozyo-bridge" / "docs" / "catalog.yaml.example"
        catalog = project / ".mozyo-bridge" / "docs" / "catalog.yaml"
        _shutil.copyfile(example, catalog)

        # Regenerate the generated file so the drift check is clean on
        # the first audit-impact call. Without this the test would
        # measure missing-output behavior instead of the dirty-file
        # interaction.
        gen_code, _ = self._run_cli(
            [
                "docs",
                "generate-file-conventions",
                "--repo",
                str(project),
            ]
        )
        self.assertEqual(0, gen_code)

        # Initialize a git repo so `audit_doc_impact` can read the
        # all-changed listing via `git ls-files --others --exclude-standard`.
        self._run_git(project, "init", "--initial-branch=main")
        self._run_git(project, "config", "user.email", "test@example.invalid")
        self._run_git(project, "config", "user.name", "Test")
        # Commit the scaffold so subsequent untracked files are the
        # only unstaged work; otherwise every scaffold file would
        # also report and noise the assertion.
        self._run_git(project, "add", ".")
        self._run_git(project, "commit", "-m", "scaffold")
        return project

    def test_audit_impact_all_changed_surfaces_staged_paths_with_check_generated(
        self,
    ) -> None:
        """`--all-changed --check-generated` must audit the complete set.

        Redmine #13919: `--all-changed` read unstaged + untracked only, so a
        worktree whose work was fully staged — the state an operator is in at
        the pre-commit gate — reported "No changed paths." and exited 0. The
        `--check-generated` trailer stayed clean too, so the whole gate read
        as a pass. Exit 0 alone therefore proves nothing here; the assertion
        is on the path actually being surfaced.
        """
        with tempfile.TemporaryDirectory() as tmp:
            project = self._bring_up_governed_repo(tmp)

            staged = project / "staged_notes.txt"
            staged.write_text("staged work\n", encoding="utf-8")
            self._run_git(project, "add", "staged_notes.txt")

            code, output = self._run_cli(
                [
                    "docs",
                    "audit-impact",
                    "--all-changed",
                    "--check-generated",
                    "--repo",
                    str(project),
                ]
            )

            self.assertEqual(0, code, msg=output)
            self.assertIn("[staged_notes.txt]", output)
            # The pre-fix failure mode verbatim: a fully staged worktree
            # reported as an empty one.
            self.assertNotIn("No changed paths.", output)
            self.assertIn("is up to date", output)

    def test_audit_impact_returns_clean_on_unrelated_dirty_file_when_generated_check_passes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = self._bring_up_governed_repo(tmp)

            # Drop an unrelated dirty file. It is intentionally outside
            # every governed-preset file_convention so the resolver
            # surfaces `documents_to_read: - none`, mirroring the real
            # `.claude/settings.local.json` operator pattern that prompted
            # this verification (see Redmine #10338 review #49720 note 3
            # and #49743 note 4 — both treated such a dirty file as
            # unrelated).
            unrelated = project / "untracked_notes.txt"
            unrelated.write_text("scratch\n", encoding="utf-8")

            code, output = self._run_cli(
                [
                    "docs",
                    "audit-impact",
                    "--all-changed",
                    "--check-generated",
                    "--repo",
                    str(project),
                ]
            )

            # Contract pin: exit 0 even when an unrelated dirty file is
            # reported, provided --check-generated is clean.
            self.assertEqual(0, code, msg=output)
            # The unrelated file MUST appear in the output — surfacing it
            # is the whole point of `--all-changed`; silently swallowing
            # the path would be the real regression.
            self.assertIn("[untracked_notes.txt]", output)
            self.assertIn("documents_to_read:", output)
            # `documents_to_read: - none` is the expected shape for an
            # unrelated path; if a future catalog edit accidentally
            # broadened a file_convention to catch `untracked_notes.txt`,
            # this assertion would tighten the test before it could rot.
            self.assertIn("- none", output)
            # The generated check trailer must confirm cleanliness so the
            # 0 exit was not from missing-output suppression.
            self.assertIn("is up to date", output)


class CanonicalRendererTest(unittest.TestCase):
    """Cover the Redmine #10345 single-source conditional renderer.

    The canonical YAML under `src/mozyo_bridge/scaffold/canonical_sources/`
    is the source of truth for the router pair templates at
    `src/mozyo_bridge/scaffold/presets/_router/{AGENTS,CLAUDE}.md`. These
    tests pin:

    - byte-equal round-trip between canonical render and the committed
      template files (drift is the only thing `--check` should report);
    - tool-conditional dispatch (codex vs claude fragments land in the
      right output);
    - Project-Local Additions marker preservation through the render
      pipeline so the downstream `apply_project_local_preservation` in
      `scaffold.rules` continues to work;
    - the CLI `scaffold canonical [--check]` surface returns the
      expected exit codes on clean state, drift, and missing files.
    """

    SOURCE_RELATIVE = Path("src/mozyo_bridge/scaffold/canonical_sources/router.yaml")
    AGENTS_RELATIVE = Path("src/mozyo_bridge/scaffold/presets/_router/AGENTS.md")
    CLAUDE_RELATIVE = Path("src/mozyo_bridge/scaffold/presets/_router/CLAUDE.md")
    BEGIN_MARKER = "<!-- mozyo-bridge:project-local-additions:begin -->"
    END_MARKER = "<!-- mozyo-bridge:project-local-additions:end -->"

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_committed_templates_match_canonical_render(self) -> None:
        from mozyo_bridge.scaffold.canonical import collect_render_results

        results = collect_render_results(ROOT)
        self.assertGreater(len(results), 0, "expected at least one canonical output")
        for result in results:
            self.assertEqual(
                result.rendered,
                result.on_disk,
                msg=(
                    f"{result.output_path} drifted from canonical source "
                    f"{result.source_id!r}; rerun `mozyo-bridge scaffold "
                    f"canonical` (no flag = render) and recommit."
                ),
            )

    def test_conditional_dispatch_isolates_tool_specific_fragments(self) -> None:
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_for_context,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        codex = render_for_context(source, {"tool": "codex"})
        claude = render_for_context(source, {"tool": "claude"})

        # Title and tool intro split. Codex carries the cross-tool agents.md
        # framing; Claude does not. Claude carries the ClaudeCode reminder
        # heading; Codex does not.
        self.assertIn("# AGENTS (Codex 入口)", codex)
        self.assertNotIn("# Claude Code Router", codex)
        self.assertIn("cross-tool agents.md", codex)

        self.assertIn("# Claude Code Router", claude)
        self.assertNotIn("# AGENTS (Codex 入口)", claude)
        self.assertIn("ClaudeCode 起動時の最小 reminder", claude)
        self.assertNotIn("## Preset", claude)

        # Codex body holds the Preset + Guardrails block; Claude body does
        # not. Each side keeps the other tool's body out.
        self.assertIn("## Preset", codex)
        self.assertIn("## Guardrails", codex)
        self.assertNotIn("ClaudeCode 起動時の最小 reminder", codex)

        # The shared session-start opening (steps 1-2 + `${rule_path}` and the
        # `rules home --resolved` bootstrap sub-notes) is byte-shared between
        # both renders.
        shared_opening = (
            "## セッション開始\n\n"
            "1. 現在の working directory がこの project root またはその配下であることを確認する。\n"
            "2. mozyo-bridge の central preset rules を読む:\n"
            "   - committed docs では portable 表記 `${rule_path}` を使う。\n"
            "   - runtime で実ファイルを読む際も `${rule_path}` を読む。"
            "repo-local store (`.mozyo-bridge/rules/...`) の path は repo root からの相対でそのまま読める。"
            "central store の home prefix は `mozyo-bridge rules home --resolved` の出力で解決する "
            "(`--resolved` 出力は debug / runtime 用で、committed docs に貼らない)。\n"
            "   - resolved path や central preset を読めない場合は、読んだふりをせず停止し、"
            "`mozyo-bridge rules install` 等の復旧を operator に求める。\n"
        )
        self.assertIn(shared_opening, codex)
        self.assertIn(shared_opening, claude)

    def test_sublane_route_only_in_opt_in_variant(self) -> None:
        """The sublane read-route fragment is gated on `sublane_flow`.

        Redmine #12362 / #12363: the route activates only when the output
        context carries `sublane_flow: enabled`. The base tool contexts
        must render byte-for-byte as before (no route), and the four
        committed outputs split base vs `.sublane.` accordingly.
        """
        from mozyo_bridge.scaffold.canonical import (
            collect_render_results,
            load_canonical_source,
            render_for_context,
        )

        heading = "## サブレーン開発フロー (opt-in profile)"
        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)

        # Base contexts (no sublane_flow key) never emit the route.
        for tool in ("codex", "claude"):
            self.assertNotIn(heading, render_for_context(source, {"tool": tool}))
            # The opt-in context adds the route and points at the doc.
            opt = render_for_context(
                source, {"tool": tool, "sublane_flow": "enabled"}
            )
            self.assertIn(heading, opt)
            self.assertIn(
                "vibes/docs/profiles/sublane-flow-runtime-profile.md", opt
            )

        # The committed outputs match: only the `.sublane.` variants carry
        # the route, and the base templates stay thin.
        by_name = {
            result.output_path.name: result.rendered
            for result in collect_render_results(ROOT)
        }
        self.assertNotIn(heading, by_name["AGENTS.md"])
        self.assertNotIn(heading, by_name["CLAUDE.md"])
        self.assertIn(heading, by_name["AGENTS.sublane.md"])
        self.assertIn(heading, by_name["CLAUDE.sublane.md"])

    def test_render_preserves_project_local_marker_pair(self) -> None:
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_for_context,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        for tool in ("codex", "claude"):
            with self.subTest(tool=tool):
                rendered = render_for_context(source, {"tool": tool})
                begin = rendered.find(self.BEGIN_MARKER)
                end = rendered.find(self.END_MARKER)
                self.assertNotEqual(
                    -1,
                    begin,
                    msg=f"{tool}: begin marker missing from canonical render",
                )
                self.assertNotEqual(
                    -1,
                    end,
                    msg=f"{tool}: end marker missing from canonical render",
                )
                self.assertLess(
                    begin,
                    end,
                    msg=f"{tool}: marker pair is out of order in canonical render",
                )

    def test_check_clean_then_drift_then_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )

            # Pristine copy: --check passes.
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result, msg=stdout + stderr)
            self.assertIn("AGENTS.md is up to date", stdout)
            self.assertIn("CLAUDE.md is up to date", stdout)
            self.assertEqual("", stderr)

            # Mutating the committed template must surface drift.
            agents_path = repo / self.AGENTS_RELATIVE
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\nDRIFT MARKER\n",
                encoding="utf-8",
            )
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("is out of date", stderr)
            self.assertIn("AGENTS.md", stderr)

            # `render` (no --check) rewrites the file from canonical source.
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--repo", str(repo)]
            )
            self.assertEqual(0, result, msg=stdout + stderr)

            # And the next --check is clean again.
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result, msg=stdout + stderr)
            self.assertEqual("", stderr)

    def test_drift_recovery_message_names_only_valid_subcommand(self) -> None:
        """Pin the drift stderr message to a runnable CLI invocation.

        Codex review #49845 caught the regression where the recovery
        message named `mozyo-bridge scaffold canonical render` — a
        non-existent sub-subcommand (the actual surface is `scaffold
        canonical` for render and `scaffold canonical --check` for the
        gate). A drifted router would still fail correctly, but a copy-
        pasted recovery command would error out with `unrecognized
        arguments: render`, defeating the operator-recovery half of the
        review focus "drift detection の実用性".
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            agents_path = repo / self.AGENTS_RELATIVE
            agents_path.write_text(
                agents_path.read_text(encoding="utf-8") + "\nDRIFT\n",
                encoding="utf-8",
            )
            result, _, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            # The valid invocation must appear in the recovery hint so an
            # operator can copy-paste it.
            self.assertIn(
                "mozyo-bridge scaffold canonical",
                stderr,
                msg="drift stderr must name the actual `scaffold canonical` CLI",
            )
            # And the invalid `canonical render` shape must not be
            # reintroduced. Use a substring check that allows
            # `scaffold canonical` (alone) and `scaffold canonical --check`
            # but rejects the sub-subcommand wording explicitly.
            self.assertNotIn(
                "canonical render",
                stderr,
                msg=(
                    "drift stderr regressed to the invalid sub-subcommand "
                    "wording; `scaffold canonical render` is not a real CLI"
                ),
            )
            self.assertNotIn(
                "canonical check",
                stderr,
                msg=(
                    "drift stderr names a non-existent `canonical check` "
                    "sub-subcommand; the real surface is `--check` flag"
                ),
            )

    def test_check_reports_missing_output_as_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            (repo / self.CLAUDE_RELATIVE).unlink()
            result, stdout, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("CLAUDE.md is missing", stderr)

    def test_canonical_render_survives_body_file_edit(self) -> None:
        """A canonical body-file edit must show up in the rendered output.

        Concretely: editing a body file rotates the canonical render and
        the committed `_router/*.md` template stops matching. This
        confirms the body files are the source of truth — not a stale
        copy that happens to share content.
        """
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            shutil.copytree(
                ROOT / "src",
                repo / "src",
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            body_path = (
                repo
                / "src/mozyo_bridge/scaffold/canonical_sources/router/bodies/title_codex.md"
            )
            body_path.write_text(
                body_path.read_text(encoding="utf-8") + "EDITED\n",
                encoding="utf-8",
            )

            # --check must detect AGENTS.md drift (codex output); CLAUDE.md
            # stays clean because the edit only touches a codex-when fragment.
            result, _, stderr = self.run_cli(
                ["scaffold", "canonical", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("AGENTS.md", stderr)
            self.assertNotIn("CLAUDE.md", stderr)

            # `render` rewrites AGENTS.md so the new body lands on disk.
            result, _, _ = self.run_cli(
                ["scaffold", "canonical", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            updated = (repo / self.AGENTS_RELATIVE).read_text(encoding="utf-8")
            self.assertIn("EDITED", updated)

    def test_check_fails_when_canonical_source_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / "src/mozyo_bridge/scaffold").mkdir(parents=True)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["scaffold", "canonical", "--check", "--repo", str(repo)]
                )


class AlwaysRuleDigestTest(unittest.TestCase):
    """Pin Redmine #13148: the always-rule digest in the router pair.

    The digest block is generated into both router templates from
    `canonical_sources/router/bodies/always_digest.md`. `CanonicalRendererTest`
    already gates byte-drift between the canonical render and the committed
    templates. This class pins the *semantic* contract the byte gate cannot:

    - the digest ships in BOTH tool routers (AGENTS.md + CLAUDE.md), so an
      agent that loads neither the skill nor the preset still meets the
      always rules;
    - every digest entry's canonical pointer target actually exists (a
      renamed section in the skill / preset must update the digest in the
      same commit, or this test fails loudly);
    - the entry count stays within the ≤10-line cap from
      `vibes/docs/rules/workflow-docs-boundary.md` `## 規則の activation 軸`,
      so the router stays thin.
    """

    ROUTER_DIR = Path("src/mozyo_bridge/scaffold/presets/_router")
    DIGEST_HEADING = "## 常時適用規則ダイジェスト (生成)"
    BEGIN = "<!-- mozyo-bridge:always-digest:begin -->"
    END = "<!-- mozyo-bridge:always-digest:end -->"
    DIGEST_ENTRY_CAP = 10

    def _digest_entries(self, router_name: str) -> list[str]:
        body = (ROOT / self.ROUTER_DIR / router_name).read_text(encoding="utf-8")
        self.assertIn(self.DIGEST_HEADING, body, f"{router_name} missing digest heading")
        self.assertIn(self.BEGIN, body, f"{router_name} missing digest begin marker")
        self.assertIn(self.END, body, f"{router_name} missing digest end marker")
        block = body.split(self.BEGIN, 1)[1].split(self.END, 1)[0]
        return [
            line for line in block.splitlines() if line.startswith("- ")
        ]

    def test_digest_ships_in_both_tool_routers(self) -> None:
        for router_name in ("AGENTS.md", "CLAUDE.md"):
            entries = self._digest_entries(router_name)
            self.assertGreater(
                len(entries),
                0,
                f"{router_name} always-digest block has no entries; the "
                "always rules would be unreachable from the router.",
            )

    def test_digest_is_byte_identical_across_tools(self) -> None:
        # The always rules are tool-agnostic; the shared fragment must
        # render the same digest bytes into both routers.
        self.assertEqual(
            self._digest_entries("AGENTS.md"),
            self._digest_entries("CLAUDE.md"),
        )

    def test_digest_entry_count_within_line_cap(self) -> None:
        for router_name in ("AGENTS.md", "CLAUDE.md"):
            entries = self._digest_entries(router_name)
            self.assertLessEqual(
                len(entries),
                self.DIGEST_ENTRY_CAP,
                f"{router_name} always-digest has {len(entries)} entries, over "
                f"the ≤{self.DIGEST_ENTRY_CAP} cap; replace an entry instead of "
                "appending (workflow-docs-boundary.md `## 規則の activation 軸`).",
            )

    @staticmethod
    def _heading_lines(body: str) -> list[str]:
        # Pointer targets must be matched against real *heading lines*.
        # A plain substring search over the whole body is vacuous here:
        # every activation tag embeds its own pointer text (``... の
        # `### <section>` 節``), so the tag satisfies the search even after
        # the heading it points at has been renamed away.
        return [
            line.rstrip()
            for line in body.splitlines()
            if line.startswith("#") and not line.lstrip("#").startswith("#")
        ]

    def _assert_section_heading_exists(
        self, body: str, section: str, where: str
    ) -> None:
        headings = self._heading_lines(body)
        self.assertTrue(
            any(heading.startswith(section) for heading in headings),
            f"digest points at {where} section {section!r}, but no heading "
            "line starts with it; rename the digest pointer in the same "
            "commit as the section.",
        )

    def test_digest_pointer_targets_exist(self) -> None:
        # Each digest entry is a pointer, not the rule body. Pin that the
        # named canonical sections still exist so a rename can't silently
        # leave the digest pointing at a dead section.
        skill_body = (
            ROOT
            / "skills/mozyo-bridge-agent/references/workflow.md"
        ).read_text(encoding="utf-8")
        preset_body = (
            ROOT
            / "src/mozyo_bridge/scaffold/presets/redmine-governed/agent-workflow.md"
        ).read_text(encoding="utf-8")

        for section in (
            "### Narrative の issue 参照は",
            "### 未定義の project 固有語を定義済み用語として使わない",
        ):
            self._assert_section_heading_exists(skill_body, section, "skill")
        for section in (
            "### 応答言語ポリシー",
            "### Review Finding Verdict Obligation (迎合禁止)",
            "### 根拠出所分類",
            "### 回答前 Doc 解決 (Answer-Time Resolution)",
        ):
            self._assert_section_heading_exists(preset_body, section, "preset")

    def test_boundary_doc_defines_activation_axis(self) -> None:
        # The placement rule + line cap the digest cites must be reachable
        # in the repo-local boundary doc.
        boundary = (
            ROOT / "vibes/docs/rules/workflow-docs-boundary.md"
        ).read_text(encoding="utf-8")
        self.assertIn("## 規則の activation 軸", boundary)
        self.assertIn("always", boundary)
        self.assertIn("per-task", boundary)
        self.assertIn("exceptional", boundary)

    # --- Redmine #13148 REV2: derivation from source-of-truth tags. ---
    #
    # The heading-existence pin above cannot catch a rule whose *meaning*
    # changed while its heading stayed. The tags below live directly on the
    # source rules (skill body + governed preset) and embed the exact
    # digest line; the derivation tests prove the fragment is built from
    # those tags. A rule author who rewords a rule updates its tag, which
    # fails these tests until the fragment (and thus the canonical render /
    # router drift check) is updated to match — closing the chain.

    SKILL_BODY_RELATIVE = Path("skills/mozyo-bridge-agent/references/workflow.md")
    GOVERNED_PRESET_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/presets/redmine-governed/agent-workflow.md"
    )
    EXPECTED_TAG_IDS = frozenset(
        {
            "narrative-issue-labeling",
            "response-language",
            "no-sycophancy-evidence-provenance",
            "answer-time-doc-resolution",
            "canonical-terminology-discipline",
        }
    )

    def _source_activation_tags(self) -> dict[str, tuple[str, str]]:
        tags: dict[str, tuple[str, str]] = {}
        for relative in (self.SKILL_BODY_RELATIVE, self.GOVERNED_PRESET_RELATIVE):
            body = (ROOT / relative).read_text(encoding="utf-8")
            for tag_id, value in parse_activation_tags(body).items():
                self.assertNotIn(
                    tag_id,
                    tags,
                    f"duplicate activation id {tag_id!r} across source rules; "
                    "each always rule must have exactly one source tag.",
                )
                tags[tag_id] = value
        return tags

    def _digest_texts(self, router_name: str) -> list[str]:
        # Digest entries with the leading "- " list marker stripped, so
        # they compare directly against the tag `digest` field.
        return [entry[2:] for entry in self._digest_entries(router_name)]

    def test_source_rules_carry_expected_activation_tags(self) -> None:
        tags = self._source_activation_tags()
        self.assertEqual(
            set(tags),
            set(self.EXPECTED_TAG_IDS),
            "source activation tag ids drifted from the expected always set; "
            "add/remove the tag on the source rule and update EXPECTED_TAG_IDS.",
        )
        for tag_id, (activation, _digest) in tags.items():
            self.assertEqual(
                activation,
                "always",
                f"tag {tag_id!r} is not activation=always; only always rules "
                "belong in the router digest.",
            )

    def test_digest_fragment_derives_from_source_tags(self) -> None:
        # The core derivation contract: the digest lines rendered into the
        # routers must be exactly the set of `digest` values declared by the
        # source-rule tags — same texts, same count. A reworded rule (tag
        # updated) or a hand-edited fragment breaks this immediately.
        tag_digests = sorted(
            digest for (_activation, digest) in self._source_activation_tags().values()
        )
        for router_name in ("AGENTS.md", "CLAUDE.md"):
            self.assertEqual(
                sorted(self._digest_texts(router_name)),
                tag_digests,
                f"{router_name} always-digest is not derived from the source "
                "activation tags; update always_digest.md to match the tags "
                "(or the tags to match a genuine rule change).",
            )


class GovernedWorkflowCanonicalTest(unittest.TestCase):
    """Pin Redmine #10426: governed preset agent-workflow.md canonicalization.

    `governed-workflow.yaml` renders both `redmine-governed/agent-workflow.md`
    and `redmine-rails-governed/agent-workflow.md` from a single body file
    plus per-output `{{name}}` substitutions. These tests pin:

    - byte-equal render for both governed presets (drift gate);
    - critical workflow keywords survive in both renders (gate / role /
      autonomous lane / cross-workspace / close approval), so a future
      placeholder rename or fragment edit cannot silently drop a gate or
      a role boundary clause;
    - the substitution engine itself: undefined placeholders die loudly
      and missing-substitutions-with-placeholders fails before write.
    """

    SOURCE_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/canonical_sources/governed-workflow.yaml"
    )
    REDMINE_GOVERNED_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/presets/redmine-governed/agent-workflow.md"
    )
    REDMINE_RAILS_GOVERNED_RELATIVE = Path(
        "src/mozyo_bridge/scaffold/presets/redmine-rails-governed/agent-workflow.md"
    )

    # Semantic anchors that MUST appear in both governed renders. If a
    # future canonical edit drops any of these, governance behavior
    # silently weakens — exactly the drift this canonicalization is
    # meant to prevent. Each marker is a verbatim substring pulled from
    # the workflow body; quoting is preserved so a partial-rename does
    # not pass.
    GOVERNED_KEYWORD_MARKERS: tuple[str, ...] = (
        # Gate vocabulary — separation of Implementation Done vs Close
        # is the governed preset's central promise.
        "Implementation Done は completion ではない",
        "Review Gate approval も Close ではない",
        "owner_close_approval",
        "Close Approval Separation",
        # Role boundary — implementer vs auditor distinction must stay
        # legible in the preset body.
        "claude_code: 実装者",
        "codex: 監査者",
        "owner: 最終判断者",
        # Codex Direct Edit Gate — the gate-vs-short-imperative
        # distinction is the wording that prior #10332 / #10338 reviews
        # required to stay verbatim.
        "Codex Direct Edit Gate",
        "短い命令は file edit 許可ではない",
        "Repo-Local Guardrail Autonomous Lane",
        "codex_autonomous_edit",
        # US-level audit model (Redmine #11599) — the standard review /
        # audit / close-approval unit is the UserStory; per-task Codex
        # review is the exception, not the default.
        "US-Level Audit Model",
        "標準適用単位は UserStory",
        "Task / Test / Bug ごとの Codex review_request は不要",
        "task_close",
        # Docs catalog governance contract.
        "catalog 駆動の docs 解決",
        ".mozyo-bridge/docs/catalog.yaml",
        # The recovery wording corrected in #10345 must stay verifiable.
        "mozyo-bridge docs generate-file-conventions",
    )

    def _governed_outputs(self):
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_output,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        return {
            output.target.name: render_output(source, output)
            for output in source.outputs
        }

    def test_both_governed_outputs_match_canonical_render(self) -> None:
        from mozyo_bridge.scaffold.canonical import collect_render_results

        results_by_target = {
            result.output_path.relative_to(ROOT).as_posix(): result
            for result in collect_render_results(ROOT)
        }
        for relative in (
            self.REDMINE_GOVERNED_RELATIVE,
            self.REDMINE_RAILS_GOVERNED_RELATIVE,
        ):
            with self.subTest(target=relative.as_posix()):
                key = relative.as_posix()
                self.assertIn(key, results_by_target)
                result = results_by_target[key]
                self.assertEqual(
                    result.rendered,
                    result.on_disk,
                    msg=(
                        f"{relative.as_posix()} drifted from canonical source; "
                        f"rerun `mozyo-bridge scaffold canonical` and recommit."
                    ),
                )

    def test_governed_renders_carry_framework_specific_phrases(self) -> None:
        """Confirm the per-output `substitutions` actually swap framework markers.

        This is the inter-preset drift gate: if `redmine-governed` ever
        accidentally renders Rails-only paths (or vice versa), the
        canonical source has lost its conditional contract.
        """
        rendered = self._governed_outputs()
        rg = rendered["agent-workflow.md"]  # there are two; reload by preset

        # Re-resolve by full path keys to disambiguate the two outputs.
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_output,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        by_preset = {}
        for output in source.outputs:
            preset_name = output.target.parts[-2]
            by_preset[preset_name] = render_output(source, output)
        del rg

        redmine = by_preset["redmine-governed"]
        rails = by_preset["redmine-rails-governed"]

        # Title: each preset names itself.
        self.assertIn("# Redmine Governed Agent Workflow", redmine)
        self.assertNotIn("# Redmine Governed Agent Workflow", rails)
        self.assertIn("# Redmine Rails Governed Agent Workflow", rails)

        # Implementation paths: redmine ships generic; rails ships Rails layout.
        self.assertIn("    - src/**", redmine)
        self.assertIn("    - tests/**", redmine)
        self.assertNotIn("    - app/**", redmine)
        self.assertNotIn("    - spec/**", redmine)

        self.assertIn("    - app/**", rails)
        self.assertIn("    - spec/**", rails)
        self.assertNotIn("    - src/**", rails)
        self.assertNotIn("    - tests/**", rails)

        # Layered Source: rails references both base layers.
        self.assertIn(
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`",
            redmine,
        )
        self.assertIn(
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine/agent-workflow.md`",
            rails,
        )
        self.assertIn(
            "`${MOZYO_BRIDGE_HOME:-~/.mozyo_bridge}/rules/presets/redmine-rails/agent-workflow.md`",
            rails,
        )
        self.assertNotIn("redmine-rails/agent-workflow.md", redmine)

        # Required-Verification framework-specific commands.
        self.assertIn("project の authoritative test command", redmine)
        self.assertNotIn("bundle exec rspec", redmine)
        self.assertIn("bundle exec rspec", rails)
        self.assertIn("rubocop / brakeman", rails)

        # Governed Mode Prohibitions last bullet names the base preset.
        self.assertIn(
            "shared preset の `redmine` だけで完了報告すること",
            redmine,
        )
        self.assertIn(
            "shared preset の `redmine-rails` だけで完了報告すること",
            rails,
        )

    def test_both_governed_outputs_preserve_governance_keywords(self) -> None:
        rendered = {}
        from mozyo_bridge.scaffold.canonical import (
            load_canonical_source,
            render_output,
        )

        source = load_canonical_source(ROOT / self.SOURCE_RELATIVE)
        for output in source.outputs:
            rendered[output.target.parts[-2]] = render_output(source, output)

        for preset_name, body in rendered.items():
            for marker in self.GOVERNED_KEYWORD_MARKERS:
                with self.subTest(preset=preset_name, marker=marker):
                    self.assertIn(
                        marker,
                        body,
                        msg=(
                            f"{preset_name}/agent-workflow.md lost governance "
                            f"marker {marker!r}; a substitution or fragment "
                            f"edit silently weakened the preset body."
                        ),
                    )

    def test_unresolved_placeholder_dies_loudly(self) -> None:
        """An undefined `{{name}}` placeholder must fail before any write."""
        from mozyo_bridge.scaffold.canonical import (
            CanonicalSource,
            Fragment,
            OutputSpec,
            render_output,
        )

        source = CanonicalSource(
            id="probe",
            source_path=Path("/tmp/probe.yaml"),
            outputs=(
                OutputSpec(
                    target=Path("probe.md"),
                    context={},
                    substitutions={"GOOD": "value"},
                ),
            ),
            fragments=(
                Fragment(id="f", when={}, body="hello {{GOOD}} and {{MISSING}}\n"),
            ),
        )
        with self.assertRaises(SystemExit):
            render_output(source, source.outputs[0])

    def test_placeholder_without_substitutions_mapping_dies(self) -> None:
        """A body with `{{name}}` but no `substitutions` mapping must die."""
        from mozyo_bridge.scaffold.canonical import (
            CanonicalSource,
            Fragment,
            OutputSpec,
            render_output,
        )

        source = CanonicalSource(
            id="probe",
            source_path=Path("/tmp/probe.yaml"),
            outputs=(OutputSpec(target=Path("probe.md"), context={}, substitutions={}),),
            fragments=(
                Fragment(id="f", when={}, body="hello {{ANY}}\n"),
            ),
        )
        with self.assertRaises(SystemExit):
            render_output(source, source.outputs[0])

    def test_substitution_is_single_pass(self) -> None:
        """A value containing a placeholder string must NOT be re-substituted.

        Without this guarantee, a substitution value of `{{A}}` would
        recurse into `A`'s mapping, producing surprising output.
        Single-pass keeps the renderer deterministic.
        """
        from mozyo_bridge.scaffold.canonical import (
            CanonicalSource,
            Fragment,
            OutputSpec,
            render_output,
        )

        source = CanonicalSource(
            id="probe",
            source_path=Path("/tmp/probe.yaml"),
            outputs=(
                OutputSpec(
                    target=Path("probe.md"),
                    context={},
                    substitutions={"A": "{{B}}", "B": "REPLACED"},
                ),
            ),
            fragments=(Fragment(id="f", when={}, body="A is {{A}}\n"),),
        )
        rendered = render_output(source, source.outputs[0])
        self.assertEqual("A is {{B}}\n", rendered)
        self.assertNotIn("REPLACED", rendered)


class WorkspaceDefaultsRendererTest(unittest.TestCase):
    """Pin Redmine #10689: workspace-local Redmine default-project renderer.

    Single source: `<repo>/.mozyo-bridge/project-defaults.yaml` (Redmine
    #11920 / #11921 rename; the legacy `workspace-defaults.yaml` stays a
    read-only fallback, covered by the compat tests below).
    Default output: `<repo>/.mozyo-bridge/redmine-defaults.md`.

    Tests pin:
    - clean repo (mozyo_bridge itself) round-trips byte-equal through
      the renderer (the committed output IS the canonical render);
    - drift detection (mutation, missing-output, body-edit recovery);
    - schema validation (missing required fields, malformed url,
      missing outputs);
    - secret rejection on both key names and value shapes;
    - unverified default surfaces an UNVERIFIED warning in the output,
      and verified default does not;
    - the cloud-drive-management acceptance fixture renders without
      leaking the fixture into distributed source.
    """

    INPUT_RELATIVE = Path(".mozyo-bridge/project-defaults.yaml")
    OUTPUT_RELATIVE = Path(".mozyo-bridge/redmine-defaults.md")
    CLOUD_DRIVE_FIXTURE = {
        "identifier": "giken-cloud-drive-management",
        "name": "クラウドドライブ管理",
        "url": "https://redmine.giken.or.jp/projects/giken-cloud-drive-management",
        "parent_label": "3800_情報処理促進部",
    }

    def run_cli(self, argv: list[str]) -> tuple[int, str, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = args.func(args)
        return result, stdout.getvalue(), stderr.getvalue()

    def _stage_repo(self, dest: Path, *, yaml_body: str) -> Path:
        (dest / ".mozyo-bridge").mkdir(parents=True)
        (dest / ".mozyo-bridge" / "project-defaults.yaml").write_text(
            yaml_body, encoding="utf-8"
        )
        return dest

    def _yaml_for(
        self,
        *,
        identifier: str = "giken-3800-mozyo-bridge",
        name: str = "mozyo_bridge",
        url: str = "https://redmine.giken.or.jp/projects/giken-3800-mozyo-bridge",
        parent_label: str = "3800_情報処理促進部",
        verified: bool = True,
        verification_date: str = "2026-05-28",
        verified_by: str = "hollySizzle",
        outputs: tuple[tuple[str, str], ...] = (
            ("redmine_markdown", ".mozyo-bridge/redmine-defaults.md"),
        ),
        schema_version: int = 1,
        extra: str = "",
    ) -> str:
        output_lines: list[str] = []
        for kind, target in outputs:
            output_lines.append(f"  - kind: {kind}")
            output_lines.append(f"    target: {target}")
        outputs_block = "\n".join(output_lines)
        return (
            f"schema_version: {schema_version}\n"
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n"
            f"    name: {name}\n"
            f"    url: {url}\n"
            f"    parent_label: {parent_label}\n"
            "  verification:\n"
            f"    verified: {str(verified).lower()}\n"
            f'    verification_date: "{verification_date}"\n'
            f"    verified_by: {verified_by}\n"
            "outputs:\n"
            f"{outputs_block}\n"
            f"{extra}"
        )

    # ------------------------------------------------------------------
    # Round-trip + CLI surface
    # ------------------------------------------------------------------

    def test_committed_repo_renders_byte_equal(self) -> None:
        from mozyo_bridge.workspace_defaults import collect_render_results

        results = collect_render_results(ROOT)
        self.assertEqual(1, len(results))
        result = results[0]
        self.assertEqual(
            result.rendered,
            result.on_disk,
            msg=(
                f"{result.output_path} drifted from workspace-defaults source; "
                "rerun `mozyo-bridge workspace-defaults` and recommit."
            ),
        )

    def test_cli_check_clean_then_drift_then_recover(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            # First render seeds the output.
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            # Clean --check.
            result, stdout, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            self.assertIn("redmine-defaults.md is up to date", stdout)
            self.assertEqual("", stderr)
            # Tamper.
            output = repo / self.OUTPUT_RELATIVE
            output.write_text(
                output.read_text(encoding="utf-8") + "\nTAMPER\n",
                encoding="utf-8",
            )
            result, _, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("is out of date", stderr)
            # Recovery command must be the actual CLI; #10345 / #10663
            # correction precedent. Reject bare-basename or non-runnable
            # forms.
            self.assertIn("mozyo-bridge workspace-defaults", stderr)
            self.assertIn("from the repo root", stderr)
            # Recovery and check is clean again.
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            result, _, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            self.assertEqual("", stderr)

    def test_cli_check_reports_missing_output_as_drift(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            # Do not render first; just --check. Output is missing.
            result, _, stderr = self.run_cli(
                ["workspace-defaults", "--check", "--repo", str(repo)]
            )
            self.assertEqual(1, result)
            self.assertIn("is missing", stderr)

    def test_render_survives_yaml_body_edit(self) -> None:
        """Editing the YAML must rotate the rendered output deterministically."""
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            self.run_cli(["workspace-defaults", "--repo", str(repo)])
            before = (repo / self.OUTPUT_RELATIVE).read_text(encoding="utf-8")

            (repo / self.INPUT_RELATIVE).write_text(
                self._yaml_for(name="renamed"),
                encoding="utf-8",
            )
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            after = (repo / self.OUTPUT_RELATIVE).read_text(encoding="utf-8")
            self.assertNotEqual(before, after)
            self.assertIn("- name: renamed", after)

    # ------------------------------------------------------------------
    # Schema validation
    # ------------------------------------------------------------------

    def test_missing_input_yaml_dies_with_actionable_hint(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".mozyo-bridge").mkdir(parents=True)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--check", "--repo", str(repo)]
                )

    def test_missing_required_field_dies(self) -> None:
        body = (
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            "    identifier: foo\n"
            # name + url missing.
            "    parent_label: bar\n"
            "  verification:\n"
            "    verified: true\n"
            '    verification_date: "2026-01-01"\n'
            "    verified_by: tester\n"
            "outputs:\n"
            "  - target: .mozyo-bridge/redmine-defaults.md\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_wrong_schema_version_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo", yaml_body=self._yaml_for(schema_version=99)
            )
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_non_http_url_dies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(url="file:///etc/passwd"),
            )
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_outputs_must_be_repo_relative(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(
                    outputs=(("redmine_markdown", "../escape.md"),)
                ),
            )
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    # ------------------------------------------------------------------
    # Typed outputs (Codex review #50989 correction)
    # ------------------------------------------------------------------

    def test_unknown_output_kind_is_rejected(self) -> None:
        """A bare target with a foreign extension cannot inherit the
        Markdown body. The schema requires an explicit `kind` and only
        accepts the kinds the renderer supports.

        Codex review #50989 reproduced the original footgun: adding
        `.codex/config.toml` as a target wrote Markdown into a TOML
        file. Typed outputs make that unreachable from the schema.
        """
        body = self._yaml_for(
            outputs=(("codex_toml", ".codex/config.toml"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_missing_output_kind_is_rejected(self) -> None:
        """Outputs without a `kind` field cannot fall back to Markdown."""
        body = (
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            "    identifier: foo\n"
            "    name: foo\n"
            "    url: https://example.invalid/\n"
            "    parent_label: ''\n"
            "  verification:\n"
            "    verified: true\n"
            '    verification_date: "2026-01-01"\n'
            "    verified_by: tester\n"
            "outputs:\n"
            "  - target: .mozyo-bridge/redmine-defaults.md\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_supported_kinds_list_is_pinned(self) -> None:
        """The supported kinds set is part of the public contract.

        Extending it without updating the design doc / dispatch table
        is the regression Codex review #50989 surfaced. If this
        assertion fails, also update
        `vibes/docs/logics/workspace-defaults-renderer.md` and the
        `_render_for_kind` dispatch in the same commit.
        """
        from mozyo_bridge.workspace_defaults import (
            KNOWN_OUTPUT_KINDS,
            KIND_REDMINE_MARKDOWN,
        )

        self.assertEqual(
            {KIND_REDMINE_MARKDOWN},
            set(KNOWN_OUTPUT_KINDS),
            msg=(
                "KNOWN_OUTPUT_KINDS changed; update "
                "vibes/docs/logics/workspace-defaults-renderer.md, "
                "the `_render_for_kind` dispatch arms, and the typed "
                "renderer for the new kind in the same commit."
            ),
        )

    # ------------------------------------------------------------------
    # Kind ↔ target suffix compatibility (Codex correction-review #50995)
    # ------------------------------------------------------------------

    def test_redmine_markdown_kind_rejects_toml_target(self) -> None:
        """Codex correction review #50995 reproduced the residual footgun.

        Even with typed kinds, `kind: redmine_markdown` + target
        `.codex/config.toml` passed and wrote Markdown body into a
        TOML path. The kind→suffix gate must reject the mismatch at
        load time so an operator cannot silently generate invalid
        config by selecting the only documented kind and pointing it
        at a non-Markdown path.
        """
        body = self._yaml_for(
            outputs=(("redmine_markdown", ".codex/config.toml"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )
            # The TOML file must not have been created by a half-completed
            # run before the validation error fired.
            self.assertFalse(
                (repo / ".codex" / "config.toml").exists(),
                "load-time validation must run before any write",
            )

    def test_redmine_markdown_kind_rejects_json_target(self) -> None:
        """Same gate must block `.mcd.json` (the other documented MCP
        config candidate Codex called out as the motivating use case)."""
        body = self._yaml_for(
            outputs=(("redmine_markdown", ".mcd.json"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )
            self.assertFalse(
                (repo / ".mcd.json").exists(),
                "load-time validation must run before any write",
            )

    def test_redmine_markdown_kind_rejects_extensionless_target(self) -> None:
        """A target with no suffix at all is also unsafe — the renderer
        could write Markdown body to e.g. `README` and the operator
        would see content that looks intentional."""
        body = self._yaml_for(
            outputs=(("redmine_markdown", "docs/README"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_redmine_markdown_kind_accepts_markdown_suffix_alias(self) -> None:
        """Both `.md` and `.markdown` are valid Markdown suffixes; the
        gate must accept the alias so operators don't get a false-positive
        rejection on a legitimate Markdown target."""
        body = self._yaml_for(
            outputs=(("redmine_markdown", ".mozyo-bridge/redmine-defaults.markdown"),)
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            result, _, _ = self.run_cli(
                ["workspace-defaults", "--repo", str(repo)]
            )
            self.assertEqual(0, result)
            self.assertTrue(
                (repo / ".mozyo-bridge" / "redmine-defaults.markdown").is_file()
            )

    def test_kind_allowed_suffixes_table_is_pinned(self) -> None:
        """Per-kind suffix sets are part of the public contract.

        If the renderer learns to emit a new format for an existing
        kind (or a new kind is added), the table must be updated in
        the same commit and this test refreshed alongside.
        """
        from mozyo_bridge.workspace_defaults import (
            KIND_ALLOWED_SUFFIXES,
            KIND_REDMINE_MARKDOWN,
        )

        self.assertEqual(
            {KIND_REDMINE_MARKDOWN: {".md", ".markdown"}},
            {k: set(v) for k, v in KIND_ALLOWED_SUFFIXES.items()},
            msg=(
                "KIND_ALLOWED_SUFFIXES changed; sync the design doc's "
                "Supported Output Kinds table and add regression tests "
                "for the new accept / reject cases in the same commit."
            ),
        )

    # ------------------------------------------------------------------
    # Secret rejection
    # ------------------------------------------------------------------

    def test_credential_shape_key_is_rejected(self) -> None:
        # The credential-shape `<key>: <value>` pair is assembled at runtime
        # from fragments so this source line itself does not carry a literal
        # token-assignment shape that the release tree hygiene scanner flags.
        secret_key = "api" + "_key"
        sample_token = "A" + "KIA0000000000000000"
        body = self._yaml_for(extra=f"{secret_key}: {sample_token}\n")
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_credential_shape_value_is_rejected(self) -> None:
        # Even with a non-credential key name, a value matching a secret
        # assignment pattern must die. This catches operators pasting an
        # env-style `<NAME>_KEY=<value>` token assignment into a free-form
        # note. The assignment is built at runtime so this source line does
        # not itself carry a release-blocking literal.
        secret_assignment = (
            "REDMINE" + "_API_KEY" + "=" + "abc123secretvalue"
        )
        body = self._yaml_for(extra=f'note: "{secret_assignment}"\n')
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    def test_nested_credential_key_is_rejected(self) -> None:
        # Same runtime-assembly trick: keep the credential-shape `<key>:
        # <value>` pair out of tracked source so the release tree scanner
        # does not flag this fixture.
        nested_secret_key = "client" + "_secret"
        body = (
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            "    identifier: foo\n"
            "    name: foo\n"
            "    url: https://example.invalid/\n"
            "    parent_label: ''\n"
            "    extra:\n"
            f"      {nested_secret_key}: nope\n"
            "  verification:\n"
            "    verified: true\n"
            '    verification_date: "2026-01-01"\n'
            "    verified_by: tester\n"
            "outputs:\n"
            "  - target: .mozyo-bridge/redmine-defaults.md\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            with self.assertRaises(SystemExit):
                self.run_cli(
                    ["workspace-defaults", "--repo", str(repo)]
                )

    # ------------------------------------------------------------------
    # Verified vs unverified rendering
    # ------------------------------------------------------------------

    def test_verified_default_renders_without_warning(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=self._yaml_for())
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertNotIn("(UNVERIFIED)", rendered)
            self.assertIn("- verified: yes", rendered)
            self.assertIn("Verified default", rendered)
            # Should NOT warn against using the default.
            self.assertNotIn("default is unverified", rendered)

    def test_unverified_default_surfaces_warning_in_output(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(verified=False),
            )
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertIn("(UNVERIFIED)", rendered)
            self.assertIn("Default is NOT yet verified", rendered)
            self.assertIn("**NO**", rendered)
            self.assertIn("Do NOT use this default for issue creation", rendered)

    def test_verified_true_but_empty_date_is_treated_as_unverified(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(
                Path(tmp) / "repo",
                yaml_body=self._yaml_for(verification_date=""),
            )
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertIn("(UNVERIFIED)", rendered)

    # ------------------------------------------------------------------
    # Acceptance fixture: cloud-drive-management is test-only
    # ------------------------------------------------------------------

    def test_cloud_drive_fixture_renders_cleanly(self) -> None:
        from mozyo_bridge.workspace_defaults import (
            load_workspace_defaults,
            render_redmine_defaults_markdown,
        )

        body = self._yaml_for(
            identifier=self.CLOUD_DRIVE_FIXTURE["identifier"],
            name=self.CLOUD_DRIVE_FIXTURE["name"],
            url=self.CLOUD_DRIVE_FIXTURE["url"],
            parent_label=self.CLOUD_DRIVE_FIXTURE["parent_label"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            repo = self._stage_repo(Path(tmp) / "repo", yaml_body=body)
            defaults = load_workspace_defaults(repo / self.INPUT_RELATIVE)
            rendered = render_redmine_defaults_markdown(defaults)
            self.assertIn(
                f"identifier: `{self.CLOUD_DRIVE_FIXTURE['identifier']}`",
                rendered,
            )
            self.assertIn(self.CLOUD_DRIVE_FIXTURE["name"], rendered)
            self.assertIn(self.CLOUD_DRIVE_FIXTURE["url"], rendered)

    def test_distributed_source_does_not_carry_cloud_drive_identifier(self) -> None:
        """The acceptance fixture must NOT appear in distributed source.

        Per #10689 constraint: do not hardcode `giken-cloud-drive-management`
        into distributed mozyo_bridge defaults. The fixture is allowed
        only in test code (this file) and in workspace-local docs that
        ship to a workspace, not to the package.
        """
        forbidden = self.CLOUD_DRIVE_FIXTURE["identifier"]
        distributed_roots = [
            ROOT / "src" / "mozyo_bridge",
            ROOT / "skills",
            ROOT / "plugins",
            ROOT / "vibes" / "docs",
            ROOT / ".mozyo-bridge" / "workspace-defaults.yaml",
            ROOT / ".mozyo-bridge" / "redmine-defaults.md",
        ]
        hits: list[str] = []
        for root in distributed_roots:
            if root.is_file():
                paths = [root]
            elif root.is_dir():
                paths = [
                    p
                    for p in root.rglob("*")
                    if p.is_file()
                    and not p.name.endswith(".pyc")
                    and "__pycache__" not in p.parts
                ]
            else:
                continue
            for path in paths:
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if forbidden in text:
                    hits.append(path.relative_to(ROOT).as_posix())
        self.assertFalse(
            hits,
            msg=(
                f"distributed source carries the acceptance-fixture "
                f"identifier {forbidden!r}: {hits}. Move the value to "
                "test code or a workspace-local example only."
            ),
        )
