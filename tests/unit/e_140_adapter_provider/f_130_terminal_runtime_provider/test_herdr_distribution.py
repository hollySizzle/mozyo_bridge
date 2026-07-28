"""Unit pins for the herdr distribution surface (Redmine #13249).

Covers the supply-chain **pin posture** (render + validate) and the **opt-in
integration-hook installer** (plan / apply / rollback). Every test runs against an
isolated temp HOME/XDG and an injected fake herdr runner — no test touches the real
``~/.claude`` / ``~/.codex``, credentials, the network, or a live herdr binary
(issue #13249 requirement 4).
"""

from __future__ import annotations

import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_pin_posture import (  # noqa: E402
    PIN_MODE_OFFLINE,
    PIN_MODE_PINNED_MIRROR,
    REASON_MANIFEST_CHECK_UNPINNED,
    REASON_MIRROR_URL_INSECURE,
    REASON_UPDATE_TABLE_MALFORMED,
    REASON_VERSION_CHECK_ENABLED,
    HerdrPinPosture,
    HerdrPinPostureError,
    PinVerdict,
    render_pin_config,
    validate_pin_record,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pin_posture_ops import (  # noqa: E402
    render_posture,
    verify_config,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_integration_install import (  # noqa: E402
    AGENT_CLAUDE,
    AGENT_CODEX,
    REASON_CONFIG_DIR_MISSING,
    REASON_CONFIG_DIR_UNREADABLE,
    REASON_CONFIG_PIN_MISMATCH,
    REASON_HERDR_ERROR,
    REASON_HERDR_UNRESOLVED,
    REASON_PARTIAL_FAILURE,
    REASON_ROLLBACK_INCOMPLETE,
    REASON_UNPINNED_REMOTE,
    REASON_UNSAFE_CONFIG_PATH,
    DirSnapshot,
    HerdrIntegrationInstallError,
    diff_snapshots,
    is_credential_shaped,
    is_safe_config_dir,
    normalize_agents,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E402
    herdr_integration_install_dir_io as dir_io,
    herdr_integration_install_ops as install_ops,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_integration_install_ops import (  # noqa: E402
    HERDR_CONFIG_PATH_ENV,
    InstallInputs,
    apply_install,
    plan_install,
)


# --- pin posture: domain -----------------------------------------------------


class PinPostureModelTest(unittest.TestCase):
    def test_offline_render(self) -> None:
        text = render_pin_config(HerdrPinPosture.offline())
        self.assertIn("version_check = false", text)
        self.assertIn("manifest_check = false", text)
        # deterministic: same posture → byte-identical render
        self.assertEqual(text, render_pin_config(HerdrPinPosture.offline()))

    def test_pinned_mirror_render_and_env(self) -> None:
        url = "https://mirror.internal/agent-catalog"
        posture = HerdrPinPosture.pinned_mirror(url)
        text = render_pin_config(posture)
        self.assertIn("version_check = false", text)
        self.assertIn("manifest_check = true", text)
        self.assertEqual(
            posture.env_directives(),
            (("HERDR_AGENT_DETECTION_MANIFEST_CATALOG_URL", url),),
        )

    def test_offline_rejects_mirror_url(self) -> None:
        with self.assertRaises(HerdrPinPostureError):
            HerdrPinPosture(mode=PIN_MODE_OFFLINE, manifest_catalog_url="https://x/y")

    def test_pinned_mirror_requires_url(self) -> None:
        with self.assertRaises(HerdrPinPostureError):
            HerdrPinPosture(mode=PIN_MODE_PINNED_MIRROR)

    def test_pinned_mirror_rejects_insecure_url(self) -> None:
        for bad in ("http://mirror/x", "mirror/x", "https://", "", "https:// x"):
            with self.subTest(bad=bad):
                with self.assertRaises(HerdrPinPostureError):
                    HerdrPinPosture.pinned_mirror(bad)

    def test_unknown_mode(self) -> None:
        with self.assertRaises(HerdrPinPostureError):
            HerdrPinPosture(mode="loose")


class ValidatePinRecordTest(unittest.TestCase):
    def test_offline_pinned(self) -> None:
        verdict = validate_pin_record({"version_check": False, "manifest_check": False})
        self.assertTrue(verdict.pinned)
        self.assertEqual(verdict.mode, PIN_MODE_OFFLINE)

    def test_pinned_mirror(self) -> None:
        verdict = validate_pin_record(
            {"version_check": False, "manifest_check": True},
            manifest_catalog_url="https://mirror/x",
        )
        self.assertTrue(verdict.pinned)
        self.assertEqual(verdict.mode, PIN_MODE_PINNED_MIRROR)

    def test_absent_keys_unpinned(self) -> None:
        # An empty [update] table = herdr defaults (on) = unpinned.
        verdict = validate_pin_record({})
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_VERSION_CHECK_ENABLED)

    def test_none_table_unpinned(self) -> None:
        verdict = validate_pin_record(None)
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_VERSION_CHECK_ENABLED)

    def test_version_check_on_unpinned(self) -> None:
        verdict = validate_pin_record({"version_check": True, "manifest_check": False})
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_VERSION_CHECK_ENABLED)

    def test_manifest_on_without_url_unpinned(self) -> None:
        verdict = validate_pin_record({"version_check": False, "manifest_check": True})
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_MANIFEST_CHECK_UNPINNED)

    def test_manifest_on_insecure_url_unpinned(self) -> None:
        verdict = validate_pin_record(
            {"version_check": False, "manifest_check": True},
            manifest_catalog_url="http://mirror/x",
        )
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_MIRROR_URL_INSECURE)

    def test_non_bool_switch_malformed(self) -> None:
        verdict = validate_pin_record({"version_check": 0, "manifest_check": False})
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_UPDATE_TABLE_MALFORMED)

    def test_non_mapping_table_malformed(self) -> None:
        verdict = validate_pin_record(["not", "a", "table"])
        self.assertFalse(verdict.pinned)
        self.assertEqual(verdict.reason, REASON_UPDATE_TABLE_MALFORMED)

    def test_verdict_invariants(self) -> None:
        with self.assertRaises(HerdrPinPostureError):
            PinVerdict(pinned=True, mode=None)
        with self.assertRaises(HerdrPinPostureError):
            PinVerdict(pinned=False, reason=None)


