"""Repo-local YAML config loader tests (Redmine #12190).

Pins the file-IO + parse layer on top of the #12189 schema boundary:

- missing file and empty / comment-only file resolve to the behavior-preserving
  default (current behavior is never changed by an absent config);
- a valid file normalizes into the typed ``cli`` / ``providers`` /
  ``presentation`` records;
- ``yaml.safe_load`` only — a tag that would construct a Python object is not
  honored;
- fail-closed, with no raw ``yaml.YAMLError`` / ``OSError`` leaking at the
  public boundary: malformed YAML and an unreadable present file raise
  ``RepoLocalConfigLoadError`` (a ``RepoLocalConfigError`` subclass), and
  schema violations (unknown / invalid / unsupported-version / authority-shaped)
  propagate as ``RepoLocalConfigError`` from the schema layer;
- the config path resolves under ``<repo_root>/.mozyo-bridge/config.yaml``.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.repo_local_config_loader import (
    CONFIG_FILE_RELPATH,
    RepoLocalConfigLoadError,
    load_repo_local_config,
    load_repo_local_config_from_path,
    repo_local_config_path,
)
from mozyo_bridge.domain.module_registry import (
    CliCompositionConfig,
    ModuleRegistryError,
)
from mozyo_bridge.domain.presentation_adapter import (
    SURFACE_TEXT,
    SURFACE_TMUX_USER_OPTION,
)
from mozyo_bridge.domain.provider_registry import ProviderSelectionConfig
from mozyo_bridge.domain.repo_local_config import (
    RepoLocalConfig,
    RepoLocalConfigError,
)


def _write_config(repo_root: Path, text: str) -> Path:
    """Write ``text`` to ``<repo_root>/.mozyo-bridge/config.yaml`` and return it."""
    path = repo_root / CONFIG_FILE_RELPATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


class MissingAndEmptyDefaultTest(unittest.TestCase):
    def test_missing_file_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # No config.yaml written at all.
            config = load_repo_local_config(tmp)
        self.assertEqual(config, RepoLocalConfig.default())

    def test_missing_explicit_path_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nope" / "config.yaml"
            config = load_repo_local_config_from_path(path)
        self.assertEqual(config, RepoLocalConfig.default())

    def test_empty_file_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "")
            config = load_repo_local_config(tmp)
        self.assertEqual(config, RepoLocalConfig.default())

    def test_comment_only_file_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "# just a comment\n")
            config = load_repo_local_config(tmp)
        self.assertEqual(config, RepoLocalConfig.default())

    def test_explicit_null_document_is_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "null\n")
            config = load_repo_local_config(tmp)
        self.assertEqual(config, RepoLocalConfig.default())


class ValidConfigTest(unittest.TestCase):
    def test_full_valid_config_normalizes(self) -> None:
        text = (
            "version: 1\n"
            "cli:\n"
            "  disabled: [observability]\n"
            "providers:\n"
            "  selections:\n"
            "    ticket: redmine\n"
            "presentation:\n"
            "  surface: text\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), text)
            config = load_repo_local_config(tmp)
        self.assertEqual(config.cli, CliCompositionConfig(disabled=frozenset({"observability"})))
        self.assertEqual(
            config.providers,
            ProviderSelectionConfig(selections=(("ticket", "redmine"),)),
        )
        self.assertEqual(config.presentation.surface, SURFACE_TEXT)

    def test_version_only_is_default_records(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "version: 1\n")
            config = load_repo_local_config(tmp)
        self.assertEqual(config, RepoLocalConfig.default())
        self.assertEqual(config.presentation.surface, SURFACE_TMUX_USER_OPTION)

    def test_partial_block_keeps_other_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "presentation:\n  surface: text\n")
            config = load_repo_local_config(tmp)
        self.assertEqual(config.cli, CliCompositionConfig.default())
        self.assertEqual(config.providers, ProviderSelectionConfig.default())
        self.assertEqual(config.presentation.surface, SURFACE_TEXT)


class SafeLoadOnlyTest(unittest.TestCase):
    def test_python_object_tag_is_not_constructed(self) -> None:
        # A ``!!python/object/apply`` tag is honored only by yaml.load /
        # full_load. safe_load must reject it as a parser error, which the
        # loader wraps — it must never construct the object.
        text = "version: !!python/object/apply:os.getcwd []\n"
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), text)
            with self.assertRaises(RepoLocalConfigError) as ctx:
                load_repo_local_config(tmp)
        self.assertIsInstance(ctx.exception, RepoLocalConfigLoadError)


class FailClosedIoAndParseTest(unittest.TestCase):
    def test_malformed_yaml_is_wrapped_domain_error(self) -> None:
        # Unbalanced flow mapping -> yaml.YAMLError; must surface as the domain
        # error subclass, not a raw parser exception.
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "cli: {disabled: [a, b}\n")
            with self.assertRaises(RepoLocalConfigLoadError):
                load_repo_local_config(tmp)

    def test_no_raw_yaml_error_leaks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "a: b: c\n")
            try:
                load_repo_local_config(tmp)
            except yaml.YAMLError:  # pragma: no cover - would be the bug
                self.fail("raw yaml.YAMLError leaked at the public boundary")
            except RepoLocalConfigError:
                pass
            else:  # pragma: no cover - malformed input must raise
                self.fail("malformed YAML did not fail closed")

    def test_unreadable_present_file_fails_closed(self) -> None:
        # A directory standing where config.yaml should be is a present-but-
        # unreadable file: read raises IsADirectoryError (OSError) -> wrapped.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / CONFIG_FILE_RELPATH
            path.mkdir(parents=True)
            with self.assertRaises(RepoLocalConfigLoadError):
                load_repo_local_config(tmp)

    def test_non_mapping_document_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "- one\n- two\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)


class FailClosedSchemaTest(unittest.TestCase):
    """Schema-layer rejections must propagate through the loader unchanged."""

    def test_unknown_top_level_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "unknown_block: {}\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)

    def test_invalid_version_type(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "version: notanint\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)

    def test_unsupported_version(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "version: 2\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)

    def test_authority_shaped_top_level_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "owner_approval: true\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)

    def test_presentation_route_field_rejected(self) -> None:
        # 'route' is a projection-only boundary token; presentation may select a
        # surface, never address a route.
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "presentation:\n  route: somewhere\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)

    def test_module_path_shaped_top_level_key_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "module_path: a.b.c\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)

    def test_cli_sub_record_error_is_domain_error(self) -> None:
        # A ``cli`` shape violation propagates as the sibling domain error
        # ``ModuleRegistryError`` (a fail-closed ValueError) from the delegated
        # sub-record — the loader preserves the schema layer's error contract
        # and only wraps yaml / IO failures, never a raw parser exception.
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "cli:\n  disabled: notalist\n")
            with self.assertRaises(ModuleRegistryError):
                load_repo_local_config(tmp)

    def test_unknown_presentation_surface_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write_config(Path(tmp), "presentation:\n  surface: webviewer\n")
            with self.assertRaises(RepoLocalConfigError):
                load_repo_local_config(tmp)


class PathResolutionTest(unittest.TestCase):
    def test_path_is_under_mozyo_bridge_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = repo_local_config_path(tmp)
        self.assertEqual(path, Path(tmp).resolve() / ".mozyo-bridge" / "config.yaml")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
