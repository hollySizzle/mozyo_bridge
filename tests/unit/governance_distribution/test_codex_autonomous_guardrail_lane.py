"""Repo-Local Guardrail Autonomous Lane tests (Redmine #12149, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of CodexAutonomousGuardrailLaneTest out of the
monolithic test spine, per #12145 Priority 2 and
vibes/docs/logics/refactor-split-strategy.md. No test logic changed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))


class CodexAutonomousGuardrailLaneTest(unittest.TestCase):
    """Pin Repo-Local Guardrail Autonomous Lane wording (Redmine #10338).

    Product-wide policy distributed via the governed presets, recorded in
    the project-local rule doc, registered in the catalog, and surfaced in
    the root routers + canonical skill body. Each assertion below names the
    surface so a regression failure points at one file.
    """

    PRESET_AUTONOMOUS_LANE_MARKERS = (
        # Section heading must be present in distributed preset.
        "### Repo-Local Guardrail Autonomous Lane",
        # Default lane paths must be enumerated in the preset.
        "vibes/docs/rules/**",
        "vibes/docs/logics/**",
        "vibes/docs/specs/**",
        ".mozyo-bridge/docs/catalog.yaml",
        # Journal vocabulary and required fields must be named verbatim.
        "codex_autonomous_edit",
        "follow_up_review_required",
        "Codex Direct Edit Gate の carve-out",
        # Required verification command surface for catalog edits.
        "mozyo-bridge docs generate-file-conventions --check",
    )

    def _packaged_preset(self, preset: str) -> str:
        path = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "scaffold"
            / "presets"
            / preset
            / "agent-workflow.md"
        )
        self.assertTrue(path.is_file(), f"missing packaged preset: {path}")
        return path.read_text(encoding="utf-8")

    def _packaged_preset_version(self, preset: str) -> str:
        path = (
            ROOT
            / "src"
            / "mozyo_bridge"
            / "scaffold"
            / "presets"
            / preset
            / "VERSION"
        )
        return path.read_text(encoding="utf-8").strip()

    def test_redmine_governed_preset_ships_autonomous_lane(self) -> None:
        body = self._packaged_preset("redmine-governed")
        for marker in self.PRESET_AUTONOMOUS_LANE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"redmine-governed preset is missing autonomous-lane "
                    f"marker {marker!r}; see Redmine #10338."
                ),
            )

    def test_redmine_rails_governed_preset_ships_autonomous_lane(self) -> None:
        body = self._packaged_preset("redmine-rails-governed")
        for marker in self.PRESET_AUTONOMOUS_LANE_MARKERS:
            self.assertIn(
                marker,
                body,
                msg=(
                    f"redmine-rails-governed preset is missing autonomous-lane "
                    f"marker {marker!r}; see Redmine #10338."
                ),
            )

    def test_governed_preset_versions_were_bumped(self) -> None:
        # The autonomous-lane change is a workflow / guardrail change, so
        # both governed presets must be bumped beyond their last-hardened
        # 2026.05.25.1 version. The scaffold manifest's preset_hash drift
        # check is what enforces consumers re-install; this assertion pins
        # the version label so a future preset edit that forgets to bump
        # fails loudly.
        for preset in ("redmine-governed", "redmine-rails-governed"):
            version = self._packaged_preset_version(preset)
            self.assertNotEqual(
                "2026.05.25.1",
                version,
                msg=(
                    f"{preset} VERSION is still pre-#10338; the autonomous-lane "
                    f"distribution requires a bump."
                ),
            )

    def test_project_local_lane_doc_is_registered_in_catalog(self) -> None:
        catalog_text = (
            ROOT / ".mozyo-bridge" / "docs" / "catalog.yaml"
        ).read_text(encoding="utf-8")
        # Document entry must be registered so the resolver can pull the
        # policy from any lane path. file_convention must exist so the
        # autonomous-lane paths actually resolve to it.
        self.assertIn("id: rule-codex-autonomous-guardrail-lane", catalog_text)
        self.assertIn(
            "vibes/docs/rules/codex-autonomous-guardrail-lane.md",
            catalog_text,
        )
        self.assertIn("fc-codex-autonomous-guardrail-lane", catalog_text)
        # The lane policy file itself must exist (catalog references it).
        lane_doc = (
            ROOT / "vibes" / "docs" / "rules" / "codex-autonomous-guardrail-lane.md"
        )
        self.assertTrue(lane_doc.is_file(), f"lane doc missing: {lane_doc}")

    def test_lane_resolves_from_each_autonomous_path_via_catalog(self) -> None:
        # The lane policy must be reachable from every default-lane path
        # via `mozyo-bridge docs resolve`. We exercise the resolver
        # directly so a future catalog edit that drops the path coverage
        # fails this test, not just a manual `docs resolve` invocation.
        try:
            from mozyo_bridge.docs_tools import CatalogContext, resolve_paths
        except ImportError as exc:
            self.skipTest(f"docs_tools not importable: {exc}")
        context = CatalogContext.build(str(ROOT), None)
        for path in (
            "vibes/docs/rules/codex-autonomous-guardrail-lane.md",
            "vibes/docs/logics/scaffold-rules.md",
            "vibes/docs/specs/project-map.md",
            ".mozyo-bridge/docs/catalog.yaml",
        ):
            results = resolve_paths(context, [path])
            ids = {
                doc["id"]
                for entry in results
                for doc in entry.get("documents", [])
            }
            self.assertIn(
                "rule-codex-autonomous-guardrail-lane",
                ids,
                msg=(
                    f"`mozyo-bridge docs resolve {path}` did not surface the "
                    f"autonomous-lane rule; catalog file_convention coverage "
                    f"regressed."
                ),
            )

    def test_root_routers_name_autonomous_lane(self) -> None:
        for router_name in ("AGENTS.md", "CLAUDE.md"):
            body = (ROOT / router_name).read_text(encoding="utf-8")
            for marker in (
                "Repo-Local Guardrail Autonomous Lane",
                "codex_autonomous_edit",
                "vibes/docs/rules/codex-autonomous-guardrail-lane.md",
                # Routers must restate the gate-still-applies surfaces so
                # an autonomous-lane reader does not assume the carve-out
                # covers everything.
                "skills",
                "src/**",
            ):
                self.assertIn(
                    marker,
                    body,
                    msg=(
                        f"{router_name} is missing autonomous-lane marker "
                        f"{marker!r}; see Redmine #10338."
                    ),
                )

    def test_canonical_skill_reference_carries_autonomous_lane(self) -> None:
        body = (
            ROOT
            / "skills"
            / "mozyo-bridge-agent"
            / "references"
            / "workflow.md"
        ).read_text(encoding="utf-8")
        for marker in (
            "Repo-Local Guardrail Autonomous Lane",
            "codex_autonomous_edit",
            "lane: autonomous",
            "vibes/docs/rules/**",
            "vibes/docs/logics/**",
            "vibes/docs/specs/**",
            ".mozyo-bridge/docs/catalog.yaml",
        ):
            self.assertIn(
                marker,
                body,
                msg=(
                    f"skills/.../workflow.md is missing autonomous-lane "
                    f"marker {marker!r}; see Redmine #10338."
                ),
            )

    def test_plugin_skill_mirror_carries_autonomous_lane(self) -> None:
        # PluginMarketplaceTest already enforces byte equality, but a
        # marker check here points a future regression at "lane wording
        # missing from mirror" rather than a generic mirror-drift error.
        mirror = (
            ROOT
            / "plugins"
            / "mozyo-bridge-agent"
            / "skills"
            / "mozyo-bridge-agent"
            / "references"
            / "workflow.md"
        ).read_text(encoding="utf-8")
        self.assertIn("Repo-Local Guardrail Autonomous Lane", mirror)
        self.assertIn("codex_autonomous_edit", mirror)

    def test_readme_advertises_autonomous_lane(self) -> None:
        body = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("Repo-Local Guardrail Autonomous Lane", body)
        self.assertIn("codex_autonomous_edit", body)
        self.assertIn("vibes/docs/rules/codex-autonomous-guardrail-lane.md", body)


if __name__ == "__main__":
    unittest.main()
