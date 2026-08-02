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

#: The governing rule the distributed doctrine must not contradict. Read from the in-repo
#: scaffold source rather than the installed repo-local copy: the two are kept byte-identical
#: by `scaffold canonical --check`, and this one is the version-controlled distribution input.
CENTRAL_PRESET = (
    ROOT
    / "src"
    / "mozyo_bridge"
    / "scaffold"
    / "presets"
    / "redmine-governed"
    / "agent-workflow.md"
)

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

#: Concepts that only exist when the integration branch and the public-history branch are
#: DIFFERENT. Stated without naming a topology they read as universal norms — which is exactly
#: how a single-canonical project ends up waiting for a checkpoint that does not exist.
#: ``昇格`` (promotion to public history) is on this list because a topology whose integration
#: target IS the public-history branch has no promotion step to gate.
STAGED_ONLY_TOKENS = ("Redmine Version", "staging branch", "push_waiting", "昇格")

#: Naming any of these binds the surrounding claim to a topology, which is all this rule asks.
TOPOLOGY_QUALIFIERS = ("staged", "single-canonical", "topology")

#: The gate order the whole US exists to make unambiguous. Review approval gates the
#: INTEGRATION; owner close approval gates the ISSUE CLOSE. Stating them as an unordered set is
#: what let ``owner close approval`` be read as a pre-integration gate.
GATE_ORDER = (
    "Review Gate approval",
    "integration disposition",
    "owner close approval",
    "Close Gate",
)

#: ``「…」`` spans quote the owner verbatim; the quote's own ``。`` are not sentence breaks of
#: the surrounding prose.
_QUOTED_SPAN_RE = re.compile(r"「[^」]*」")


def _preamble(section: str) -> str:
    """The section body BEFORE its first ``###`` subsection.

    The applicability markers live on subsections, so a norm parked above the first one is
    read by every topology while being checked by none.
    """
    lines = section.splitlines()[1:]  # drop the ``##`` heading itself
    for index, line in enumerate(lines):
        if line.startswith("### "):
            return "\n".join(lines[:index])
    return "\n".join(lines)


def _sentences(text: str) -> list[str]:
    without_quotes = _QUOTED_SPAN_RE.sub("「」", text)
    return [part for part in re.split(r"(?<=。)", without_quotes) if part.strip()]


