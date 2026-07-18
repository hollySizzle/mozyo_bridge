"""Agent provider profile registry — Increment 1 acceptance (Redmine #13441 j#76725).

Pins the four things the Design Answer asks Increment 1 to prove:

1. **Built-in parity.** The derived vocabularies are byte-equal to the hard-coded sets
   they replaced, so the refactor changes no built-in behavior.
2. **Data absorbs a new provider.** A synthetic same-protocol provider reaches the
   launch vocabulary, the config vocabulary, discovery identity, AND a rendered launch
   argv by adding *profile data only* — no source branch anywhere.
3. **argv[0]-excepted compatibility.** Built-in launches are byte-invariant in every
   token except argv[0], which becomes the injected resolver's absolute realpath. Tests
   pin the *injected* absolute path, never a host path literal.
4. **Fail-closed before side effects.** Unknown provider / unsupported protocol /
   missing capability / unresolvable / ambiguous / hostile-config executable all raise
   before any pane or process exists.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "src"))
_TESTS_ROOT = Path(__file__).resolve().parents[3]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    AgentProviderExecutableError,
    ResolvedAgentExecutable,
    preflight_launch_providers,
    require_launchable,
    resolve_agent_argv0,
    resolve_agent_executable,
    resolve_agent_launch,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain import (
    agent_provider_profile as profile_module,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
    AGENT_PROVIDER_PROFILES,
    agent_commands,
    agent_discovery_aliases,
    agent_process_names,
    agent_provider_ids,
    load_agent_provider_config,
    reserved_managed_flags,
)
from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
    AgentCapability,
    AgentProviderProfile,
    AgentProviderProfileConfig,
    AgentProviderProfileError,
    InteractionProtocol,
    ManagedFlagConcept,
    TrustedExecutable,
)


def _synthetic_record(provider_id="mistral-cli", **over):
    """A same-protocol provider declared purely as data."""
    record = {
        "protocol": "interactive_cli_tui",
        "executable": {
            "command": provider_id,
            "env_override": "MOZYO_AGENT_MISTRAL_BINARY",
        },
        "discovery_aliases": [provider_id],
        "process_names": [provider_id],
        "capabilities": [
            "interactive_tui",
            "launch_argv_override",
            "managed_permission_mode",
        ],
        "managed_flags": {"permission_mode": "--approval"},
    }
    record.update(over)
    return record


def _config_record(profiles):
    return {
        "version": "1",
        "source": "test",
        "profiles": profiles,
    }


def _install_binary(directory: Path, name: str) -> str:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / name
    target.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    target.chmod(0o755)
    return os.path.realpath(str(target))


class BuiltinParityTest(unittest.TestCase):
    """The packaged profiles reproduce the pre-#13441 hard-coded sets exactly."""

    def test_provider_vocabulary_is_claude_and_codex(self) -> None:
        self.assertEqual({"claude", "codex"}, set(agent_provider_ids()))

    def test_agent_commands_match_the_historical_table(self) -> None:
        # Was: pane_resolver.AGENT_COMMANDS = {"claude": "claude", "codex": "codex"}
        self.assertEqual({"claude": "claude", "codex": "codex"}, agent_commands())

    def test_reserved_managed_flags_match_the_historical_table(self) -> None:
        # Was: RESERVED_MANAGED_FLAGS = {"claude": ("--permission-mode",)}
        self.assertEqual({"claude": ("--permission-mode",)}, reserved_managed_flags())

    def test_codex_reserves_no_managed_flag(self) -> None:
        self.assertNotIn("codex", reserved_managed_flags())
        self.assertIsNone(
            AGENT_PROVIDER_PROFILES.require("codex").managed_flag(
                ManagedFlagConcept.PERMISSION_MODE
            )
        )

    def test_discovery_vocabulary_matches_the_historical_kinds(self) -> None:
        self.assertEqual({"claude": "claude", "codex": "codex"}, agent_discovery_aliases())
        self.assertEqual({"claude", "codex"}, set(agent_process_names()))

    def test_codex_declares_the_tool_shell_override_capability(self) -> None:
        # The `-c` shell_environment_policy overrides (#13614) are now driven by this
        # capability rather than an `if provider == "codex"` branch.
        self.assertTrue(
            AGENT_PROVIDER_PROFILES.require("codex").has_capability(
                AgentCapability.TOOL_SHELL_ENV_OVERRIDES
            )
        )
        self.assertFalse(
            AGENT_PROVIDER_PROFILES.require("claude").has_capability(
                AgentCapability.TOOL_SHELL_ENV_OVERRIDES
            )
        )

    def test_packaged_artifact_carries_no_host_path(self) -> None:
        # A committed profile must never bake in a machine's layout (#13245): the
        # executable is a bare basename plus an env-override NAME.
        config = load_agent_provider_config()
        for profile in config.profiles:
            self.assertFalse(os.path.isabs(profile.executable.command))
            self.assertNotIn("/", profile.executable.command)


