"""Redmine #15514 — an unreadable docs catalog is reported without its content.

PyYAML reports a parse failure by quoting the offending source line, so every
`docs` subcommand that read the catalog printed the catalog's own text into the
terminal and the command log when it failed to parse — measured across
`validate`, `resolve`, `generate-file-conventions`, and `audit-impact`, all of
which raised a traceback carrying the sentinel. A rule against putting secrets
in the catalog does not make echoing its contents acceptable; the same
reasoning closed the narrower stdout leak in #15513 (review j#105791
finding_2).

The reader now raises :class:`CatalogUnreadableError`, whose message is a fixed
phrase plus at most a line/column position, and the commands render that
instead of the cause. The tests below are written against the leak rather than
the wording: a sentinel that appears anywhere in stdout OR stderr fails,
whichever subcommand produced it.
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.docs_tools import CatalogUnreadableError, load_catalog  # noqa: E402

#: A value that must never be echoed back, standing in for anything an operator
#: might have in a catalog when it fails to parse.
SENTINEL = "SENTINEL_MUST_NOT_BE_ECHOED"

#: Catalogs that cannot be read, each carrying the sentinel.
UNREADABLE_CATALOGS = {
    "broken_yaml": f"documents: [{SENTINEL}\n",
    "non_mapping_root": f"- {SENTINEL}\n- still not a mapping\n",
}

#: Every `docs` subcommand that reads the catalog.
SUBCOMMANDS = (
    ("validate", ["docs", "validate", "--repo", "{repo}"]),
    ("resolve", ["docs", "resolve", "--repo", "{repo}", "README.md"]),
    ("generate", ["docs", "generate-file-conventions", "--repo", "{repo}"]),
    ("audit_impact", ["docs", "audit-impact", "--repo", "{repo}"]),
)


class CatalogReaderIsValueFreeTest(unittest.TestCase):
    """The reader itself must not carry catalog text in its error."""

    def _load(self, text: str):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / "catalog.yaml"
        path.write_text(text, encoding="utf-8")
        return load_catalog(path)

    def test_every_unreadable_shape_raises_without_the_content(self) -> None:
        for label, text in UNREADABLE_CATALOGS.items():
            with self.subTest(shape=label):
                with self.assertRaises(CatalogUnreadableError) as caught:
                    self._load(text)
                self.assertNotIn(SENTINEL, str(caught.exception))

    def test_broken_yaml_reports_a_position_but_no_text(self) -> None:
        with self.assertRaises(CatalogUnreadableError) as caught:
            self._load(UNREADABLE_CATALOGS["broken_yaml"])
        message = str(caught.exception)
        self.assertIn("not valid YAML", message)
        self.assertIn("line", message)
        self.assertNotIn(SENTINEL, message)

    def test_a_missing_file_is_the_same_typed_error(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        with self.assertRaises(CatalogUnreadableError):
            load_catalog(Path(tmp.name) / "absent.yaml")

    def test_a_readable_catalog_still_loads(self) -> None:
        loaded = self._load("schema_version: 1\ndocuments: []\n")
        self.assertEqual(1, loaded["schema_version"])


class NoDocsSubcommandEchoesTheCatalogTest(unittest.TestCase):
    """End-to-end: run the real CLI and search both streams for the sentinel.

    Driven as a subprocess because the leak was in what reached the operator's
    terminal — an in-process call could pass while an escaping traceback still
    printed the catalog at the process boundary.
    """

    def _repo(self, catalog_text: str) -> Path:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        repo = Path(tmp.name)
        docs = repo / ".mozyo-bridge" / "docs"
        docs.mkdir(parents=True)
        (docs / "catalog.yaml").write_text(catalog_text, encoding="utf-8")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        return repo

    def _run(self, repo: Path, argv: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-m", "mozyo_bridge", *argv],
            cwd=repo,
            capture_output=True,
            text=True,
            env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src")},
        )

    def test_no_subcommand_prints_the_catalog_on_either_stream(self) -> None:
        for shape, catalog_text in UNREADABLE_CATALOGS.items():
            repo = self._repo(catalog_text)
            for name, template in SUBCOMMANDS:
                with self.subTest(shape=shape, subcommand=name):
                    argv = [part.format(repo=str(repo)) for part in template]
                    result = self._run(repo, argv)

                    streams = result.stdout + result.stderr
                    self.assertNotIn(SENTINEL, streams)
                    # A traceback is how the content used to escape, so its
                    # absence is part of the contract, not a nicety.
                    self.assertNotIn("Traceback", streams)
                    self.assertNotEqual(
                        0, result.returncode, "an unreadable catalog must fail"
                    )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