# --- pin posture: ops (temp files) -------------------------------------------


class PinPostureOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def _write(self, text: str) -> Path:
        path = self.tmp / "herdr.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_render_ops_offline(self) -> None:
        result = render_posture(PIN_MODE_OFFLINE)
        self.assertEqual(result.mode, PIN_MODE_OFFLINE)
        self.assertIn("manifest_check = false", result.config_text)

    def test_verify_offline_config(self) -> None:
        path = self._write("[update]\nversion_check = false\nmanifest_check = false\n")
        result = verify_config(path)
        self.assertTrue(result.ok)
        self.assertEqual(result.verdict.mode, PIN_MODE_OFFLINE)

    def test_verify_unpinned_config(self) -> None:
        path = self._write("[update]\nversion_check = true\n")
        result = verify_config(path)
        self.assertFalse(result.ok)
        self.assertEqual(result.verdict.reason, REASON_VERSION_CHECK_ENABLED)

    def test_verify_no_update_table(self) -> None:
        path = self._write("[other]\nx = 1\n")
        result = verify_config(path)
        self.assertFalse(result.ok)

    def test_verify_missing_file(self) -> None:
        result = verify_config(self.tmp / "nope.toml")
        self.assertFalse(result.ok)
        self.assertEqual(result.verdict.reason, REASON_UPDATE_TABLE_MALFORMED)

    def test_verify_invalid_toml(self) -> None:
        path = self._write("this is = = not toml [[[")
        result = verify_config(path)
        self.assertFalse(result.ok)
        self.assertEqual(result.verdict.reason, REASON_UPDATE_TABLE_MALFORMED)

    def test_verify_pinned_mirror_with_url(self) -> None:
        path = self._write("[update]\nversion_check = false\nmanifest_check = true\n")
        result = verify_config(path, manifest_catalog_url="https://mirror/x")
        self.assertTrue(result.ok)
        self.assertEqual(result.verdict.mode, PIN_MODE_PINNED_MIRROR)


# --- integration install: domain ---------------------------------------------


class InstallDomainTest(unittest.TestCase):
    def test_normalize_default_both(self) -> None:
        self.assertEqual(normalize_agents(None), (AGENT_CLAUDE, AGENT_CODEX))
        self.assertEqual(normalize_agents([]), (AGENT_CLAUDE, AGENT_CODEX))

    def test_normalize_dedup(self) -> None:
        self.assertEqual(normalize_agents(["codex", "codex"]), ("codex",))

    def test_normalize_unknown_raises(self) -> None:
        with self.assertRaises(HerdrIntegrationInstallError):
            normalize_agents(["gemini"])

    def test_diff_snapshots(self) -> None:
        before = DirSnapshot.of({"a": "1", "b": "2"})
        after = DirSnapshot.of({"a": "1", "b": "9", "c": "3"})
        diff = diff_snapshots(before, after)
        self.assertEqual(diff.added, ("c",))
        self.assertEqual(diff.changed, ("b",))
        self.assertEqual(diff.removed, ())
        self.assertFalse(diff.is_empty)

    def test_snapshot_rejects_duplicate(self) -> None:
        with self.assertRaises(HerdrIntegrationInstallError):
            DirSnapshot(entries=(("a", "1"), ("a", "2")))

    def test_safe_config_dir(self) -> None:
        self.assertTrue(is_safe_config_dir(resolved="/home/u/.claude", home_resolved="/home/u"))
        self.assertTrue(is_safe_config_dir(resolved="/home/u", home_resolved="/home/u"))
        self.assertFalse(is_safe_config_dir(resolved="/etc/passwd", home_resolved="/home/u"))
        self.assertFalse(is_safe_config_dir(resolved="/home/user2/.claude", home_resolved="/home/u"))

    def test_credential_shaped(self) -> None:
        self.assertTrue(is_credential_shaped(".credentials.json"))
        self.assertTrue(is_credential_shaped("auth_token"))
        self.assertTrue(is_credential_shaped("server.pem"))
        self.assertFalse(is_credential_shaped("hooks"))
        self.assertFalse(is_credential_shaped("session.sh"))


# --- integration install: ops (temp HOME + fake runner) ----------------------

_HOOK_REL = "hooks/mozyo-session.sh"
_HOOK_BODY = "#!/bin/sh\n# herdr session hook (fake)\n"


class FakeHerdrIntegration:
    """A fake ``herdr integration install`` runner: writes a hook into the agent dir.

    Mirrors the live behaviour proven in PoC E2 (the hook is a local file under the
    agent config dir) without spawning anything. ``fail_for`` names agents the fake
    should fail (non-zero exit) to drive the rollback path.
    """

    def __init__(self, *, fail_for: "frozenset[str]" = frozenset()):
        self.fail_for = fail_for
        self.calls: "list[list[str]]" = []
        self.envs: "list[dict[str, str]]" = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **_):
        self.calls.append(list(argv))
        self.envs.append(dict(env or {}))
        # argv == [binary, "integration", "install", <agent>]
        agent = argv[3]
        home = Path((env or {}).get("HOME", ""))
        dirname = ".claude" if agent == AGENT_CLAUDE else ".codex"
        config_dir = home / dirname
        if agent in self.fail_for:
            # Simulate herdr writing a partial artifact then failing, so rollback
            # has something to undo.
            (config_dir / "hooks").mkdir(parents=True, exist_ok=True)
            (config_dir / "hooks" / "partial.tmp").write_text("partial", encoding="utf-8")
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="boom")
        (config_dir / "hooks").mkdir(parents=True, exist_ok=True)
        (config_dir / _HOOK_REL).write_text(_HOOK_BODY, encoding="utf-8")
        return subprocess.CompletedProcess(list(argv), 0, stdout="installed", stderr="")


