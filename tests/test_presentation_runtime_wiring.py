"""Presentation selection -> runtime resolution wiring tests (Redmine #12251).

Pins the connection of the internal presentation-selection layer (#12189) to its
first real runtime surface: the ``mozyo-bridge`` CLI entrypoint. Before #12251
``config.presentation`` was read and schema-validated by the #12190 loader but
never resolved to a concrete provider; this lane closes that gap, mirroring the
#12191 CLI family resolution and the #12249 provider resolution.

- **default / absent selection is behavior-preserving.**
  ``resolve_presentation_provider()`` with the default config resolves to the
  built-in tmux provider (surface ``tmux_user_option``), and a repo with no
  ``.mozyo-bridge/config.yaml`` runs ``main`` exactly as before.
- **a valid non-default selection resolves.** Selecting ``text`` resolves to the
  built-in text provider, whose ``surface`` is ``text`` — the existing
  projection provider, not a new one.
- **the resolution table is derived from the providers.** Every resolvable
  surface maps to a provider that actually serves that surface, so the table
  cannot drift from the providers' own ``surface`` attributes.
- **unknown / authority- / target-shaped surfaces fail closed at construction.**
  A surface outside the core-owned vocabulary is rejected before it can reach
  runtime resolution; selection can never become routing / send / approve truth.
- **runtime resolution threads through ``main`` and stays fail-closed.** A valid
  selection lets ``main`` proceed to argparse; the entrypoint converts a
  presentation resolution failure into the same actionable stderr line and exit
  code ``2`` the provider/CLI wiring uses.

This file exercises only the resolver and the ``main`` seam; no tmux, network,
or command handler runs.
"""
from __future__ import annotations

import contextlib
import io
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application import cli as cli_module
from mozyo_bridge.application import presentation_runtime as presentation_runtime_module
from mozyo_bridge.application.cli import main
from mozyo_bridge.application.presentation_runtime import (
    PresentationRuntimeError,
    resolve_presentation_provider,
)
from mozyo_bridge.application.repo_local_config_loader import (
    CONFIG_FILE_RELPATH,
    load_repo_local_config,
)
from mozyo_bridge.application.text_attention_presentation_provider import (
    TEXT_ATTENTION_PRESENTATION_PROVIDER,
)
from mozyo_bridge.application.tmux_attention_presentation_provider import (
    TMUX_ATTENTION_PRESENTATION_PROVIDER,
)
from mozyo_bridge.domain.presentation_adapter import (
    PRESENTATION_SURFACES,
    SURFACE_TEXT,
    SURFACE_TMUX_USER_OPTION,
    PresentationProvider,
)
from mozyo_bridge.domain.repo_local_config import (
    PresentationSelectionConfig,
    RepoLocalConfig,
    RepoLocalConfigError,
)


