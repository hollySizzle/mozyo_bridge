"""Provider-neutral config schema v2 + migration tests (Redmine #14148).

Pins the role-canonical ``agents`` topology (named runtime profiles + role -> profile
bindings), the ``role -> profile -> (provider, launch_argv[lane_class])`` canonical launch
resolution (finding 2), the v1 -> v2 migration (finding 5 registered-adapter diagnostic,
idempotency, redundant-binding drop), and the lossless migration of the closed #13451
``origin/main-next`` staging config (coordinator addendum j#84004).

No file IO or CLI is exercised here — the schema + migration transform only. The CLI
(`config migrate --check/--write`) atomic-write / dry-run behavior is exercised separately.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (
    REPO_LOCAL_CONFIG_V2,
    V1_COMPAT_MINIMUM_MINORS,
    V1_EARLIEST_REMOVAL_VERSION,
    V2_INTRODUCED_VERSION,
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agents_topology import (
    AgentsTopologyConfig,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.config_migration import (
    ConfigMigrationError,
    migrate_record,
)


#: The exact closed #13451 ``origin/main-next@ceab289e`` ``.mozyo-bridge/config.yaml``
#: (verified from git). It is the live lossless-migration fixture the coordinator pinned in
#: j#84004: coordinator/default Codex on ``gpt-5.6-sol`` + ``xhigh``, a Codex *sublane* on
#: ``high`` with NO model, and the Claude worker sublane on ``opus``.
MAIN_NEXT_13451_V1: "dict[str, object]" = {
    "version": 1,
    "presentation": {"project_group_presentation": "project_group_tmux_window"},
    "agent_launch": {
        "launch_argv": {
            "claude": {"sublane": ["--model", "claude-opus-4-8"]},
            "codex": {
                "default": [
                    "--model",
                    "gpt-5.6-sol",
                    "--config",
                    "model_reasoning_effort=xhigh",
                ],
                "sublane": ["--config", "model_reasoning_effort=high"],
            },
        }
    },
    "provider_binding": {"bindings": {"coordinator": "codex"}},
    "terminal_transport": {"backend": "herdr"},
}


class SchemaV2Test(unittest.TestCase):
    def test_v2_default_is_role_canonical_topology(self) -> None:
        # A v2 config with no ``agents`` block resolves to the canonical role -> profile
        # topology (finding 1): not an empty override that falls back to a provider map.
        cfg = RepoLocalConfig.from_record({"version": 2})
        self.assertEqual(cfg.schema_version, REPO_LOCAL_CONFIG_V2)
        self.assertEqual(cfg.agents.resolve_provider_for_role("coordinator"), "codex")
        self.assertEqual(cfg.agents.resolve_provider_for_role("implementer"), "claude")
        # default profiles carry no launch argv -> byte-for-byte historical launch.
        self.assertEqual(cfg.agents.resolve_launch_argv_for_role("coordinator", "default"), [])

    def test_role_profile_launch_resolution_same_provider(self) -> None:
        # Two roles on the same provider but different named profiles get different argv —
        # the provider-keyed model cannot express this (finding 2). The canonical
        # AgentsTopologyConfig resolves it by role -> profile.
        ag = AgentsTopologyConfig.from_record(
            {
                "profiles": {
                    "coordination": {"provider": "codex", "launch_argv": {"default": ["--a"]}},
                    "gateway": {"provider": "codex", "launch_argv": {"default": ["--b"]}},
                },
                "roles": {"coordinator": "coordination", "project_gateway": "gateway"},
            }
        )
        self.assertEqual(ag.resolve_launch_argv_for_role("coordinator", "default"), ["--a"])
        self.assertEqual(ag.resolve_launch_argv_for_role("project_gateway", "default"), ["--b"])

    def test_same_provider_lane_collision_fails_closed_at_load(self) -> None:
        # j#84267 condition 2: two profiles binding the same (provider, lane_class) to
        # different argv is a provider-unit launch collision. It fails closed at the earliest
        # pre-side-effect boundary (config load), naming both profiles and pointing at the
        # canonical role -> profile resolution + #13647 — never a silent select / merge.
        collision = {
            "version": 2,
            "agents": {
                "profiles": {
                    "coordination": {"provider": "codex", "launch_argv": {"default": ["--a"]}},
                    "gateway": {"provider": "codex", "launch_argv": {"default": ["--b"]}},
                },
                "roles": {"coordinator": "coordination", "project_gateway": "gateway"},
            },
        }
        with self.assertRaises(RepoLocalConfigError) as ctx:
            RepoLocalConfig.from_record(collision)
        msg = str(ctx.exception)
        self.assertIn("coordination", msg)
        self.assertIn("gateway", msg)
        self.assertIn("#13647", msg)


class BrandIndependenceAcceptanceTest(unittest.TestCase):
    """Role logic is brand-independent: the role's provider follows its profile, not a
    hard-coded map (Redmine #14148 item 8 / j#83977 finding 5)."""

    def test_provider_swap_role_logic_follows_config(self) -> None:
        # Swap the brands: bind the coordinator to a claude profile and the implementer to a
        # codex profile (the inverse of the default). Role -> provider must follow the config,
        # proving the resolution is not hard-coded to codex-coordinates / claude-implements.
        ag = AgentsTopologyConfig.from_record(
            {
                "profiles": {
                    "coord_on_claude": {"provider": "claude"},
                    "impl_on_codex": {"provider": "codex"},
                },
                "roles": {"coordinator": "coord_on_claude", "implementer": "impl_on_codex"},
            }
        )
        self.assertEqual(ag.resolve_provider_for_role("coordinator"), "claude")
        self.assertEqual(ag.resolve_provider_for_role("implementer"), "codex")
        # The role authority itself is unchanged — only the provider binding swapped.
        self.assertEqual(ag.resolve_profile_for_role("coordinator").name, "coord_on_claude")

    def test_third_fake_provider_via_injected_trusted_registry(self) -> None:
        # A same-protocol third provider, injected through the *trusted registry* (not repo
        # config code/executable injection): role logic resolves it identically, so nothing
        # is special-cased to claude/codex. Patch the registry the profile schema validates
        # against, exactly as a packaged third adapter would extend it.
        import mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.agents_topology as at

        real_ids = at.agent_provider_ids()
        fake_ids = frozenset(real_ids | {"fabler"})
        original = at.agent_provider_ids
        at.agent_provider_ids = lambda: fake_ids
        try:
            ag = AgentsTopologyConfig.from_record(
                {
                    "profiles": {"third": {"provider": "fabler", "launch_argv": {"default": ["--m", "fab-1"]}}},
                    "roles": {"coordinator": "third"},
                }
            )
            self.assertEqual(ag.resolve_provider_for_role("coordinator"), "fabler")
            self.assertEqual(ag.resolve_launch_argv_for_role("coordinator", "default"), ["--m", "fab-1"])
        finally:
            at.agent_provider_ids = original

    def test_repo_config_cannot_inject_unregistered_executable(self) -> None:
        # Trusted executable boundary (item 9): a profile provider must be a registered
        # adapter id; a repo config can never point the runtime at an arbitrary program.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"version": 2, "agents": {"profiles": {"x": {"provider": "/usr/bin/evil"}}}}
            )


