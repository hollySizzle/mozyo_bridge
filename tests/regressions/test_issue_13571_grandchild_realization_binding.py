"""Regression pins for the #13571 grandchild realization binding remediation.

Redmine #13571 (parent US #12454). Pins the confirmed failure shapes the
round-2 re-review (#13571 j#75473) surfaced, so they can never silently regress:

- **F1 live producer**: the delegation-unit discovery must read the real
  ``TargetCandidate.role`` (the projection returned by ``_agents_target_candidates``
  uses ``role``, not ``agent_kind``) and resolve the route-bound grandchild only
  when exactly one strong / non-ambiguous ``role==codex`` gateway pane is present.
  A prior build read a nonexistent ``agent_kind`` field, so ``has_codex_gateway``
  was always ``False`` and the live happy path always blocked.
- **F4 public-catalog resolver contract**: the realization CLI parser path must
  resolve the block-severity ``fc-delegated-coordinator-runtime-source`` in the
  PUBLIC-only catalog view (``include_local=False`` — the fresh-clone / CI view),
  and the critical delegated-route governance docs must be reachable, so a future
  catalog edit that drops the parser path fails a test.

Per tests-placement policy a confirmed #13571 defect pin lives in
``tests/regressions/test_issue_13571_*.py``. Abstract placeholder identities only
(no private home paths) per the public/private boundary rule.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

_TESTS_ROOT = Path(__file__).resolve().parents[1]
ROOT = _TESTS_ROOT.parent
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))
_SRC = ROOT / "src"
if _SRC.is_dir() and str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from mozyo_bridge.e_110_execution_platform.f_120_agent_discovery_pane_resolution.domain.agent_discovery import (  # noqa: E402
    CONFIDENCE_NONE,
    CONFIDENCE_STRONG,
    TargetCandidate,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.grandchild_stamp import (  # noqa: E402
    _canonical_repo_identity,
    _discover_delegation_units,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (  # noqa: E402
    BINDING_REALIZED,
    GrandchildTargetIdentity,
    resolve_realized_grandchild_binding,
)

_CHILD_REPO = "/workspace/child-project"
_DELEG_UNIT = "mz/lane-deleg"
_GC_UNIT = "ws-gc/lane-gc"

# Patch targets are the lazily-imported names inside `_discover_delegation_units`.
_CANDS_PATCH = "mozyo_bridge.application.commands._agents_target_candidates"
_TMUX_PATCH = (
    "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing."
    "infrastructure.tmux_client.require_tmux"
)


def _tc(
    *,
    pane_id,
    role,
    workspace_id,
    lane_id,
    lane_kind,
    delegation_parent,
    repo_root=_CHILD_REPO,
    confidence=CONFIDENCE_STRONG,
    ambiguous=False,
):
    """A real ``TargetCandidate`` (the projection the discovery pipeline emits)."""
    return TargetCandidate(
        pane_id=pane_id,
        role=role,
        role_source="process",
        confidence=confidence,
        ambiguous=ambiguous,
        session="s",
        window_name="w",
        window_index="0",
        pane_index="0",
        active=False,
        workspace_id=workspace_id,
        workspace_label=None,
        lane_id=lane_id,
        lane_label=None,
        repo_short=None,
        repo_root=repo_root,
        cwd=repo_root or "",
        host="local",
        view_kind="cockpit_pane",
        branch=None,
        lane_kind=lane_kind,
        delegation_parent=delegation_parent,
    )


def _chain(*grandchild_panes):
    """A parent->delegated->grandchild chain; grandchild panes passed explicitly.

    The parent coordinator and delegated coordinator each carry a codex pane so
    the delegation tree derives depth-2 for the grandchild; the grandchild's panes
    are supplied by the test to exercise the gateway-role resolution.
    """
    return [
        _tc(pane_id="%1", role="codex", workspace_id="gk", lane_id="p",
            lane_kind="coordinator", delegation_parent=""),
        _tc(pane_id="%2", role="codex", workspace_id="mz", lane_id="lane-deleg",
            lane_kind="delegated_coordinator", delegation_parent="gk/p"),
        *grandchild_panes,
    ]


def _gc_pane(pane_id, role, *, confidence=CONFIDENCE_STRONG, ambiguous=False, repo_root=_CHILD_REPO):
    return _tc(
        pane_id=pane_id,
        role=role,
        workspace_id="ws-gc",
        lane_id="lane-gc",
        lane_kind="implementation",
        delegation_parent=_DELEG_UNIT,
        confidence=confidence,
        ambiguous=ambiguous,
        repo_root=repo_root,
    )


class LiveProducerGatewayResolutionTest(unittest.TestCase):
    """#13571 j#75473 F1: the producer reads real `TargetCandidate.role`."""

    def _units(self, candidates):
        with mock.patch(_CANDS_PATCH, return_value=candidates), mock.patch(_TMUX_PATCH):
            import argparse

            return _discover_delegation_units(argparse.Namespace(agent=None, session=None))

    def _grandchild(self, candidates):
        units = {u.unit_id: u for u in self._units(candidates)}
        return units.get(_GC_UNIT)

    def test_strong_codex_gateway_resolves_and_realizes(self) -> None:
        gc = self._grandchild(_chain(_gc_pane("%3", "codex")))
        self.assertIsNotNone(gc)
        self.assertTrue(gc.has_codex_gateway)
        self.assertFalse(gc.ambiguous)
        self.assertEqual("implementation", gc.lane_kind)
        self.assertEqual(2, gc.delegation_depth)
        # And it re-verifies end to end through the real binding.
        target = GrandchildTargetIdentity(
            unit_id=_GC_UNIT,
            delegation_parent=_DELEG_UNIT,
            repo_identity=_canonical_repo_identity(_CHILD_REPO),
        )
        binding = resolve_realized_grandchild_binding(
            self._units(_chain(_gc_pane("%3", "codex"))),
            target=target,
            delegated_coordinator_unit=_DELEG_UNIT,
        )
        self.assertEqual(BINDING_REALIZED, binding.outcome)
        self.assertEqual(_GC_UNIT, binding.matched_unit)

    def test_claude_only_unit_has_no_gateway(self) -> None:
        # The codex gateway vanished (only a Claude worker remains): not route-bound.
        gc = self._grandchild(_chain(_gc_pane("%3", "claude")))
        self.assertIsNotNone(gc)
        self.assertFalse(gc.has_codex_gateway)

    def test_two_strong_codex_gateways_is_ambiguous(self) -> None:
        gc = self._grandchild(
            _chain(_gc_pane("%3", "codex"), _gc_pane("%4", "codex"))
        )
        self.assertFalse(gc.has_codex_gateway)
        self.assertTrue(gc.ambiguous)

    def test_weak_codex_gateway_is_not_route_bound(self) -> None:
        gc = self._grandchild(
            _chain(_gc_pane("%3", "codex", confidence=CONFIDENCE_NONE))
        )
        self.assertFalse(gc.has_codex_gateway)

    def test_gateway_repo_is_its_own_not_synthesized_from_sibling(self) -> None:
        # A repo-unknown codex gateway must NOT inherit the Claude sibling's repo.
        gc = self._grandchild(
            _chain(
                _gc_pane("%3", "codex", repo_root=None),
                _gc_pane("%4", "claude", repo_root=_CHILD_REPO),
            )
        )
        self.assertTrue(gc.has_codex_gateway)
        self.assertIsNone(gc.repo_identity)