class _ResidueLockingFake:
    """A fake herdr that leaves un-rollback-able residue (drives finding-1 verification).

    For a ``fail_for`` agent it writes ``hooks/partial.tmp`` then chmods ``hooks`` to
    ``0o500`` so the installer's rollback ``unlink`` fails and residue remains. For a
    ``lock_agent`` it installs the hook successfully then locks ``hooks`` too, so that
    agent's later transactional rollback (triggered by another agent's failure) also
    cannot restore.
    """

    def __init__(self, *, fail_for="", lock_agent=None):
        self.fail_for = frozenset(fail_for) if fail_for else frozenset()
        self.lock_agent = lock_agent
        self.calls: "list[list[str]]" = []

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **_):
        self.calls.append(list(argv))
        agent = argv[3]
        home = Path((env or {}).get("HOME", ""))
        config_dir = home / (".claude" if agent == AGENT_CLAUDE else ".codex")
        hooks = config_dir / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        if agent in self.fail_for:
            (hooks / "partial.tmp").write_text("partial", encoding="utf-8")
            hooks.chmod(0o500)  # rollback unlink will fail → residue remains
            return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="boom")
        (config_dir / _HOOK_REL).write_text(_HOOK_BODY, encoding="utf-8")
        if agent == self.lock_agent:
            hooks.chmod(0o500)  # this agent's later rollback will fail too
        return subprocess.CompletedProcess(list(argv), 0, stdout="installed", stderr="")


