"""Plugin marketplace packaging tests (Redmine #12147, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of PluginMarketplaceTest out of the monolithic
test spine, per #12145 Priority 2 and
vibes/docs/logics/refactor-split-strategy.md. No test logic changed."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))


class PluginMarketplaceTest(unittest.TestCase):
    """Guardrails for the Claude plugin marketplace packaging.

    The repo ships a `.claude-plugin/marketplace.json` at the root and a plugin
    at `plugins/mozyo-bridge-agent/`. The plugin bundles its own copy of the
    shared skill body so it works after Claude Code copies the plugin
    directory into its cache (plugin install cannot reach outside the plugin
    root). This test class enforces:

    1. Marketplace and plugin manifests load and carry the required fields.
    2. The plugin skill mirror at `plugins/mozyo-bridge-agent/skills/mozyo-bridge-agent/`
       stays in lockstep with the canonical `skills/mozyo-bridge-agent/`. Drift
       must be resolved by running `scripts/sync_plugin_skill.sh`, not by
       hand-editing the mirror.
    """

    def setUp(self) -> None:
        self.marketplace_path = ROOT / ".claude-plugin" / "marketplace.json"
        self.plugin_manifest_path = (
            ROOT / "plugins" / "mozyo-bridge-agent" / ".claude-plugin" / "plugin.json"
        )
        self.canonical_skill_dir = ROOT / "skills" / "mozyo-bridge-agent"
        self.plugin_skill_dir = (
            ROOT / "plugins" / "mozyo-bridge-agent" / "skills" / "mozyo-bridge-agent"
        )

    def test_marketplace_manifest_present_and_valid(self) -> None:
        self.assertTrue(
            self.marketplace_path.is_file(),
            f"expected marketplace manifest at {self.marketplace_path}",
        )
        data = json.loads(self.marketplace_path.read_text(encoding="utf-8"))
        # Required top-level fields per
        # https://code.claude.com/docs/en/plugin-marketplaces#marketplace-schema
        self.assertIn("name", data)
        self.assertIn("owner", data)
        self.assertIn("plugins", data)
        self.assertIsInstance(data["owner"], dict)
        self.assertIn("name", data["owner"], "owner.name is required")
        self.assertIsInstance(data["plugins"], list)
        self.assertEqual(
            "mozyo-bridge",
            data["name"],
            "marketplace name pins the install command `@mozyo-bridge` suffix",
        )
        # Marketplace name must not impersonate Anthropic-reserved names.
        reserved = {
            "claude-code-marketplace",
            "claude-code-plugins",
            "claude-plugins-official",
            "anthropic-marketplace",
            "anthropic-plugins",
            "agent-skills",
            "knowledge-work-plugins",
            "life-sciences",
        }
        self.assertNotIn(data["name"], reserved)

    def test_marketplace_lists_mozyo_bridge_agent_plugin(self) -> None:
        data = json.loads(self.marketplace_path.read_text(encoding="utf-8"))
        names = [entry.get("name") for entry in data["plugins"]]
        self.assertIn("mozyo-bridge-agent", names)
        entry = next(p for p in data["plugins"] if p.get("name") == "mozyo-bridge-agent")
        self.assertIn("source", entry)
        # Relative paths must start with "./" per plugin source rules.
        source = entry["source"]
        if isinstance(source, str):
            self.assertTrue(
                source.startswith("./"),
                "relative plugin source must start with './'",
            )
            # When metadata.pluginRoot is set, the source is resolved under it.
            plugin_root = data.get("metadata", {}).get("pluginRoot")
            if plugin_root:
                base = (
                    ROOT / plugin_root.lstrip("./").rstrip("/")
                    if plugin_root.startswith("./")
                    else ROOT / plugin_root
                )
                resolved = (base / source.lstrip("./").rstrip("/")).resolve()
            else:
                resolved = (ROOT / source.lstrip("./").rstrip("/")).resolve()
            self.assertTrue(
                resolved.is_dir(),
                f"plugin source path does not resolve to a directory: {resolved}",
            )

    def test_plugin_manifest_present_and_valid(self) -> None:
        self.assertTrue(
            self.plugin_manifest_path.is_file(),
            f"expected plugin manifest at {self.plugin_manifest_path}",
        )
        data = json.loads(self.plugin_manifest_path.read_text(encoding="utf-8"))
        # `name` is the only required plugin manifest field.
        self.assertIn("name", data)
        self.assertEqual("mozyo-bridge-agent", data["name"])

    def test_plugin_skill_mirror_matches_canonical(self) -> None:
        """The plugin's skill copy must be byte-identical to the canonical
        skill body. Run `scripts/sync_plugin_skill.sh` to regenerate the mirror
        whenever you edit `skills/mozyo-bridge-agent/`."""

        def relative_files(base: Path) -> dict[str, str]:
            mapping: dict[str, str] = {}
            for path in base.rglob("*"):
                if not path.is_file():
                    continue
                rel = path.relative_to(base).as_posix()
                mapping[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
            return mapping

        self.assertTrue(
            self.canonical_skill_dir.is_dir(),
            f"canonical skill missing: {self.canonical_skill_dir}",
        )
        self.assertTrue(
            self.plugin_skill_dir.is_dir(),
            f"plugin skill mirror missing: {self.plugin_skill_dir}",
        )

        canonical = relative_files(self.canonical_skill_dir)
        mirror = relative_files(self.plugin_skill_dir)

        missing = sorted(set(canonical) - set(mirror))
        extra = sorted(set(mirror) - set(canonical))
        differing = sorted(
            rel for rel in canonical.keys() & mirror.keys() if canonical[rel] != mirror[rel]
        )

        hint = "run scripts/sync_plugin_skill.sh to regenerate the mirror"
        self.assertFalse(missing, f"plugin mirror missing files: {missing}; {hint}")
        self.assertFalse(extra, f"plugin mirror has unexpected files: {extra}; {hint}")
        self.assertFalse(
            differing, f"plugin mirror content differs from canonical: {differing}; {hint}"
        )

    def test_plugin_skill_mirror_has_skill_md(self) -> None:
        self.assertTrue(
            (self.plugin_skill_dir / "SKILL.md").is_file(),
            "plugin must ship SKILL.md so Claude Code can discover the skill after install",
        )

    # ------------------------------------------------------------------
    # Redmine #10663: pin the sync script's --check mode so CI can gate
    # on plugin mirror drift without modifying the worktree. The Python
    # walker in test_plugin_skill_mirror_matches_canonical above does the
    # same check via a different code path; both must agree.
    # ------------------------------------------------------------------

    SYNC_SCRIPT_PATH = ROOT / "scripts" / "sync_plugin_skill.sh"

    def test_sync_script_check_mode_clean_exits_zero(self) -> None:
        """`scripts/sync_plugin_skill.sh --check` exits 0 when in sync.

        This pins the operator-facing CI gate for plugin mirror drift.
        If the check mode regresses to silently writing to the mirror,
        or to always-exit-0, this test fails.
        """
        self.assertTrue(self.SYNC_SCRIPT_PATH.is_file())
        result = subprocess.run(
            ["sh", str(self.SYNC_SCRIPT_PATH), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(
            0,
            result.returncode,
            msg=(
                f"sync_plugin_skill.sh --check exited {result.returncode}; "
                f"stdout={result.stdout!r} stderr={result.stderr!r}. "
                "Either the mirror drifted or the --check mode regressed."
            ),
        )
        self.assertIn("up to date", result.stdout)

    def test_sync_script_check_mode_detects_drift(self) -> None:
        """`--check` must exit non-zero on drift and name the recovery command."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "scripts").mkdir()
            shutil.copy(self.SYNC_SCRIPT_PATH, stage / "scripts" / "sync_plugin_skill.sh")
            (stage / "scripts" / "sync_plugin_skill.sh").chmod(0o755)
            shutil.copytree(self.canonical_skill_dir, stage / "skills" / "mozyo-bridge-agent")
            shutil.copytree(
                self.plugin_skill_dir,
                stage / "plugins" / "mozyo-bridge-agent" / "skills" / "mozyo-bridge-agent",
            )

            tampered = (
                stage
                / "plugins"
                / "mozyo-bridge-agent"
                / "skills"
                / "mozyo-bridge-agent"
                / "references"
                / "workflow.md"
            )
            tampered.write_text(
                tampered.read_text(encoding="utf-8") + "\nTAMPER\n",
                encoding="utf-8",
            )

            result = subprocess.run(
                ["sh", str(stage / "scripts" / "sync_plugin_skill.sh"), "--check"],
                cwd=str(stage),
                capture_output=True,
                text=True,
            )
            self.assertEqual(
                1,
                result.returncode,
                msg=(
                    f"--check did not flag drift; stdout={result.stdout!r} "
                    f"stderr={result.stderr!r}"
                ),
            )
            # Recovery hint must be copy-paste runnable from the repo root,
            # not just the basename. Codex review #50344 caught a regression
            # where `$(basename "$0")` printed only `sync_plugin_skill.sh`,
            # which fails with `command not found` when pasted into a
            # repo-root shell. Pin the full `scripts/<name>` form so a
            # future edit cannot quietly drop the directory prefix.
            self.assertIn("scripts/sync_plugin_skill.sh", result.stderr)
            self.assertIn("from the repo root", result.stderr)
            self.assertIn("references/workflow.md", result.stderr)
            # And the bare basename without a directory prefix must never
            # appear as a standalone recovery command.
            self.assertNotIn("'sync_plugin_skill.sh'", result.stderr)

    def test_sync_script_check_mode_does_not_modify_worktree(self) -> None:
        """`--check` must be read-only — no rsync to disk."""
        with tempfile.TemporaryDirectory() as tmp:
            stage = Path(tmp)
            (stage / "scripts").mkdir()
            shutil.copy(self.SYNC_SCRIPT_PATH, stage / "scripts" / "sync_plugin_skill.sh")
            (stage / "scripts" / "sync_plugin_skill.sh").chmod(0o755)
            shutil.copytree(self.canonical_skill_dir, stage / "skills" / "mozyo-bridge-agent")
            shutil.copytree(
                self.plugin_skill_dir,
                stage / "plugins" / "mozyo-bridge-agent" / "skills" / "mozyo-bridge-agent",
            )

            mirror_workflow = (
                stage
                / "plugins"
                / "mozyo-bridge-agent"
                / "skills"
                / "mozyo-bridge-agent"
                / "references"
                / "workflow.md"
            )
            # Force a drift the script's check would report.
            mirror_workflow.write_text(
                mirror_workflow.read_text(encoding="utf-8") + "\nTAMPER\n",
                encoding="utf-8",
            )
            before = mirror_workflow.read_bytes()

            subprocess.run(
                ["sh", str(stage / "scripts" / "sync_plugin_skill.sh"), "--check"],
                cwd=str(stage),
                capture_output=True,
                text=True,
            )

            after = mirror_workflow.read_bytes()
            self.assertEqual(
                before,
                after,
                msg=(
                    "--check modified the mirror file; the recovery command "
                    "is the rewrite path, not --check."
                ),
            )

    def test_sync_script_rejects_unknown_flag(self) -> None:
        """Reject typos to avoid silently running the wrong mode."""
        result = subprocess.run(
            ["sh", str(self.SYNC_SCRIPT_PATH), "--bogus"],
            cwd=ROOT,
            capture_output=True,
            text=True,
        )
        self.assertEqual(64, result.returncode)
        self.assertIn("unknown argument", result.stderr)


if __name__ == "__main__":
    unittest.main()