def _states_in_order(line: str, tokens: tuple[str, ...]) -> bool:
    """Does ``line`` contain ``tokens`` in this order?

    Scanned sequentially — each token is looked for AFTER the previous match — because a
    prose line legitimately names some of these terms more than once (``integration
    disposition`` appears both as a lane-state pointer and inside the ordering chain).
    Comparing first occurrences would report a correctly ordered chain as unordered.
    """
    cursor = 0
    for token in tokens:
        index = line.find(token, cursor)
        if index < 0:
            return False
        cursor = index + len(token)
    return True


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

    def test_adoption_record_orders_the_gates(self) -> None:
        ordered = [
            line
            for line in self.section.splitlines()
            if _states_in_order(line, GATE_ORDER)
        ]
        self.assertTrue(
            ordered,
            "the repo-local adoption record does not state the gate order "
            f"{' -> '.join(GATE_ORDER)}; the distributed doctrine and the adoption record "
            "have to answer 'what triggers the integration push' the same way",
        )

    def test_no_bullet_claims_both_a_review_trigger_and_a_close_trigger(self) -> None:
        # The regression form: one bullet calling origin/main the "review 承認後の
        # integration target" AND the "UserStory close 後の自律 push 先". Two triggers for
        # one push is not a clarification, it is a fork the next agent has to guess at.
        conflicting = [
            line.strip()
            for line in self.section.splitlines()
            if "close 後" in line and ("push 先" in line or "push する" in line)
        ]
        self.assertEqual(
            conflicting,
            [],
            "these bullets tie the integration push to issue close as well as to review "
            f"approval: {conflicting}",
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

    def test_section_preamble_declares_its_own_topology_scope(self) -> None:
        # The subsection rule above starts at the FIRST ``###``. Everything above it was
        # unchecked, so the section could open with a staged-only norm and still pass.
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                preamble = _preamble(self._publication_section(path))
                self.assertRegex(
                    preamble,
                    _APPLICABILITY_RE,
                    "the Publication checkpoint preamble carries no '**適用: ...**' marker; "
                    "text above the first subsection is read by both topologies",
                )

    def test_section_preamble_states_no_unconditional_staged_norm(self) -> None:
        # A staged-only concept named in the preamble must say which topology it belongs to,
        # in the same sentence. This is the shape the review found missing: the preamble
        # defined promotion-to-public-history as a separate owner-gated checkpoint before the
        # reader ever reaches the topology declaration.
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                preamble = _preamble(self._publication_section(path))
                offenders = [
                    sentence.strip()
                    for sentence in _sentences(preamble)
                    if any(token in sentence for token in STAGED_ONLY_TOKENS)
                    and not any(
                        qualifier in sentence for qualifier in TOPOLOGY_QUALIFIERS
                    )
                ]
                self.assertEqual(
                    offenders,
                    [],
                    "these preamble sentences state a staged-only concept without naming a "
                    "topology, so a single-canonical project reads them as binding: "
                    f"{offenders}",
                )

    def test_single_canonical_subsection_orders_the_gates(self) -> None:
        # Review approval gates the integration; owner close approval gates the close. Listed
        # as an unordered set, ``owner close approval`` reads as a pre-integration gate and a
        # review-approved integration waits for a close that is waiting for the integration.
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                subsection = _section(
                    self._publication_section(path),
                    "### single-canonical-branch topology",
                    3,
                )
                self.assertTrue(subsection, f"{path} lost the single-canonical subsection")
                ordered = [
                    line
                    for line in subsection.splitlines()
                    if _states_in_order(line, GATE_ORDER)
                ]
                self.assertTrue(
                    ordered,
                    "no line states the gate order "
                    f"{' -> '.join(GATE_ORDER)}; without it the four gates read as an "
                    "unordered set and the integration trigger is ambiguous",
                )

    def test_single_canonical_subsection_denies_close_as_an_integration_precondition(
        self,
    ) -> None:
        # The ordering statement alone can be read as descriptive. The subsection also has to
        # rule out the wrong reading, because that reading is the one that stalls a lane.
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                subsection = _section(
                    self._publication_section(path),
                    "### single-canonical-branch topology",
                    3,
                )
                self.assertIn(
                    "統合の前提条件",
                    subsection,
                    "the subsection never says owner close approval is NOT a precondition of "
                    "integration; stating the order without denying the inverse leaves the "
                    "stalling reading available",
                )

    def test_staged_integration_is_triggered_by_review_approval(self) -> None:
        # The staged layer integrates into a DIFFERENT branch than the public history, but it
        # is triggered by the same thing: Review Gate approval. Saying "after the UserStory is
        # closed" makes a staged adopter wait for owner close approval before integrating —
        # the same stall this US fixed on the single-canonical side, and a contradiction with
        # `## Integration disposition と push authority`, which the subsection itself cites.
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                subsection = _section(
                    self._publication_section(path), "### integration 層", 3
                )
                self.assertTrue(subsection, f"{path} lost the staged integration subsection")
                granting = [
                    line
                    for line in subsection.splitlines()
                    # Bullets only. The subsection heading names the same action ("staging
                    # branch への自律 push") without granting it, and a heading carries no
                    # trigger by construction.
                    if line.startswith("- ")
                    and ("自律 push" in line or "push してよい" in line)
                ]
                self.assertTrue(
                    granting,
                    "the staged integration subsection no longer grants the staging push; "
                    "if it moved, this rule has to move with it",
                )
                for line in granting:
                    self.assertIn(
                        "Review Gate approval",
                        line,
                        "a line granting the staging push must name Review Gate approval as "
                        f"its trigger; this one does not: {line.strip()!r}",
                    )

    def test_staged_integration_denies_issue_close_as_a_precondition(self) -> None:
        for path in SKILL_WORKFLOW_BODIES:
            with self.subTest(path=str(path.relative_to(ROOT))):
                subsection = _section(
                    self._publication_section(path), "### integration 層", 3
                )
                self.assertIn(
                    "前提条件ではない",
                    subsection,
                    "the staged subsection never rules out UserStory close / owner close "
                    "approval as an integration precondition; stating the right trigger "
                    "without denying the wrong one leaves the stalling reading available",
                )

    def test_central_preset_still_triggers_integration_on_review_approval(self) -> None:
        # The cross-check that makes the two tests above more than a duplicated constant: if
        # the governing preset ever moves the integration trigger, this fails and forces the
        # distributed doctrine to be re-derived instead of silently drifting from it.
        text = CENTRAL_PRESET.read_text(encoding="utf-8")
        trigger = (
            "integration branch (origin/main / release branch) を前進させるのは "
            "review 承認後の coordinator"
        )
        # assertTrue, not assertIn: the haystack is a ~96KB rule book and assertIn prints it
        # in full on failure, burying the message.
        self.assertTrue(
            trigger in text,
            f"{CENTRAL_PRESET.name} no longer states a review-approval integration trigger "
            f"({trigger!r}); the skill's staged and single-canonical subsections are derived "
            "from it and must be re-derived, not left to drift",
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