class InstallOpsTest(unittest.TestCase):
    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        # isolated temp HOME with both agent config dirs present
        self.home = self.tmp / "home"
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        # a pinned herdr config
        self.herdr_config = self.tmp / "herdr.toml"
        self.herdr_config.write_text(
            "[update]\nversion_check = false\nmanifest_check = false\n", encoding="utf-8"
        )
        # a stub executable so resolve_herdr_binary succeeds (never actually spawned)
        self.binary = self.tmp / "herdr-stub"
        self.binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.env = {
            "MOZYO_HERDR_BINARY": str(self.binary),
            "PATH": "/usr/bin:/bin",
        }

    def _inputs(self, *, agents=(AGENT_CLAUDE, AGENT_CODEX), runner=None, herdr_config=None):
        return InstallInputs(
            home=self.home,
            agents=agents,
            herdr_config=self.herdr_config if herdr_config is None else herdr_config,
            env=self.env,
            runner=runner,
        )

    def _dir_state(self) -> dict:
        """A byte-level manifest of both agent dirs, for zero-mutation asserts."""
        state = {}
        for sub in (".claude", ".codex"):
            root = self.home / sub
            for dirpath, _dirs, files in os.walk(root):
                for name in files:
                    p = Path(dirpath) / name
                    state[str(p.relative_to(self.home))] = p.read_bytes()
        return state

    # -- plan (read-only) --

    def test_plan_all_ready(self) -> None:
        report = plan_install(self._inputs())
        self.assertTrue(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(len(report.plans), 2)
        self.assertTrue(all(p.ready for p in report.plans))
        self.assertEqual(report.pin_mode, PIN_MODE_OFFLINE)

    def test_plan_is_zero_mutation(self) -> None:
        before = self._dir_state()
        plan_install(self._inputs())
        self.assertEqual(before, self._dir_state())

    def test_plan_gated_unpinned(self) -> None:
        unpinned = self.tmp / "unpinned.toml"
        unpinned.write_text("[update]\nversion_check = true\n", encoding="utf-8")
        report = plan_install(self._inputs(herdr_config=unpinned))
        self.assertFalse(report.ok)
        self.assertTrue(all(p.reason == REASON_UNPINNED_REMOTE for p in report.plans))

    def test_plan_gated_no_herdr_config(self) -> None:
        report = plan_install(
            InstallInputs(home=self.home, agents=(AGENT_CLAUDE,), env=self.env)
        )
        self.assertFalse(report.ok)
        self.assertEqual(report.plans[0].reason, REASON_UNPINNED_REMOTE)

    def test_plan_gated_missing_dir(self) -> None:
        import shutil

        shutil.rmtree(self.home / ".codex")
        report = plan_install(self._inputs())
        by_agent = {p.agent: p for p in report.plans}
        self.assertTrue(by_agent[AGENT_CLAUDE].ready)
        self.assertEqual(by_agent[AGENT_CODEX].reason, REASON_CONFIG_DIR_MISSING)
        self.assertFalse(report.ok)

    def test_plan_gated_unsafe_symlink(self) -> None:
        # Replace .codex with a symlink escaping home.
        outside = self.tmp / "outside"
        outside.mkdir()
        (self.home / ".codex").rmdir()
        os.symlink(outside, self.home / ".codex")
        report = plan_install(self._inputs())
        by_agent = {p.agent: p for p in report.plans}
        self.assertEqual(by_agent[AGENT_CODEX].reason, REASON_UNSAFE_CONFIG_PATH)

    # -- apply (opt-in) --

    def test_apply_happy_path(self) -> None:
        fake = FakeHerdrIntegration()
        report = apply_install(self._inputs(runner=fake.run))
        self.assertTrue(report.ok)
        self.assertTrue(report.applied)
        self.assertEqual(len(fake.calls), 2)
        for o in report.outcomes:
            self.assertTrue(o.ok)
            self.assertIn(_HOOK_REL, o.diff.added)
        # the hook file is really there
        self.assertTrue((self.home / ".claude" / _HOOK_REL).exists())

    def test_apply_refused_when_gated_is_zero_mutation(self) -> None:
        unpinned = self.tmp / "unpinned.toml"
        unpinned.write_text("[update]\nversion_check = true\n", encoding="utf-8")
        before = self._dir_state()
        fake = FakeHerdrIntegration()
        report = apply_install(self._inputs(runner=fake.run, herdr_config=unpinned))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked
        self.assertEqual(before, self._dir_state())  # nothing mutated

    def test_apply_partial_failure_rolls_back(self) -> None:
        before = self._dir_state()
        fake = FakeHerdrIntegration(fail_for=frozenset({AGENT_CODEX}))
        report = apply_install(
            self._inputs(agents=(AGENT_CLAUDE, AGENT_CODEX), runner=fake.run)
        )
        self.assertFalse(report.ok)
        self.assertTrue(report.applied)
        # claude was installed then reverted; codex failed.
        by_agent = {o.agent: o for o in report.outcomes}
        self.assertTrue(by_agent[AGENT_CLAUDE].rolled_back)
        self.assertEqual(by_agent[AGENT_CLAUDE].reason, REASON_PARTIAL_FAILURE)
        self.assertEqual(by_agent[AGENT_CODEX].reason, REASON_HERDR_ERROR)
        # home is byte-identical to how it was found
        self.assertEqual(before, self._dir_state())

    def test_apply_single_agent_failure_rolls_back(self) -> None:
        before = self._dir_state()
        fake = FakeHerdrIntegration(fail_for=frozenset({AGENT_CLAUDE}))
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertEqual(report.outcomes[0].reason, REASON_HERDR_ERROR)
        self.assertTrue(report.outcomes[0].rolled_back)
        self.assertEqual(before, self._dir_state())

    def test_plan_binary_unresolved_is_gated_zero_mutation(self) -> None:
        # Review j#83613 finding 2: an unresolvable herdr binary must gate the plan
        # closed (not report ok=true), and still mutate nothing.
        before = self._dir_state()
        inputs = InstallInputs(
            home=self.home,
            agents=(AGENT_CLAUDE,),
            herdr_config=self.herdr_config,
            env={"PATH": ""},  # no MOZYO_HERDR_BINARY, empty PATH → unresolvable
        )
        report = plan_install(inputs)
        self.assertFalse(report.ok)
        self.assertEqual(report.plans[0].reason, REASON_HERDR_UNRESOLVED)
        self.assertEqual(before, self._dir_state())

    def test_apply_binary_unresolved_refused(self) -> None:
        fake = FakeHerdrIntegration()
        report = apply_install(
            InstallInputs(
                home=self.home,
                agents=(AGENT_CLAUDE,),
                herdr_config=self.herdr_config,
                env={"PATH": ""},
                runner=fake.run,
            )
        )
        self.assertFalse(report.ok)
        self.assertEqual(fake.calls, [])

    def test_apply_rollback_failure_reports_incomplete(self) -> None:
        # Review j#83613 finding 1: if rollback cannot restore the dir (residue
        # remains), the outcome must be rollback_incomplete / rolled_back=False, and
        # the report must NOT claim "home left as found".
        self.addCleanup(self._restore_perms)
        fake = _ResidueLockingFake(fail_for=frozenset({AGENT_CLAUDE}))
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        outcome = report.outcomes[0]
        self.assertEqual(outcome.reason, REASON_ROLLBACK_INCOMPLETE)
        self.assertFalse(outcome.rolled_back)
        self.assertIn("INCOMPLETE", report.detail)
        # the residue herdr wrote is really still there (rollback could not remove it)
        self.assertTrue((self.home / ".claude" / "hooks" / "partial.tmp").exists())

    def test_apply_partial_failure_rollback_failure_marks_prior_incomplete(self) -> None:
        # claude installs, codex fails; if claude's rollback cannot restore, its
        # outcome is rollback_incomplete (not a false partial_failure/rolled_back).
        self.addCleanup(self._restore_perms)
        # Seed a claude file that will be changed by the fake then locked against restore.
        fake = _ResidueLockingFake(
            fail_for=frozenset({AGENT_CODEX}), lock_agent=AGENT_CLAUDE
        )
        report = apply_install(
            self._inputs(agents=(AGENT_CLAUDE, AGENT_CODEX), runner=fake.run)
        )
        self.assertFalse(report.ok)
        by_agent = {o.agent: o for o in report.outcomes}
        self.assertEqual(by_agent[AGENT_CLAUDE].reason, REASON_ROLLBACK_INCOMPLETE)
        self.assertFalse(by_agent[AGENT_CLAUDE].rolled_back)
        self.assertIn("INCOMPLETE", report.detail)

    def test_apply_refused_when_config_file_unreadable(self) -> None:
        # Review j#83674 finding 1: an unreadable non-credential file means a rollback
        # could never be byte-verified, so apply must refuse BEFORE mutating (an
        # `unreadable == unreadable` match must never read as "restored").
        self.addCleanup(self._restore_perms)
        settings = self.home / ".claude" / "settings.json"
        settings.write_text("orig", encoding="utf-8")
        settings.chmod(0o000)  # owner cannot read → snapshot/backup fail
        fake = FakeHerdrIntegration()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked → zero mutation
        self.assertEqual(report.plans[0].reason, REASON_CONFIG_DIR_UNREADABLE)
        # the file is untouched
        settings.chmod(0o600)
        self.assertEqual(settings.read_text(encoding="utf-8"), "orig")

    def test_apply_refused_when_backup_read_fails(self) -> None:
        # Review j#83737 finding 1: a file readable at snapshot time but failing the
        # SEPARATE backup read leaves the backup incomplete, so rollback is unprovable
        # and apply must refuse before mutating — even though the snapshot pass passed.
        settings = self.home / ".claude" / "settings.json"
        settings.write_text("orig", encoding="utf-8")
        orig_read = Path.read_bytes
        counts: "dict[str, int]" = {}

        def flaky_read(path_self):
            key = str(path_self)
            counts[key] = counts.get(key, 0) + 1
            # settings.json: 1st read (snapshot) succeeds, 2nd read (backup) fails.
            if key.endswith("settings.json") and counts[key] >= 2:
                raise OSError("injected backup read failure")
            return orig_read(path_self)

        fake = FakeHerdrIntegration()
        with mock.patch.object(Path, "read_bytes", flaky_read):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked → zero mutation
        self.assertEqual(report.plans[0].reason, REASON_CONFIG_DIR_UNREADABLE)
        self.assertEqual(settings.read_text(encoding="utf-8"), "orig")

    def test_apply_binds_the_verified_config_into_the_herdr_env(self) -> None:
        # Review j#91688 finding 1: the pin posture is proven against a specific file,
        # so the apply must make THAT file the one herdr reads — otherwise the gate
        # verifies one config while herdr obeys another.
        fake = FakeHerdrIntegration()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertTrue(report.ok)
        expected = os.path.realpath(self.herdr_config)
        self.assertEqual(fake.envs[0][HERDR_CONFIG_PATH_ENV], expected)
        self.assertEqual(report.herdr_config_bound, expected)

    def test_plan_gated_when_env_names_a_different_config(self) -> None:
        # Review j#91688 finding 1: an unrelated pinned file must not be usable as a
        # decoy while the environment points herdr at an unpinned config.
        decoy_unpinned = self.tmp / "decoy.toml"
        decoy_unpinned.write_text(
            "[update]\nversion_check = true\nmanifest_check = true\n", encoding="utf-8"
        )
        env = dict(self.env)
        env[HERDR_CONFIG_PATH_ENV] = str(decoy_unpinned)
        inputs = InstallInputs(
            home=self.home,
            agents=(AGENT_CLAUDE,),
            herdr_config=self.herdr_config,  # pinned, but NOT what herdr would read
            env=env,
        )
        plan = plan_install(inputs)
        self.assertFalse(plan.ok)
        self.assertEqual(plan.plans[0].reason, REASON_CONFIG_PIN_MISMATCH)
        self.assertIsNone(plan.herdr_config_bound)

    def test_apply_refused_when_env_names_a_different_config(self) -> None:
        decoy_unpinned = self.tmp / "decoy.toml"
        decoy_unpinned.write_text(
            "[update]\nversion_check = true\nmanifest_check = true\n", encoding="utf-8"
        )
        env = dict(self.env)
        env[HERDR_CONFIG_PATH_ENV] = str(decoy_unpinned)
        before = self._dir_state()
        fake = FakeHerdrIntegration()
        report = apply_install(
            InstallInputs(
                home=self.home,
                agents=(AGENT_CLAUDE,),
                herdr_config=self.herdr_config,
                env=env,
                runner=fake.run,
            )
        )
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked → zero mutation
        self.assertEqual(before, self._dir_state())

    def test_env_naming_the_same_config_by_another_path_is_not_a_conflict(self) -> None:
        # A symlink to the very same file is the same effective config; the gate must
        # compare identity (realpath), not the literal string, or it would refuse a
        # legitimate operator setup.
        link = self.tmp / "herdr-link.toml"
        os.symlink(self.herdr_config, link)
        env = dict(self.env)
        env[HERDR_CONFIG_PATH_ENV] = str(link)
        fake = FakeHerdrIntegration()
        report = apply_install(
            InstallInputs(
                home=self.home,
                agents=(AGENT_CLAUDE,),
                herdr_config=self.herdr_config,
                env=env,
                runner=fake.run,
            )
        )
        self.assertTrue(report.ok)
        self.assertEqual(
            fake.envs[0][HERDR_CONFIG_PATH_ENV], os.path.realpath(self.herdr_config)
        )

    def test_apply_refused_when_a_subdir_cannot_be_enumerated(self) -> None:
        # Review j#91688 finding 2: a subtree whose listing fails drops out of the
        # snapshot AND the backup together, so every per-file completeness check
        # agrees while a real file went unseen. The listing failure itself must gate.
        self.addCleanup(self._restore_perms)
        sub = self.home / ".claude" / "sub"
        sub.mkdir()
        (sub / "settings.json").write_text("orig", encoding="utf-8")
        sub.chmod(0o000)  # scandir(sub) fails → os.walk would drop it silently
        fake = FakeHerdrIntegration()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked → zero mutation
        self.assertEqual(report.plans[0].reason, REASON_CONFIG_DIR_UNREADABLE)
        # the file inside the un-listable subtree is untouched
        sub.chmod(0o700)
        self.assertEqual((sub / "settings.json").read_text(encoding="utf-8"), "orig")

    def test_apply_not_ok_when_post_apply_dir_cannot_be_read_back(self) -> None:
        # Review j#91688 finding 3: herdr exiting 0 is not the apply having succeeded.
        # If the post-apply dir cannot be fully read, neither the exact diff nor the
        # final home state is observable, so the outcome is rolled back and closed.
        fake = FakeHerdrIntegration()
        orig_read = Path.read_bytes

        def flaky_read(path_self):
            # the hook only exists after herdr ran → its first read is the post-snapshot
            if str(path_self).endswith("mozyo-session.sh"):
                raise OSError("injected post-snapshot read failure")
            return orig_read(path_self)

        before = self._dir_state()
        with mock.patch.object(Path, "read_bytes", flaky_read):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertEqual(len(fake.calls), 1)  # herdr really did run and report success
        outcome = report.outcomes[0]
        self.assertFalse(outcome.ok)
        self.assertEqual(outcome.reason, REASON_CONFIG_DIR_UNREADABLE)
        self.assertTrue(outcome.rolled_back)
        # …and the unobservable write was reverted: home is as it was found
        self.assertEqual(before, self._dir_state())

    # -- action-time drift (review j#91762) --

    def _drift_at_apply(self, action):
        """Run ``action`` when apply re-resolves the binary — after the plan gate ran."""
        real = install_ops._resolve_binary
        state = {"n": 0}

        def wrapper(inputs):
            state["n"] += 1
            result = real(inputs)
            if state["n"] == 2:  # 1 = inside plan_install, 2 = apply's own resolution
                action()
            return result

        patcher = mock.patch.object(install_ops, "_resolve_binary", wrapper)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_apply_refused_when_config_dir_is_removed_after_the_plan_gate(self) -> None:
        # Review j#91762 finding 1: the plan gate's "the dir is there" is a statement
        # about the past. If the dir goes away before the mutation, the apply must not
        # run herdr and report success against a dir that no longer exists.
        import shutil

        fake = FakeHerdrIntegration()
        self._drift_at_apply(lambda: shutil.rmtree(self.home / ".claude"))
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked
        self.assertEqual(report.plans[0].reason, REASON_CONFIG_DIR_MISSING)

    def test_apply_refused_when_config_dir_is_repointed_outside_home(self) -> None:
        # Review j#91762 finding 1: swapping the dir for a symlink escaping home after
        # the gate must not let herdr write outside home — the whole point of the
        # path-safety gate.
        import shutil

        outside = self.tmp / "outside"
        outside.mkdir()

        def repoint():
            shutil.rmtree(self.home / ".claude")
            os.symlink(outside, self.home / ".claude")

        fake = FakeHerdrIntegration()
        self._drift_at_apply(repoint)
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked
        self.assertEqual(report.plans[0].reason, REASON_UNSAFE_CONFIG_PATH)
        # nothing at all was written into the escaped location
        self.assertEqual(list(outside.iterdir()), [])

    def test_a_drifted_target_is_never_even_read(self) -> None:
        # The preflight validates BEFORE reading, so a dir that has been re-pointed
        # outside home is not snapshotted or backed up at all. Catching it after the
        # reads would still refuse, but it would mean having read a location outside
        # home first — which is itself the thing the path gate exists to prevent.
        import shutil

        outside = self.tmp / "outside"
        outside.mkdir()
        (outside / "not-ours.txt").write_text("stranger", encoding="utf-8")
        reads: "list[str]" = []
        real_read = install_ops.read_dir

        def spy_read(root):
            reads.append(str(root))
            return real_read(root)

        def repoint():
            shutil.rmtree(self.home / ".claude")
            os.symlink(outside, self.home / ".claude")

        fake = FakeHerdrIntegration()
        self._drift_at_apply(repoint)
        with mock.patch.object(install_ops, "read_dir", spy_read):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertEqual(fake.calls, [])
        self.assertEqual(reads, [])  # the escaped location was never read

    def test_apply_refused_when_dir_drifts_between_the_read_and_the_invocation(self) -> None:
        # The window the per-invocation check owns: the reads completed against the
        # staged object, and the swap happens after that bracket closes but before
        # herdr is handed the dir. Only the pre-invocation check stands there.
        import shutil

        outside = self.tmp / "outside"
        outside.mkdir()
        real_drift = install_ops.config_dir_drift
        state = {"drifted": False}

        def drift_after_read_bracket(config_dir, home, *, expected=None):
            result = real_drift(config_dir, home, expected=expected)
            if not state["drifted"]:
                # this call IS the post-read bracket check; drift right after it
                state["drifted"] = True
                shutil.rmtree(self.home / ".claude")
                os.symlink(outside, self.home / ".claude")
            return result

        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "config_dir_drift", drift_after_read_bracket):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr was never handed the drifted dir
        self.assertEqual(report.outcomes[0].reason, REASON_UNSAFE_CONFIG_PATH)
        self.assertEqual(list(outside.iterdir()), [])

    def test_apply_refused_when_dir_is_repointed_after_preflight(self) -> None:
        # Review j#91762 finding 1 requires the dir to be re-validated before EACH
        # runner call, not only once before the preflight. Drift injected after the
        # backup is past the preflight check, so only the per-invocation check can
        # catch it.
        import shutil

        outside = self.tmp / "outside"
        outside.mkdir()
        real_backup = install_ops.backup_dir

        def repoint_after_backup(root):
            result = real_backup(root)
            shutil.rmtree(self.home / ".claude")
            os.symlink(outside, self.home / ".claude")
            return result

        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "backup_dir", repoint_after_backup):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked
        self.assertEqual(report.plans[0].reason, REASON_UNSAFE_CONFIG_PATH)
        self.assertEqual(list(outside.iterdir()), [])  # nothing written outside home

    def test_dir_repointed_while_herdr_runs_is_not_reported_ok(self) -> None:
        # The dir can also drift *during* the invocation, so it is re-validated after
        # the run too: the result is neither ok nor restorable (restoring would write
        # into the escaped location), so it must report rollback_incomplete.
        import shutil

        outside = self.tmp / "outside"
        outside.mkdir()

        class RepointingFake(FakeHerdrIntegration):
            def run(inner, argv, **kw):  # noqa: N805 - mirrors the runner signature
                result = FakeHerdrIntegration.run(inner, argv, **kw)
                shutil.rmtree(self.home / ".claude")
                os.symlink(outside, self.home / ".claude")
                return result

        fake = RepointingFake()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertEqual(len(fake.calls), 1)  # herdr did run
        self.assertEqual(report.outcomes[0].reason, REASON_ROLLBACK_INCOMPLETE)
        self.assertFalse(report.outcomes[0].rolled_back)
        self.assertEqual(list(outside.iterdir()), [])  # no restore write escaped home

    def _replace_claude_dir_same_path(self, *, seed=None) -> Path:
        """Move .claude aside and put a DIFFERENT directory at the same path."""
        import shutil

        stashed = self.tmp / "stashed-claude"
        shutil.move(str(self.home / ".claude"), str(stashed))
        (self.home / ".claude").mkdir()
        if seed:
            (self.home / ".claude" / seed).write_text("operator data", encoding="utf-8")
        return stashed

    def test_apply_refused_when_the_dir_is_replaced_by_a_different_inode(self) -> None:
        # Review j#91805 finding 1: a replacement directory at the SAME path passes
        # every path-shaped check while being an object this transaction never read.
        (self.home / ".claude" / "settings.json").write_text("orig", encoding="utf-8")
        stash = {}
        real_backup = install_ops.backup_dir

        def replace_after_backup(root):
            result = real_backup(root)
            stash["dir"] = self._replace_claude_dir_same_path()
            return result

        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "backup_dir", replace_after_backup):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never invoked
        self.assertEqual(report.plans[0].reason, REASON_UNSAFE_CONFIG_PATH)
        # the replacement was not written into, and the staged dir was left alone
        self.assertEqual(list((self.home / ".claude").iterdir()), [])
        self.assertTrue((stash["dir"] / "settings.json").exists())

    def test_rollback_never_writes_into_a_replacement_dir(self) -> None:
        # Review j#91805 finding 1B: when the swap happens during the run, the rollback
        # must not "restore" the staged backup into the replacement — that would delete
        # a stranger's contents and write another dir's bytes over them.
        (self.home / ".claude" / "settings.json").write_text("orig", encoding="utf-8")

        class ReplacingFake(FakeHerdrIntegration):
            def run(inner, argv, **kw):  # noqa: N805 - mirrors the runner signature
                result = FakeHerdrIntegration.run(inner, argv, **kw)
                self._replace_claude_dir_same_path(seed="replacement-data.txt")
                return subprocess.CompletedProcess(list(argv), 1, stdout="", stderr="x")

        fake = ReplacingFake()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        outcome = report.outcomes[0]
        self.assertEqual(outcome.reason, REASON_ROLLBACK_INCOMPLETE)
        self.assertFalse(outcome.rolled_back)
        replacement = self.home / ".claude"
        # the stranger's file survives, and no staged byte was written into it
        self.assertEqual(
            (replacement / "replacement-data.txt").read_text(encoding="utf-8"),
            "operator data",
        )
        self.assertFalse((replacement / "settings.json").exists())

    def test_transaction_start_pin_drift_reports_a_closed_reason(self) -> None:
        # Review j#91805 finding 2B: a refusal must arrive as a closed reason, not as
        # prose next to plans that still say ready.
        real_plan = install_ops.plan_install

        def unpin_after_plan(inputs):
            report = real_plan(inputs)
            self.herdr_config.write_text(
                "[update]\nversion_check = true\nmanifest_check = true\n",
                encoding="utf-8",
            )
            return report

        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "plan_install", unpin_after_plan):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertEqual(fake.calls, [])
        self.assertFalse(report.plans[0].ready)
        self.assertEqual(report.plans[0].reason, REASON_CONFIG_PIN_MISMATCH)

    def test_binary_drift_after_the_plan_reports_a_closed_reason(self) -> None:
        # Same contract as the pin-drift refusal: the plan proved the binary resolved,
        # so losing it afterwards is drift and must arrive as `herdr_unresolved` in the
        # structured payload rather than only in prose.
        real_resolve = install_ops._resolve_binary
        state = {"n": 0}

        def vanish_after_plan(inputs):
            state["n"] += 1
            if state["n"] >= 2:
                return None, "herdr binary vanished"
            return real_resolve(inputs)

        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "_resolve_binary", vanish_after_plan):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])
        self.assertFalse(report.plans[0].ready)
        self.assertEqual(report.plans[0].reason, REASON_HERDR_UNRESOLVED)

    def test_zero_mutation_refusal_does_not_claim_a_rollback(self) -> None:
        # Review j#91805 finding 2A: `rolled_back` says this agent's mutation was
        # reverted. A refusal that never wrote anything has no mutation to revert.
        real_backup = install_ops.backup_dir

        def unpin_after_backup(root):
            result = real_backup(root)
            self.herdr_config.write_text(
                "[update]\nversion_check = true\nmanifest_check = true\n",
                encoding="utf-8",
            )
            return result

        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "backup_dir", unpin_after_backup):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])
        self.assertFalse(report.outcomes[0].rolled_back)
        self.assertIn("nothing was mutated", report.detail)
        self.assertNotIn("rolled back", report.detail)

    def test_KNOWN_LIMIT_transient_config_swap_is_not_detected(self) -> None:
        # This pins a DISCLOSED LIMITATION, not desired behaviour (Redmine #13249
        # review j#91805 finding 3). The pin is re-asserted before and after each
        # invocation, which catches drift that persists across either check. A config
        # swapped to unpinned and restored *within* the invocation is invisible to both
        # checks, so herdr can read unpinned bytes and the hook stays installed. Closing
        # this would require herdr to read a config this process holds open, which the
        # runbook assigns to operator write-authority instead. If this test ever starts
        # failing because the apply now refuses, that is an improvement — update the
        # runbook's residual-window section and this test together.
        unpinned = "[update]\nversion_check = true\nmanifest_check = true\n"
        pinned_bytes = self.herdr_config.read_bytes()
        seen: "list[str]" = []

        class TransientSwapFake(FakeHerdrIntegration):
            def run(inner, argv, env=None, **kw):  # noqa: N805
                cfg = Path(env[HERDR_CONFIG_PATH_ENV])
                cfg.write_text(unpinned, encoding="utf-8")
                seen.append(cfg.read_text(encoding="utf-8"))  # what herdr would read
                cfg.write_bytes(pinned_bytes)  # restored before herdr returns
                return FakeHerdrIntegration.run(inner, argv, env=env, **kw)

        fake = TransientSwapFake()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertIn("version_check = true", seen[0])  # unpinned bytes were readable
        self.assertTrue(report.ok)  # …and the apply does NOT detect it
        self.assertTrue((self.home / ".claude" / _HOOK_REL).exists())  # hook remains

    def test_apply_refused_when_verified_config_changes_before_the_runner(self) -> None:
        # Review j#91762 finding 2: pinning the config PATH is not pinning the pin —
        # the bytes at that path can be swapped for an unpinned config after the
        # posture verified, and herdr would read those.
        real_backup = install_ops.backup_dir

        def swap_after_backup(root):
            result = real_backup(root)
            self.herdr_config.write_text(
                "[update]\nversion_check = true\nmanifest_check = true\n",
                encoding="utf-8",
            )
            return result

        before = self._dir_state()
        fake = FakeHerdrIntegration()
        with mock.patch.object(install_ops, "backup_dir", swap_after_backup):
            report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertFalse(report.applied)
        self.assertEqual(fake.calls, [])  # herdr never ran against the swapped config
        self.assertEqual(report.outcomes[0].reason, REASON_CONFIG_PIN_MISMATCH)
        self.assertEqual(before, self._dir_state())

    def test_config_swapped_while_herdr_runs_is_caught_and_rolled_back(self) -> None:
        # The check before the invocation cannot be atomic with herdr's own read, so
        # the pin is re-asserted AFTER the run too: a swap inside that window is
        # detected and reverted instead of leaving the hook installed.
        class SwappingFake(FakeHerdrIntegration):
            def run(inner, argv, **kw):  # noqa: N805 - mirrors the runner signature
                result = FakeHerdrIntegration.run(inner, argv, **kw)
                self.herdr_config.write_text(
                    "[update]\nversion_check = true\nmanifest_check = true\n",
                    encoding="utf-8",
                )
                return result

        before = self._dir_state()
        fake = SwappingFake()
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=fake.run))
        self.assertFalse(report.ok)
        self.assertEqual(len(fake.calls), 1)  # herdr did run
        outcome = report.outcomes[0]
        self.assertEqual(outcome.reason, REASON_CONFIG_PIN_MISMATCH)
        self.assertTrue(outcome.rolled_back)
        self.assertEqual(before, self._dir_state())  # hook reverted

    def test_reading_a_missing_root_is_incomplete_not_an_empty_dir(self) -> None:
        # Review j#91762 finding 1: "could not look" must not read as "nothing there".
        # This is the root-level twin of the subtree case from j#91688 finding 2.
        missing = self.home / ".claude" / "gone"
        read = dir_io.read_dir(missing)
        self.assertFalse(read.complete)
        self.assertEqual(dir_io.backup_dir(missing).complete, False)
        # …while a genuinely empty, present dir IS complete (the distinction is real)
        empty = self.home / ".claude" / "empty"
        empty.mkdir()
        self.assertTrue(dir_io.read_dir(empty).complete)

    def test_rollback_refuses_to_write_into_a_drifted_root(self) -> None:
        # The write guard lives with the write: restoring a backup into a root that now
        # resolves outside home would push operator bytes out of home.
        import shutil

        outside = self.tmp / "outside"
        outside.mkdir()
        config_dir = self.home / ".claude"
        (config_dir / "settings.json").write_text("orig", encoding="utf-8")
        before_read = dir_io.read_dir(config_dir)
        backup = dir_io.backup_dir(config_dir)
        staged_identity, _ = dir_io.observe_config_dir(config_dir, self.home)
        shutil.rmtree(config_dir)
        os.symlink(outside, config_dir)
        restored = dir_io.rollback_dir(
            config_dir,
            backup.files,
            before_read.snapshot,
            home=self.home,
            expected=staged_identity,
        )
        self.assertFalse(restored)
        self.assertEqual(list(outside.iterdir()), [])  # nothing written outside home

    def _restore_perms(self) -> None:
        import stat as _stat

        for dirpath, dirs, files in os.walk(self.home):
            for name in dirs + files:
                p = Path(dirpath) / name
                try:
                    p.chmod(p.stat().st_mode | _stat.S_IRWXU)
                except OSError:
                    pass

    def test_apply_never_touches_credentials(self) -> None:
        # Seed a credential-shaped file; it must survive apply AND rollback untouched.
        cred = self.home / ".claude" / ".credentials.json"
        cred.write_text("SECRET", encoding="utf-8")
        fake = FakeHerdrIntegration(fail_for=frozenset({AGENT_CODEX}))
        apply_install(self._inputs(runner=fake.run))
        self.assertEqual(cred.read_text(encoding="utf-8"), "SECRET")
        # and it never appeared in any diff
        report = apply_install(self._inputs(agents=(AGENT_CLAUDE,), runner=FakeHerdrIntegration().run))
        for o in report.outcomes:
            if o.diff is not None:
                self.assertNotIn(".credentials.json", o.diff.added + o.diff.changed)


