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

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application.agent_provider_executable import (
    AgentProviderExecutableError,
    require_launchable,
    resolve_agent_argv0,
    resolve_agent_executable,
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
    """End-to-end: a data-only provider renders a full launch argv, no source branch."""

    def test_synthetic_provider_renders_its_own_managed_flag_in_launch_argv(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (
            herdr_launch_argv,
        )
        from mozyo_bridge.e_140_adapter_provider.f_160_provider_registry.application import (
            agent_provider_executable,
        )

        registry = AgentProviderProfileConfig.from_record(
            _config_record({"mistral-cli": _synthetic_record()})
        ).to_registry()

        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            expected_argv0 = _install_binary(bindir, "mistral-cli")
            # Swap the built-in registry for one that knows only the synthetic provider:
            # if any launch code still branched on `claude` / `codex`, this would fail.
            with patch.object(profile_module, "AGENT_PROVIDER_PROFILES", registry), patch.object(
                agent_provider_executable, "AGENT_PROVIDER_PROFILES", registry
            ):
                argv = herdr_launch_argv.build_agent_start_argv(
                    assigned_name="mzb1_ws_mistral-cli_lane",
                    provider="mistral-cli",
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
                    claude_permission_mode="auto",
                    launch_argv_extra=[],
                    env={"PATH": str(bindir)},
                )

        run_cmd = argv[argv.index("--") + 1 :]
        # argv[0] = the resolved absolute executable; then ITS OWN flag spelling.
        self.assertEqual([expected_argv0, "--approval", "auto"], run_cmd)


class ArgvZeroCompatibilityTest(unittest.TestCase):
    """Built-in launches: argv[0] absolute; every other token byte-invariant (Q1)."""

    def _argv(self, provider, bindir, **over):
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
            build_agent_start_argv,
        )

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
            claude_permission_mode=None,
            launch_argv_extra=[],
            env={"PATH": str(bindir)},
        )
        kwargs.update(over)
        return build_agent_start_argv(**kwargs)

    def test_claude_argv0_is_the_injected_absolute_executable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bindir = Path(tmp) / "bin"
            expected = _install_binary(bindir, "claude")
            _install_binary(bindir, "codex")
            argv = self._argv("claude", bindir, claude_permission_mode="auto")
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
            argv = self._argv("codex", bindir, claude_permission_mode="auto")
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


if __name__ == "__main__":
    unittest.main()
