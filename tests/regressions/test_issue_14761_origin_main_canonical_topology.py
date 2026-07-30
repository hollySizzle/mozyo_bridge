"""Redmine #14761 — ``origin/main`` is this repo's ONE development / integration base.

The owner intent (#14761 description, Start Gate j#94705): ``origin/main`` is the base for
new lanes, the integration target after review approval, and the single canonical branch for
ordinary development. ``main-next`` is frozen — kept as a rollback ref, never a write target.

Three surfaces have to agree for that to be true, and they are exactly the three that can
drift apart silently:

1. ``.mozyo-bridge/config.yaml`` — what the runtime resolves ``integration_branch`` to.
2. ``vibes/docs/logics/coordinator-sublane-development-flow.md`` — the repo-local adoption
   record an agent reads before deciding where to integrate.
3. ``skills/mozyo-bridge-agent/references/workflow.md`` — the distributed Publication
   checkpoint doctrine, whose staged-topology normative text (``Redmine Version close`` gates
   the ``origin/main`` push) would otherwise apply unconditionally and stall every integration
   in a project whose integration target IS ``origin/main``.

The config branch is never re-listed below: surfaces 2 and 3 are checked against whatever
the config actually resolves to, so flipping the config alone cannot make this file pass.

The doctrine check is a shape rule, not a literal list. Every ``###`` subsection inside the
Publication checkpoint section must carry an explicit ``**適用: ...**`` marker naming the
topology it binds. A hand-maintained list of staged-only headings is what would rot; the
requirement that a new subsection declares its own applicability does not.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.repo_local_config_loader import (
    load_repo_local_config_from_path,
)

CONFIG_PATH = ROOT / ".mozyo-bridge" / "config.yaml"
ADOPTION_DOC = ROOT / "vibes" / "docs" / "logics" / "coordinator-sublane-development-flow.md"

#: The canonical skill body plus every mirror of it. Derived rather than listed one by one so
#: a new mirror is covered; the mirror-parity drift check lives elsewhere, what matters here
#: is that no COPY carries an unconditional staged doctrine.
SKILL_WORKFLOW_BODIES = (
    ROOT / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md",
    ROOT / ".claude" / "skills" / "mozyo-bridge-agent" / "references" / "workflow.md",
    ROOT
    / "plugins"
    / "mozyo-bridge-agent"
    / "skills"
    / "mozyo-bridge-agent"
    / "references"
    / "workflow.md",
)

#: The frozen former staging branch. It may still be NAMED (the adoption record has to say it
#: is frozen), but never as this repo's integration target.
FROZEN_STAGING_BRANCH = "main-next"

_PUBLICATION_SECTION_HEADING = "## Publication checkpoint"
_APPLICABILITY_RE = re.compile(r"^\*\*適用:\s*(?P<scope>[^*]+?)\s*\*\*", re.MULTILINE)


def _section(text: str, heading_prefix: str, level: int) -> str:
    """The body of the first ``#`` * ``level`` heading starting with ``heading_prefix``.

    Ends at the next heading of the same or a shallower level, so subsections stay inside.
    """
    lines = text.splitlines()
    start = None
    for index, line in enumerate(lines):
        if line.startswith(heading_prefix) and line.startswith("#" * level + " "):
            start = index
            break
    if start is None:  # pragma: no cover - assertion in the caller reports it
        return ""
    end = len(lines)
    for index in range(start + 1, len(lines)):
        stripped = lines[index]
        if stripped.startswith("#"):
            depth = len(stripped) - len(stripped.lstrip("#"))
            if 0 < depth <= level and stripped[depth : depth + 1] == " ":
                end = index
                break
    return "\n".join(lines[start:end])


class ConfiguredIntegrationBranchTest(unittest.TestCase):
    """The runtime answer: what does this repo integrate into?"""

    def test_config_resolves_integration_branch_to_the_public_history_branch(self) -> None:
        config = load_repo_local_config_from_path(CONFIG_PATH)
        branch = config.sublane_integration.integration_branch
        self.assertEqual(
            branch,
            "main",
            "#14761 made origin/main the single canonical integration target; "
            f".mozyo-bridge/config.yaml resolves integration_branch to {branch!r}",
        )

    def test_configured_branch_is_a_branch_name_not_a_remote_tracking_spelling(self) -> None:
        # ``origin/main`` here would make every ancestry / merge probe resolve a local
        # remote-tracking ref instead of the branch the coordinator pushes.
        branch = load_repo_local_config_from_path(CONFIG_PATH).sublane_integration
        self.assertIsNotNone(branch.integration_branch)
        assert branch.integration_branch is not None  # narrowed for type readers
        self.assertNotIn(
            "/",
            branch.integration_branch,
            "integration_branch is a branch name; remote-tracking spellings "
            "(origin/<branch>) resolve a different ref",
        )


class RepoLocalAdoptionRecordTest(unittest.TestCase):
    """The doc an agent reads before deciding where to integrate."""

    def setUp(self) -> None:
        self.branch = load_repo_local_config_from_path(
            CONFIG_PATH
        ).sublane_integration.integration_branch
        assert self.branch is not None
        self.text = ADOPTION_DOC.read_text(encoding="utf-8")
        self.section = _section(self.text, "### Publication checkpoint の採用", 3)
        self.assertTrue(
            self.section,
            f"{ADOPTION_DOC.name} lost its '### Publication checkpoint の採用' section; "
            "the repo-local topology declaration lives there",
        )

    def test_adoption_record_declares_the_single_canonical_branch_topology(self) -> None:
        self.assertIn(
            "single-canonical-branch",
            self.section,
            "the distributed doctrine requires an explicit branch-topology declaration; "
            "an undeclared repo makes an agent apply the staged checkpoint by default",
        )

    def test_adoption_record_names_the_branch_the_config_resolves(self) -> None:
        self.assertRegex(
            self.section,
            rf"`origin/{re.escape(self.branch)}`",
            "the adoption record must name the same branch the runtime config resolves; "
            f"config says {self.branch!r}",
        )

    def test_adoption_record_declares_the_former_staging_branch_frozen(self) -> None:
        self.assertIn(
            FROZEN_STAGING_BRANCH,
            self.section,
            "a former staging branch that is simply dropped from the docs is "
            "indistinguishable from a live integration target to the next agent",
        )
        self.assertIn(
            "凍結",
            self.section,
            f"{FROZEN_STAGING_BRANCH} must be declared frozen, not merely unmentioned",
        )

    def test_no_surviving_claim_that_main_next_is_the_integration_target(self) -> None:
        stale = [
            line
            for line in self.text.splitlines()
            if FROZEN_STAGING_BRANCH in line and "integration target として運用済み" in line
        ]
        self.assertEqual(
            stale,
            [],
            "the pre-#14761 sentence declaring main-next the integration target is still "
            "present; two docs answering the same question differently is the failure",
        )


class DistributedDoctrineTopologyTest(unittest.TestCase):
    """The portable doctrine must bind its staged normativity to a declared topology."""

    def _publication_section(self, path: Path) -> str:
        section = _section(
            path.read_text(encoding="utf-8"), _PUBLICATION_SECTION_HEADING, 2
        )
        self.assertTrue(
            section,
            f"{path} lost its '{_PUBLICATION_SECTION_HEADING}' section",
        )
        return section

    def test_every_body_offers_the_topology_declaration_subsection(self) -> None:
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                section = self._publication_section(path)
                self.assertIn(
                    "### branch topology を宣言する",
                    section,
                    "without the declaration duty, the staged checkpoint reads as the "
                    "only topology and stalls a single-canonical-branch project",
                )
                self.assertIn(
                    "### single-canonical-branch topology",
                    section,
                    "a project whose integration target is the public-history branch "
                    "needs its own normative subsection, not an exception buried in prose",
                )

    def test_every_subsection_declares_which_topology_it_binds(self) -> None:
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                section = self._publication_section(path)
                lines = section.splitlines()
                headings = [
                    index
                    for index, line in enumerate(lines)
                    if line.startswith("### ")
                ]
                self.assertTrue(headings, f"{path} has no Publication checkpoint subsections")
                for position, index in enumerate(headings):
                    end = headings[position + 1] if position + 1 < len(headings) else len(lines)
                    body = "\n".join(lines[index + 1 : end])
                    self.assertRegex(
                        body,
                        _APPLICABILITY_RE,
                        f"{lines[index]!r} carries no '**適用: ...**' marker; a subsection "
                        "whose topology is undeclared is applied unconditionally",
                    )

    def test_release_gate_stays_independent_of_branch_topology(self) -> None:
        # The one boundary a topology switch must NOT move: reaching the integration branch
        # is never an authorization to tag / bump / publish.
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                section = self._publication_section(path)
                release_gate = _section(section, "### release gate は publication とは別", 3)
                self.assertTrue(release_gate, f"{path} lost the release-gate subsection")
                scope = _APPLICABILITY_RE.search(release_gate)
                self.assertIsNotNone(scope, "release gate subsection lost its 適用 marker")
                assert scope is not None
                self.assertIn(
                    "両 topology",
                    scope.group("scope"),
                    "the release gate binds in both topologies; scoping it to one would let "
                    "a topology switch decide a publish",
                )
                self.assertIn(
                    "direct_owner",
                    release_gate,
                    "release / publish still requires direct_owner approval",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