from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.cli_herdr_distribution import (  # noqa: E402
    cmd_herdr_integration_install,
    cmd_herdr_pin_posture,
)


class _Args:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class CliExitCodeTest(unittest.TestCase):
    """The command boundaries return the right exit code (0 ok / 1 fail-closed)."""

    def setUp(self) -> None:
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_pin_posture_render_ok(self) -> None:
        rc = cmd_herdr_pin_posture(
            _Args(mode=PIN_MODE_OFFLINE, manifest_catalog_url=None, verify=None, json=False)
        )
        self.assertEqual(rc, 0)

    def test_pin_posture_render_bad_combo(self) -> None:
        # pinned_mirror with no URL fails closed at exit-code level.
        rc = cmd_herdr_pin_posture(
            _Args(
                mode=PIN_MODE_PINNED_MIRROR,
                manifest_catalog_url=None,
                verify=None,
                json=False,
            )
        )
        self.assertEqual(rc, 1)

    def test_pin_posture_verify_unpinned_exit_1(self) -> None:
        path = self.tmp / "herdr.toml"
        path.write_text("[update]\nversion_check = true\n", encoding="utf-8")
        rc = cmd_herdr_pin_posture(
            _Args(mode=PIN_MODE_OFFLINE, manifest_catalog_url=None, verify=str(path), json=False)
        )
        self.assertEqual(rc, 1)

    def test_integration_install_unknown_agent_exit_1(self) -> None:
        rc = cmd_herdr_integration_install(
            _Args(
                agent=["gemini"],
                home=str(self.tmp),
                herdr_config=None,
                manifest_catalog_url=None,
                apply=False,
                json=True,
            )
        )
        self.assertEqual(rc, 1)

    def test_integration_install_plan_gated_exit_1(self) -> None:
        # No herdr config → unpinned gate → plan blocked → exit 1, no mutation.
        (self.tmp / ".claude").mkdir()
        rc = cmd_herdr_integration_install(
            _Args(
                agent=["claude"],
                home=str(self.tmp),
                herdr_config=None,
                manifest_catalog_url=None,
                apply=False,
                json=False,
            )
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