class RepoIdentityCanonicalParityTest(unittest.TestCase):
    """#13571 j#75473 F3: repo identity uses one canonical form.

    A `.` / `..` / `~` / NFC-vs-NFD spelling of the same checkout must resolve to
    the same identity, so it never reads as a mismatch and the binding does not
    false-negative on a spelling difference.
    """

    def test_dot_and_parent_spellings_canonicalize_equal(self) -> None:
        self.assertEqual(
            _canonical_repo_identity("/workspace/child-project"),
            _canonical_repo_identity("/workspace/./child-project"),
        )
        self.assertEqual(
            _canonical_repo_identity("/workspace/child-project"),
            _canonical_repo_identity("/workspace/sibling/../child-project"),
        )

    def test_trailing_slash_canonicalizes_equal(self) -> None:
        self.assertEqual(
            _canonical_repo_identity("/workspace/child-project"),
            _canonical_repo_identity("/workspace/child-project/"),
        )

    def test_nfc_and_nfd_spellings_canonicalize_equal(self) -> None:
        import unicodedata

        base = "/workspace/プロジェクト"  # decomposable (dakuten) katakana
        nfc = unicodedata.normalize("NFC", base)
        nfd = unicodedata.normalize("NFD", base)
        self.assertEqual(
            _canonical_repo_identity(nfc),
            _canonical_repo_identity(nfd),
        )

    def test_binding_realizes_across_repo_spelling(self) -> None:
        # The live gateway resolves the canonical repo; an operator-supplied
        # `.`-spelling of the same repo still re-verifies (no false mismatch).
        gc = None
        for u in _discover_units_dotspelled():
            if u.unit_id == _GC_UNIT:
                gc = u
        self.assertIsNotNone(gc)
        target = GrandchildTargetIdentity(
            unit_id=_GC_UNIT,
            delegation_parent=_DELEG_UNIT,
            repo_identity=_canonical_repo_identity("/workspace/./child-project"),
        )
        binding = resolve_realized_grandchild_binding(
            [gc], target=target, delegated_coordinator_unit=_DELEG_UNIT
        )
        self.assertEqual(BINDING_REALIZED, binding.outcome)


