"""integration — e_110_execution_platform / f_130_handoff_routing (Feature #12509) tests.

Feature-level test slice under the Redmine-numbered layout (US #12622 / #12624).
Tests import the runtime via the stable public facade paths.
"""
from __future__ import annotations

from unittest import mock

# --- tmux-rail transport isolation (Redmine #13254) -------------------------
#
# The legacy fake-tmux handoff-routing tests exercise the tmux send/capture rail
# and are independent of the workspace's ``terminal_transport`` backend choice.
# They do not set ``args.repo``, so ``@bind_runtime_transport`` (on
# ``orchestrate_handoff``) resolves this repo's committed repo-local config via
# ``resolve_handoff_transport_binding``. After the herdr cutover (#13254) that
# config selects the herdr backend, which would drive every send in these tmux
# tests through the herdr shim and break them.
#
# ``setUpModule`` / ``tearDownModule`` below pin the resolver to the tmux default
# (``None`` binding -> the decorator installs nothing) for the duration of a test
# module. Tmux-rail test modules import these two names so unittest invokes them
# per module. Tests that build their own herdr config
# (``test_herdr_transport_wiring.py``) do NOT import this fixture and continue to
# run against the real resolver.

_TMUX_RAIL_TRANSPORT_PATCH: "mock._patch | None" = None


def setUpModule() -> None:
    """Isolate a tmux-rail test module from the workspace transport backend."""
    global _TMUX_RAIL_TRANSPORT_PATCH
    _TMUX_RAIL_TRANSPORT_PATCH = mock.patch(
        "mozyo_bridge.application.handoff_transport_wiring."
        "resolve_handoff_transport_binding",
        return_value=None,
    )
    _TMUX_RAIL_TRANSPORT_PATCH.start()


def tearDownModule() -> None:
    global _TMUX_RAIL_TRANSPORT_PATCH
    if _TMUX_RAIL_TRANSPORT_PATCH is not None:
        _TMUX_RAIL_TRANSPORT_PATCH.stop()
        _TMUX_RAIL_TRANSPORT_PATCH = None
