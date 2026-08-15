"""Redmine #15513 — `docs validate` refuses a missing catalog in words, not a traceback.

The scaffold ships `.mozyo-bridge/docs/catalog.yaml.example` and a project
promotes it when it adopts the governed docs catalog, so a target without
`catalog.yaml` is an EXPECTED state. `cmd_docs_validate` nevertheless walked
straight into the reader and let `FileNotFoundError` escape, printing a
traceback that reads as "the tool is broken" — hit while running the release
acceptance smoke against a freshly scaffolded target during the 1.0.0
production install QA (#15507 j#105418).

The same investigation found sibling escapes — a non-mapping root and
unparseable YAML raise through the command too — but this issue's scope
declares invalid-catalog behaviour unchanged, so fixing them is tracked
separately as #15514 (review j#105791 finding_1).

"Unchanged" is a claim, so it is pinned rather than asserted: the
characterization below records what those paths do TODAY. It is deliberately
not an endorsement — #15514 will replace that raising behaviour with a
value-free typed refusal, and is expected to update this class in the same
change. The rest of the file proves the missing-catalog branch does not
swallow the cases around it: a valid catalog passes, and a catalog that reads
fine but breaks the rules keeps its own per-rule diagnostics.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.commands_docs_scaffold import (  # noqa: E402,E501
    cmd_docs_validate,
)


class DocsValidateMissingCatalogTest(unittest.TestCase):
    def _repo(self, catalog_text: str | None) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        if catalog_text is not None:
            docs_dir = repo / ".mozyo-bridge" / "docs"
            docs_dir.mkdir(parents=True)
            (docs_dir / "catalog.yaml").write_text(catalog_text, encoding="utf-8")
        return repo

    def _run(self, repo: Path) -> tuple[int, str]:
        out = io.StringIO()
        args = argparse.Namespace(repo=str(repo))
        with contextlib.redirect_stdout(out):
            code = cmd_docs_validate(args)
        return code, out.getvalue()

    def test_absent_catalog_refuses_in_words(self) -> None:
        code, output = self._run(self._repo(None))

        self.assertEqual(1, code)
        self.assertIn("no docs catalog at", output)
        # The repo-relative path, so the message reads the same from any cwd.
        self.assertIn(".mozyo-bridge/docs/catalog.yaml", output)
        # And it says what to do about it.
        self.assertIn("catalog.yaml.example", output)
        # Never the failure mode this fixes.
        self.assertNotIn("Traceback", output)
        self.assertNotIn("FileNotFoundError", output)

    def test_absent_catalog_raises_nothing(self) -> None:
        # The traceback came from an escaping exception, so pin the absence of
        # the exception itself, not only the absence of its rendering.
        repo = self._repo(None)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                cmd_docs_validate(argparse.Namespace(repo=str(repo)))
        except Exception as exc:  # noqa: BLE001 - any escape is the regression
            self.fail(f"missing catalog must not raise, got {exc!r}")

    #: The smallest catalog the validator accepts: schema version plus the
    #: exact managed-type set it requires.
    MINIMAL_CATALOG = (
        "schema_version: 1\n"
        "managed_types:\n"
        "  - rule\n"
        "  - spec\n"
        "  - logic\n"
        "  - manual_spec\n"
        "  - task\n"
        "documents: []\n"
    )

    def test_a_valid_catalog_still_passes(self) -> None:
        code, output = self._run(self._repo(self.MINIMAL_CATALOG))

        self.assertEqual(0, code)
        self.assertIn("catalog validation passed", output)

    def test_a_structurally_invalid_catalog_keeps_per_rule_errors(self) -> None:
        # Reads fine, breaks the rules: the missing-catalog branch must not
        # swallow the validator's own findings.
        code, output = self._run(
            self._repo("schema_version: 2\nmanaged_types: []\ndocuments: []\n")
        )

        self.assertEqual(1, code)
        self.assertIn("catalog validation failed", output)
        self.assertIn("schema_version must be 1", output)
        self.assertNotIn("no docs catalog at", output)


class LegacyInvalidCatalogCharacterizationTest(unittest.TestCase):
    """What an unreadable catalog does today — pinned, not endorsed.

    The scope of #15513 is the ABSENT catalog. These cases exist so that the
    "invalid behaviour is unchanged" half of that scope is verifiable rather
    than merely stated: without them, the legacy exception could drift before
    #15514 deliberately replaces it and this suite would stay green.

    Fixtures carry no sensitive-looking value, and the YAML case pins only the
    exception CLASS: PyYAML embeds the offending source line in its message, so
    asserting that message would pull catalog input into the expectation — the
    same echo that review j#105791 finding_2 flagged.
    """

    def _validate(self, catalog_text: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        docs_dir = repo / ".mozyo-bridge" / "docs"
        docs_dir.mkdir(parents=True)
        (docs_dir / "catalog.yaml").write_text(catalog_text, encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()):
            return cmd_docs_validate(argparse.Namespace(repo=str(repo)))

    def test_a_non_mapping_root_still_raises_its_legacy_error(self) -> None:
        with self.assertRaises(ValueError) as caught:
            self._validate("- not\n- a mapping\n")
        # Input-independent text, so pinning it echoes nothing.
        self.assertEqual("catalog root must be a mapping", str(caught.exception))

    def test_unparseable_yaml_still_raises_its_legacy_exception_class(self) -> None:
        import yaml

        with self.assertRaises(yaml.YAMLError):
            self._validate("documents: [unclosed\n")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