class SyntheticProviderTest(unittest.TestCase):
    """A new same-protocol provider is absorbed by DATA — no source branch."""

    def setUp(self) -> None:
        self.registry = AgentProviderProfileConfig.from_record(
            _config_record(
                {
                    "claude": {
                        "protocol": "interactive_cli_tui",
                        "executable": {
                            "command": "claude",
                            "env_override": "MOZYO_AGENT_CLAUDE_BINARY",
                        },
                        "discovery_aliases": ["claude"],
                        "process_names": ["claude"],
                        "capabilities": [
                            "interactive_tui",
                            "launch_argv_override",
                            "managed_permission_mode",
                        ],
                        "managed_flags": {"permission_mode": "--permission-mode"},
                    },
                    "mistral-cli": _synthetic_record(),
                }
            )
        ).to_registry()

    def test_synthetic_provider_joins_the_launch_vocabulary(self) -> None:
        self.assertIn("mistral-cli", self.registry.provider_ids())
        self.assertEqual("mistral-cli", self.registry.commands()["mistral-cli"])

    def test_synthetic_provider_joins_discovery_identity(self) -> None:
        self.assertEqual("mistral-cli", self.registry.discovery_aliases()["mistral-cli"])
        self.assertIn("mistral-cli", self.registry.process_names())

    def test_synthetic_provider_reserves_its_own_managed_flag_spelling(self) -> None:
        # It spells the permission concept `--approval`, not `--permission-mode`. The
        # reservation follows the DATA, so the config guard cannot drift behind it.
        self.assertEqual(
            ("--approval",), self.registry.reserved_managed_flags()["mistral-cli"]
        )
        self.assertEqual(
            "--approval",
            self.registry.require("mistral-cli").managed_flag(
                ManagedFlagConcept.PERMISSION_MODE
            ),
        )

    def test_synthetic_provider_executable_resolves_with_no_source_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = _install_binary(Path(tmp) / "bin", "mistral-cli")
            resolved = resolve_agent_argv0(
                "mistral-cli",
                {"PATH": str(Path(tmp) / "bin")},
                registry=self.registry,
            )
        self.assertEqual(expected, resolved)

    def test_profile_addition_alone_does_not_change_default_topology(self) -> None:
        # Registering a provider makes it EXPRESSIBLE, never LAUNCHED. The default pair
        # is a separate contract and must not grow just because a profile was added.
        from mozyo_bridge.application.herdr_launch_command import LAUNCH_PROVIDERS

        self.assertEqual(("claude", "codex"), tuple(LAUNCH_PROVIDERS))
        self.assertNotIn("mistral-cli", LAUNCH_PROVIDERS)


class SyntheticProviderLaunchArgvTest(unittest.TestCase):
    """End-to-end: a data-only provider renders a full launch argv, no source branch.

    Crucially, this passes ONLY the injected registry — no global monkeypatch — so it
    exercises the real seam an added provider travels through. The pre-R2-F1 version
    patched BOTH `profile_module.AGENT_PROVIDER_PROFILES` and the executable module's
    global, which hid a registry split: the preflight resolved the executable from the
    injected registry but the managed policy / capability re-read the global, so a
    provider present only in an injected registry got an empty managed argv and then made
    the "pure" builder raise `unknown agent provider` (review R2-F1).
    """

    def _launch(self, record, *, permission_mode_default="auto"):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
            herdr_launch_argv,
        )

        registry = AgentProviderProfileConfig.from_record(
            _config_record({record["_id"]: {k: v for k, v in record.items() if k != "_id"}})
        ).to_registry()
        provider_id = record["_id"]
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            expected_argv0 = _install_binary(bindir, record["executable"]["command"])
            # NO global patch — only the injected registry is threaded, exactly as an
            # added provider would flow through a single registry snapshot.
            resolved = preflight_launch_providers(
                [provider_id],
                {"PATH": str(bindir)},
                permission_mode_default=permission_mode_default,
                registry=registry,
            )[provider_id]
            argv = herdr_launch_argv.build_agent_start_argv(
                assigned_name=f"mzb1_ws_{provider_id}_lane",
                provider=provider_id,
                repo_root=Path(tmp),
                workspace_id="ws",
                lane="lane-1",
                target_workspace="wsname",
                target_tab="",
                split="",
                focus=False,
                binary="/usr/bin/herdr",
                attest_launcher="",
                store_home=str(Path(tmp) / "home"),
                resolved=resolved,
                launch_argv_extra=[],
            )
        return resolved, expected_argv0, argv[argv.index("--") + 1 :]

    def test_injected_only_registry_renders_managed_flag_and_builder_succeeds(self) -> None:
        record = dict(_synthetic_record(), _id="mistral-cli")
        resolved, expected_argv0, run_cmd = self._launch(record)
        # The managed argv is resolved from the SAME injected registry (not empty), and
        # the builder does not raise `unknown agent provider`.
        self.assertEqual(("--approval", "auto"), resolved.managed_argv)
        self.assertEqual([expected_argv0, "--approval", "auto"], run_cmd)

    def test_injected_only_tool_shell_capability_is_pinned_and_rendered(self) -> None:
        # A provider that declares tool_shell_env_overrides ONLY in the injected registry
        # must have that capability pinned on `resolved` (not re-read from the global),
        # so the builder renders the `-c` overrides without a global lookup.
        record = _synthetic_record(
            capabilities=[
                "interactive_tui",
                "managed_permission_mode",
                "tool_shell_env_overrides",
            ]
        )
        record["_id"] = "mistral-cli"
        resolved, expected_argv0, run_cmd = self._launch(record)
        self.assertTrue(resolved.tool_shell_env_overrides)
        self.assertEqual(expected_argv0, run_cmd[0])
        self.assertEqual(["--approval", "auto"], run_cmd[1:3])
        self.assertTrue(
            any("shell_environment_policy" in token for token in run_cmd),
            run_cmd,
        )

    def test_injected_provider_without_tool_shell_renders_no_c_overrides(self) -> None:
        record = _synthetic_record(
            capabilities=["interactive_tui", "managed_permission_mode"]
        )
        record["_id"] = "mistral-cli"
        resolved, _, run_cmd = self._launch(record)
        self.assertFalse(resolved.tool_shell_env_overrides)
        self.assertNotIn("-c", run_cmd)