class MigrationTest(unittest.TestCase):

    def test_v1_and_v2_blocks_are_disjoint(self) -> None:
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"version": 2, "provider_binding": {"bindings": {}}})
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record({"version": 1, "agents": {}})

    def test_unknown_provider_in_profile_fails_closed(self) -> None:
        # Trusted executable boundary: a profile provider must be a registered adapter id.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"version": 2, "agents": {"profiles": {"x": {"provider": "/bin/sh"}}}}
            )


class DeprecationLifecycleTest(unittest.TestCase):
    def test_v1_with_migratable_content_warns(self) -> None:
        cfg = RepoLocalConfig.from_record(
            {"version": 1, "agent_launch": {"launch_argv": {"claude": {"sublane": ["--x"]}}}}
        )
        warnings = cfg.deprecation_warnings()
        self.assertEqual(len(warnings), 1)
        # The notice is actionable: concrete window, earliest-removal version, migrate pointer.
        notice = warnings[0]
        self.assertIn(V2_INTRODUCED_VERSION, notice)
        self.assertIn(V1_EARLIEST_REMOVAL_VERSION, notice)
        self.assertIn("config migrate", notice)

    def test_v1_default_and_v2_are_silent(self) -> None:
        # A bare default (missing config) has nothing to migrate; v2 is current.
        self.assertEqual(RepoLocalConfig.from_record({}).deprecation_warnings(), ())
        self.assertEqual(RepoLocalConfig.from_record({"version": 2}).deprecation_warnings(), ())

    def test_compat_window_is_concrete(self) -> None:
        # The lifecycle is fixed (not "a future minor"): a compat floor and an earliest
        # removal version both exist and are ordered after introduction.
        self.assertGreaterEqual(V1_COMPAT_MINIMUM_MINORS, 1)
        self.assertNotEqual(V2_INTRODUCED_VERSION, V1_EARLIEST_REMOVAL_VERSION)