def _discover_units_dotspelled():
    candidates = _chain(_gc_pane("%3", "codex", repo_root="/workspace/child-project"))
    with mock.patch(_CANDS_PATCH, return_value=candidates), mock.patch(_TMUX_PATCH):
        import argparse

        return _discover_delegation_units(argparse.Namespace(agent=None, session=None))


class PublicCatalogResolverContractTest(unittest.TestCase):
    """#13571 j#75473 F4: pin the PUBLIC-only resolver view for the parser path.

    Uses ``include_local=False`` (the fresh-clone / CI view) and asserts both the
    matched block convention AND the resolved documents, and does not skip on a
    missing in-repo resolver (the tooling is mandatory for this repo).
    """

    _RUNTIME_FC = "fc-delegated-coordinator-runtime-source"
    _PARSER_PATH = (
        "src/mozyo_bridge/e_110_execution_platform/"
        "f_140_delegated_coordinator_nested_handoff/application/"
        "cli_handoff_grandchild_realization.py"
    )
    _CRITICAL_DOC_IDS = (
        "spec-delegated-route-live-executor",
        "spec-route-identity-ledger",
    )

    def _resolve(self, path):
        from mozyo_bridge.docs_tools import CatalogContext, resolve_paths

        context = CatalogContext.build(str(ROOT), None)
        # include_local=False -> the public catalog view a fresh clone / CI sees.
        return resolve_paths(context, [path], include_local=False)[0]

    def test_parser_path_resolves_block_convention_public_only(self) -> None:
        entry = self._resolve(self._PARSER_PATH)
        conventions = {
            fc["id"]: fc for fc in entry.get("matched_file_conventions", [])
        }
        self.assertIn(
            self._RUNTIME_FC,
            conventions,
            msg=(
                "the grandchild realization CLI parser dropped out of "
                f"`{self._RUNTIME_FC}` in the PUBLIC catalog view; docs resolver "
                "would fall through to the generic package warn only."
            ),
        )
        self.assertEqual("block", conventions[self._RUNTIME_FC]["severity"])

    def test_parser_path_surfaces_critical_delegated_route_docs(self) -> None:
        entry = self._resolve(self._PARSER_PATH)
        resolved_doc_ids = {doc["id"] for doc in entry.get("documents", [])}
        for doc_id in self._CRITICAL_DOC_IDS:
            self.assertIn(
                doc_id,
                resolved_doc_ids,
                msg=f"{doc_id} not surfaced for the realization parser path",
            )

    def test_sibling_runtime_modules_also_block_public_only(self) -> None:
        for path in (
            "src/mozyo_bridge/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/domain/grandchild_stamp.py",
            "src/mozyo_bridge/e_110_execution_platform/"
            "f_140_delegated_coordinator_nested_handoff/application/grandchild_stamp.py",
        ):
            entry = self._resolve(path)
            conventions = {
                fc["id"]: fc for fc in entry.get("matched_file_conventions", [])
            }
            self.assertIn(self._RUNTIME_FC, conventions, msg=path)
            self.assertEqual("block", conventions[self._RUNTIME_FC]["severity"])


if __name__ == "__main__":
    unittest.main()