class ArgvZeroCompatibilityTest(unittest.TestCase):
    """Built-in launches: argv[0] absolute; every other token byte-invariant (Q1)."""

    def _argv(self, provider, bindir, *, permission_mode_default=None, **over):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
            build_agent_start_argv,
        )

        resolved = preflight_launch_providers(
            [provider],
            {"PATH": str(bindir)},
            permission_mode_default=permission_mode_default,
        )[provider]
        kwargs = dict(
            assigned_name="mzb1_ws_x_lane",
            provider=provider,
            repo_root=Path("/repo"),
            workspace_id="ws",
            lane="lane-1",
            target_workspace="wsname",
            target_tab="",
            split="",
            focus=False,
            binary="/usr/bin/herdr",
            attest_launcher="",
            store_home="/home",
            resolved=resolved,
            launch_argv_extra=[],
        )
        kwargs.update(over)
        return build_agent_start_argv(**kwargs)

    def test_claude_argv0_is_the_injected_absolute_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            expected = _install_binary(bindir, "claude")
            _install_binary(bindir, "codex")
            argv = self._argv("claude", bindir, permission_mode_default="auto")
        run_cmd = argv[argv.index("--") + 1 :]
        # argv[0] is the resolved absolute realpath — never the bare name, and never a
        # host path literal in the assertion: it is the path this test injected.
        self.assertEqual(expected, run_cmd[0])
        self.assertTrue(os.path.isabs(run_cmd[0]))
        # ...and the REMAINING tokens are byte-invariant.
        self.assertEqual(["--permission-mode", "auto"], run_cmd[1:])

    def test_codex_suffix_is_byte_invariant_after_argv0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            _install_binary(bindir, "claude")
            expected = _install_binary(bindir, "codex")
            argv = self._argv("codex", bindir)
        run_cmd = argv[argv.index("--") + 1 :]
        self.assertEqual(expected, run_cmd[0])
        # The Codex tool-shell identity overrides (#13614) still render, unchanged.
        self.assertEqual(
            [
                "-c",
                'shell_environment_policy.set.MOZYO_WORKSPACE_ID="ws"',
                "-c",
                'shell_environment_policy.set.MOZYO_AGENT_ROLE="codex"',
                "-c",
                'shell_environment_policy.set.MOZYO_LANE_ID="lane-1"',
            ],
            run_cmd[1:],
        )

    def test_codex_never_receives_the_claude_permission_flag(self) -> None:
        # Codex declares no permission-mode concept, so even a resolved mode renders
        # nothing — the old `if provider == PROVIDER_CLAUDE` guard, now data-driven.
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            _install_binary(bindir, "codex")
            argv = self._argv("codex", bindir, permission_mode_default="auto")
        self.assertNotIn("--permission-mode", argv)


