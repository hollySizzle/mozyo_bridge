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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_integration_install_ops import (  # noqa: E402
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

    def run(self, argv, capture_output=None, text=None, timeout=None, env=None, **_):
        self.calls.append(list(argv))
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
