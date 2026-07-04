"""Role-based wiring of the #12441 main-lane guard (Redmine #13174).

The pure predicate is pinned by
``tests/unit/.../f_130_handoff_routing/test_handoff_main_lane_guard.py``; here we pin
the thin f_140 gate that resolves the implementer role's runtime provider from the
repo-local :class:`RoleProviderBinding` (#12673 seam, #13157 config) and threads it into
that predicate. The two properties under test:

- **characterization**: under the default binding (no config, or a config that leaves the
  implementer at its default) the implementer resolves to ``claude`` and the guard blocks
  a ``--to claude`` main-lane ``implementation_request`` exactly as before #13174;
- **role-based rebind**: under a rebind (implementer moved to ``codex``; or a
  ``coordinator``-on-``claude`` topology) the guard follows the binding — it neither
  mis-blocks the non-implementer provider nor misses the real implementer pane — and a
  broken config fails closed through the #13157 loader error.

No live tmux is required: the binding load reads an explicit temp repo config, and the
gate's provider resolution is patched at its own seam for the wiring cases.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.application.repo_local_config_loader import (  # noqa: E402
    RepoLocalConfigError,
)
from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E402
    VIEW_KIND_COCKPIT_PANE,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E402
    main_lane_guard_gate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.main_lane_guard_gate import (  # noqa: E402
    main_lane_guard_blocked,
    resolve_implementer_provider,
)


def _repo_with_config(body: str | None) -> str:
    """A throwaway repo root carrying ``.mozyo-bridge/config.yaml`` (or none)."""
    root = tempfile.mkdtemp()
    if body is not None:
        cfg = Path(root) / ".mozyo-bridge"
        cfg.mkdir()
        (cfg / "config.yaml").write_text(body, encoding="utf-8")
    return root


@dataclass
class _FakePreflight:
    lane_id: str = "default"
    view_kind: str = VIEW_KIND_COCKPIT_PANE
    bound_provider: str = "claude"

    def binds_receiver(self, receiver: str) -> bool:
        return receiver == self.bound_provider


class _FakeArgs:
    def __init__(self, main_lane_exception=None):
        self.main_lane_exception = main_lane_exception


class ResolveImplementerProviderTest(unittest.TestCase):
    def test_default_binding_resolves_implementer_to_claude(self) -> None:
        # No config -> behavior-preserving default codex/claude map.
        self.assertEqual("claude", resolve_implementer_provider(_repo_with_config(None)))

    def test_empty_provider_binding_block_resolves_default(self) -> None:
        root = _repo_with_config("version: 1\n")
        self.assertEqual("claude", resolve_implementer_provider(root))

    def test_implementer_rebind_is_followed(self) -> None:
        root = _repo_with_config(
            "version: 1\nprovider_binding:\n  bindings:\n    implementer: codex\n"
        )
        self.assertEqual("codex", resolve_implementer_provider(root))

    def test_coordinator_on_claude_leaves_implementer_default(self) -> None:
        # The #13126 coordinator-on-claude topology binds the coordinator, not the
        # implementer: the main-lane guard keys on the implementer, so it must stay
        # `claude` (default) and the guard behavior is unchanged.
        root = _repo_with_config(
            "version: 1\nprovider_binding:\n  bindings:\n    coordinator: claude\n"
        )
        self.assertEqual("claude", resolve_implementer_provider(root))

    def test_broken_config_fails_closed(self) -> None:
        root = _repo_with_config(
            "version: 1\nprovider_binding:\n  bindings:\n    not_a_role: codex\n"
        )
        with self.assertRaises(RepoLocalConfigError):
            resolve_implementer_provider(root)


class MainLaneGuardBlockedTest(unittest.TestCase):
    def _blocked(self, *, provider, receiver, **preflight_kwargs) -> bool:
        pre = _FakePreflight(**preflight_kwargs)
        with patch.object(
            main_lane_guard_gate, "resolve_implementer_provider", return_value=provider
        ):
            return main_lane_guard_blocked(
                _FakeArgs(),
                receiver=receiver,
                kind="implementation_request",
                preflight_target=pre,
            )

    def test_default_binding_blocks_claude_main_lane_impl(self) -> None:
        self.assertTrue(
            self._blocked(provider="claude", receiver="claude", bound_provider="claude")
        )

    def test_default_binding_allows_gateway_provider(self) -> None:
        # Default implementer=claude: a `--to codex` gateway dispatch is unaffected.
        self.assertFalse(
            self._blocked(provider="claude", receiver="codex", bound_provider="codex")
        )

    def test_rebound_implementer_blocks_rebound_receiver(self) -> None:
        # implementer rebound to codex: `--to codex` to the main-lane codex pane is
        # now the guarded send; `--to claude` (the coordinator seat) is not.
        self.assertTrue(
            self._blocked(provider="codex", receiver="codex", bound_provider="codex")
        )
        self.assertFalse(
            self._blocked(provider="codex", receiver="claude", bound_provider="claude")
        )

    def test_sublane_not_blocked(self) -> None:
        self.assertFalse(
            self._blocked(
                provider="claude",
                receiver="claude",
                bound_provider="claude",
                lane_id="lane-5ba25a56f773",
            )
        )

    def test_main_lane_exception_admits(self) -> None:
        pre = _FakePreflight(bound_provider="claude")
        with patch.object(
            main_lane_guard_gate, "resolve_implementer_provider", return_value="claude"
        ):
            self.assertFalse(
                main_lane_guard_blocked(
                    _FakeArgs(main_lane_exception="#12441 j#99999"),
                    receiver="claude",
                    kind="implementation_request",
                    preflight_target=pre,
                )
            )


if __name__ == "__main__":
    unittest.main()
