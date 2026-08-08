from __future__ import annotations

import base64
import json
import unicodedata
import unittest

from mozyo_bridge.e_120_operations_cockpit.f_110_cockpit_read_model.domain.herdr_unit_board import (
    AUTHORITY_MISSING,
    AUTHORITY_RESOLVED,
    REDACTED_TEXT,
    AgentObservation,
    build_unit_board,
    clip_display,
    format_board,
    lane_work_label,
    metadata_for_unit,
    safe_text,
)


def observation(provider: str, pane: str, **overrides) -> AgentObservation:
    values = {
        "workspace_id": "workspace-a",
        "lane_id": "default",
        "provider": provider,
        "pane_id": pane,
        "runtime_state": "idle",
        "interactive_ready": True,
        "project_label": "giken-3800-mozyo-bridge",
        "workflow_role": "coordinator",
        "responsibility": "giken-3800-mozyo-bridge",
        "work_label": "default lane",
        "authority_state": AUTHORITY_RESOLVED,
    }
    values.update(overrides)
    return AgentObservation(**values)


def terminal_width(value: str) -> int:
    return sum(
        0
        if unicodedata.combining(char)
        else 2
        if unicodedata.east_asian_width(char) in {"W", "F"}
        else 1
        for char in value
    )


class UnitBoardReadModelTests(unittest.TestCase):
    def test_groups_pair_and_never_exposes_transient_pane_ids(self) -> None:
        snapshot = build_unit_board(
            (observation("codex", "w1:p1"), observation("claude", "w1:p2")),
            observed_at="2026-08-08T00:00:00+00:00",
        )

        self.assertTrue(snapshot.ok)
        self.assertEqual(len(snapshot.units), 1)
        unit = snapshot.units[0]
        self.assertEqual(unit.identity_state, "resolved")
        self.assertEqual([a.provider for a in unit.agents], ["claude", "codex"])
        rendered = repr(snapshot.as_payload())
        self.assertNotIn("w1:p1", rendered)
        self.assertNotIn("w1:p2", rendered)

    def test_duplicate_provider_and_conflicting_labels_are_ambiguous(self) -> None:
        snapshot = build_unit_board(
            (
                observation("codex", "w1:p1"),
                observation(
                    "codex",
                    "w1:p2",
                    project_label="another-project",
                    authority_state=AUTHORITY_MISSING,
                ),
            ),
            observed_at="now",
        )

        unit = snapshot.units[0]
        self.assertEqual(unit.identity_state, "ambiguous")
        self.assertEqual(unit.project_label, "ambiguous")
        self.assertEqual(unit.authority_state, "ambiguous")

    def test_issue_lane_label_keeps_readable_words_beside_id(self) -> None:
        self.assertEqual(
            lane_work_label("issue_15114_herdr_unit_board"),
            "#15114 herdr unit board",
        )
        self.assertEqual(lane_work_label("default"), "default lane")

    def test_display_values_strip_controls_and_obey_metadata_cap(self) -> None:
        value = safe_text("  project\nname\x00  " + "x" * 100)
        self.assertNotIn("\n", value)
        self.assertNotIn("\x00", value)
        self.assertLessEqual(len(value), 80)

        unit = build_unit_board(
            (observation("codex", "w1:p1", work_label="x" * 200),),
            observed_at="now",
        ).units[0]
        tokens, title = metadata_for_unit(unit)
        self.assertTrue(all(len(item) <= 80 for item in tokens.values()))
        self.assertLessEqual(len(title), 80)

    def test_projection_redacts_paths_and_credentials_and_neutralizes_controls(self) -> None:
        private_path = "/" + "/".join(("synthetic", "private", "project"))
        credential_key = "_".join(("API", "TOKEN"))
        credential_shape = "=".join((credential_key, "synthetic-value"))
        controlled = "safe\u009b\u202etext"
        unit = build_unit_board(
            (
                observation(
                    "codex",
                    "w1:p1",
                    project_label=private_path,
                    responsibility=credential_shape,
                    work_label=controlled,
                ),
            ),
            observed_at="now",
        ).units[0]

        payload = repr(unit.as_payload())
        tokens, title = metadata_for_unit(unit)
        metadata = repr((tokens, title))
        self.assertEqual(unit.project_label, REDACTED_TEXT)
        self.assertEqual(unit.responsibility, REDACTED_TEXT)
        self.assertNotIn(private_path, payload)
        self.assertNotIn(credential_shape, payload)
        self.assertNotIn(private_path, metadata)
        self.assertNotIn(credential_shape, metadata)
        for control in ("\u009b", "\u202e"):
            self.assertNotIn(control, payload)
            self.assertNotIn(control, metadata)

    def test_public_safe_text_rejects_cross_platform_path_and_opaque_credential_shapes(self) -> None:
        windows_path = "".join(("C", ":", "\\", "synthetic", "\\", "project"))
        unc_path = "".join(("\\", "\\", "synthetic", "\\", "project"))
        opaque_prefix = "".join(("g", "h", "p", "_"))
        opaque_credential = opaque_prefix + ("x" * 24)
        controlled_credential = "".join(
            ("AUTH_", "\u202e", "TOKEN", "=", "synthetic-value")
        )

        for unsafe in (
            "/" + "/".join(("synthetic", "project")),
            "~/synthetic/project",
            windows_path,
            unc_path,
            opaque_credential,
            controlled_credential,
            "label(config:/synthetic/project)",
        ):
            with self.subTest(shape=unsafe[:2]):
                self.assertEqual(safe_text(unsafe), REDACTED_TEXT)
        self.assertEqual(
            safe_text("https://example.invalid/project"),
            REDACTED_TEXT,
        )

    def test_public_safe_projection_redacts_common_credential_shapes_on_every_rail(self) -> None:
        access_key = "_".join(("AWS", "ACCESS", "KEY", "ID"))
        access_assignment = "=".join(
            (access_key, "-".join(("synthetic", "material", "123456")))
        )
        camel_access_assignment = "=".join(
            ("".join(("access", "Key", "Id")), "synthetic-material-654321")
        )
        authorization = "".join(("Author", "ization"))
        basic_header = ": ".join(
            (authorization, " ".join(("Ba" + "sic", "c3ludGhldGljLW1hdGVyaWFs")))
        )
        jwt_assignment = "=".join(
            (
                "session",
                ".".join(
                    (
                        "eyJzeW50aGV0aWMiOiJ0ZXN0In0",
                        "eyJ2YWx1ZSI6InRlc3QifQ",
                        "c3ludGhldGljc2lnbmF0dXJl",
                    )
                ),
            )
        )
        spaced_access_assignment = " = ".join(
            ("AWS ACCESS KEY ID", "synthetic-material-765432")
        )
        unsigned_jwt_assignment = "=".join(
            (
                "session",
                ".".join(
                    (
                        "eyJhbGciOiJub25lIn0",
                        "eyJzdWIiOiJzeW50aGV0aWMifQ",
                        "",
                    )
                ),
            )
        )
        empty_claims_unsigned_jwt = "session=eyJhbGciOiJub25lIn0.e30."
        empty_claims_signed_jwt = "session=eyJhbGciOiJIUzI1NiJ9.e30.c2ln"
        quoted_json_token = '{"token":"synthetic-material-112233"}'
        quoted_json_password = '{"password": "synthetic-material-223344"}'
        quoted_python_api_key = "{'api_key':'synthetic-material-334455'}"
        session_alias = "session=synthetic-material-445566"
        session_id_alias = "session_id=synthetic-material-556677"
        auth_alias = "auth=synthetic-material-667788"
        camel_session_json = '{"sessionId":"synthetic-material-778899"}'
        escaped_token_json = r'{"to\u006ben":"synthetic-material-889900"}'
        nested_escaped_password_json = (
            r'{"public":[{"pass\u0077ord":"synthetic-material-990011"}]}'
        )
        escaped_bearer_json_value = (
            r'{"public":"Bearer\u0020synthetic-material-12345678"}'
        )
        escaped_opaque_json_value = (
            r'{"public":"ghp_\u0078xxxxxxxxxxxxxxxxxxxxxxx"}'
        )
        normalized_fullwidth_token_key = (
            r'{"\uff54oken":"synthetic-material-101112"}'
        )
        embedded_escaped_token_json = (
            r'prefix={"to\u006ben":"synthetic-material-121314"}'
        )
        escaped_session_assignment_value = (
            r'{"public":"session\u003dsynthetic-material-141516"}'
        )
        escaped_api_key_assignment_value = (
            r'{"public":"api_key\u003dsynthetic-material-161718"}'
        )
        encoded_child_credential_json = (
            r'{"public":"{\"token\":\"synthetic-material-181920\"}"}'
        )
        tab_separated_bearer = "Bearer\tsynthetic-material-20212223"
        tab_separated_basic = "Basic\tc3ludGhldGljLW1hdGVyaWFs"
        escaped_tab_bearer_json = (
            r'{"public":"Bearer\u0009synthetic-material-24252627"}'
        )
        parser_limited_number_json = (
            r'{"to\u006ben":"synthetic-material-282930","n":'
            + ("9" * 5_000)
            + "}"
        )
        escaped_posix_path_key = (
            r'{"\u002fsynthetic\u002fprivate\u002fproject":"value"}'
        )
        escaped_windows_path_key = (
            r'{"\u0043\u003a\u005csynthetic\u005cprivate":"value"}'
        )
        escaped_opaque_credential_key = (
            r'{"ghp_\u0078xxxxxxxxxxxxxxxxxxxxxxx":"value"}'
        )
        mysql_password_alias = "MYSQL_PWD=synthetic-material-313233"
        database_password_alias = "DB_PASS=synthetic-material-343536"
        short_password_alias = "pwd=synthetic-material-373839"
        short_pass_alias = "pass=synthetic-material-404142"
        camel_database_password_alias = "dbPass=synthetic-material-434445"
        camel_mysql_password_alias = "mysqlPwd=synthetic-material-464748"
        camel_database_password_json = (
            '{"dbPass":"synthetic-material-495051"}'
        )
        camel_mysql_password_json = (
            '{"mysqlPwd":"synthetic-material-525354"}'
        )

        for credential_shape in (
            access_assignment,
            camel_access_assignment,
            basic_header,
            jwt_assignment,
            spaced_access_assignment,
            unsigned_jwt_assignment,
            empty_claims_unsigned_jwt,
            empty_claims_signed_jwt,
            quoted_json_token,
            quoted_json_password,
            quoted_python_api_key,
            session_alias,
            session_id_alias,
            auth_alias,
            camel_session_json,
            escaped_token_json,
            nested_escaped_password_json,
            escaped_bearer_json_value,
            escaped_opaque_json_value,
            normalized_fullwidth_token_key,
            embedded_escaped_token_json,
            escaped_session_assignment_value,
            escaped_api_key_assignment_value,
            encoded_child_credential_json,
            tab_separated_bearer,
            tab_separated_basic,
            escaped_tab_bearer_json,
            parser_limited_number_json,
            escaped_posix_path_key,
            escaped_windows_path_key,
            escaped_opaque_credential_key,
            mysql_password_alias,
            database_password_alias,
            short_password_alias,
            short_pass_alias,
            camel_database_password_alias,
            camel_mysql_password_alias,
            camel_database_password_json,
            camel_mysql_password_json,
        ):
            with self.subTest(shape=credential_shape.split("=", 1)[0][:8]):
                observation_with_credential = observation(
                    "codex",
                    "w1:p1",
                    responsibility=credential_shape,
                )
                snapshot = build_unit_board(
                    (observation_with_credential,), observed_at="now"
                )
                unit = snapshot.units[0]
                payload = repr(unit.as_payload())
                text = format_board(snapshot, width=120)
                metadata = repr(metadata_for_unit(unit))

                self.assertEqual(unit.responsibility, REDACTED_TEXT)
                self.assertNotIn(credential_shape, payload)
                self.assertNotIn(credential_shape, text)
                self.assertNotIn(credential_shape, metadata)
                self.assertEqual(
                    metadata_for_unit(unit)[0]["mozyo_responsibility"],
                    REDACTED_TEXT,
                )

        self.assertEqual(safe_text("release=1.2.3"), "release=1.2.3")
        self.assertEqual(
            safe_text("release=20260808.20260809.20260810"),
            "release=20260808.20260809.20260810",
        )
        benign_json = json.dumps(
            [{"id": index} for index in range(100)], separators=(",", ":")
        )
        self.assertNotEqual(safe_text(benign_json), REDACTED_TEXT)
        self.assertEqual(safe_text("[" * 16_383), REDACTED_TEXT)
        for benign in (
            "private keyboard layout",
            "private-keyboard support",
            "private_keynote draft",
            "tokenizer=gpt",
            "secretary=office",
            "passwordless=enabled",
            "cookiecutter=template",
            '{"tokenizer":"gpt"}',
            '{"secretary":"office"}',
            '{"passwordless":"enabled"}',
            '{"cookiecutter":"template"}',
        ):
            self.assertEqual(safe_text(benign), benign)
        jwt_header = base64.urlsafe_b64encode(b'{"alg":"none"}').decode().rstrip("=")
        jwt_claims = base64.urlsafe_b64encode(
            ('{"n":' + ("9" * 5_000) + "}").encode()
        ).decode().rstrip("=")
        self.assertEqual(safe_text(f"{jwt_header}.{jwt_claims}."), REDACTED_TEXT)

    def test_long_distinct_lane_identities_keep_distinct_unit_and_metadata_ids(self) -> None:
        common = "lane-" + ("x" * 90)
        snapshot = build_unit_board(
            (
                observation("codex", "w1:p1", lane_id=common + "a"),
                observation("codex", "w1:p2", lane_id=common + "b"),
            ),
            observed_at="now",
        )

        self.assertEqual(len(snapshot.units), 2)
        self.assertEqual(len({unit.unit_id for unit in snapshot.units}), 2)
        metadata_ids = {
            metadata_for_unit(unit)[0]["mozyo_unit"] for unit in snapshot.units
        }
        self.assertEqual(len(metadata_ids), 2)
        self.assertTrue(all(len(value) <= 80 for value in metadata_ids))

        delimiter_snapshot = build_unit_board(
            (
                observation(
                    "codex", "w1:p3", workspace_id="a", lane_id="b\x00c"
                ),
                observation(
                    "codex", "w1:p4", workspace_id="a\x00b", lane_id="c"
                ),
            ),
            observed_at="now",
        )
        self.assertEqual(
            len({unit.unit_id for unit in delimiter_snapshot.units}),
            2,
        )

    def test_narrow_text_render_clips_wide_characters_without_control_data(self) -> None:
        snapshot = build_unit_board(
            (
                observation(
                    "codex",
                    "w1:p1",
                    project_label="情報運用部のとても長い担当名",
                    work_label="長い作業名" * 20,
                ),
            ),
            observed_at="now",
        )
        rendered = format_board(snapshot, width=60)
        self.assertIn("mozyo Unit board", rendered)
        self.assertIn("responsibility:", rendered)
        self.assertIn("…", rendered)
        self.assertNotIn("w1:p1", rendered)
        self.assertLessEqual(len(clip_display("情報運用部", 6)), 4)

    def test_render_never_exceeds_requested_positive_terminal_width(self) -> None:
        snapshot = build_unit_board(
            (
                observation(
                    "codex",
                    "w1:p1",
                    project_label="情報運用部の長い担当名",
                    work_label="長い作業名" * 20,
                ),
            ),
            observed_at="now",
        )

        for width in (1, 10, 20, 40):
            with self.subTest(width=width):
                rendered = format_board(snapshot, width=width)
                self.assertTrue(rendered)
                self.assertTrue(
                    all(terminal_width(line) <= width for line in rendered.splitlines())
                )


if __name__ == "__main__":
    unittest.main()
