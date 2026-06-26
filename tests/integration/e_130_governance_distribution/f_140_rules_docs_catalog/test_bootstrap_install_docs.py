"""Bootstrap entrypoint + install-command consistency doc tests (Redmine #12150, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of the README/bootstrap entrypoint and install-command
occurrence-count doc gates out of the monolithic test spine, per #12150 and
vibes/docs/logics/refactor-split-strategy.md (Priority 1 low-risk families).
No test logic changed."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))


class InstallCommandConsistencyTest(unittest.TestCase):
    """Pin Redmine #10699: install-command snippets stay byte-equal across docs.

    Investigation cataloged the install guidance duplication. The
    operator-facing install commands (plugin marketplace add / plugin
    install / pipx install / rules install / Codex `$skill-installer`)
    appear verbatim in README.md, skill-distribution.md, and bootstrap.md
    — multiple occurrences in each. These are *exact-string* copies,
    not audience-specific variants: if one drifts (e.g. a marketplace
    name change updates README but not skill-distribution), users get
    inconsistent copy-paste recipes.

    Owner decision explicitly excludes whole-file README / ReleaseDocs
    canonicalization. So drift is gated at the test layer — the lightest
    available mechanic — mirroring the `SkillCrossWorkspaceGuidanceTest`
    / `SkillWorkflowSemanticAnchorsTest` pattern.

    Codex correction review #51114 caught the soundness gap in the
    original `assertIn`-based gate: a doc with N occurrences whose
    single occurrence drifts still satisfies `assertIn` because the
    other (N-1) occurrences remain. The fix is to pin **exact
    occurrence counts** per (command, doc). One occurrence drifting
    flips the count by 1 and fails the gate. The counts are small
    (<10 per doc) and stable enough that intentional doc edits update
    the same map in the same commit, mirroring how
    `SkillWorkflowSemanticAnchorsTest` adds new markers.

    The intentionally audience-specific variants (`pipx install --force
    git+https://...` for Beta Tester Install, `claude plugin install
    --scope <other>` for fallback paths) are pinned separately so a
    future edit cannot collapse them into the canonical form.
    """

    # Per (canonical command, doc) → expected exact occurrence count.
    # Counts come from `str.count(command)` over the doc body. A 0 means
    # the command must NOT appear in that doc (audience scope guard).
    #
    # Updating counts: when a doc legitimately gains or loses an
    # install-command mention (prose addition, section removal, etc.),
    # update the count in the same commit. The test failure message
    # spells out the expected vs actual count to make the diff obvious.
    PINNED_INSTALL_OCCURRENCES: tuple[tuple[str, dict[str, int]], ...] = (
        (
            "claude plugin marketplace add hollySizzle/mozyo_bridge",
            {
                "README.md": 2,
                "vibes/docs/logics/skill-distribution.md": 4,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
        (
            "claude plugin install mozyo-bridge-agent@mozyo-bridge --scope user",
            {
                "README.md": 2,
                "vibes/docs/logics/skill-distribution.md": 4,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
        (
            "pipx install mozyo-bridge",
            {
                "README.md": 1,
                "vibes/docs/logics/skill-distribution.md": 3,
                "vibes/docs/logics/bootstrap.md": 2,
                "vibes/docs/logics/scaffold-rules.md": 1,
            },
        ),
        (
            "mozyo-bridge rules install",
            {
                "README.md": 5,
                "vibes/docs/logics/skill-distribution.md": 5,
                "vibes/docs/logics/bootstrap.md": 9,
                "vibes/docs/logics/scaffold-rules.md": 9,
            },
        ),
        # Codex `$skill-installer` invocation against the canonical
        # GitHub skill path. The `$` shell sigil is included so the
        # full operator-pasted command is pinned. The single
        # skill-distribution occurrence is in the Install Command
        # Drift subsection that records this gate's policy.
        (
            "$skill-installer https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent",
            {
                "README.md": 1,
                "vibes/docs/logics/skill-distribution.md": 1,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
        # The canonical-path string Codex must call the installer
        # against. The URL is the most drift-prone token (a repo move
        # would invalidate every occurrence at once) and appears in
        # multiple wording shapes per doc; pinning the exact count
        # catches a single-occurrence rename.
        (
            "https://github.com/hollySizzle/mozyo_bridge/tree/main/skills/mozyo-bridge-agent",
            {
                "README.md": 2,
                "vibes/docs/logics/skill-distribution.md": 4,
                "vibes/docs/logics/bootstrap.md": 1,
            },
        ),
    )

    # Intentional audience-specific variants. Each (variant, doc) →
    # expected count. The variant must remain present at the recorded
    # count so an accidental collapse to the canonical PyPI form fails
    # loudly. Beta Tester / GitHub main install + Fresh Install Smoke
    # are intentionally distinguishable from the standard PyPI form.
    INTENTIONAL_VARIANT_OCCURRENCES: tuple[tuple[str, dict[str, int]], ...] = (
        (
            "pipx install --force git+https://github.com/hollySizzle/mozyo_bridge.git",
            {"README.md": 1},
        ),
    )

    def _read(self, relative: str) -> str:
        return (ROOT / relative).read_text(encoding="utf-8")

    def _assert_count(self, body: str, command: str, *, doc: str, expected: int) -> None:
        actual = body.count(command)
        self.assertEqual(
            expected,
            actual,
            msg=(
                f"{doc} occurrence count for {command!r} drifted: "
                f"expected {expected}, found {actual}. "
                f"Either one occurrence was rewritten while others stayed "
                f"intact (single-occurrence drift — fix the rewrite), or "
                f"a doc edit intentionally added/removed a mention "
                f"(update PINNED_INSTALL_OCCURRENCES in the same commit). "
                f"Codex review #51114 introduced count-pinning specifically "
                f"to catch single-occurrence drift that assertIn missed."
            ),
        )

    def test_canonical_install_commands_have_pinned_occurrence_counts(self) -> None:
        for command, doc_counts in self.PINNED_INSTALL_OCCURRENCES:
            for doc, expected in doc_counts.items():
                with self.subTest(command=command, doc=doc):
                    self._assert_count(
                        self._read(doc), command, doc=doc, expected=expected
                    )

    def test_intentional_install_variants_have_pinned_occurrence_counts(self) -> None:
        for variant, doc_counts in self.INTENTIONAL_VARIANT_OCCURRENCES:
            for doc, expected in doc_counts.items():
                with self.subTest(variant=variant, doc=doc):
                    self._assert_count(
                        self._read(doc), variant, doc=doc, expected=expected
                    )

    def test_count_gate_catches_single_occurrence_drift(self) -> None:
        """Regression meta-test: prove the count-pinning gate detects
        single-occurrence drift that the prior `assertIn` gate missed.

        Codex correction review #51114 requested explicit proof that a
        single-occurrence rewrite (one of N copies drifts while the
        others stay verbatim) fails the gate. This test takes a real
        doc with N > 1 occurrences of a pinned command, mutates the
        FIRST occurrence in memory, and asserts the count delta would
        fail the gate's equality check.
        """
        # Pick a command whose pinned count is > 1 in at least one doc.
        # README.md has marketplace_add count == 2.
        command = "claude plugin marketplace add hollySizzle/mozyo_bridge"
        doc = "README.md"
        expected = next(
            counts[doc]
            for cmd, counts in self.PINNED_INSTALL_OCCURRENCES
            if cmd == command and doc in counts
        )
        self.assertGreater(
            expected,
            1,
            msg="meta-test premise: pick a (command, doc) with count > 1",
        )

        body = self._read(doc)
        # Mutate FIRST occurrence only (the typo a real reviewer might
        # introduce when only updating one mention).
        drifted_form = "claude plugin marketplace add hollyizzle/mozyo_bridge"
        mutated = body.replace(command, drifted_form, 1)

        # Sanity: the mutation was applied AND only the first occurrence
        # was changed.
        self.assertEqual(expected - 1, mutated.count(command))
        self.assertEqual(1, mutated.count(drifted_form))

        # The count gate fires on the (expected vs expected-1) mismatch.
        # The prior `assertIn(command, mutated)` would still pass
        # because (expected - 1) >= 1 (one intact occurrence remains).
        self.assertNotEqual(expected, mutated.count(command))
        self.assertIn(command, mutated)  # documents the gap assertIn left


class BootstrapEntrypointDocsTest(unittest.TestCase):
    """Redmine #10857: README is the install/bootstrap entrypoint.

    Pins the refactor's intent so the docs cannot silently regress to
    routing first-time readers straight into the deep bootstrap doc:
    - README routes through `doctor` + `runtime-config check` first
      (renamed from `instruction doctor` in Redmine #11051);
    - README no longer calls bootstrap.md the canonical / read-first
      entrypoint;
    - bootstrap.md no longer self-describes as the canonical entrypoint
      to read before README;
    - the runtime-config-check FAQ lives in bootstrap.md;
    - the CLI taxonomy migration (old `instruction` names) is documented.
    """

    def setUp(self) -> None:
        self.readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.bootstrap = (
            ROOT / "vibes" / "docs" / "logics" / "bootstrap.md"
        ).read_text(encoding="utf-8")

    def test_readme_quick_start_routes_through_both_doctors(self) -> None:
        quick_start = self.readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
        self.assertIn("mozyo-bridge doctor --target .", quick_start)
        self.assertIn(
            "mozyo-bridge runtime-config check --target . --profile redmine-codex",
            quick_start,
        )
        # The new read-only recovery runbook is advertised in the Quick Start.
        self.assertIn("mozyo-bridge doctor instruction --target .", quick_start)
        # The README states it is the entrypoint.
        self.assertIn("entrypoint for install and bootstrap", quick_start)

    def test_readme_documents_taxonomy_migration(self) -> None:
        # Redmine #11051: README must teach the rename + deprecation so users
        # are not stranded on the old names.
        quick_start = self.readme.split("## Quick Start", 1)[1].split("\n## ", 1)[0]
        self.assertIn("runtime-config check", quick_start)
        self.assertIn("runtime-config install", quick_start)
        self.assertIn("deprecated", quick_start.lower())

    def test_readme_does_not_call_bootstrap_the_canonical_entrypoint(self) -> None:
        self.assertNotIn("canonical LLM-first bootstrap guide", self.readme)
        self.assertNotIn("Read this first for end-to-end setup", self.readme)

    def test_readme_links_runtime_config_check_failures_to_faq(self) -> None:
        self.assertIn("runtime-config check` failures", self.readme)
        for symptom in ("`<repo>/.codex/config.toml` is missing", "X-Default-Project"):
            self.assertIn(symptom, self.readme)

    def test_bootstrap_demoted_from_canonical_entrypoint(self) -> None:
        # Must not re-assert "canonical entrypoint ... Read this BEFORE README".
        self.assertNotIn("canonical entrypoint for", self.bootstrap)
        self.assertIn("detailed stage-order / FAQ / troubleshooting reference", self.bootstrap)

    def test_bootstrap_has_runtime_config_check_faq(self) -> None:
        faq = self.bootstrap.split("### `runtime-config check` FAQ", 1)
        self.assertEqual(2, len(faq), msg="runtime-config check FAQ section missing")
        section = faq[1]
        for needle in (
            "config.toml is missing",
            "X-Default-Project` mismatch",
            ".mcp.json` is `info",
            "home config must not",
            "auto-fix vs",
        ):
            self.assertIn(needle, section, msg=f"FAQ missing {needle!r}")

    def test_bootstrap_documents_taxonomy_migration_and_runbook(self) -> None:
        # The migration section names old -> new and points at the runbook.
        self.assertIn("CLI taxonomy migration", self.bootstrap)
        self.assertIn("mozyo-bridge doctor instruction", self.bootstrap)
        self.assertIn("runtime-config check", self.bootstrap)


if __name__ == "__main__":
    unittest.main()
