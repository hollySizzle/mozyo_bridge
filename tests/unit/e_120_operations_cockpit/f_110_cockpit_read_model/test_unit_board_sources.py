from __future__ import annotations

import unittest

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.unit_board_sources import (
    LOCAL_HOST_ID,
    MAX_SOURCES,
    UnitBoardSource,
    UnitBoardSourceError,
    UnitBoardSourcesConfig,
    source_command_argv,
    ssh_argv,
)


def ssh_record(host_id: str = "devbox", **overrides) -> dict:
    record = {"host_id": host_id, "kind": "ssh", "ssh_target": "devbox"}
    record.update(overrides)
    return record


def container_record(host_id: str = "devcontainer", **overrides) -> dict:
    record = {"host_id": host_id, "kind": "container", "container": "workspace-dev"}
    record.update(overrides)
    return record


class SourceSchemaTests(unittest.TestCase):
    def test_missing_document_is_local_only(self) -> None:
        config = UnitBoardSourcesConfig.default()

        self.assertTrue(config.is_local_only)
        self.assertEqual(config.local.host_id, LOCAL_HOST_ID)
        self.assertEqual(config.remote_sources, ())

    def test_local_source_is_added_when_only_remotes_are_declared(self) -> None:
        config = UnitBoardSourcesConfig.from_record(
            {"version": 1, "sources": [ssh_record()]}
        )

        self.assertEqual(
            [source.host_id for source in config.sources], [LOCAL_HOST_ID, "devbox"]
        )
        self.assertFalse(config.is_local_only)

    def test_declared_local_source_keeps_its_operator_label(self) -> None:
        config = UnitBoardSourcesConfig.from_record(
            {
                "version": 1,
                "sources": [
                    {"host_id": LOCAL_HOST_ID, "kind": "local", "label": "laptop"},
                    ssh_record(),
                ],
            }
        )

        self.assertEqual(config.local.label, "laptop")
        self.assertEqual(len(config.sources), 2)

    def test_remote_source_may_not_claim_the_reserved_local_id(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(ssh_record(host_id=LOCAL_HOST_ID))

    def test_local_source_may_not_be_renamed_away_from_the_reserved_id(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record({"host_id": "laptop", "kind": "local"})

    def test_duplicate_host_ids_fail_closed(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSourcesConfig.from_record(
                {"version": 1, "sources": [ssh_record(), ssh_record()]}
            )

    def test_unknown_keys_and_versions_fail_closed(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSourcesConfig.from_record({"version": 2, "sources": []})
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSourcesConfig.from_record({"version": 1, "hosts": []})
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(ssh_record(command="rm -rf /"))

    def test_misplaced_kind_fields_fail_closed(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(
                {"host_id": "devbox", "kind": "local", "ssh_target": "devbox"}
            )
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(container_record(ssh_target="devbox"))

    def test_connection_values_reject_option_and_metacharacter_shapes(self) -> None:
        for target in ("-oProxyCommand=x", "host;rm -rf /", "host name", "a$b"):
            with self.subTest(target=target):
                with self.assertRaises(UnitBoardSourceError):
                    UnitBoardSource.from_record(ssh_record(ssh_target=target))

    def test_binary_must_be_a_command_name_or_absolute_path(self) -> None:
        UnitBoardSource.from_record(ssh_record(mozyo_binary="mozyo-bridge"))
        UnitBoardSource.from_record(ssh_record(mozyo_binary="/opt/bin/mozyo-bridge"))
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(ssh_record(mozyo_binary="../evil"))

    def test_connect_timeout_is_bounded_and_not_a_boolean(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(ssh_record(connect_timeout=True))
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(ssh_record(connect_timeout=0))
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSource.from_record(ssh_record(connect_timeout=10_000))

    def test_source_count_is_bounded(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSourcesConfig.from_record(
                {
                    "version": 1,
                    "sources": [
                        ssh_record(host_id=f"host{index}")
                        for index in range(MAX_SOURCES + 1)
                    ],
                }
            )

    def test_container_via_must_reference_a_declared_non_container_source(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSourcesConfig.from_record(
                {"version": 1, "sources": [container_record(via="absent")]}
            )
        with self.assertRaises(UnitBoardSourceError):
            UnitBoardSourcesConfig.from_record(
                {
                    "version": 1,
                    "sources": [
                        container_record(host_id="outer"),
                        container_record(host_id="inner", via="outer"),
                    ],
                }
            )

    def test_payload_never_carries_a_connection_value(self) -> None:
        source = UnitBoardSource.from_record(
            ssh_record(ssh_target="SSH-DESTINATION-SENTINEL", label="dev host")
        )

        payload = source.as_payload()

        self.assertEqual(
            payload, {"host_id": "devbox", "host_label": "dev host", "host_kind": "ssh"}
        )
        self.assertNotIn("SSH-DESTINATION-SENTINEL", str(payload))


class SourceArgvTests(unittest.TestCase):
    def test_local_source_has_no_subprocess_shape(self) -> None:
        with self.assertRaises(UnitBoardSourceError):
            source_command_argv(UnitBoardSource.local_default(), ("herdr",))

    def test_ssh_argv_is_batch_mode_and_quotes_the_remote_command(self) -> None:
        source = UnitBoardSource.from_record(ssh_record())

        argv = source_command_argv(source, ("herdr", "unit-board", "show", "--json"))

        self.assertEqual(argv[0], "ssh")
        self.assertIn("BatchMode=yes", argv)
        self.assertIn("--", argv)
        self.assertEqual(argv[argv.index("--") + 1], "devbox")
        self.assertEqual(
            argv[-1], "mozyo-bridge herdr unit-board show --json"
        )

    def test_ssh_remote_command_quotes_values_containing_spaces(self) -> None:
        source = UnitBoardSource.from_record(ssh_record())

        argv = ssh_argv(source, ("mozyo-bridge", "--summary", "two words"))

        self.assertEqual(argv[-1], "mozyo-bridge --summary 'two words'")

    def test_container_argv_uses_exec_without_a_shell(self) -> None:
        config = UnitBoardSourcesConfig.from_record(
            {"version": 1, "sources": [container_record()]}
        )
        source = config.by_id["devcontainer"]

        argv = source_command_argv(source, ("herdr",), by_id=config.by_id)

        self.assertEqual(
            argv, ("docker", "exec", "workspace-dev", "mozyo-bridge", "herdr")
        )

    def test_container_via_ssh_nests_exactly_one_hop(self) -> None:
        config = UnitBoardSourcesConfig.from_record(
            {
                "version": 1,
                "sources": [ssh_record(), container_record(via="devbox")],
            }
        )
        source = config.by_id["devcontainer"]

        argv = source_command_argv(source, ("herdr",), by_id=config.by_id)

        self.assertEqual(argv[0], "ssh")
        self.assertEqual(
            argv[-1], "docker exec workspace-dev mozyo-bridge herdr"
        )


if __name__ == "__main__":
    unittest.main()