class MigrationTest(unittest.TestCase):
    def test_default_migration_drops_redundant_binding(self) -> None:
        v1 = {"version": 1, "provider_binding": {"bindings": {"coordinator": "codex"}}}
        res = migrate_record(v1)
        self.assertFalse(res.already_current)
        self.assertEqual(res.target_version, REPO_LOCAL_CONFIG_V2)
        # coordinator: codex equals the default -> not emitted as a role override.
        self.assertNotIn("roles", res.migrated.get("agents", {}))
        self.assertTrue(any("redundant" in c for c in res.changes))

    def test_migration_is_idempotent(self) -> None:
        res = migrate_record(MAIN_NEXT_13451_V1)
        again = migrate_record(res.migrated)
        self.assertTrue(again.already_current)
        self.assertEqual(again.migrated, res.migrated)

    def test_unknown_provider_migration_diagnostic(self) -> None:
        # Finding 5: a valid-v1 open provider that is not a registered adapter fails closed
        # with an actionable, value-non-secret diagnostic (role + provider named).
        with self.assertRaises(ConfigMigrationError) as ctx:
            migrate_record({"version": 1, "provider_binding": {"bindings": {"auditor": "grok"}}})
        msg = str(ctx.exception)
        self.assertIn("registered adapter profile", msg)
        self.assertIn("auditor", msg)
        self.assertIn("grok", msg)


class MainNext13451LosslessTest(unittest.TestCase):
    """The closed #13451 staging config migrates losslessly (coordinator addendum j#84004)."""

    def test_three_launch_variants_preserved_via_role_profile(self) -> None:
        res = migrate_record(MAIN_NEXT_13451_V1)
        cfg = RepoLocalConfig.from_record(res.migrated)
        ag = cfg.agents
        # 1. coordinator / default Codex: gpt-5.6-sol + xhigh.
        self.assertEqual(
            ag.resolve_launch_argv_for_role("coordinator", "default"),
            ["--model", "gpt-5.6-sol", "--config", "model_reasoning_effort=xhigh"],
        )
        # 2. Codex sublane: high, and NO model inherited from the default profile.
        gw_sublane = ag.resolve_launch_argv_for_role("project_gateway", "sublane")
        self.assertEqual(gw_sublane, ["--config", "model_reasoning_effort=high"])
        self.assertNotIn("--model", gw_sublane)
        # 3. Claude worker sublane: opus.
        self.assertEqual(
            ag.resolve_launch_argv_for_role("implementation_worker", "sublane"),
            ["--model", "claude-opus-4-8"],
        )

    def test_providers_preserved(self) -> None:
        cfg = RepoLocalConfig.from_record(migrate_record(MAIN_NEXT_13451_V1).migrated)
        self.assertEqual(cfg.agents.resolve_provider_for_role("coordinator"), "codex")
        self.assertEqual(cfg.agents.resolve_provider_for_role("implementation_worker"), "claude")

    def test_unrelated_blocks_copied_verbatim(self) -> None:
        res = migrate_record(MAIN_NEXT_13451_V1)
        self.assertEqual(res.migrated["terminal_transport"], {"backend": "herdr"})
        self.assertEqual(
            res.migrated["presentation"],
            {"project_group_presentation": "project_group_tmux_window"},
        )
        self.assertNotIn("provider_binding", res.migrated)
        self.assertNotIn("agent_launch", res.migrated)


class RuntimeProjectionTest(unittest.TestCase):
    """The migrated v2 config projects onto the provider-keyed AgentLaunchConfig facade the
    herdr launch chokepoint actually consumes (j#84267 condition 3)."""

    def test_v1_to_v2_to_runtime_facade_preserves_13451(self) -> None:
        cfg = RepoLocalConfig.from_record(migrate_record(MAIN_NEXT_13451_V1).migrated)
        # This is exactly what herdr_session_start calls: resolve_launch_argv(provider, lane).
        al = cfg.agent_launch
        self.assertEqual(
            al.resolve_launch_argv("codex", "default"),
            ["--model", "gpt-5.6-sol", "--config", "model_reasoning_effort=xhigh"],
        )
        self.assertEqual(
            al.resolve_launch_argv("codex", "sublane"),
            ["--config", "model_reasoning_effort=high"],
        )
        self.assertEqual(al.resolve_launch_argv("claude", "sublane"), ["--model", "claude-opus-4-8"])

    def test_runtime_facade_matches_role_profile_canonical(self) -> None:
        # The provider-keyed facade agrees with the canonical role -> profile resolution for
        # every launchable (<=1 profile per provider) config.
        cfg = RepoLocalConfig.from_record(migrate_record(MAIN_NEXT_13451_V1).migrated)
        self.assertEqual(
            cfg.agent_launch.resolve_launch_argv("codex", "default"),
            cfg.agents.resolve_launch_argv_for_role("coordinator", "default"),
        )