class ResolvePresentationProviderTest(unittest.TestCase):
    """The runtime resolver maps a surface to its built-in provider, fail-closed."""

    def test_default_resolves_tmux_provider(self) -> None:
        # The default (no selection) must resolve to the tmux provider — the
        # behavior-preserving identity. None and the explicit default config
        # resolve identically.
        resolved = resolve_presentation_provider()
        self.assertIs(resolved, TMUX_ATTENTION_PRESENTATION_PROVIDER)
        self.assertIs(
            resolve_presentation_provider(PresentationSelectionConfig.default()),
            resolved,
        )
        self.assertEqual(resolved.surface, SURFACE_TMUX_USER_OPTION)

    def test_text_selection_resolves_text_provider(self) -> None:
        # Selecting the ``text`` surface resolves to the existing built-in text
        # provider — not a newly minted one.
        config = PresentationSelectionConfig(surface=SURFACE_TEXT)
        resolved = resolve_presentation_provider(config)
        self.assertIs(resolved, TEXT_ATTENTION_PRESENTATION_PROVIDER)
        self.assertEqual(resolved.surface, SURFACE_TEXT)

    def test_resolved_provider_conforms_to_protocol(self) -> None:
        # Both resolvable surfaces resolve to something honouring the read /
        # projection-first PresentationProvider protocol (a ``project`` method,
        # no send / route / approve).
        for surface in (SURFACE_TMUX_USER_OPTION, SURFACE_TEXT):
            provider = resolve_presentation_provider(
                PresentationSelectionConfig(surface=surface)
            )
            self.assertIsInstance(provider, PresentationProvider)

    def test_every_builtin_surface_is_resolvable(self) -> None:
        # The resolution table is derived from the providers, so each recognized
        # built-in surface resolves to a provider that actually serves it. (Both
        # core surfaces ship a provider today.)
        for surface in PRESENTATION_SURFACES:
            provider = resolve_presentation_provider(
                PresentationSelectionConfig(surface=surface)
            )
            self.assertEqual(provider.surface, surface)

    def test_unknown_surface_fails_closed_at_construction(self) -> None:
        # A surface outside the core-owned vocabulary never reaches runtime
        # resolution: the config record itself fails closed at construction.
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig(surface="webview")
        with self.assertRaises(RepoLocalConfigError):
            PresentationSelectionConfig(surface="routing_authority")

    def test_recognized_surface_without_provider_fails_closed(self) -> None:
        # Defensive contract: if a core-recognized surface has no built-in
        # provider, resolution raises rather than silently falling back to the
        # default. Simulate it by pointing the resolver at a table missing the
        # surface — exercising the fail-closed branch shape-only validation
        # cannot reach.
        original = presentation_runtime_module._PRESENTATION_PROVIDERS_BY_SURFACE
        presentation_runtime_module._PRESENTATION_PROVIDERS_BY_SURFACE = {}
        try:
            with self.assertRaises(PresentationRuntimeError):
                resolve_presentation_provider(PresentationSelectionConfig.default())
        finally:
            presentation_runtime_module._PRESENTATION_PROVIDERS_BY_SURFACE = original

    def test_selection_carries_no_routing_or_authority(self) -> None:
        # Projection-only: resolving a selection yields a provider that exposes
        # only ``project`` — no send / route / approve attribute is introduced by
        # the resolution seam.
        provider = resolve_presentation_provider(
            PresentationSelectionConfig(surface=SURFACE_TEXT)
        )
        for forbidden in ("send", "route", "approve", "close"):
            self.assertFalse(hasattr(provider, forbidden))


class MainResolvesPresentationSelectionTest(unittest.TestCase):
    """``main`` connects presentation resolution at the entrypoint, fail-closed."""

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

    def test_valid_text_selection_lets_version_flag_run(self) -> None:
        # A valid (realizable) non-default surface selection must thread through
        # main and reach argparse: --version exits 0, proving resolution is
        # transparent when the selection is realizable.
        good = RepoLocalConfig(
            presentation=PresentationSelectionConfig(surface=SURFACE_TEXT)
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

    def test_presentation_resolution_failure_fails_closed_through_main(self) -> None:
        # A presentation resolution failure (a recognized surface with no built-in
        # provider) must fail closed at the entrypoint with the same actionable
        # text the provider/CLI wiring uses — not a traceback and not a silent
        # default CLI. Simulate the unrealizable surface by emptying the resolver
        # table for the duration of the call.
        good = RepoLocalConfig(presentation=PresentationSelectionConfig.default())
        original_cfg = self._with_loaded_config(good)
        original_table = presentation_runtime_module._PRESENTATION_PROVIDERS_BY_SURFACE
        presentation_runtime_module._PRESENTATION_PROVIDERS_BY_SURFACE = {}
        try:
            code, err = self._run_main(["mozyo-bridge", "status"])
        finally:
            presentation_runtime_module._PRESENTATION_PROVIDERS_BY_SURFACE = (
                original_table
            )
            cli_module.load_repo_local_config = original_cfg  # type: ignore[assignment]

        self.assertEqual(code, 2)
        self.assertIn("invalid repo-local config", err)
        self.assertIn(str(CONFIG_FILE_RELPATH), err)

    def test_repo_without_config_runs_default_resolution(self) -> None:
        # A real repo with no config resolves to the default RepoLocalConfig, and
        # resolving its default presentation selection raises nothing — the
        # entrypoint connection never changes default behavior.
        with tempfile.TemporaryDirectory() as tmp:
            config = load_repo_local_config(Path(tmp))
        self.assertEqual(config, RepoLocalConfig.default())
        # Must not raise; default resolves to the tmux provider.
        provider = resolve_presentation_provider(config.presentation)
        self.assertIs(provider, TMUX_ATTENTION_PRESENTATION_PROVIDER)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
