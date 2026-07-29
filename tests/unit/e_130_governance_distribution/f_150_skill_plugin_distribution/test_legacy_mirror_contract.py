"""Rule-level tests for the legacy mirror contract (Redmine #14580).

These exercise the *pure* contract — the rule taxonomy, the write-blocking set,
and the recovery derivation — with no filesystem at all. That is the point of
the Python authority the design consultation chose (j#90402): under the previous
shell implementation every rule could only be reached through a subprocess, so a
composition bug like j#90397 R5-F3 (source invalid + content drift advertising a
resync that refuses) had no place to be tested directly.

Recovery precedence is the part worth being exhaustive about. Six review rounds
produced the same shape of defect — reporting something the command cannot
deliver — and three of them were specifically about advice: a rerun offered for
a class the sync refuses (j#90322 F1), a blanket line for every class
(j#90342 R2-F1), and a composite that mixed a converging class with a
non-converging one (j#90397 R5-F3).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_150_skill_plugin_distribution.domain.legacy_mirror_contract import (  # noqa: E402
    CONTENT_DRIFT,
    MIRRORED_REFERENCES,
    RECOVERY_DISPOSITION_UNPINNED,
    RECOVERY_REPLACE_ENTRY,
    RECOVERY_RESTORE_MIRROR_PATH,
    RECOVERY_RESTORE_SOURCE,
    RECOVERY_RESYNC,
    RULE_CONTENT_PARITY,
    RULE_DEST_ENTRY_SET,
    RULE_DEST_ENTRY_TYPES,
    RULE_DEST_TOPOLOGY,
    RULE_SOURCE_ENTRIES,
    RULE_SOURCE_TOPOLOGY,
    SOURCE_MISSING,
    UNPINNED_ENTRY,
    WRITE_BLOCKING_RULES,
    MirrorAudit,
    Violation,
    describe_name,
)


def _v(rule: str, kind: str = "kind", subject: str = "subject") -> Violation:
    return Violation(rule=rule, kind=kind, subject=subject)


class PinnedSetTest(unittest.TestCase):
    def test_pinned_set_is_the_partial_set(self) -> None:
        self.assertEqual(
            ("project-map.md", "release.md", "safety.md", "workflow.md"),
            MIRRORED_REFERENCES,
        )

    def test_pinned_set_has_no_duplicates(self) -> None:
        self.assertEqual(len(MIRRORED_REFERENCES), len(set(MIRRORED_REFERENCES)))


class DescribeNameTest(unittest.TestCase):
    """A filename must never be able to forge extra report lines."""

    def test_control_characters_are_escaped(self) -> None:
        forged = describe_name("a\nlegacy project skill mirror is up to date")
        self.assertNotIn("\n", forged)
        self.assertIn("\\n", forged)

    def test_tab_is_escaped(self) -> None:
        self.assertNotIn("\t", describe_name("a\tb.md"))

    def test_ordinary_name_stays_readable(self) -> None:
        self.assertIn("workflow.md", describe_name("workflow.md"))


class WriteBlockingRuleTest(unittest.TestCase):
    def test_rules_a_through_e_block_the_write(self) -> None:
        for rule in (
            RULE_SOURCE_TOPOLOGY,
            RULE_SOURCE_ENTRIES,
            RULE_DEST_TOPOLOGY,
            RULE_DEST_ENTRY_SET,
            RULE_DEST_ENTRY_TYPES,
        ):
            with self.subTest(rule=rule):
                self.assertIn(rule, WRITE_BLOCKING_RULES)
                self.assertTrue(MirrorAudit(violations=(_v(rule),)).blocks_write)

    def test_content_parity_does_not_block_the_write(self) -> None:
        """Rule F is the drift the sync repairs — blocking on it would make the
        command refuse its own job."""
        self.assertNotIn(RULE_CONTENT_PARITY, WRITE_BLOCKING_RULES)
        audit = MirrorAudit(violations=(_v(RULE_CONTENT_PARITY, CONTENT_DRIFT),))
        self.assertFalse(audit.blocks_write)

    def test_missing_mirror_does_not_block_the_write(self) -> None:
        """Creating the directory is what the sync is for."""
        self.assertFalse(MirrorAudit(dest_missing=True).blocks_write)


class RecoveryDerivationTest(unittest.TestCase):
    def test_clean_audit_has_no_recovery(self) -> None:
        audit = MirrorAudit()
        self.assertTrue(audit.ok)
        self.assertEqual((), audit.recovery_actions())

    def test_content_drift_alone_offers_the_resync(self) -> None:
        audit = MirrorAudit(violations=(_v(RULE_CONTENT_PARITY, CONTENT_DRIFT),))
        self.assertEqual((RECOVERY_RESYNC,), audit.recovery_actions())

    def test_missing_mirror_alone_offers_the_resync(self) -> None:
        self.assertEqual((RECOVERY_RESYNC,), MirrorAudit(dest_missing=True).recovery_actions())

    def test_each_blocking_class_gets_its_own_disposition(self) -> None:
        cases = {
            RULE_SOURCE_TOPOLOGY: RECOVERY_RESTORE_SOURCE,
            RULE_SOURCE_ENTRIES: RECOVERY_RESTORE_SOURCE,
            RULE_DEST_TOPOLOGY: RECOVERY_RESTORE_MIRROR_PATH,
            RULE_DEST_ENTRY_SET: RECOVERY_DISPOSITION_UNPINNED,
            RULE_DEST_ENTRY_TYPES: RECOVERY_REPLACE_ENTRY,
        }
        for rule, expected in cases.items():
            with self.subTest(rule=rule):
                actions = MirrorAudit(violations=(_v(rule),)).recovery_actions()
                self.assertEqual((expected,), actions)
                self.assertNotIn(RECOVERY_RESYNC, actions)

    def test_source_invalid_plus_content_drift_never_offers_the_resync(self) -> None:
        """Redmine #14580 j#90397 R5-F3, as a pure composition.

        The shell version emitted each class's advice as it was discovered, so a
        broken canonical source printed "restore the source" AND "rerun the
        sync" — and that sync then refused at its own source preflight.
        """
        audit = MirrorAudit(
            violations=(
                _v(RULE_SOURCE_ENTRIES, SOURCE_MISSING),
                _v(RULE_CONTENT_PARITY, CONTENT_DRIFT),
            )
        )
        actions = audit.recovery_actions()
        self.assertIn(RECOVERY_RESTORE_SOURCE, actions)
        self.assertNotIn(RECOVERY_RESYNC, actions)

    def test_source_invalid_plus_missing_mirror_never_offers_the_resync(self) -> None:
        audit = MirrorAudit(
            violations=(_v(RULE_SOURCE_ENTRIES, SOURCE_MISSING),), dest_missing=True
        )
        self.assertNotIn(RECOVERY_RESYNC, audit.recovery_actions())

    def test_every_blocking_rule_suppresses_the_resync_when_combined_with_drift(
        self,
    ) -> None:
        """Exhaustive over the blocking set, not just the reported case.

        The one that shipped was source-invalid + drift; nothing about the bug
        was specific to the source, so each blocking rule is checked.
        """
        for rule in sorted(WRITE_BLOCKING_RULES):
            with self.subTest(rule=rule):
                audit = MirrorAudit(
                    violations=(_v(rule), _v(RULE_CONTENT_PARITY, CONTENT_DRIFT))
                )
                self.assertNotIn(RECOVERY_RESYNC, audit.recovery_actions())

    def test_unpinned_disposition_never_claims_the_sync_clears_it(self) -> None:
        """Residue is indistinguishable from a file someone meant to keep.

        j#90397 R5-F2: the previous implementation told the operator a rerun
        would clear a stale temp, and it did — by deleting an arbitrary file
        that merely shared the name prefix.
        """
        audit = MirrorAudit(violations=(_v(RULE_DEST_ENTRY_SET, UNPINNED_ENTRY),))
        text = "\n".join(audit.recovery_lines())
        self.assertIn("reviewed disposition", text)
        self.assertIn("never deletes them for you", text)
        self.assertNotIn("Rerun 'scripts/sync_legacy_project_skill.sh'", text)

    def test_multiple_blocking_classes_report_all_their_dispositions(self) -> None:
        audit = MirrorAudit(
            violations=(
                _v(RULE_DEST_ENTRY_SET, UNPINNED_ENTRY),
                _v(RULE_DEST_ENTRY_TYPES),
            )
        )
        self.assertEqual(
            (RECOVERY_DISPOSITION_UNPINNED, RECOVERY_REPLACE_ENTRY),
            audit.recovery_actions(),
        )


class ReportRenderingTest(unittest.TestCase):
    def test_report_is_empty_for_a_clean_audit(self) -> None:
        self.assertEqual((), MirrorAudit().report_lines())

    def test_report_lists_violations_then_recovery(self) -> None:
        audit = MirrorAudit(violations=(_v(RULE_CONTENT_PARITY, CONTENT_DRIFT, "x.md"),))
        lines = audit.report_lines()
        self.assertIn("x.md", lines[0])
        self.assertIn("Rerun 'scripts/sync_legacy_project_skill.sh'", "\n".join(lines))

    def test_missing_mirror_is_reported_even_without_violations(self) -> None:
        lines = MirrorAudit(dest_missing=True).report_lines()
        self.assertTrue(any("mirror directory does not exist" in line for line in lines))

    def test_no_report_line_contains_an_embedded_newline(self) -> None:
        audit = MirrorAudit(
            violations=(
                _v(RULE_DEST_ENTRY_SET, UNPINNED_ENTRY, describe_name("a\nb.md")),
            )
        )
        for line in audit.report_lines():
            self.assertNotIn("\n", line)

    def test_as_dict_round_trips_the_machine_readable_shape(self) -> None:
        audit = MirrorAudit(
            violations=(_v(RULE_DEST_ENTRY_SET, UNPINNED_ENTRY),),
            skipped_rules=(RULE_CONTENT_PARITY,),
        )
        data = audit.as_dict()
        self.assertFalse(data["ok"])
        self.assertEqual([RULE_CONTENT_PARITY], data["skipped_rules"])
        self.assertEqual(UNPINNED_ENTRY, data["violations"][0]["kind"])
        self.assertEqual([RECOVERY_DISPOSITION_UNPINNED], data["recovery_actions"])


if __name__ == "__main__":
    unittest.main()
