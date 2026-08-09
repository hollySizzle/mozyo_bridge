from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    LOCAL_HOST_ID,
    UnitBoardSourceError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.unit_board_sources_loader import (
    UNIT_BOARD_SOURCES_RELPATH,
    UnitBoardSourcesLoadError,
    load_unit_board_sources,
    load_unit_board_sources_from_path,
    unit_board_sources_path,
)


VALID_DOCUMENT = """
version: 1
sources:
  - host_id: devbox
    kind: ssh
    ssh_target: SSH-DESTINATION-SENTINEL
    label: dev host
  - host_id: devcontainer
    kind: container
    container: workspace-dev
    via: devbox
"""


class LoaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name)

    def write(self, text: str) -> Path:
        path = self.home / UNIT_BOARD_SOURCES_RELPATH
        path.write_text(text, encoding="utf-8")
        return path

    def test_missing_file_is_the_local_only_default(self) -> None:
        config = load_unit_board_sources(self.home)

        self.assertTrue(config.is_local_only)
        self.assertEqual(config.local.host_id, LOCAL_HOST_ID)

    def test_empty_file_is_the_local_only_default(self) -> None:
        self.write("")

        self.assertTrue(load_unit_board_sources(self.home).is_local_only)

    def test_valid_document_loads_local_plus_declared_sources(self) -> None:
        self.write(VALID_DOCUMENT)

        config = load_unit_board_sources(self.home)

        self.assertEqual(
            [source.host_id for source in config.sources],
            [LOCAL_HOST_ID, "devbox", "devcontainer"],
        )
        self.assertEqual(config.by_id["devcontainer"].via, "devbox")

    def test_duplicate_key_fails_closed_rather_than_taking_the_last_value(self) -> None:
        self.write(
            "version: 1\n"
            "sources:\n"
            "  - host_id: devbox\n"
            "    kind: ssh\n"
            "    ssh_target: first\n"
            "    ssh_target: second\n"
        )

        with self.assertRaises(UnitBoardSourcesLoadError):
            load_unit_board_sources(self.home)

    def test_malformed_yaml_fails_closed_as_a_domain_error(self) -> None:
        self.write("version: 1\nsources: [\n")

        with self.assertRaises(UnitBoardSourceError):
            load_unit_board_sources(self.home)

    def test_unreadable_present_file_fails_closed_instead_of_defaulting(self) -> None:
        # A directory in the file's place is a present-but-unreadable file; an
        # operator who configured remote hosts must not silently get a
        # local-only board that looks complete.
        (self.home / UNIT_BOARD_SOURCES_RELPATH).mkdir()

        with self.assertRaises(UnitBoardSourcesLoadError):
            load_unit_board_sources(self.home)

    def test_error_text_names_the_file_but_not_its_absolute_path(self) -> None:
        (self.home / UNIT_BOARD_SOURCES_RELPATH).mkdir()

        with self.assertRaises(UnitBoardSourcesLoadError) as caught:
            load_unit_board_sources(self.home)

        self.assertIn(UNIT_BOARD_SOURCES_RELPATH.name, str(caught.exception))
        self.assertNotIn(str(self.home), str(caught.exception))

    def test_path_resolves_under_the_given_home(self) -> None:
        self.assertEqual(
            unit_board_sources_path(self.home),
            self.home.resolve() / UNIT_BOARD_SOURCES_RELPATH,
        )

    def test_explicit_path_loader_shares_the_same_contract(self) -> None:
        path = self.write(VALID_DOCUMENT)

        config = load_unit_board_sources_from_path(path)

        self.assertFalse(config.is_local_only)


if __name__ == "__main__":
    unittest.main()
