"""Provider selection -> runtime resolution wiring tests (Redmine #12249).

Pins the connection of the internal provider-selection layer (#12035 / #12184)
to its first real runtime surface: the ``mozyo-bridge`` CLI entrypoint. Before
#12249 ``config.providers`` was read and schema-validated by the #12190 loader
but never resolved against the live registry; this lane closes that gap,
mirroring the #12191 CLI family resolution.

- **default / absent selection is behavior-preserving.**
  ``resolve_builtin_providers()`` with the default config resolves every
  populated category to its current built-in default, and a repo with no
  ``.mozyo-bridge/config.yaml`` runs ``main`` exactly as before.
- **a valid non-default selection resolves and runs.** Selecting the already
  registered built-in for a category resolves to that provider and lets ``main``
  proceed to argparse.
- **runtime resolution fails closed where schema validation cannot.** An unknown
  provider id, an unknown category, and a category/provider mismatch each raise
  ``ProviderRegistryError`` from the resolver, and ``main`` converts that into
  the same actionable stderr line and exit code ``2`` the CLI wiring uses.
- **authority-shaped values fail closed at config construction.** A selection
  naming a core-owned authority is rejected before it ever reaches the registry.

This file exercises only the resolver and the ``main`` fail-closed seam; no
tmux, network, or command handler runs.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import cli as cli_module
from mozyo_bridge.application.cli import main
from mozyo_bridge.application.provider_runtime import resolve_builtin_providers
from mozyo_bridge.application.repo_local_config_loader import (
    CONFIG_FILE_RELPATH,
    load_repo_local_config,
)
from mozyo_bridge.domain.provider_registry import (
    BUILTIN_PROVIDER_REGISTRY,
    ProviderCategory,
    ProviderRegistryError,
    ProviderSelectionConfig,
)
from mozyo_bridge.domain.repo_local_config import RepoLocalConfig


def _write_config(repo_root: Path, body: str) -> None:
    config_path = repo_root / CONFIG_FILE_RELPATH
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(body, encoding="utf-8")


class ResolveBuiltinProvidersTest(unittest.TestCase):
    """The runtime resolver mirrors registry resolution and fails closed."""

    def test_default_resolves_current_builtins(self) -> None:
        # The default (no selection) must resolve every populated category to its
        # current built-in default — the behavior-preserving identity. None and
        # the explicit default config resolve identically.
        resolved = resolve_builtin_providers()
        self.assertEqual(
            resolve_builtin_providers(ProviderSelectionConfig.default()), resolved
        )
        ids = {cat: prov.provider_id for cat, prov in resolved.items()}
        self.assertEqual(ids[ProviderCategory.TICKET], "redmine")
        self.assertEqual(ids[ProviderCategory.TERMINAL_RUNTIME], "tmux")
        self.assertEqual(ids[ProviderCategory.PRESENTATION], "tmux-presentation")
        # Empty categories (no built-in provider) are simply absent, not errors.
        self.assertNotIn(ProviderCategory.CATALOG, resolved)
        self.assertNotIn(ProviderCategory.TELEMETRY, resolved)

    def test_matches_registry_resolution_directly(self) -> None:
        # The thin runtime layer must be a transparent delegate to the registry's
        # own resolve_selection — no divergent behavior.
        config = ProviderSelectionConfig(selections={"ticket": "redmine"})
        self.assertEqual(
            resolve_builtin_providers(config),
            BUILTIN_PROVIDER_REGISTRY.resolve_selection(config),
        )

    def test_valid_non_default_selection_resolves(self) -> None:
        # Naming the already-registered built-in for a category resolves to it
        # (the only realizable selection while each category ships one provider).
        config = ProviderSelectionConfig(selections={"presentation": "tmux-presentation"})
        resolved = resolve_builtin_providers(config)
        self.assertEqual(
            resolved[ProviderCategory.PRESENTATION].provider_id, "tmux-presentation"
        )

    def test_unknown_provider_id_fails_closed(self) -> None:
        config = ProviderSelectionConfig(selections={"ticket": "no-such-provider"})
        with self.assertRaises(ProviderRegistryError):
            resolve_builtin_providers(config)

    def test_unknown_category_fails_closed(self) -> None:
        # A category name that is not a known ProviderCategory passes shape
        # validation but fails at runtime resolution.
        config = ProviderSelectionConfig(selections={"not_a_category": "redmine"})
        with self.assertRaises(ProviderRegistryError):
            resolve_builtin_providers(config)

    def test_category_provider_mismatch_fails_closed(self) -> None:
        # tmux is a terminal_runtime provider; selecting it for the ticket
        # category is a mismatch the registry rejects.
        config = ProviderSelectionConfig(selections={"ticket": "tmux"})
        with self.assertRaises(ProviderRegistryError):
            resolve_builtin_providers(config)

    def test_authority_shaped_selection_rejected_at_construction(self) -> None:
        # Authority-shaped category/provider names never reach the registry: the
        # config record itself fails closed at construction.
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig(selections={"routing_authority": "redmine"})
        with self.assertRaises(ProviderRegistryError):
            ProviderSelectionConfig(selections={"ticket": "owner_approval"})


class MainResolvesProviderSelectionTest(unittest.TestCase):
    """``main`` connects provider resolution at the entrypoint, fail-closed."""

    def _run_main(self, argv: list[str]) -> tuple[int, str]:
        stderr = io.StringIO()
        old_argv = sys.argv
        sys.argv = argv
        try:
            with contextlib.redirect_stderr(stderr):
                code = main()
        finally:
            sys.argv = old_argv
        return code, stderr.getvalue()

    def _with_loaded_config(self, config: RepoLocalConfig):
        original = cli_module.load_repo_local_config
        cli_module.load_repo_local_config = lambda *a, **k: config  # type: ignore[assignment]
        return original

    def test_invalid_provider_selection_fails_closed_through_main(self) -> None:
        # A schema-valid but unrealizable provider selection (unknown provider id)
        # must fail closed at the entrypoint with the same actionable text the
        # CLI wiring uses — not a traceback and not a silent default CLI.
        bad = RepoLocalConfig(
            providers=ProviderSelectionConfig(selections={"ticket": "no-such-provider"})
        )
        original = self._with_loaded_config(bad)
        try:
            code, err = self._run_main(["mozyo-bridge", "status"])
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", err)
        self.assertIn(str(CONFIG_FILE_RELPATH), err)
        self.assertIn("remove", err)

    def test_category_mismatch_fails_closed_through_main(self) -> None:
        bad = RepoLocalConfig(
            providers=ProviderSelectionConfig(selections={"ticket": "tmux"})
        )
        original = self._with_loaded_config(bad)
        try:
            code, err = self._run_main(["mozyo-bridge", "--version"])
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]

        # Fail-closed is global: a bad provider selection blocks even --version.
        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", err)

    def test_valid_provider_selection_lets_version_flag_run(self) -> None:
        # A valid (realizable) non-default selection must thread through main and
        # reach argparse: --version exits 0, proving resolution is transparent
        # when the selection is realizable.
        good = RepoLocalConfig(
            providers=ProviderSelectionConfig(selections={"ticket": "redmine"})
        )
        original = self._with_loaded_config(good)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                old_argv = sys.argv
                sys.argv = ["mozyo-bridge", "--version"]
                try:
                    with self.assertRaises(SystemExit) as ctx:
                        main()
                finally:
                    sys.argv = old_argv
        finally:
            cli_module.load_repo_local_config = original  # type: ignore[assignment]
        self.assertEqual(ctx.exception.code, 0)

    def test_repo_without_config_runs_default_resolution(self) -> None:
        # A real repo with no config resolves to the default RepoLocalConfig, and
        # resolving its default providers selection raises nothing — the
        # entrypoint connection never changes default behavior.
        with tempfile.TemporaryDirectory() as tmp:
            config = load_repo_local_config(Path(tmp))
        self.assertEqual(config, RepoLocalConfig.default())
        # Must not raise.
        resolve_builtin_providers(config.providers)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