class FreshScaffoldRoundTripTest(unittest.TestCase):
    """A freshly-scaffolded repo (no config.yaml) resolves to the role-canonical default;
    a v1 fixture round-trips through migrate (item 7 fresh-scaffold / v1 round-trip)."""

    def test_fresh_scaffold_no_config_is_canonical_default(self) -> None:
        import tempfile
        from pathlib import Path

        from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

        with tempfile.TemporaryDirectory() as tmp:
            # No .mozyo-bridge/config.yaml at all -> behavior-preserving default.
            cfg = load_repo_local_config(str(Path(tmp)))
            self.assertEqual(cfg.agents.resolve_provider_for_role("coordinator"), "codex")
            self.assertEqual(cfg.agents.resolve_provider_for_role("implementer"), "claude")
            self.assertEqual(cfg.deprecation_warnings(), ())  # nothing to migrate

    def test_v1_fixture_round_trips_to_stable_v2(self) -> None:
        # Migrate is deterministic + idempotent, so v1 -> v2 -> v2 is stable.
        once = migrate_record(MAIN_NEXT_13451_V1).migrated
        twice = migrate_record(once)
        self.assertTrue(twice.already_current)
        self.assertEqual(twice.migrated, once)


class ConfigMigrateCliTest(unittest.TestCase):
    """`config migrate` dry-run writes nothing; `--write` is atomic + idempotent."""

    def _run(self, repo, *, write, as_json=False):
        import contextlib
        import io
        import types
        from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.application.cli_config import (
            cmd_config_migrate,
        )
        args = types.SimpleNamespace(repo=str(repo), write=write, json=as_json)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return cmd_config_migrate(args)

    def _write_v1(self, repo):
        import textwrap
        cfgdir = repo / ".mozyo-bridge"
        cfgdir.mkdir(parents=True, exist_ok=True)
        (cfgdir / "config.yaml").write_text(
            textwrap.dedent(
                """\
                version: 1
                provider_binding:
                  bindings:
                    coordinator: codex
                agent_launch:
                  launch_argv:
                    claude:
                      sublane: ["--model", "claude-opus-4-8"]
                """
            ),
            encoding="utf-8",
        )
        return cfgdir / "config.yaml"

    def test_check_writes_nothing(self) -> None:
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = self._write_v1(repo)
            before = path.read_bytes()
            rc = self._run(repo, write=False)
            self.assertEqual(rc, 0)
            self.assertEqual(path.read_bytes(), before)  # dry-run: byte-for-byte unchanged
            self.assertFalse((path.parent / "config.yaml.bak").exists())

    def test_write_is_atomic_and_idempotent(self) -> None:
        import tempfile
        import yaml
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            path = self._write_v1(repo)
            rc = self._run(repo, write=True)
            self.assertEqual(rc, 0)
            written = yaml.safe_load(path.read_text())
            self.assertEqual(written["version"], REPO_LOCAL_CONFIG_V2)
            self.assertNotIn("provider_binding", written)
            self.assertNotIn("agent_launch", written)
            self.assertTrue((path.parent / "config.yaml.bak").exists())
            self.assertFalse((path.parent / "config.yaml.tmp").exists())  # temp cleaned up
            # Idempotent: a second --write is a no-op (already v2).
            after_first = path.read_bytes()
            rc2 = self._run(repo, write=True)
            self.assertEqual(rc2, 0)
            self.assertEqual(path.read_bytes(), after_first)

    def test_migrated_file_reloads_as_valid_v2(self) -> None:
        import tempfile
        from pathlib import Path

        from mozyo_bridge.application.repo_local_config_loader import load_repo_local_config

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)
            self._write_v1(repo)
            self._run(repo, write=True)
            cfg = load_repo_local_config(str(repo))
            self.assertEqual(cfg.schema_version, REPO_LOCAL_CONFIG_V2)
            self.assertEqual(cfg.agents.resolve_launch_argv_for_role("implementation_worker", "sublane"),
                             ["--model", "claude-opus-4-8"])


if __name__ == "__main__":
    unittest.main()
