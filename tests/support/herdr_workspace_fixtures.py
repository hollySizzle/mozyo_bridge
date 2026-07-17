"""Anchored, ambient-free repo roots for herdr workspace identity (Redmine #13924).

The herdr retire / dispatch surfaces mint their pair's identity from a repo root:
:func:`herdr_workspace_segment` reads that checkout's workspace anchor (and, for a linked
worktree, the main checkout's home-registry row). A fixture that hands them the *source
checkout* therefore inherits whatever the operator happened to register — an untracked
anchor, a registry row. Locally the segment resolves and the tests pass; on a fresh CI
checkout it resolves to ``""`` and every identity-derived assertion dies in setUp (89 tests,
GitHub Actions run ``29555154225`` — #13924 j#80691).

So the fixture owns its root. :func:`anchored_repo_root` builds a throwaway directory and
writes a workspace anchor with the **production writer**, which makes the identity a fact
the fixture states rather than an inference from ambient user state. The production resolver
still runs, over a real anchor, on its real read path — so an identity-resolution regression
still fails the tests that use this. The ambient coupling is removed; the coverage is not.

This is deliberately NOT a patched resolver: stubbing ``herdr_workspace_segment`` to return a
constant would also be hermetic, but it retires the resolver from the tests that depend on
it, and the retire rail's identity resolution is exactly what these regressions pin.
"""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from mozyo_bridge.core.state.workspace_registry import WorkspaceRecord, write_anchor
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    herdr_workspace_segment,
)

#: The identity every anchored fixture root resolves to. Shaped like a real workspace id
#: (the project-identity slug of #13377, hyphens included) so the assigned-name codec's
#: field escaping runs exactly as it does in production — but owned by the tests, so it
#: names no operator workspace.
FIXTURE_WORKSPACE_ID = "fixture-13892-workspace"

#: Anchor payload constants. ``read_anchor`` only accepts a structurally valid anchor
#: (supported schema version, non-empty id, tmux-safe canonical session), so these are the
#: fixture's half of that contract. Fixed timestamps keep the anchor byte-stable across runs.
_FIXTURE_CANONICAL_SESSION = "fixture_13892"
_FIXTURE_PROJECT_NAME = "fixture-project"
_FIXTURE_TIMESTAMP = "2026-01-01T00:00:00Z"


def _anchor_record(workspace_id: str, root: Path) -> WorkspaceRecord:
    return WorkspaceRecord(
        workspace_id=workspace_id,
        canonical_path=str(root),
        display_path=str(root),
        project_name=_FIXTURE_PROJECT_NAME,
        canonical_session=_FIXTURE_CANONICAL_SESSION,
        preset=None,
        preset_version=None,
        created_at=_FIXTURE_TIMESTAMP,
        updated_at=_FIXTURE_TIMESTAMP,
        last_seen=None,
    )


def anchored_repo_root(
    case: unittest.TestCase, *, workspace_id: str = FIXTURE_WORKSPACE_ID
) -> Path:
    """A throwaway repo root whose workspace anchor resolves to ``workspace_id``.

    Registered for cleanup on ``case``. The root is a plain directory — not a git checkout
    — so the resolver takes its standalone/anchor branch and never reads the home registry:
    no user registration, no untracked anchor of the source tree, participates.

    Fails loudly (rather than handing back a root the surfaces would resolve differently) if
    the production resolver does not return the anchored identity — e.g. when ``TMPDIR``
    itself sits inside a git worktree, where the resolver would inherit that checkout's
    ambient identity instead.
    """
    root = Path(tempfile.mkdtemp()).resolve()
    case.addCleanup(shutil.rmtree, root, True)
    write_anchor(root, _anchor_record(workspace_id, root))
    resolved = herdr_workspace_segment(root)
    if resolved != workspace_id:
        raise RuntimeError(
            f"the anchored fixture root {root} resolved to {resolved!r}, not the anchored "
            f"{workspace_id!r}; the fixture cannot state an identity the product resolver "
            "does not agree with (is TMPDIR inside a git worktree?)"
        )
    return root