class ExecutableTrustBoundaryTest(unittest.TestCase):
    """Resolution is fail-closed BEFORE any pane / process side effect."""

    def test_unknown_provider_fails_closed(self) -> None:
        with self.assertRaises(AgentProviderProfileError) as ctx:
            resolve_agent_executable("gpt-cli", {"PATH": "/usr/bin"})
        self.assertIn("unknown agent provider", str(ctx.exception))

    def test_missing_binary_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(AgentProviderExecutableError):
                resolve_agent_executable("claude", {"PATH": tmp})

    def test_empty_path_never_falls_back_to_ambient_path(self) -> None:
        # A bare command must NOT resolve against the real process PATH — otherwise the
        # trust boundary is decorative.
        with self.assertRaises(AgentProviderExecutableError):
            resolve_agent_executable("claude", {})

    def test_relative_path_component_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            _install_binary(Path(tmp) / "bin", "claude")
            with self.assertRaises(AgentProviderExecutableError) as ctx:
                resolve_agent_executable(
                    "claude", {"PATH": f"{Path(tmp) / 'bin'}{os.pathsep}relative/dir"}
                )
        self.assertIn("unsafe", str(ctx.exception))

    def test_empty_path_component_is_refused(self) -> None:
        # An empty component is how a shell says "the current directory".
        with tempfile.TemporaryDirectory() as tmp:
            _install_binary(Path(tmp) / "bin", "claude")
            with self.assertRaises(AgentProviderExecutableError):
                resolve_agent_executable(
                    "claude", {"PATH": f"{Path(tmp) / 'bin'}{os.pathsep}"}
                )

    def test_ambiguous_binary_is_refused_not_first_match(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            a = Path(tmp) / "a"
            b = Path(tmp) / "b"
            _install_binary(a, "claude")
            _install_binary(b, "claude")
            with self.assertRaises(AgentProviderExecutableError) as ctx:
                resolve_agent_executable(
                    "claude", {"PATH": f"{a}{os.pathsep}{b}"}
                )
        self.assertIn("ambiguously", str(ctx.exception))

    def test_non_executable_file_is_not_resolved(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            (bindir / "claude").write_text("not executable", encoding="utf-8")
            (bindir / "claude").chmod(0o644)
            with self.assertRaises(AgentProviderExecutableError):
                resolve_agent_executable("claude", {"PATH": str(bindir)})

    def test_trusted_env_override_wins_and_is_verified(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            pinned = _install_binary(Path(tmp) / "pinned", "claude-real")
            _install_binary(Path(tmp) / "bin", "claude")
            resolved = resolve_agent_executable(
                "claude",
                {
                    "PATH": str(Path(tmp) / "bin"),
                    "MOZYO_AGENT_CLAUDE_BINARY": pinned,
                },
            )
        self.assertEqual(pinned, resolved)

    def test_relative_env_override_is_refused(self) -> None:
        with self.assertRaises(AgentProviderExecutableError) as ctx:
            resolve_agent_executable(
                "claude",
                {"PATH": "/usr/bin", "MOZYO_AGENT_CLAUDE_BINARY": "./claude"},
            )
        self.assertIn("absolute", str(ctx.exception))

    def test_non_executable_env_override_is_refused(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bogus = Path(tmp) / "claude"
            bogus.write_text("x", encoding="utf-8")
            bogus.chmod(0o644)
            with self.assertRaises(AgentProviderExecutableError):
                resolve_agent_executable(
                    "claude",
                    {"PATH": "/usr/bin", "MOZYO_AGENT_CLAUDE_BINARY": str(bogus)},
                )

    def test_symlink_resolves_to_its_realpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            real = _install_binary(Path(tmp) / "real", "claude")
            linkdir = Path(tmp) / "bin"
            linkdir.mkdir()
            os.symlink(real, linkdir / "claude")
            resolved = resolve_agent_executable("claude", {"PATH": str(linkdir)})
        self.assertEqual(real, resolved)

    def test_unsupported_protocol_is_rejected_before_resolution(self) -> None:
        # A profile whose protocol the mechanism cannot drive must never reach a pane —
        # the honest limit of the data-driven design (#13441 description).
        profile = AgentProviderProfile(
            provider_id="weird",
            protocol=InteractionProtocol.INTERACTIVE_CLI_TUI,
            executable=TrustedExecutable(command="weird", env_override="X"),
            capabilities=frozenset({AgentCapability.LAUNCH_ARGV_OVERRIDE}),
        )
        # Declares no INTERACTIVE_TUI capability -> not launchable.
        with self.assertRaises(AgentProviderExecutableError) as ctx:
            require_launchable(profile)
        self.assertIn("interactive_tui", str(ctx.exception))


class ResolveAgentLaunchArgv0DecouplingTest(unittest.TestCase):
    """Redmine #14017: exec target (realpath) and argv[0] (trusted alias) are distinct.

    ``resolve_agent_executable`` keeps returning the exec-safe realpath;
    ``resolve_agent_launch`` additionally exposes the trusted absolute alias as argv[0]
    data. The alias is never executed — the realpath is the exec target on every path.
    """

    def _symlink_alias(self, base: Path, name: str) -> tuple[str, str]:
        real = _install_binary(base / "real", f"{name}-real")
        bindir = base / "bin"
        bindir.mkdir(parents=True, exist_ok=True)
        alias = bindir / name
        os.symlink(real, alias)
        return str(alias), real

    def test_path_symlink_alias_is_argv0_realpath_is_exec_target(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            alias, real = self._symlink_alias(base, "claude")
            env = {"PATH": str(base / "bin"), "MOZYO_AGENT_CLAUDE_BINARY": ""}
            launch = resolve_agent_launch("claude", env)
            # The compat accessor still returns the exec-safe realpath (unwrapped sites).
            compat = resolve_agent_executable("claude", env)
        self.assertIsInstance(launch, ResolvedAgentExecutable)
        self.assertEqual(launch.exec_target, real)
        self.assertEqual(launch.argv0, alias)
        self.assertEqual(compat, real)

    def test_plain_path_entry_has_equal_exec_target_and_argv0(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            real = _install_binary(base / "bin", "codex")
            launch = resolve_agent_launch(
                "codex", {"PATH": str(base / "bin"), "MOZYO_AGENT_CODEX_BINARY": ""}
            )
        self.assertEqual(launch.exec_target, real)
        self.assertEqual(launch.argv0, real)

    def test_absolute_override_symlink_splits_alias_from_realpath(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            alias, real = self._symlink_alias(base, "claude")
            launch = resolve_agent_launch(
                "claude", {"PATH": "/nonexistent", "MOZYO_AGENT_CLAUDE_BINARY": alias}
            )
        self.assertEqual(launch.exec_target, real)
        self.assertEqual(launch.argv0, alias)

    def test_ambiguity_check_is_on_distinct_realpaths_not_aliases(self) -> None:
        # Two PATH aliases that resolve to the SAME realpath are NOT ambiguous — one
        # exec target — and the first alias is kept as argv[0].
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            real = _install_binary(base / "real", "claude-real")
            d1 = base / "d1"
            d2 = base / "d2"
            d1.mkdir()
            d2.mkdir()
            os.symlink(real, d1 / "claude")
            os.symlink(real, d2 / "claude")
            launch = resolve_agent_launch(
                "claude",
                {
                    "PATH": f"{d1}{os.pathsep}{d2}",
                    "MOZYO_AGENT_CLAUDE_BINARY": "",
                },
            )
        self.assertEqual(launch.exec_target, real)
        self.assertEqual(launch.argv0, str(d1 / "claude"))

    def test_two_distinct_realpaths_still_ambiguous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(os.path.realpath(tmp))
            _install_binary(base / "a", "claude")
            _install_binary(base / "b", "claude")
            with self.assertRaises(AgentProviderExecutableError) as ctx:
                resolve_agent_launch(
                    "claude",
                    {"PATH": f"{base / 'a'}{os.pathsep}{base / 'b'}", "MOZYO_AGENT_CLAUDE_BINARY": ""},
                )
        self.assertIn("ambiguously", str(ctx.exception))


class HostileConfigTest(unittest.TestCase):
    """A repo-committed profile can never name the binary that runs (#13245)."""

    def test_absolute_command_path_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError) as ctx:
            TrustedExecutable(command="/tmp/evil", env_override="X")
        self.assertIn("bare basename", str(ctx.exception))

    def test_relative_command_path_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            TrustedExecutable(command="./evil", env_override="X")

    def test_nested_command_path_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            TrustedExecutable(command="bin/evil", env_override="X")

    def test_executable_block_rejects_a_path_key(self) -> None:
        # The key that would let data name a host binary does not exist, and an unknown
        # key fails closed rather than being ignored.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfile.from_record(
                "evil",
                _synthetic_record(
                    executable={
                        "command": "evil",
                        "env_override": "X",
                        "path": "/tmp/evil",
                    }
                ),
            )
        self.assertIn("'path'", str(ctx.exception))

    def test_module_path_key_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfile.from_record(
                "evil", _synthetic_record(module="evil.plugin:load")
            )

    def test_repo_config_cannot_define_a_provider_profile(self) -> None:
        # The repo-local config schema exposes `launch_argv` (flag tokens) only; there
        # is no key through which a checkout could declare a profile or an executable.
        # A `providers` / `executable` block fails closed as an unknown key rather than
        # being ignored (which is what would let a hostile checkout smuggle one in).
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
            RepoLocalConfig,
            RepoLocalConfigError,
        )

        for hostile in (
            {"providers": {"claude": {"command": "/tmp/evil"}}},
            {"executable": "/tmp/evil"},
            {"launch_argv": {"claude": {"default": ["--x"]}}, "module": "evil:load"},
        ):
            with self.subTest(hostile=hostile):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record({"agent_launch": hostile})


class ForbiddenAuthorityTest(unittest.TestCase):
    """A profile may never claim a role, a binding, or a core-owned authority."""

    def test_workflow_role_as_provider_id_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfile.from_record("coordinator", _synthetic_record())
        self.assertIn("workflow role", str(ctx.exception))

    def test_routing_authority_as_capability_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfile.from_record(
                "x", _synthetic_record(capabilities=["routing_authority"])
            )

    def test_bare_string_capabilities_cannot_smuggle_an_authority(self) -> None:
        # A bare string is iterable: without the guard, "routing_authority" would become
        # a set of single characters and slip past the authority check.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfile.from_record(
                "x", _synthetic_record(capabilities="routing_authority")
            )
        self.assertIn("not a bare", str(ctx.exception))

    def test_unknown_capability_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfile.from_record(
                "x", _synthetic_record(capabilities=["interactive_tui", "be_coordinator"])
            )

    def test_unknown_managed_flag_concept_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfile.from_record(
                "x", _synthetic_record(managed_flags={"sudo_mode": "--sudo"})
            )

    def test_unknown_protocol_is_rejected(self) -> None:
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfile.from_record(
                "x", _synthetic_record(protocol="telepathy")
            )
        self.assertIn("adapter code", str(ctx.exception))

    def test_half_declared_managed_posture_is_rejected(self) -> None:
        # Capability without spelling (or vice versa) would silently drop the managed
        # permission flag — the #13360 prompt-gated-worker stall.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfile.from_record(
                "x", _synthetic_record(managed_flags={})
            )
        self.assertIn("half-declared", str(ctx.exception))

    def test_managed_flag_must_be_a_long_option(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfile.from_record(
                "x", _synthetic_record(managed_flags={"permission_mode": "approval"})
            )

    def test_duplicate_discovery_alias_is_rejected(self) -> None:
        # An ambiguous alias would make pane discovery guess a provider.
        config = AgentProviderProfileConfig.from_record(
            _config_record(
                {
                    "a": _synthetic_record("a", discovery_aliases=["shared"]),
                    "b": _synthetic_record("b", discovery_aliases=["shared"]),
                }
            )
        )
        with self.assertRaises(AgentProviderProfileError) as ctx:
            config.to_registry()
        self.assertIn("ambiguous", str(ctx.exception))


class ReservedManagedFlagConfigGuardTest(unittest.TestCase):
    """Operator config may not re-specify a profile-owned managed flag (#13425 Q4)."""

    def test_config_cannot_override_the_managed_permission_flag(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (
            AgentLaunchArgvError,
            parse_launch_argv_record,
            validate_launch_argv,
        )

        parsed = parse_launch_argv_record(
            {"claude": {"sublane": ["--permission-mode", "bypassPermissions"]}},
            source="repo config",
        )
        with self.assertRaises(AgentLaunchArgvError) as ctx:
            validate_launch_argv(
                parsed, sublane_claude_model_set=False, source="repo config"
            )
        self.assertIn("--permission-mode", str(ctx.exception))

    def test_equals_form_is_also_rejected(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (
            AgentLaunchArgvError,
            parse_launch_argv_record,
            validate_launch_argv,
        )

        parsed = parse_launch_argv_record(
            {"claude": {"default": ["--permission-mode=plan"]}}, source="repo config"
        )
        with self.assertRaises(AgentLaunchArgvError):
            validate_launch_argv(
                parsed, sublane_claude_model_set=False, source="repo config"
            )

    def test_unknown_provider_key_in_config_is_rejected(self) -> None:
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agent_launch_argv import (
            AgentLaunchArgvError,
            parse_launch_argv_record,
        )

        with self.assertRaises(AgentLaunchArgvError):
            parse_launch_argv_record(
                {"gpt-cli": {"default": ["--x"]}}, source="repo config"
            )


class PackagedArtifactTest(unittest.TestCase):
    def test_packaged_config_loads_and_validates(self) -> None:
        config = load_agent_provider_config()
        self.assertTrue(config.version)
        self.assertTrue(config.source)
        self.assertEqual(
            {"claude", "codex"}, {p.provider_id for p in config.profiles}
        )

    def test_every_builtin_is_a_launchable_interactive_cli(self) -> None:
        for profile in AGENT_PROVIDER_PROFILES:
            require_launchable(profile)  # raises if not drivable

    def test_registry_rejects_a_duplicate_id(self) -> None:
        registry = AgentProviderProfileConfig.from_record(
            _config_record({"mistral-cli": _synthetic_record()})
        ).to_registry()
        with self.assertRaises(AgentProviderProfileError):
            registry.register(registry.require("mistral-cli"))


class R1F1PreflightBeforeSideEffectsTest(unittest.TestCase):
    """R1-F1: an unresolvable provider must leave ZERO herdr side effects behind.

    The pre-correction code resolved argv[0] lazily inside each slot's builder, so a
    ``(codex, claude)`` pair created the workspace, created the tab, and STARTED codex
    before discovering claude's binary was missing — a partial lane with a live agent.
    These pin the whole-plan preflight.
    """

    def _run(self, providers, resolvable):
        import stat as _stat

        from tests.support.herdr_fake import FakeHerdr
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            HerdrSessionStartError,
            prepare_session,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            herdr_bin = Path(tmp) / "herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(herdr_bin.stat().st_mode | _stat.S_IEXEC)
            bindir = Path(tmp) / "bin"
            bindir.mkdir()
            for name in resolvable:
                _install_binary(bindir, name)

            herdr = FakeHerdr()
            env = {"MOZYO_HERDR_BINARY": str(herdr_bin), "PATH": str(bindir)}
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(HerdrSessionStartError):
                    prepare_session(
                        repo_root=repo,
                        providers=providers,
                        lane_id="lane-1",
                        env=env,
                        runner=herdr.run,
                    )
            return [" ".join(call[:3]) for call in herdr.calls]

    def _mutations(self, calls):
        return [
            c
            for c in calls
            if any(
                verb in c
                for verb in ("workspace create", "tab create", "agent start")
            )
        ]

    def test_single_unresolvable_provider_creates_no_workspace_or_tab(self) -> None:
        calls = self._run(["claude"], resolvable=[])
        self.assertEqual([], self._mutations(calls))

    def test_second_provider_failure_starts_no_agent_at_all(self) -> None:
        # The regression that matters: codex resolves, claude does not. Nothing may be
        # created, and codex must NOT be started — no partial lane.
        calls = self._run(["codex", "claude"], resolvable=["codex"])
        self.assertEqual([], self._mutations(calls))

    def test_adopt_only_session_does_not_require_a_provider_binary(self) -> None:
        # The preflight covers `launch` plans only: a dry-run starts no process, so it
        # must not begin to require a resolvable binary the pre-#13441 code never needed.
        import stat as _stat

        from tests.support.herdr_fake import FakeHerdr
        from mozyo_bridge.core.state.workspace_registry import register_workspace
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            prepare_session,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            herdr_bin = Path(tmp) / "herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(herdr_bin.stat().st_mode | _stat.S_IEXEC)
            herdr = FakeHerdr()
            empty_bin = Path(tmp) / "bin"
            empty_bin.mkdir()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                # A dry run needs a durable workspace identity (#13595) — register it, so
                # this test isolates the provider-binary question and nothing else.
                register_workspace(repo)
                # No provider binary anywhere, yet the dry run still plans successfully:
                # the preflight covers `launch` plans only.
                result = prepare_session(
                    repo_root=repo,
                    providers=["claude", "codex"],
                    lane_id="lane-1",
                    env={
                        "MOZYO_HERDR_BINARY": str(herdr_bin),
                        "PATH": str(empty_bin),
                    },
                    runner=herdr.run,
                    dry_run=True,
                )
        self.assertEqual(["planned", "planned"], [s.outcome for s in result.slots])


class R1F2ManagedFlagIsDataDrivenTest(unittest.TestCase):
    """R1-F2: the managed flag spelling comes from the profile on BOTH chokepoints."""

    def _registry_with(self, spelling, provider_id="claude", command="claude"):
        return AgentProviderProfileConfig.from_record(
            _config_record(
                {
                    provider_id: {
                        "protocol": "interactive_cli_tui",
                        "executable": {
                            "command": command,
                            "env_override": "MOZYO_AGENT_X_BINARY",
                        },
                        "discovery_aliases": [provider_id],
                        "process_names": [provider_id],
                        "capabilities": [
                            "interactive_tui",
                            "launch_argv_override",
                            "managed_permission_mode",
                        ],
                        "managed_flags": {"permission_mode": spelling},
                    }
                }
            )
        ).to_registry()

    def test_tmux_chokepoint_follows_a_data_rename(self) -> None:
        # Renaming the flag in the packaged data must move the TMUX renderer too. Before
        # the correction it kept emitting the literal `--permission-mode`.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
            permission_mode_flag,
        )

        with patch.object(
            profile_module, "AGENT_PROVIDER_PROFILES", self._registry_with("--approval-mode")
        ):
            self.assertEqual(
                " --approval-mode auto",
                permission_mode_flag("claude", policy_default="auto", env={}),
            )

    def test_synthetic_provider_gets_its_own_flag_on_the_tmux_path(self) -> None:
        # Previously `agent != "claude"` meant a capable synthetic provider got NO flag.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
            permission_mode_flag,
        )

        registry = self._registry_with(
            "--approval", provider_id="mistral-cli", command="mistral-cli"
        )
        with patch.object(profile_module, "AGENT_PROVIDER_PROFILES", registry):
            self.assertEqual(
                " --approval auto",
                permission_mode_flag("mistral-cli", policy_default="auto", env={}),
            )

    def test_provider_without_the_capability_still_gets_no_flag(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
            permission_mode_flag,
        )

        self.assertEqual("", permission_mode_flag("codex", policy_default="auto", env={}))

    def test_unregistered_label_never_raises_and_gets_no_flag(self) -> None:
        # Diagnostics ask about arbitrary labels; answering must not crash.
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
            permission_mode_flag,
        )

        self.assertEqual("", permission_mode_flag("nope", policy_default="auto", env={}))


class R1F3IdentityVocabularyTest(unittest.TestCase):
    """R1-F3: reserved sentinels, receiver-agnostic processes, exact-one process names."""

    def _mk(self, pid, **over):
        record = {
            "protocol": "interactive_cli_tui",
            "executable": {"command": pid, "env_override": "X"},
            "discovery_aliases": [pid],
            "process_names": [pid],
            "capabilities": ["interactive_tui"],
            "managed_flags": {},
        }
        record.update(over)
        return record

    def test_provider_id_may_not_be_the_unknown_sentinel(self) -> None:
        # `unknown` is what the role resolvers return for an UNidentified agent; a
        # provider claiming it would make "no provider" resolve to a real provider.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfileConfig.from_record(
                _config_record({"unknown": self._mk("unknown")})
            ).to_registry()
        self.assertIn("reserved core identity", str(ctx.exception))

    def test_discovery_alias_may_not_be_the_unknown_sentinel(self) -> None:
        with self.assertRaises(AgentProviderProfileError):
            AgentProviderProfileConfig.from_record(
                _config_record({"x": self._mk("x", discovery_aliases=["unknown"])})
            ).to_registry()

    def test_receiver_agnostic_node_process_is_rejected(self) -> None:
        # Both built-in CLIs are Node programs, so `node` names a runtime, not a
        # provider: claiming it would let any node process resolve as that provider.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfileConfig.from_record(
                _config_record({"x": self._mk("x", process_names=["node"])})
            ).to_registry()
        self.assertIn("receiver-agnostic", str(ctx.exception))

    def test_duplicate_process_name_across_providers_is_rejected(self) -> None:
        # Previously accepted, and the consumer map resolved last-wins.
        with self.assertRaises(AgentProviderProfileError) as ctx:
            AgentProviderProfileConfig.from_record(
                _config_record(
                    {
                        "a": self._mk("a", process_names=["shared"]),
                        "b": self._mk("b", process_names=["shared"]),
                    }
                )
            ).to_registry()
        self.assertIn("last-wins", str(ctx.exception))

    def test_builtin_process_owners_are_exact_one(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile import (
            agent_process_owners,
        )

        self.assertEqual({"claude": "claude", "codex": "codex"}, agent_process_owners())


class PackagedResourceShipsInTheWheelTest(unittest.TestCase):
    """The profile artifact must travel with the package, not just the repo.

    ``agent_provider_profile.py`` reads the YAML at import. Inside the repo it resolves
    from the source tree, so a missing ``package-data`` entry is invisible here — but an
    installed wheel would have no profiles, the registry would fail to load, and EVERY
    launch would fail closed. This pins the declaration so the packaging can never
    silently drop it (review R1-F4).
    """

    # The resource path RELATIVE TO THE PACKAGE ROOT — exactly the form a `package-data`
    # entry takes. A literal, deliberately NOT derived from the reader module's
    # ``__file__``: on the installed-package lane that module lives in site-packages, so
    # a repo-relative derivation raises. And no ``tomllib`` (3.11+, while the matrix also
    # runs 3.10) — a guard against a portability bug must not itself be unportable.
    RESOURCE_REL = (
        "e_140_adapter_provider/f_160_provider_registry/domain/"
        "agent_provider_profiles.yaml"
    )

    def _source_checkout(self):
        """The repo checkout, or ``None`` when running against an installed package."""
        return REPO_ROOT if (REPO_ROOT / "pyproject.toml").is_file() else None

    def test_profile_yaml_is_declared_as_package_data(self) -> None:
        repo = self._source_checkout()
        if repo is None:
            self.skipTest("installed-package lane: no pyproject.toml to inspect")
        text = (repo / "pyproject.toml").read_text(encoding="utf-8")
        self.assertIn(
            self.RESOURCE_REL,
            text,
            f"{self.RESOURCE_REL} is not declared in [tool.setuptools.package-data]; "
            f"an installed wheel would ship no agent provider profiles, the registry "
            f"would fail to load, and every launch would fail closed",
        )

    def test_declared_path_is_the_file_the_reader_loads(self) -> None:
        # A path typo would satisfy the check above and still ship nothing.
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.domain.agent_provider_profile_config import (
            AGENT_PROVIDER_PROFILE_RESOURCE,
        )

        self.assertTrue(self.RESOURCE_REL.endswith(AGENT_PROVIDER_PROFILE_RESOURCE))
        repo = self._source_checkout()
        if repo is None:
            self.skipTest("installed-package lane: no source tree to inspect")
        self.assertTrue((repo / "src" / "mozyo_bridge" / self.RESOURCE_REL).is_file())

    def test_the_reader_resolves_the_resource_package_anchored(self) -> None:
        # Portable in BOTH lanes: `importlib.resources` resolves the artifact from the
        # package itself, which is the property that must actually hold inside a wheel.
        config = load_agent_provider_config()
        self.assertTrue(config.profiles)


class R2F1SingleRegistrySnapshotTest(unittest.TestCase):
    """R2-F1: preflight resolves executable, managed policy, AND capability from ONE
    registry snapshot, and the builder re-reads nothing.

    The first R1-F1 cut threaded the injected registry into executable resolution only;
    `permission_mode_argv` and the builder's capability check re-read the global. So a
    provider present only in an injected registry got an empty managed argv and made the
    builder raise. These pin the whole `ResolvedProviderLaunch` to the injected snapshot.
    """

    def _injected_registry(self, **caps):
        capabilities = caps.get(
            "capabilities",
            ["interactive_tui", "managed_permission_mode"],
        )
        return AgentProviderProfileConfig.from_record(
            _config_record(
                {
                    "mistral-cli": {
                        "protocol": "interactive_cli_tui",
                        "executable": {
                            "command": "mistral-cli",
                            "env_override": "MOZYO_AGENT_MISTRAL_BINARY",
                        },
                        "discovery_aliases": ["mistral-cli"],
                        "process_names": ["mistral-cli"],
                        "capabilities": capabilities,
                        "managed_flags": {"permission_mode": "--approval"},
                    }
                }
            )
        ).to_registry()

    def test_managed_argv_resolves_from_the_injected_registry(self) -> None:
        registry = self._injected_registry()
        with tempfile.TemporaryDirectory() as tmp:
            _install_binary(Path(tmp) / "bin", "mistral-cli")
            resolved = preflight_launch_providers(
                ["mistral-cli"],
                {"PATH": str(Path(tmp) / "bin")},
                permission_mode_default="auto",
                registry=registry,
            )["mistral-cli"]
        # Was `()` before R2-F1 because permission_mode_argv re-read the global.
        self.assertEqual(("--approval", "auto"), resolved.managed_argv)

    def test_the_provider_is_absent_from_the_global_registry(self) -> None:
        # Guards the test's own premise: if `mistral-cli` were ever a built-in, the split
        # would be invisible again. It must NOT be in the global set.
        self.assertNotIn("mistral-cli", agent_provider_ids())

    def test_capability_is_pinned_not_re_read(self) -> None:
        registry = self._injected_registry(
            capabilities=[
                "interactive_tui",
                "managed_permission_mode",
                "tool_shell_env_overrides",
            ]
        )
        with tempfile.TemporaryDirectory() as tmp:
            _install_binary(Path(tmp) / "bin", "mistral-cli")
            resolved = preflight_launch_providers(
                ["mistral-cli"],
                {"PATH": str(Path(tmp) / "bin")},
                registry=registry,
            )["mistral-cli"]
        self.assertTrue(resolved.tool_shell_env_overrides)

    def test_permission_mode_argv_honors_an_injected_registry(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.claude_permission_policy import (
            permission_mode_argv,
        )

        registry = self._injected_registry()
        # Global does not know mistral-cli -> () ; injected registry does -> tokens.
        self.assertEqual((), permission_mode_argv("mistral-cli", policy_default="auto", env={}))
        self.assertEqual(
            ("--approval", "auto"),
            permission_mode_argv(
                "mistral-cli", policy_default="auto", env={}, registry=registry
            ),
        )


class R2F1PreflightIdentityGuardTest(unittest.TestCase):
    """R2-F1 must-fix 4: a launch plan with no matching resolved fails BEFORE side effects."""

    def test_launch_with_unresolved_provider_creates_nothing(self) -> None:
        import stat as _stat

        from tests.support.herdr_fake import FakeHerdr
        from mozyo_bridge.core.state.workspace_registry import register_workspace
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (
            HerdrSessionStartError,
            prepare_session,
        )

        # A provider that is not registered at all -> preflight raises, zero side effects.
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            herdr_bin = Path(tmp) / "herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(herdr_bin.stat().st_mode | _stat.S_IEXEC)
            bindir = Path(tmp) / "bin"
            _install_binary(bindir, "claude")
            herdr = FakeHerdr()
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                register_workspace(repo)
                with self.assertRaises(HerdrSessionStartError):
                    prepare_session(
                        repo_root=repo,
                        providers=["ghost-provider"],
                        lane_id="lane-1",
                        env={"MOZYO_HERDR_BINARY": str(herdr_bin), "PATH": str(bindir)},
                        runner=herdr.run,
                    )
            calls = [" ".join(c[:3]) for c in herdr.calls]
        self.assertEqual(
            [],
            [
                c
                for c in calls
                if any(v in c for v in ("workspace create", "tab create", "agent start"))
            ],
        )


class R1F4HostIndependenceTest(unittest.TestCase):
    """R1-F4: provider resolution never falls back to the host's ambient environment."""

    def test_bare_command_never_resolves_from_the_ambient_process_path(self) -> None:
        # The whole point of the trusted boundary: an empty env resolves nothing, even
        # on a developer machine where `claude` is on the real PATH. This is what makes
        # the suite portable — CI has no provider binary at all.
        with self.assertRaises(AgentProviderExecutableError):
            resolve_agent_executable("claude", {})

    def test_resolution_uses_only_the_passed_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            expected = _install_binary(Path(tmp) / "bin", "claude")
            resolved = resolve_agent_executable(
                "claude", {"PATH": str(Path(tmp) / "bin")}
            )
        self.assertEqual(expected, resolved)


if __name__ == "__main__":
    unittest.main()
