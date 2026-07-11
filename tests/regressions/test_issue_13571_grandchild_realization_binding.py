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
    cmd_handoff_grandchild_gate,
    cmd_handoff_grandchild_stamp,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (  # noqa: E402
    BINDING_MISMATCH,
    BINDING_REALIZED,
    BINDING_UNBOUND,
    REDACTED_UNIT_TOKEN,
    GrandchildTargetIdentity,
    redact_unit_token,
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

    def test_strong_plus_weak_codex_is_ambiguous_not_unique(self) -> None:
        # j#75480 F1: a strong codex + a weak (non-ambiguous) codex are TWO codex
        # candidates -> not a single trusted gateway. Fail closed, not realized.
        gc = self._grandchild(
            _chain(
                _gc_pane("%3", "codex"),
                _gc_pane("%4", "codex", confidence=CONFIDENCE_NONE),
            )
        )
        self.assertFalse(gc.has_codex_gateway)
        self.assertTrue(gc.ambiguous)
        target = GrandchildTargetIdentity(
            unit_id=_GC_UNIT,
            delegation_parent=_DELEG_UNIT,
            repo_identity=_canonical_repo_identity(_CHILD_REPO),
        )
        binding = resolve_realized_grandchild_binding(
            [gc], target=target, delegated_coordinator_unit=_DELEG_UNIT
        )
        self.assertNotEqual(BINDING_REALIZED, binding.outcome)

    def test_member_repo_conflict_is_ambiguous_both_orders(self) -> None:
        # j#75480 F2: the codex gateway and a sibling Claude pane on DIFFERENT
        # checkouts is a conflicted identity -> ambiguous regardless of pane order.
        for panes in (
            (_gc_pane("%3", "codex", repo_root="/ws/repo-a"),
             _gc_pane("%4", "claude", repo_root="/ws/repo-b")),
            (_gc_pane("%3", "claude", repo_root="/ws/repo-b"),
             _gc_pane("%4", "codex", repo_root="/ws/repo-a")),
        ):
            gc = self._grandchild(_chain(*panes))
            self.assertTrue(gc.ambiguous)
            target = GrandchildTargetIdentity(
                unit_id=_GC_UNIT,
                delegation_parent=_DELEG_UNIT,
                repo_identity=_canonical_repo_identity("/ws/repo-a"),
            )
            binding = resolve_realized_grandchild_binding(
                [gc], target=target, delegated_coordinator_unit=_DELEG_UNIT
            )
            self.assertNotEqual(BINDING_REALIZED, binding.outcome)


class TypedInventoryUnitEvidenceTest(unittest.TestCase):
    """#13571 j#75480 F3: typed identity must carry explicit gateway evidence.

    ``has_codex_gateway`` / ``ambiguous`` are required constructor fields, so a
    caller cannot build a positive-looking unit by omitting the evidence (the
    fail-open that a tuple->dataclass swap would otherwise re-introduce).
    """

    def test_inventory_unit_requires_gateway_and_ambiguity_fields(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (
            InventoryUnit,
        )

        with self.assertRaises(TypeError):
            InventoryUnit(  # type: ignore[call-arg]
                unit_id="ws/gc",
                lane_kind="implementation",
                delegation_depth=2,
                delegation_parent="ws/d",
                status="derived",
                repo_identity="/ws/child",
            )

    def test_explicit_no_gateway_fails_closed(self) -> None:
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (
            InventoryUnit,
        )

        unit = InventoryUnit(
            unit_id="ws/gc",
            lane_kind="implementation",
            delegation_depth=2,
            delegation_parent="ws/d",
            status="derived",
            repo_identity="/ws/child",
            has_codex_gateway=False,
            ambiguous=False,
        )
        target = GrandchildTargetIdentity(
            unit_id="ws/gc", delegation_parent="ws/d", repo_identity="/ws/child"
        )
        binding = resolve_realized_grandchild_binding(
            [unit], target=target, delegated_coordinator_unit="ws/d"
        )
        self.assertNotEqual(BINDING_REALIZED, binding.outcome)


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

    def test_home_tilde_is_expanded(self) -> None:
        # `~` must expand to the resolved home so a `~`-spelled repo compares equal
        # to its absolute form (nonblocking note in #13571 j#75480).
        import os

        home = os.path.expanduser("~")
        self.assertEqual(
            _canonical_repo_identity("~/child-project"),
            _canonical_repo_identity(f"{home}/child-project"),
        )
        self.assertNotIn("~", _canonical_repo_identity("~/child-project") or "")

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


class CanonicalGrandchildShapeTest(unittest.TestCase):
    """#13571 j#75487 R4-F1: KIND/depth is the fixed grandchild invariant.

    A caller cannot relax the acceptance shape (implementation lane at depth 2)
    to bind a non-grandchild lane; the live re-verification compares against the
    canonical constants, not caller-supplied target values.
    """

    def _unit(self, kind, depth):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (
            InventoryUnit,
        )

        return InventoryUnit(
            unit_id=_GC_UNIT,
            lane_kind=kind,
            delegation_depth=depth,
            delegation_parent=_DELEG_UNIT,
            status="derived",
            repo_identity="/ws/child",
            has_codex_gateway=True,
            ambiguous=False,
        )

    def _target(self, kind, depth):
        return GrandchildTargetIdentity(
            unit_id=_GC_UNIT,
            delegation_parent=_DELEG_UNIT,
            lane_kind=kind,
            delegation_depth=depth,
            repo_identity="/ws/child",
        )

    def test_non_grandchild_target_shape_is_exactly_unbound(self) -> None:
        # A caller aligning BOTH target and live unit to a non-grandchild shape
        # must be EXACTLY unbound (not misclassified as missing/ambiguous), the
        # prior fail-open (Redmine #13571 j#75494 R5-F2).
        for kind, depth in (
            ("coordinator", 2),
            ("delegated_coordinator", 2),
            ("implementation", 1),
            ("implementation", 3),
            ("implementation", True),
        ):
            binding = resolve_realized_grandchild_binding(
                [self._unit(kind, depth)],
                target=self._target(kind, depth),
                delegated_coordinator_unit=_DELEG_UNIT,
            )
            self.assertEqual(BINDING_UNBOUND, binding.outcome, msg=f"{kind}/{depth}")
            self.assertFalse(self._target(kind, depth).is_bindable, msg=f"{kind}/{depth}")

    def test_unbound_reason_names_the_canonical_kind_depth_condition(self) -> None:
        # The durable unbound reason must explain WHY a shape-wrong target failed
        # (not the generic missing-field text), and must not leak a host path.
        kind_binding = resolve_realized_grandchild_binding(
            [self._unit("coordinator", 2)],
            target=self._target("coordinator", 2),
            delegated_coordinator_unit=_DELEG_UNIT,
        )
        self.assertIn("KIND", kind_binding.reason)
        self.assertIn("implementation", kind_binding.reason)
        depth_binding = resolve_realized_grandchild_binding(
            [self._unit("implementation", 1)],
            target=self._target("implementation", 1),
            delegated_coordinator_unit=_DELEG_UNIT,
        )
        self.assertIn("DEPTH", depth_binding.reason)
        self.assertIn("plain int 2", depth_binding.reason)
        for reason in (kind_binding.reason, depth_binding.reason):
            self.assertNotIn("/Users", reason)
            self.assertNotIn("/home", reason)

    def test_canonical_target_but_wrong_live_kind_is_exactly_mismatch(self) -> None:
        # A canonical (bindable) target whose live unit is a coordinator lane must
        # be EXACTLY identity_mismatch — the live row is checked against the
        # canonical constant.
        binding = resolve_realized_grandchild_binding(
            [self._unit("coordinator", 2)],
            target=self._target("implementation", 2),
            delegated_coordinator_unit=_DELEG_UNIT,
        )
        self.assertEqual(BINDING_MISMATCH, binding.outcome)

    def test_canonical_target_but_wrong_live_depth_is_exactly_mismatch(self) -> None:
        binding = resolve_realized_grandchild_binding(
            [self._unit("implementation", 1)],
            target=self._target("implementation", 2),
            delegated_coordinator_unit=_DELEG_UNIT,
        )
        self.assertEqual(BINDING_MISMATCH, binding.outcome)

    def test_canonical_shape_is_exactly_realized(self) -> None:
        binding = resolve_realized_grandchild_binding(
            [self._unit("implementation", 2)],
            target=self._target("implementation", 2),
            delegated_coordinator_unit=_DELEG_UNIT,
        )
        self.assertEqual(BINDING_REALIZED, binding.outcome)


class UnitIdentityPrivacyTest(unittest.TestCase):
    """#13571 j#75501 R6-F1: a malformed unit id must not leak a private path.

    An operator typo such as an absolute ``/Users/...`` / ``/home/...`` path
    passed as ``--grandchild-unit`` fails closed (unbound), and the raw value must
    not reach the binding reason, the CLI JSON, or the pasteable gate record.
    """

    # Built from parts at runtime so the tracked source never contains a literal
    # `/Users/<name>` / `C:\\Users\\<name>` home-path shape (the tracked-file
    # secret-literal rule), while still exercising those shapes at runtime. Also
    # covers forward-slash Windows drive paths and dot components (R7-F1).
    _SYNTHETIC = (
        "/" + "Users" + "/synthetic/private/repo",
        "/" + "home" + "/synthetic/private/repo",
        "~/synthetic-private",
        "C:" + chr(92) + "Users" + chr(92) + "synthetic",
        "C:/synthetic-private",
        "D:/x",
        "C:relative",
        "ws/.",
        "ws/..",
    )

    def test_redact_unit_token_redacts_pathlike_keeps_stable(self) -> None:
        for value in self._SYNTHETIC:
            self.assertEqual(REDACTED_UNIT_TOKEN, redact_unit_token(value), msg=value)
        for stable in ("ws/gc", "mozyo/lane-1", "ws-child-project/lane-grandchild"):
            self.assertEqual(stable, redact_unit_token(stable), msg=stable)
        self.assertEqual("none", redact_unit_token(""))
        self.assertEqual("none", redact_unit_token(None))

    def test_binding_reason_does_not_leak_pathlike_unit(self) -> None:
        for value in self._SYNTHETIC:
            target = GrandchildTargetIdentity(
                unit_id=value, delegation_parent=_DELEG_UNIT, repo_identity="/ws/child"
            )
            binding = resolve_realized_grandchild_binding(
                [], target=target, delegated_coordinator_unit=_DELEG_UNIT
            )
            self.assertEqual(BINDING_UNBOUND, binding.outcome, msg=value)
            self.assertNotIn(value.strip(), binding.reason, msg=value)
            self.assertIn(REDACTED_UNIT_TOKEN, binding.reason, msg=value)

    def _gate_output(self, unit, *, as_json):
        import argparse
        import contextlib
        import io

        args = argparse.Namespace(
            delegated_coordinator_unit="mz/lane-deleg",
            grandchild_unit=unit,
            grandchild_repo="/ws/child",
            require_grandchild=True,
            parent_issue="1",
            child_issue="2",
            session=None,
            as_json=as_json,
        )
        buf = io.StringIO()
        patch = (
            "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
            "application.grandchild_stamp._discover_delegation_units"
        )
        with mock.patch(patch, return_value=[]), contextlib.redirect_stdout(buf):
            rc = cmd_handoff_grandchild_gate(args)
        return rc, buf.getvalue()

    def test_cli_json_and_gate_record_do_not_leak_pathlike_unit(self) -> None:
        for value in self._SYNTHETIC:
            for as_json in (True, False):
                rc, out = self._gate_output(value, as_json=as_json)
                self.assertEqual(3, rc, msg=f"{value}/{as_json}")  # unbound -> blocked
                self.assertNotIn(value.strip(), out, msg=f"{value}/{as_json}")
                self.assertIn(REDACTED_UNIT_TOKEN, out, msg=f"{value}/{as_json}")

    def test_valid_stable_unit_still_displayed_in_gate_record(self) -> None:
        rc, out = self._gate_output("ws-gc/lane-gc", as_json=True)
        self.assertIn("ws-gc/lane-gc", out)


class ParentIdentityStableUnitTest(unittest.TestCase):
    """#13571 j#75508 R7-F2: the delegation parent is a stable unit, never a path.

    A path-like parent / coordinator context must neither open the realization
    gate (routing safety) nor leak a raw private path into any durable surface,
    in both directions (malformed target/gate context, and malformed live parent).
    """

    _PATH_PARENT = "/" + "Users" + "/synthetic/private/delegated"

    def _unit(self, parent, *, kind="implementation", depth=2):
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.grandchild_stamp import (
            InventoryUnit,
        )

        return InventoryUnit(
            unit_id=_GC_UNIT,
            lane_kind=kind,
            delegation_depth=depth,
            delegation_parent=parent,
            status="derived",
            repo_identity="/ws/child",
            has_codex_gateway=True,
            ambiguous=False,
        )

    def _target(self, parent):
        return GrandchildTargetIdentity(
            unit_id=_GC_UNIT, delegation_parent=parent, repo_identity="/ws/child"
        )

    def test_pathlike_parent_and_context_is_unbound_no_leak(self) -> None:
        # All three parent strings equal the same path (the R7-F2 realize repro):
        # must fail closed and never leak.
        binding = resolve_realized_grandchild_binding(
            [self._unit(self._PATH_PARENT)],
            target=self._target(self._PATH_PARENT),
            delegated_coordinator_unit=self._PATH_PARENT,
        )
        self.assertEqual(BINDING_UNBOUND, binding.outcome)
        self.assertNotIn(self._PATH_PARENT, binding.reason)
        self.assertIn(REDACTED_UNIT_TOKEN, binding.reason)

    def test_pathlike_gate_context_with_valid_target_is_unbound_no_leak(self) -> None:
        # A malformed gate coordinator context fails closed even with a valid
        # target parent (the gate context is validated first).
        binding = resolve_realized_grandchild_binding(
            [self._unit("mz/lane-deleg")],
            target=self._target("mz/lane-deleg"),
            delegated_coordinator_unit=self._PATH_PARENT,
        )
        self.assertEqual(BINDING_UNBOUND, binding.outcome)
        self.assertNotIn(self._PATH_PARENT, binding.reason)
        self.assertIn(REDACTED_UNIT_TOKEN, binding.reason)

    def test_valid_target_parent_malformed_live_parent_is_mismatch_no_leak(self) -> None:
        # The other direction: a valid target/gate parent but a malformed live
        # observed parent -> mismatch, and the raw live value is redacted.
        binding = resolve_realized_grandchild_binding(
            [self._unit(self._PATH_PARENT)],
            target=self._target("mz/lane-deleg"),
            delegated_coordinator_unit="mz/lane-deleg",
        )
        self.assertEqual(BINDING_MISMATCH, binding.outcome)
        self.assertNotIn(self._PATH_PARENT, binding.reason)

    def test_valid_parent_realizes(self) -> None:
        binding = resolve_realized_grandchild_binding(
            [self._unit("mz/lane-deleg")],
            target=self._target("mz/lane-deleg"),
            delegated_coordinator_unit="mz/lane-deleg",
        )
        self.assertEqual(BINDING_REALIZED, binding.outcome)

    def test_cli_pathlike_context_blocks_and_does_not_leak(self) -> None:
        import argparse
        import contextlib
        import io

        for as_json in (True, False):
            args = argparse.Namespace(
                delegated_coordinator_unit=self._PATH_PARENT,
                grandchild_unit=_GC_UNIT,
                grandchild_repo="/ws/child",
                require_grandchild=True,
                parent_issue="1",
                child_issue="2",
                session=None,
                as_json=as_json,
            )
            buf = io.StringIO()
            patch = (
                "mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff."
                "application.grandchild_stamp._discover_delegation_units"
            )
            with mock.patch(patch, return_value=[self._unit(self._PATH_PARENT)]), contextlib.redirect_stdout(buf):
                rc = cmd_handoff_grandchild_gate(args)
            out = buf.getvalue()
            self.assertEqual(3, rc, msg=f"as_json={as_json}")
            self.assertNotIn(self._PATH_PARENT, out, msg=f"as_json={as_json}")
            self.assertIn(REDACTED_UNIT_TOKEN, out, msg=f"as_json={as_json}")


class StampProducerStableUnitTest(unittest.TestCase):
    """#13571 j#75515 R8-F1: the stamp producer enforces the stable-unit contract.

    `delegate-grandchild-stamp` (the producer of the live KIND/DEPTH/PARENT
    breadcrumb the gate reads) must fail closed on a path-like declared unit /
    parent / grandchild BEFORE any plan or tmux write, must not leak the raw
    value, and must perform zero tmux writes on invalid input.
    """

    _PATH_GC = "/" + "Users" + "/synthetic/private/gc"
    _PATH_PARENT = "/" + "Users" + "/synthetic/private/deleg"

    def _run(self, lanes, gc_unit, realization, *, adopt_reason=None,
             as_json=True, apply=False, delegated_coordinator=None):
        import argparse
        import contextlib
        import io

        args = argparse.Namespace(
            lane=lanes,
            grandchild_unit=gc_unit,
            realization=realization,
            adopt_reason=adopt_reason,
            parent_issue="1",
            child_issue="2",
            delegated_coordinator=delegated_coordinator,
            dispatch_anchor=None,
            apply=apply,
            dry_run=False,
            as_json=as_json,
        )
        buf = io.StringIO()
        rc = None
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            try:
                rc = cmd_handoff_grandchild_stamp(args)
            except SystemExit as exc:  # die() on fail-closed
                rc = f"die:{exc.code}"
        return rc, buf.getvalue()

    def _chain(self, *, gc_unit, parent="mz/d"):
        return [
            "kind=coordinator,unit=gk/p,parent=-",
            f"kind=delegated_coordinator,unit={parent},parent=gk/p",
            f"kind=implementation,unit={gc_unit},parent={parent},pane=%3",
        ]

    def test_pathlike_grandchild_fails_closed_no_leak(self) -> None:
        for realization, ar in (("launch", None), ("adopt", "because")):
            rc, out = self._run(
                self._chain(gc_unit=self._PATH_GC), self._PATH_GC, realization,
                adopt_reason=ar,
            )
            self.assertTrue(str(rc).startswith("die:"), msg=f"{realization}: {rc}")
            self.assertNotIn(self._PATH_GC, out, msg=realization)
            self.assertIn(REDACTED_UNIT_TOKEN, out, msg=realization)

    def test_pathlike_parent_fails_closed_no_leak(self) -> None:
        for realization, ar in (("launch", None), ("adopt", "because")):
            rc, out = self._run(
                self._chain(gc_unit="mz/gc", parent=self._PATH_PARENT), "mz/gc",
                realization, adopt_reason=ar,
            )
            self.assertTrue(str(rc).startswith("die:"), msg=f"{realization}: {rc}")
            self.assertNotIn(self._PATH_PARENT, out, msg=realization)

    def test_drive_dot_multisegment_unit_fails_closed(self) -> None:
        for bad in ("C:/x", "ws/.", "ws/..", "ws/a/b"):
            rc, out = self._run(self._chain(gc_unit=bad), bad, "launch")
            self.assertTrue(str(rc).startswith("die:"), msg=f"{bad}: {rc}")
            self.assertNotIn(bad, out, msg=bad)

    def test_record_only_delegated_coordinator_path_is_redacted(self) -> None:
        # A valid chain with a path-like record-only --delegated-coordinator:
        # the plan succeeds but the override is redacted (never leaked).
        rc, out = self._run(
            self._chain(gc_unit="mz/gc"), "mz/gc", "launch",
            delegated_coordinator=self._PATH_PARENT,
        )
        self.assertEqual(0, rc)
        self.assertNotIn(self._PATH_PARENT, out)
        self.assertIn(REDACTED_UNIT_TOKEN, out)

    def test_all_valid_chain_still_succeeds(self) -> None:
        rc, out = self._run(self._chain(gc_unit="mz/gc"), "mz/gc", "launch")
        self.assertEqual(0, rc)
        self.assertIn("mz/gc", out)

    def test_apply_invalid_input_performs_zero_tmux_writes(self) -> None:
        tmux = "mozyo_bridge.e_110_execution_platform.f_130_handoff_routing.infrastructure.tmux_client"
        with mock.patch(f"{tmux}.run_tmux") as run_tmux, mock.patch(f"{tmux}.require_tmux"):
            rc, out = self._run(
                self._chain(gc_unit=self._PATH_GC), self._PATH_GC, "launch", apply=True
            )
            self.assertTrue(str(rc).startswith("die:"))
            self.assertEqual(0, run_tmux.call_count)
            self.assertNotIn(self._PATH_GC, out)


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
