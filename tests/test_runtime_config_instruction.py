"""Runtime-config / instruction doctor + install tests (Redmine #12150, split from tests/test_mozyo_bridge.py).

Behavior-preserving move of the instruction/runtime-config doctor + install
test families out of the monolithic test spine, per #12150 and
vibes/docs/logics/refactor-split-strategy.md (Priority 1 low-risk families).
No test logic changed."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.cli import build_parser


class DoctorInstructionTaxonomyTest(unittest.TestCase):
    """Redmine #11051: `doctor instruction` runbook + `runtime-config` rename.

    Design consultation answer #53306 fixed the taxonomy: option A (rename the
    top-level `instruction` group to `runtime-config`, add a read-only
    `doctor instruction` runbook), a 1-cycle deprecated alias that warns on
    stderr, and additive JSON.
    """

    def _help_text(self, argv: list[str]) -> str:
        parser = build_parser()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            with self.assertRaises(SystemExit):
                parser.parse_args(argv)
        return stdout.getvalue()

    def test_doctor_instruction_is_a_doctor_subcommand(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["doctor", "instruction"])
        self.assertEqual("doctor", args.command)
        self.assertEqual("instruction", args.doctor_command)
        self.assertEqual("cmd_doctor_instruction", args.func.__name__)

    def test_bare_doctor_still_runs_diagnostics(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["doctor"])
        self.assertEqual("doctor", args.command)
        self.assertIsNone(args.doctor_command)
        self.assertEqual("cmd_doctor", args.func.__name__)

    def test_runtime_config_group_parses(self) -> None:
        parser = build_parser()
        check = parser.parse_args(["runtime-config", "check"])
        self.assertEqual("runtime-config", check.command)
        self.assertEqual("check", check.runtime_config_command)
        self.assertEqual("cmd_instruction_doctor", check.func.__name__)
        install = parser.parse_args(["runtime-config", "install", "--write"])
        self.assertEqual("install", install.runtime_config_command)
        self.assertEqual("cmd_instruction_install", install.func.__name__)
        self.assertTrue(install.write)

    def test_canonical_runtime_config_is_not_a_deprecated_alias(self) -> None:
        parser = build_parser()
        args = parser.parse_args(["runtime-config", "check"])
        self.assertIsNone(getattr(args, "deprecated_alias", None))

    def test_instruction_alias_carries_deprecation_metadata(self) -> None:
        parser = build_parser()
        doctor_alias = parser.parse_args(["instruction", "doctor"])
        self.assertEqual(
            "mozyo-bridge instruction doctor", doctor_alias.deprecated_alias
        )
        self.assertEqual(
            "mozyo-bridge runtime-config check", doctor_alias.canonical_command
        )
        # Same underlying implementation as the canonical command.
        self.assertEqual("cmd_instruction_doctor", doctor_alias.func.__name__)
        install_alias = parser.parse_args(["instruction", "install"])
        self.assertEqual(
            "mozyo-bridge instruction install", install_alias.deprecated_alias
        )
        self.assertEqual(
            "mozyo-bridge runtime-config install", install_alias.canonical_command
        )

    def test_deprecated_alias_warns_on_stderr_only(self) -> None:
        from mozyo_bridge.application.cli import _warn_deprecated_alias

        # Alias -> warning on stderr.
        args = argparse.Namespace(
            deprecated_alias="mozyo-bridge instruction doctor",
            canonical_command="mozyo-bridge runtime-config check",
        )
        stderr = io.StringIO()
        stdout = io.StringIO()
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(stdout):
            _warn_deprecated_alias(args)
        self.assertIn("deprecated", stderr.getvalue())
        self.assertIn("runtime-config check", stderr.getvalue())
        # stdout untouched so JSON consumers stay additive.
        self.assertEqual("", stdout.getvalue())

        # Canonical command -> no warning.
        quiet = io.StringIO()
        with contextlib.redirect_stderr(quiet):
            _warn_deprecated_alias(argparse.Namespace(deprecated_alias=None))
        self.assertEqual("", quiet.getvalue())

    def test_runtime_config_help_describes_responsibility_split(self) -> None:
        help_text = self._help_text(["runtime-config", "--help"])
        self.assertIn("check", help_text)
        self.assertIn("install", help_text)
        self.assertIn("read-only", help_text)
        self.assertIn("dry-run", help_text)

    def _run_func(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            rc = args.func(args)
        return rc, stdout.getvalue()

    def test_canonical_runtime_config_text_uses_new_names(self) -> None:
        # Review Gate #53340 finding: the canonical commands must not print the
        # legacy `instruction doctor/install` names on stdout. The check command
        # is exercised end-to-end; the install header is asserted on its
        # formatter (the command body needs a workspace-defaults source).
        from mozyo_bridge.application.instruction_install import (
            format_instruction_install_text,
        )

        with tempfile.TemporaryDirectory() as tmp:
            _, check_out = self._run_func(["runtime-config", "check", "--target", tmp])
            self.assertIn("runtime-config check:", check_out)
            self.assertNotIn("instruction doctor:", check_out)

        install_text = format_instruction_install_text(
            {
                "ok": True,
                "profile": "redmine-codex",
                "action": "up-to-date",
                "target": "/repo",
                "messages": [],
            }
        )
        self.assertIn("runtime-config install:", install_text)
        self.assertNotIn("instruction install:", install_text)


class DoctorInstructionRunbookTest(unittest.TestCase):
    """The runbook synthesis (`build_runbook`) is pure given doctor results."""

    def _doctor_result(self, **overrides: str) -> dict:
        sections = {
            "cli": {"status": "ok", "version": "9.9.9", "executable": "/x/mozyo-bridge"},
            "rules": {"status": "ok", "next_action": []},
            "codex_skill": {"status": "ok"},
            "claude_skill": {"status": "plugin-managed"},
            "scaffold": {"status": "ok", "detail": {"preset": "redmine-governed"}},
            "claude_nagger": {"status": "ok"},
            "tmux": {"status": "ok", "artifact": {"host_wiring": {"next_action": []}}},
        }
        for key, status in overrides.items():
            sections[key] = {**sections.get(key, {}), "status": status}
        ok = all(
            s.get("status") not in {"missing", "drifted", "warning", "incomplete"}
            for s in sections.values()
        )
        return {"ok": ok, "sections": sections}

    def test_runbook_order_and_migration_present(self) -> None:
        from mozyo_bridge.application.doctor_instruction import build_runbook

        steps = build_runbook(
            self._doctor_result(), {"ok": True}, "/repo", "redmine-codex"
        )
        ids = [s["id"] for s in steps]
        self.assertEqual(
            ids,
            [
                "cli",
                "rules",
                "agent_skills",
                "scaffold",
                "runtime_config",
                "optional_utilities",
                "final_verification",
            ],
        )

    def test_clean_environment_needs_no_action(self) -> None:
        from mozyo_bridge.application.doctor_instruction import (
            STATUS_ACTION,
            build_runbook,
        )

        steps = build_runbook(
            self._doctor_result(), {"ok": True}, "/repo", "redmine-codex"
        )
        self.assertFalse([s for s in steps if s["status"] == STATUS_ACTION])

    def test_skill_step_labels_primary_and_fallback(self) -> None:
        from mozyo_bridge.application.doctor_instruction import build_runbook

        steps = build_runbook(
            self._doctor_result(claude_skill="missing", codex_skill="missing"),
            {"ok": True},
            "/repo",
            "redmine-codex",
        )
        skills = next(s for s in steps if s["id"] == "agent_skills")
        self.assertEqual("action", skills["status"])
        roles = {c["role"] for c in skills["commands"]}
        self.assertIn("primary", roles)
        self.assertIn("fallback", roles)
        # Claude primary path is the plugin marketplace, not curl.
        primary_claude = next(
            c for c in skills["commands"]
            if c["role"] == "primary" and "claude" in c.get("for", "")
        )
        self.assertIn("plugin", primary_claude["command"])

    def test_cli_step_surfaces_stale_source_drift_note(self) -> None:
        """Redmine #11855: a stale-installed-CLI warning in the cli section
        surfaces the repo-local invocation in the CLI readiness step."""
        from mozyo_bridge.application.doctor_instruction import (
            STATUS_ACTION,
            build_runbook,
        )

        doctor_result = self._doctor_result()
        doctor_result["sections"]["cli"] = {
            "status": "warning",
            "version": "9.9.9",
            "executable": "/x/mozyo-bridge",
            "source_drift": {
                "relation": "version-differs",
                "repo_local_invocation": "PYTHONPATH=src python3 -m mozyo_bridge",
            },
            "next_action": ["use repo-local CLI"],
        }
        steps = build_runbook(doctor_result, {"ok": True}, "/repo", "redmine-codex")
        cli_step = next(s for s in steps if s["id"] == "cli")
        self.assertEqual(STATUS_ACTION, cli_step["status"])
        self.assertTrue(
            any(
                "PYTHONPATH=src python3 -m mozyo_bridge" in note
                for note in cli_step["notes"]
            )
        )

    def test_scaffold_drift_is_review_before_restore(self) -> None:
        from mozyo_bridge.application.doctor_instruction import build_runbook

        steps = build_runbook(
            self._doctor_result(scaffold="drifted"),
            {"ok": True},
            "/repo",
            "redmine-codex",
        )
        scaffold = next(s for s in steps if s["id"] == "scaffold")
        self.assertEqual("action", scaffold["status"])
        commands = scaffold["commands"]
        # status/diff are primary; apply --backup is the fallback that comes last.
        self.assertEqual("primary", commands[0]["role"])
        self.assertIn("scaffold status", commands[0]["command"])
        self.assertEqual("fallback", commands[-1]["role"])
        self.assertIn("--backup", commands[-1]["command"])

    def test_result_reports_pending_and_migrations(self) -> None:
        from mozyo_bridge.application.doctor_instruction import run_doctor_instruction

        with patch(
            "mozyo_bridge.application.doctor_instruction.run_doctor",
            return_value=self._doctor_result(rules="missing"),
        ), patch(
            "mozyo_bridge.application.doctor_instruction.run_instruction_doctor",
            return_value={"ok": True},
        ), patch(
            "mozyo_bridge.application.doctor_instruction.doctor_target",
            return_value=Path("/repo"),
        ):
            result = run_doctor_instruction(argparse.Namespace(repo="/repo"))
        self.assertFalse(result["ok"])
        self.assertIn("rules", result["pending_step_ids"])
        olds = {m["old"] for m in result["migrations"]}
        self.assertIn("mozyo-bridge instruction doctor", olds)
        self.assertIn("mozyo-bridge instruction install", olds)


class InstructionDoctorTest(unittest.TestCase):
    """Redmine #10854: opt-in `instruction doctor --profile redmine-codex`.

    Read-only, profile-aware check that a Redmine/Codex workspace carries
    the repo-root runtime config the bootstrap docs require. Pins the
    completion conditions: missing config fails, valid config passes,
    X-Default-Project mismatch fails, credential-shape values fail, and
    `.mcp.json` is parsed + secret-scanned while staying non-authoritative.
    """

    VALID_TOML = (
        "[redmine]\n"
        'default_project = "giken-3800-mozyo-bridge"\n'
        'default_project_name = "mozyo-bridge"\n'
        'default_project_url = "https://redmine.example.invalid/projects/x"\n'
        "\n"
        "[mcp_servers.redmine_epic_grid]\n"
        'url = "https://redmine.example.invalid/mcp/rpc"\n'
        'http_headers = { X-Default-Project = "giken-3800-mozyo-bridge" }\n'
    )

    def run_cli(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            result = args.func(args)
        return result, stdout.getvalue()

    def _write_codex(self, project: Path, toml_text: str) -> None:
        (project / ".codex").mkdir(parents=True, exist_ok=True)
        (project / ".codex" / "config.toml").write_text(toml_text, encoding="utf-8")

    def _result(self, project: Path) -> dict:
        rc, out = self.run_cli(
            ["instruction", "doctor", "--target", str(project), "--json"]
        )
        payload = json.loads(out)
        return {"rc": rc, "payload": payload}

    def _check_status(self, payload: dict, name: str) -> str | None:
        for check in payload["checks"]:
            if check["name"] == name:
                return check["status"]
        return None

    def test_missing_codex_config_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertFalse(r["payload"]["ok"])
            self.assertEqual(
                "fail", self._check_status(r["payload"], "codex_config_present")
            )

    def test_valid_repo_root_config_ok(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_codex(project, self.VALID_TOML)
            r = self._result(project)
            self.assertEqual(0, r["rc"])
            self.assertTrue(r["payload"]["ok"])
            self.assertEqual(
                "ok",
                self._check_status(r["payload"], "codex_default_project_consistent"),
            )
            # No .mcp.json: deferral keeps it informational, not a failure.
            self.assertEqual(
                "info", self._check_status(r["payload"], "mcp_json_present")
            )

    def test_default_project_mismatch_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            mismatched = self.VALID_TOML.replace(
                'X-Default-Project = "giken-3800-mozyo-bridge"',
                'X-Default-Project = "some-other-project"',
            )
            self._write_codex(project, mismatched)
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertEqual(
                "fail",
                self._check_status(r["payload"], "codex_default_project_consistent"),
            )

    def test_credential_shaped_value_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            # Build the secret-shaped assignment at runtime so the test file
            # itself carries no release-tree-blocking credential literal.
            secret_line = "api_key" + " = " + '"' + "x" * 24 + '"\n'
            self._write_codex(project, self.VALID_TOML + secret_line)
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertEqual(
                "fail",
                self._check_status(r["payload"], "codex_config_no_credentials"),
            )

    def test_invalid_toml_fails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_codex(project, "[redmine\nnot valid toml")
            r = self._result(project)
            self.assertEqual(1, r["rc"])
            self.assertEqual(
                "fail", self._check_status(r["payload"], "codex_config_parse")
            )

    def test_mcp_json_present_is_parsed_and_secret_scanned(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            self._write_codex(project, self.VALID_TOML)

            # Clean .mcp.json: parsed, no secrets, stays non-authoritative
            # (present check is info, not fail) -> overall ok.
            (project / ".mcp.json").write_text(
                json.dumps({"mcpServers": {"redmine": {"url": "https://x.invalid"}}}),
                encoding="utf-8",
            )
            r = self._result(project)
            self.assertEqual(0, r["rc"])
            self.assertEqual("info", self._check_status(r["payload"], "mcp_json_present"))
            self.assertEqual("ok", self._check_status(r["payload"], "mcp_json_parse"))
            self.assertEqual(
                "ok", self._check_status(r["payload"], "mcp_json_no_credentials")
            )

            # Malformed .mcp.json fails the parse check.
            (project / ".mcp.json").write_text("{not json", encoding="utf-8")
            r2 = self._result(project)
            self.assertEqual(1, r2["rc"])
            self.assertEqual("fail", self._check_status(r2["payload"], "mcp_json_parse"))

            # Credential-shaped key in .mcp.json fails the secret scan. Use a
            # key name the shared workspace-defaults heuristic flags
            # (`client_secret`) so this stays consistent with the release-tree
            # hygiene gate rather than inventing a second heuristic. Build the
            # value at runtime so the test file carries no literal secret.
            secret_key = "client_secret"
            (project / ".mcp.json").write_text(
                json.dumps({"servers": {"redmine": {secret_key: "x" * 30}}}),
                encoding="utf-8",
            )
            r3 = self._result(project)
            self.assertEqual(1, r3["rc"])
            self.assertEqual(
                "fail", self._check_status(r3["payload"], "mcp_json_no_credentials")
            )

    def test_text_output_is_human_readable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            project = Path(tmp)
            rc, out = self.run_cli(
                ["instruction", "doctor", "--target", str(project)]
            )
            self.assertEqual(1, rc)
            # Redmine #11051: stdout uses the canonical command name even when
            # invoked through the deprecated `instruction doctor` alias.
            self.assertIn("runtime-config check: FAIL", out)
            self.assertNotIn("instruction doctor:", out)
            self.assertIn("codex_config_present", out)

    def test_target_defaults_to_mozyo_repo_env(self) -> None:
        # Regression for Codex review #52114 Finding 2: with no --target, the
        # command must honour MOZYO_REPO (matching the --target help text and
        # the rest of the CLI's repo resolution), not just cwd.
        with tempfile.TemporaryDirectory() as repo_tmp, tempfile.TemporaryDirectory() as cwd_tmp:
            project = Path(repo_tmp)
            self._write_codex(project, self.VALID_TOML)
            prev_repo = os.environ.get("MOZYO_REPO")
            prev_cwd = os.getcwd()
            try:
                os.environ["MOZYO_REPO"] = str(project)
                os.chdir(cwd_tmp)  # cwd is a DIFFERENT dir with no .codex config
                rc, out = self.run_cli(
                    ["instruction", "doctor", "--profile", "redmine-codex", "--json"]
                )
                payload = json.loads(out)
            finally:
                os.chdir(prev_cwd)
                if prev_repo is None:
                    os.environ.pop("MOZYO_REPO", None)
                else:
                    os.environ["MOZYO_REPO"] = prev_repo
            self.assertEqual(0, rc)
            self.assertTrue(payload["ok"])
            self.assertEqual(str(project.resolve()), payload["target"])

    def test_toml_parser_falls_back_for_python_310(self) -> None:
        # Regression for Codex review #52114 Finding 1: the package supports
        # Python >=3.10 but `tomllib` is stdlib only on 3.11+. The module must
        # bind a TOML parser (tomllib on 3.11+, tomli on 3.10) rather than
        # importing tomllib unconditionally.
        from mozyo_bridge.application import instruction_doctor as mod

        self.assertIn(mod._toml.__name__, ("tomllib", "tomli"))
        self.assertTrue(hasattr(mod._toml, "loads"))
        self.assertIs(mod._TOMLDecodeError, mod._toml.TOMLDecodeError)


class InstructionInstallTest(unittest.TestCase):
    """Pin Redmine #10930: project workspace-defaults into Codex runtime config.

    The single source of truth stays `<repo>/.mozyo-bridge/workspace-defaults.yaml`;
    `instruction install` renders/merges the verified Redmine default project into
    `<repo>/.codex/config.toml` so `instruction doctor` turns green, without ever
    touching home config or generating credentials.
    """

    def _stage(self, repo: Path, *, verified: bool = True, identifier: str = "giken-3800-mozyo-bridge") -> None:
        (repo / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
        verification_date = "2026-05-28" if verified else ""
        verified_by = "hollySizzle" if verified else '""'
        (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
            "schema_version: 1\n"
            "redmine:\n"
            "  default_project:\n"
            f"    identifier: {identifier}\n"
            "    name: mozyo_bridge\n"
            f"    url: https://redmine.giken.or.jp/projects/{identifier}\n"
            "    parent_label: parent\n"
            "  verification:\n"
            f"    verified: {str(verified).lower()}\n"
            f'    verification_date: "{verification_date}"\n'
            f"    verified_by: {verified_by}\n"
            "outputs:\n"
            "  - kind: redmine_markdown\n"
            "    target: .mozyo-bridge/redmine-defaults.md\n",
            encoding="utf-8",
        )

    def _run(self, argv: list[str]) -> tuple[int, str]:
        parser = build_parser()
        args = parser.parse_args(argv)
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            code = args.func(args)
        return code, stdout.getvalue()

    def _doctor_green(self, repo: Path) -> bool:
        from mozyo_bridge.application.instruction_doctor import run_instruction_doctor

        return bool(
            run_instruction_doctor(
                argparse.Namespace(target=str(repo), profile="redmine-codex")
            )["ok"]
        )

    def test_missing_config_dry_run_then_write_makes_doctor_green(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex" / "config.toml"

            # Dry-run must not write.
            code, out = self._run(
                ["instruction", "install", "--target", str(repo)]
            )
            self.assertEqual(0, code)
            self.assertIn("would write", out)
            self.assertFalse(config.exists())
            self.assertFalse(self._doctor_green(repo))

            # Write makes the doctor green.
            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            self.assertTrue(config.exists())
            # Redmine #11051: post-write message uses the canonical command name.
            self.assertIn("runtime-config check is green", out)
            self.assertTrue(self._doctor_green(repo))

    def test_generated_config_has_consistent_default_project(self) -> None:
        from mozyo_bridge.application.instruction_install import _toml

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo, identifier="giken-3800-mozyo-bridge")
            self._run(["instruction", "install", "--target", str(repo), "--write"])

            parsed = _toml.loads(
                (repo / ".codex" / "config.toml").read_text(encoding="utf-8")
            )
            self.assertEqual(
                "giken-3800-mozyo-bridge", parsed["redmine"]["default_project"]
            )
            self.assertEqual(
                "https://redmine.giken.or.jp/mcp/rpc",
                parsed["mcp_servers"]["redmine_epic_grid"]["url"],
            )
            self.assertEqual(
                "giken-3800-mozyo-bridge",
                parsed["mcp_servers"]["redmine_epic_grid"]["http_headers"][
                    "X-Default-Project"
                ],
            )

    def test_existing_unrelated_table_is_preserved_on_append(self) -> None:
        from mozyo_bridge.application.instruction_install import _toml

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            (config / "config.toml").write_text(
                "[history]\nmax_size = 1000\n", encoding="utf-8"
            )

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            parsed = _toml.loads((config / "config.toml").read_text(encoding="utf-8"))
            # Unrelated table preserved AND managed block added.
            self.assertEqual(1000, parsed["history"]["max_size"])
            self.assertEqual(
                "giken-3800-mozyo-bridge", parsed["redmine"]["default_project"]
            )
            self.assertTrue(self._doctor_green(repo))

    def test_conflict_fails_and_does_not_write_without_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            original = '[redmine]\ndefault_project = "other-proj"\n[history]\nx = 1\n'
            (config / "config.toml").write_text(original, encoding="utf-8")

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(1, code)
            self.assertIn("--force", out)
            # File must be left untouched.
            self.assertEqual(
                original, (config / "config.toml").read_text(encoding="utf-8")
            )

    def test_force_regenerates_managed_tables_and_preserves_others(self) -> None:
        from mozyo_bridge.application.instruction_install import _toml

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            (config / "config.toml").write_text(
                '[redmine]\ndefault_project = "other-proj"\n\n[history]\nx = 1\n',
                encoding="utf-8",
            )

            code, _ = self._run(
                ["instruction", "install", "--target", str(repo), "--write", "--force"]
            )
            self.assertEqual(0, code)
            parsed = _toml.loads((config / "config.toml").read_text(encoding="utf-8"))
            self.assertEqual(
                "giken-3800-mozyo-bridge", parsed["redmine"]["default_project"]
            )
            self.assertEqual(1, parsed["history"]["x"])  # unrelated table preserved
            self.assertTrue(self._doctor_green(repo))

    def test_already_up_to_date_is_noop(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            self._run(["instruction", "install", "--target", str(repo), "--write"])
            before = (repo / ".codex" / "config.toml").read_text(encoding="utf-8")

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(0, code)
            self.assertIn("already matches", out)
            self.assertEqual(
                before, (repo / ".codex" / "config.toml").read_text(encoding="utf-8")
            )

    def test_unverified_default_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo, verified=False)

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(1, code)
            self.assertIn("verification is incomplete", out)
            self.assertFalse((repo / ".codex" / "config.toml").exists())

    def test_credential_shape_in_workspace_defaults_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            (repo / ".mozyo-bridge").mkdir(parents=True)
            # A credential-shape value must make load fail (die -> SystemExit),
            # so no runtime config is ever generated from it.
            secret_key = "api" + "_key"
            (repo / ".mozyo-bridge" / "workspace-defaults.yaml").write_text(
                "schema_version: 1\n"
                "redmine:\n"
                "  default_project:\n"
                "    identifier: giken-3800-mozyo-bridge\n"
                "    name: mozyo_bridge\n"
                "    url: https://redmine.giken.or.jp/projects/giken-3800-mozyo-bridge\n"
                "    parent_label: parent\n"
                f"    {secret_key}: AKIAEXAMPLEEXAMPLE12\n"
                "  verification:\n"
                "    verified: true\n"
                '    verification_date: "2026-05-28"\n'
                "    verified_by: hollySizzle\n"
                "outputs:\n"
                "  - kind: redmine_markdown\n"
                "    target: .mozyo-bridge/redmine-defaults.md\n",
                encoding="utf-8",
            )
            with contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    self._run(["instruction", "install", "--target", str(repo), "--write"])
            self.assertFalse((repo / ".codex" / "config.toml").exists())

    def test_invalid_existing_toml_is_not_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            self._stage(repo)
            config = repo / ".codex"
            config.mkdir()
            original = "this is = not valid = toml ]["
            (config / "config.toml").write_text(original, encoding="utf-8")

            code, out = self._run(
                ["instruction", "install", "--target", str(repo), "--write"]
            )
            self.assertEqual(1, code)
            self.assertIn("not valid TOML", out)
            self.assertEqual(
                original, (config / "config.toml").read_text(encoding="utf-8")
            )

    def test_instruction_install_cli_parses(self) -> None:
        parser = build_parser()
        args = parser.parse_args(
            ["instruction", "install", "--target", "/r", "--write", "--force"]
        )
        self.assertEqual("instruction", args.command)
        self.assertEqual("install", args.instruction_command)
        self.assertEqual("/r", args.target)
        self.assertTrue(args.write)
        self.assertTrue(args.force)


if __name__ == "__main__":
    unittest.main()
