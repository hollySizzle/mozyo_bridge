"""`config status` per-key effective value / source classification (Redmine #14222/#14223).

Pins the pure classifier (:mod:`repo_local_config_status`) and its wiring into the ONE
public `config status` command — #14223's close condition explicitly forbids a second
status surface, so these tests drive the same CLI entry point the pre-existing v1
deprecation-warning tests already use (`ConfigStatusDeprecationSurfaceTest` in
`test_config_migration_v2.py`), not a new command.
"""

from __future__ import annotations

import contextlib
import io
import json
import tempfile
import types
import unittest
from pathlib import Path

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (  # noqa: E501
    cmd_config_status,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E501
    CONFIG_BLOCK_KEYS,
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    classify_config_sources,
)


def _status_json(repo) -> dict:
    args = types.SimpleNamespace(repo=str(repo), json=True)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_config_status(args)
    assert rc == 0
    return json.loads(buf.getvalue())


def _status_text(repo) -> str:
    args = types.SimpleNamespace(repo=str(repo), json=False)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = cmd_config_status(args)
    assert rc == 0
    return buf.getvalue()


def _write(repo, text: str) -> None:
    d = Path(repo) / ".mozyo-bridge"
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(text, encoding="utf-8")


def _settings_by_key(payload: dict) -> dict:
    return {s["key"]: s for s in payload["settings"]}


class ClassifyConfigSourcesPureTest(unittest.TestCase):
    """Pure classifier tests — no CLI, no filesystem."""

    def test_missing_record_is_all_default(self) -> None:
        config = RepoLocalConfig.default()
        statuses = classify_config_sources(
            raw_record=None, config=config, schema_version=1, legacy_migratable=False
        )
        self.assertEqual({s.key for s in statuses}, set(CONFIG_BLOCK_KEYS))
        self.assertTrue(all(s.source == SOURCE_DEFAULT for s in statuses))
        self.assertTrue(all(s.note == "" for s in statuses if s.key != "lane_placement"))

    def test_declared_key_is_declared_even_when_value_equals_default(self) -> None:
        # An operator writing `work_unit: {granularity: user_story}` declares the ALREADY-
        # default value -- still `declared`, not `default` (declaring intent counts).
        config = RepoLocalConfig.default()
        statuses = classify_config_sources(
            raw_record={"work_unit": {"granularity": "user_story"}},
            config=config,
            schema_version=2,
            legacy_migratable=False,
        )
        by_key = {s.key: s for s in statuses}
        self.assertEqual(by_key["work_unit"].source, SOURCE_DECLARED)
        self.assertEqual(by_key["sublane_integration"].source, SOURCE_DEFAULT)

    def test_v1_legacy_declared_block_carries_a_migrate_note(self) -> None:
        config = RepoLocalConfig.default()
        statuses = classify_config_sources(
            raw_record={"agent_launch": {"launch_argv": {}}},
            config=config,
            schema_version=1,
            legacy_migratable=True,
        )
        by_key = {s.key: s for s in statuses}
        self.assertEqual(by_key["agent_launch"].source, SOURCE_DECLARED)
        self.assertIn("config migrate", by_key["agent_launch"].note)
        # provider_binding was NOT in the raw record -> default, no migrate note (the
        # note is about a DECLARED legacy block, not a blanket v1 warning).
        self.assertEqual(by_key["provider_binding"].source, SOURCE_DEFAULT)
        self.assertEqual(by_key["provider_binding"].note, "")

    def test_lane_placement_carries_unintegrated_schema_note_regardless_of_source(
        self,
    ) -> None:
        config = RepoLocalConfig.default()
        statuses = classify_config_sources(
            raw_record=None, config=config, schema_version=2, legacy_migratable=False
        )
        by_key = {s.key: s for s in statuses}
        self.assertIn("#13647", by_key["lane_placement"].note)

    def test_effective_value_is_json_safe_frozenset_becomes_sorted_list(self) -> None:
        from mozyo_bridge.e_150_quality_architecture.f_130_module_health.domain.module_registry import (  # noqa: E501
            CliCompositionConfig,
        )

        config = RepoLocalConfig(
            cli=CliCompositionConfig(disabled=frozenset({"codex", "claude"}))
        )
        statuses = classify_config_sources(
            raw_record=None, config=config, schema_version=2, legacy_migratable=False
        )
        by_key = {s.key: s for s in statuses}
        payload = by_key["cli"].as_payload()
        # json.dumps must not raise (frozenset is not natively serializable).
        json.dumps(payload)
        self.assertEqual(payload["effective_value"]["disabled"], ["claude", "codex"])


class ConfigStatusSettingsSurfaceTest(unittest.TestCase):
    """CLI-level: the same `config status` command carries the new `settings` payload."""

    def test_declared_blocks_are_reported_declared(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(
                tmp,
                "version: 2\nwork_unit:\n  granularity: user_story\n"
                "sublane_integration:\n  integration_branch: main-next\n",
            )
            out = _status_json(Path(tmp))
            settings = _settings_by_key(out)
            self.assertEqual(settings["work_unit"]["source"], SOURCE_DECLARED)
            self.assertEqual(
                settings["work_unit"]["effective_value"], {"granularity": "user_story"}
            )
            self.assertEqual(
                settings["sublane_integration"]["effective_value"]["integration_branch"],
                "main-next",
            )
            self.assertEqual(settings["agents"]["source"], SOURCE_DEFAULT)

    def test_missing_config_is_all_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = _status_json(Path(tmp))
            settings = _settings_by_key(out)
            self.assertTrue(all(s["source"] == SOURCE_DEFAULT for s in settings.values()))

    def test_settings_key_set_matches_closed_vocabulary(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = _status_json(Path(tmp))
            self.assertEqual({s["key"] for s in out["settings"]}, set(CONFIG_BLOCK_KEYS))

    def test_text_output_renders_source_per_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "version: 2\nwork_unit:\n  granularity: user_story\n")
            text = _status_text(Path(tmp))
            self.assertIn("settings:", text)
            self.assertIn("work_unit: declared", text)
            self.assertIn("agents: default", text)

    def test_no_credential_shaped_content_in_json_output(self) -> None:
        # The schema forbids credential-shaped keys at load time; this is a sanity check
        # that the settings payload never introduces a new leak surface of its own.
        with tempfile.TemporaryDirectory() as tmp:
            _write(tmp, "version: 2\nwork_unit:\n  granularity: user_story\n")
            raw = json.dumps(_status_json(Path(tmp)))
            for banned in ("token", "secret", "password", "api_key", "credential"):
                self.assertNotIn(banned, raw.lower())


if __name__ == "__main__":
    unittest.main()
