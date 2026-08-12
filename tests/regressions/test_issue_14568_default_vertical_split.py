"""Regression pins for Redmine #14568 — the undeclared pair splits vertically.

Owner intent 2026-07-27 (+ scope amendment j#89848): a workspace that declares no
``lane_placement`` had its coordinator pair AND its sublane gateway/worker pair rendered
side by side. That was not an accident — #13646 deliberately froze the undeclared launch
byte-for-byte (the ``default`` lane emitted no ``--split`` and delegated to herdr's own
default, a ``sublane`` emitted the literal ``--split right``), and herdr's own default is
``right`` (live-measured j#76622), so both pairs came out horizontal unless every adopter
wrote the same block. #14568 moves that decision into the product.

What these pins characterize, stated as the behaviour rather than the implementation:

1. **the product default is ``down`` on BOTH lane classes** — the thing owner intent asked
   for, and the thing #13646 pinned the other way;
2. **the upper pane is deterministic** — the pair's OCCUPANT is whoever launches first, and
   the coordinator pair gets a product-default order of ``(codex, claude)`` so it does not
   inherit ``DEFAULT_EXPECTED_AGENTS``' claude-first launch order. The sublane deliberately
   gets NO product-default order: it already launches ``(gateway, worker)`` from the role
   binding, and imposing one would override a rebound binding instead of respecting it;
3. **``split: right`` is the rollback**, per lane class and per lane kind, and
   ``by_lane_kind > lane_class > product default`` still holds in that order;
4. **the safety contracts are untouched** — a single-provider request never gains a peer, a
   heal never focuses / moves / closes the live sibling, a partial launch still reports the
   failure, and the root / tab-root pane is still reclaimed;
5. **``config status`` reports what the launch will do**, sourced ``default`` when the
   operator declared nothing.

The fix lives in ``lane_placement.py`` (``product_default_placement`` +
``LanePlacementConfig.resolve_effective``), ``herdr_lane_topology.py`` (the effective
policy adapter and the focus policy, now keyed on the effective split direction rather than
on "did the operator declare a block"), and ``repo_local_config_status.py`` (the leaf rows).

Herdr 0.8 makes the split target explicit: mozyo first runs ``pane split <anchor>
--direction down`` and then binds the provider with ``agent start --pane``. The historical
active-pane/focus failure is retained below as a negative geometry control, but production
assertions now pin the exact anchor and are taken after root reclaim.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LanePlacementConfig,
    ResolvedPlacement,
    product_default_placement,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E501
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    classify_config_sources,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E501
    resolve_placement_policy_for_role,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_lane_launch_context import (  # noqa: E501
    LaneLaunchContext,
)

# The two herdr fakes live with the unit suite that introduced them (#13646 / R1-F1).
# `_LayoutHerdr` models the live-measured layout semantics — active-pane splitting,
# implicit `right`, focus, and split collapse on close — which is the only way to assert
# what the operator SEES rather than what the argv says. Importing them keeps this file a
# pin on the behaviour instead of a second, subtly different simulation of herdr. The
# harness below is local on purpose: subclassing the unit TestCases would re-run their
# whole suite here and blur which module a failure belongs to.
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_session_start import (  # noqa: E501
    _Herdr,
    _LayoutHerdr,
    _launch_env,
)

ISSUE = "14568"


def _prepare(
    tmp,
    *,
    herdr,
    providers,
    lane,
    lane_placement=None,
    launch_context=None,
    rows=None,
):
    """Run the real ``prepare_session`` against a fake herdr in an isolated home.

    Returns ``(result, workspace_id, {provider: pane_locator})`` — the pane map is what
    the layout assertions key on, since a pane id is the only handle
    ``_LayoutHerdr.direction_between`` understands.
    """
    repo = Path(tmp) / "repo"
    repo.mkdir()
    home = Path(tmp) / "home"
    home.mkdir()
    binpath = Path(tmp) / "fake-herdr"
    binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
        register_workspace(repo, home=home)
        workspace_id = read_anchor(repo)["workspace_id"]
        if rows is not None:
            herdr.existing_rows = rows(workspace_id)
        result = prepare_session(
            repo_root=repo,
            providers=providers,
            lane_id=lane,
            env=_launch_env(binpath),
            runner=herdr.run,
            lane_placement=lane_placement,
            launch_context=launch_context,
        )
    return result, workspace_id, {s.provider: s.locator for s in result.slots}


def _second_split(herdr) -> str:
    second = herdr.pane_splits[1]
    return second[second.index("--direction") + 1]


def _launched_role(start_argv) -> str:
    """The provider selected explicitly by a Herdr 0.8 ``agent start`` argv."""
    return start_argv[start_argv.index("--kind") + 1]


class ProductDefaultPolicyTest(unittest.TestCase):
    """The policy value itself, and the precedence it sits at the bottom of."""

    def test_both_lane_classes_default_to_down(self) -> None:
        for lane_class in ("default", "sublane"):
            with self.subTest(lane_class=lane_class):
                self.assertEqual(product_default_placement(lane_class).split, "down")

    def test_only_the_default_lane_gets_a_product_default_order(self) -> None:
        # The asymmetry is the deliberate part: the coordinator pair's launch order is a
        # fixed topology this policy must correct, while a lane's order comes from the
        # role binding and must be left alone.
        self.assertEqual(
            product_default_placement("default").order, ("codex", "claude")
        )
        self.assertIsNone(product_default_placement("sublane").order)

    def test_an_undeclared_config_resolves_to_the_product_default(self) -> None:
        config = LanePlacementConfig.default()
        for lane_class in ("default", "sublane"):
            with self.subTest(lane_class=lane_class):
                self.assertEqual(
                    resolve_placement_policy_for_role(config, lane_class, None),
                    (
                        product_default_placement(lane_class).split,
                        product_default_placement(lane_class).order,
                        # The adapter grew a third field with Redmine #14569; comparing the
                        # WHOLE tuple against the product default keeps this test a
                        # statement about the fallback rather than about two chosen fields.
                        product_default_placement(lane_class).ratio,
                    ),
                )

    def test_a_declared_class_wins_over_the_product_default(self) -> None:
        config = LanePlacementConfig.from_record({"sublane": {"split": "right"}})
        self.assertEqual(
            resolve_placement_policy_for_role(config, "sublane", None)[0], "right"
        )
        # ...and does not leak into the other class.
        self.assertEqual(
            resolve_placement_policy_for_role(config, "default", None)[0], "down"
        )

    def test_a_declared_kind_wins_over_a_declared_class(self) -> None:
        # Three distinguishable layers in one config: kind says `right`, class says `down`
        # (which happens to equal the product default, so the class layer is only visible
        # when the kind does NOT apply — hence the second assertion's `None` kind).
        config = LanePlacementConfig.from_record(
            {
                "by_lane_kind": {"implementation": {"split": "right"}},
                "sublane": {"split": "down"},
            }
        )
        self.assertEqual(
            resolve_placement_policy_for_role(config, "sublane", "implementation")[0],
            "right",
        )
        self.assertEqual(
            resolve_placement_policy_for_role(config, "sublane", "coordinator")[0],
            "down",
        )

    def test_the_empty_policy_is_a_declaration_state_not_a_preserved_behaviour(self) -> None:
        # R1-F1 (review j#90277) was a documentation defect of exactly this shape: several
        # schema / call-site docstrings still called the empty policy "behavior-preserving".
        # Pin the claim itself as a runtime fact so the prose has something to be wrong
        # against: `default()` declares nothing (both tables empty) AND resolves to a
        # geometry that is NOT the pre-#14568 launch (`sublane` was `right`, `default` was
        # no `--split` at all).
        empty = LanePlacementConfig.default()
        self.assertEqual((empty.placements, empty.kind_placements), ((), ()))
        self.assertEqual(empty.resolve("sublane"), ResolvedPlacement())
        self.assertEqual(empty.resolve_effective("sublane").split, "down")
        self.assertEqual(empty.resolve_effective("default").split, "down")
        # ...and the same geometry holds for a present-but-empty block. Note it is NOT the
        # same parse: `{}` records a class entry with both FIELDS undeclared, so the
        # distinction that matters is per-field, not per-block. `{}` is not a rollback.
        parsed = LanePlacementConfig.from_record({"sublane": {}, "default": {}})
        self.assertEqual(
            parsed.placements,
            (("default", None, None, None), ("sublane", None, None, None)),
        )
        self.assertEqual(parsed.resolve("sublane"), ResolvedPlacement())
        for lane_class in ("default", "sublane"):
            self.assertEqual(
                parsed.resolve_effective(lane_class), empty.resolve_effective(lane_class)
            )

    def test_no_config_object_at_all_still_resolves_the_product_default(self) -> None:
        # A caller that hands the chokepoint no policy must not silently reach a DIFFERENT
        # geometry than one that hands it an empty policy — that divergence is exactly how
        # a "default" ends up applying on some launch paths and not others.
        for lane_class in ("default", "sublane"):
            with self.subTest(lane_class=lane_class):
                self.assertEqual(
                    resolve_placement_policy_for_role(None, lane_class, None),
                    resolve_placement_policy_for_role(
                        LanePlacementConfig.default(), lane_class, None
                    ),
                )


class UndeclaredPairLandsVerticalTest(unittest.TestCase):
    """The close conditions at the layout layer with an unbound root preserved."""

    def test_undeclared_default_pair_is_down_with_codex_on_top(self) -> None:
        herdr = _LayoutHerdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            # Deliberately the claude-first request the real bare-`mozyo` topology makes,
            # so a missing product-default order would show up as claude on top.
            _, _, panes = _prepare(
                tmp, herdr=herdr, providers=["claude", "codex"], lane=""
            )
        self.assertEqual(herdr.pane_closes, [], "an unbound root has no close authority")
        self.assertEqual(herdr.direction_between(panes["codex"], panes["claude"]), "down")
        self.assertEqual(_launched_role(herdr.start_argvs[0]), "codex")

    def test_undeclared_sublane_pair_is_down_with_the_gateway_on_top(self) -> None:
        herdr = _LayoutHerdr(created_workspace="wZ", created_tab="wZ:t1")
        with tempfile.TemporaryDirectory() as tmp:
            _, _, panes = _prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane="lane-1"
            )
        self.assertEqual(herdr.direction_between(panes["codex"], panes["claude"]), "down")
        self.assertEqual(_launched_role(herdr.start_argvs[0]), "codex")

    def test_splitting_the_second_pane_from_root_would_lose_the_direction(self) -> None:
        # Herdr 0.8 removed the implicit active-pane dependency, but the anchor still
        # matters: deliberately splitting both panes from the disposable root makes the
        # inner `down` collapse when that root is reclaimed. This negative control keeps
        # the final-layout assertions above from passing on argv alone.
        herdr = _LayoutHerdr(created_workspace="wZ")

        def call(*tail):
            return herdr.run(
                ["/fake-herdr", *tail],
                capture_output=True,
                text=True,
                timeout=5,
                env={},
            )

        call("workspace", "create", "--cwd", "/x", "--no-focus")
        first = call(
            "pane", "split", "wZ:p1", "--direction", "right",
            "--cwd", "/x", "--no-focus",
        )
        second = call(
            "pane", "split", "wZ:p1", "--direction", "down",
            "--cwd", "/x", "--no-focus",
        )
        root = "wZ:p1"
        a1 = json.loads(first.stdout)["result"]["pane"]["pane_id"]
        a2 = json.loads(second.stdout)["result"]["pane"]["pane_id"]
        call("pane", "close", root)
        self.assertEqual(herdr.direction_between(a1, a2), "right")


class RollbackAndSafetyContractsTest(unittest.TestCase):
    """The rollback, and the contracts the new default must not disturb."""

    def test_explicit_right_rolls_back_each_lane_class(self) -> None:
        for lane, lane_class, tab in (("", "default", None), ("lane-1", "sublane", "wZ:t1")):
            with self.subTest(lane_class=lane_class):
                herdr = _Herdr(created_workspace="wZ", created_tab=tab)
                with tempfile.TemporaryDirectory() as tmp:
                    _prepare(
                        tmp,
                        herdr=herdr,
                        providers=["codex", "claude"],
                        lane=lane,
                        lane_placement=LanePlacementConfig.from_record(
                            {lane_class: {"split": "right"}}
                        ),
                    )
                self.assertEqual(_second_split(herdr), "right")

    def test_by_lane_kind_right_rolls_back_one_kind_only(self) -> None:
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1")
        with tempfile.TemporaryDirectory() as tmp:
            _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-1",
                lane_placement=LanePlacementConfig.from_record(
                    {"by_lane_kind": {"implementation": {"split": "right"}}}
                ),
                launch_context=LaneLaunchContext(lane_kind="implementation"),
            )
        self.assertEqual(_second_split(herdr), "right")

    def test_an_unmatched_lane_kind_keeps_the_product_default(self) -> None:
        # The other side of the same rollback: rolling back ONE kind leaves every other
        # lane on `down`, so a kind-scoped rollback cannot silently become a global one.
        herdr = _Herdr(created_workspace="wZ", created_tab="wZ:t1")
        with tempfile.TemporaryDirectory() as tmp:
            _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-1",
                lane_placement=LanePlacementConfig.from_record(
                    {"by_lane_kind": {"coordinator": {"split": "right"}}}
                ),
                launch_context=LaneLaunchContext(lane_kind="implementation"),
            )
        self.assertEqual(_second_split(herdr), "down")

    def test_single_provider_request_stays_single_and_never_splits_first(self) -> None:
        # The product-default `order` names both providers; a single-provider request must
        # not grow a peer. Herdr 0.8 still prepares exactly one pane for that one process;
        # there is no second, pair-forming split.
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _prepare(tmp, herdr=herdr, providers=["claude"], lane="")
        self.assertEqual(len(herdr.start_argvs), 1)
        self.assertEqual(len(herdr.pane_splits), 1)
        self.assertEqual([s.provider for s in result.slots], ["claude"])

    def test_heal_splits_down_beside_the_live_sibling_and_moves_nothing(self) -> None:
        # An undeclared default-lane heal: the relaunched slot joins the live sibling with
        # the product-default direction, and the live pane is never focused, closed, or
        # moved (Non-goal: no live relayout / no implicit re-placement of a live pair).
        herdr = _Herdr()
        with tempfile.TemporaryDirectory() as tmp:
            _prepare(
                tmp,
                herdr=herdr,
                providers=["claude"],
                lane="",
                rows=lambda ws: [
                    {"name": encode_assigned_name(ws, "codex", ""), "pane_id": "w5:pC"}
                ],
            )
        split = herdr.pane_splits[0]
        self.assertEqual(split[2], "w5:pC")
        self.assertEqual(split[split.index("--direction") + 1], "down")
        self.assertNotIn("--focus", split)
        self.assertEqual(herdr.pane_closes, [])

    def test_an_undeclared_fresh_pair_preserves_the_unbound_root_pane(self) -> None:
        herdr = _Herdr(created_workspace="wZ")
        with tempfile.TemporaryDirectory() as tmp:
            result, _, _ = _prepare(
                tmp, herdr=herdr, providers=["codex", "claude"], lane=""
            )
        self.assertEqual(herdr.pane_closes, [])
        self.assertFalse(result.base_pane_reclaimed)
        self.assertIn("generation_unproven_root_preserved", result.base_pane_detail)

    def test_a_failing_launch_still_fails_closed_and_leaves_the_root_pane(self) -> None:
        # The product default runs before the first launch and must not disturb the
        # fail-closed contract: a provider failure still raises, and the created root pane
        # is left as visible residue rather than blindly reclaimed (#13330).
        herdr = _Herdr(created_workspace="wZ", start_fails=True)
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError):
                _prepare(tmp, herdr=herdr, providers=["codex", "claude"], lane="")
        self.assertEqual(len(herdr.workspace_creates), 1)
        self.assertEqual(herdr.pane_closes, [])


class StatusProjectionTest(unittest.TestCase):
    """`config status` must state the geometry a fresh launch will actually take."""

    def test_undeclared_reports_down_sourced_default(self) -> None:
        statuses = {
            s.key: s
            for s in classify_config_sources(
                raw_record=None,
                config=RepoLocalConfig.default(),
                schema_version=2,
                legacy_migratable=False,
            )
        }
        for lane_class in ("default", "sublane"):
            row = statuses[f"lane_placement.{lane_class}.split"]
            self.assertEqual((row.effective_value, row.source), ("down", SOURCE_DEFAULT))

    def test_a_rollback_reports_right_sourced_declared(self) -> None:
        record = {"version": 2, "lane_placement": {"sublane": {"split": "right"}}}
        statuses = {
            s.key: s
            for s in classify_config_sources(
                raw_record=record,
                config=RepoLocalConfig.from_record(record),
                schema_version=2,
                legacy_migratable=False,
            )
        }
        row = statuses["lane_placement.sublane.split"]
        self.assertEqual((row.effective_value, row.source), ("right", SOURCE_DECLARED))


_REPO_ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root

#: The docs that tell an operator what to WRITE into ``.mozyo-bridge/config.yaml`` for pane
#: geometry. Scope is deliberately exactly these two and is NOT widened to every doc that
#: mentions the block: a doc that narrates the pre-#13646 rename (e.g.
#: ``vibes/docs/logics/coordinator-autonomy-evaluation.md``, which records that
#: ``pane_placement`` collided with the hostile-checkout key filter and became
#: ``lane_placement``) quotes the rejected spelling CORRECTLY, as history. A guard that
#: forbade the token outright would break accurate records. What this guard does not cover,
#: it does not claim to cover.
_CONFIG_GUIDANCE_DOCS = (
    "vibes/docs/logics/herdr-live-relayout-runbook.md",
    "vibes/docs/specs/herdr-native-identity.md",
)

#: An inline-code token shaped like a top-level repo-local config key ending in
#: ``_placement``. Python identifiers that merely CONTAIN "placement"
#: (``resolve_placement_policy_for_role``, ``evaluate_mutation_placement_gate``) do not
#: match, so the guard only ever judges things written in config-key shape.
_CONFIG_KEY_TOKEN = re.compile(r"`([a-z][a-z0-9]*(?:_[a-z0-9]+)*_placement)`")


def _boundary_verdict(token: str) -> str:
    """Classify ``token`` with the REAL boundary screen, not a copied token list.

    ``accepted`` / ``boundary`` (rejected by ``_FORBIDDEN_KEY_PARTS`` before the allowed-key
    check) / ``unknown`` (a well-formed name that is simply not a config key). Only
    ``boundary`` is the failure mode R2-F2 is about: a repo that writes such a key is
    refused outright, so guidance naming it alone is guidance that cannot be executed.
    """
    try:
        RepoLocalConfig.from_record({token: {}})
    except RepoLocalConfigError as exc:
        return "boundary" if "boundary token" in str(exc) else "unknown"
    return "accepted"


class ConfigGuidanceNamesAnExecutableKeyTest(unittest.TestCase):
    """Operator-facing placement guidance names a key the boundary screen accepts (R2-F2).

    ``herdr-live-relayout-runbook.md`` told operators to add a ``pane_placement`` block. The
    repo-local schema boundary rejects any key containing ``pane`` BEFORE the allowed-key
    check, so that config is refused and the permanent placement never applies — the runbook
    documented a config that cannot exist. The canonical key is ``lane_placement``
    (``herdr-native-identity.md`` §5.1).

    The rejected spelling may still appear when a line names the accepted one in the same
    breath ("the key is ``lane_placement``, NOT ``pane_placement``") — you cannot state the
    contrast without both. What fails is a line that names ONLY a rejected key, which is
    exactly the shape of all four original occurrences.
    """

    def test_the_boundary_screen_still_rejects_the_pre_rename_key(self) -> None:
        # Non-vacuity: the guard is worthless if nothing is boundary-rejected any more.
        self.assertEqual(_boundary_verdict("pane_placement"), "boundary")
        self.assertEqual(_boundary_verdict("lane_placement"), "accepted")

    def test_no_guidance_line_names_only_a_rejected_config_key(self) -> None:
        offenders: list[str] = []
        rejected_seen = 0
        for relative in _CONFIG_GUIDANCE_DOCS:
            doc = _REPO_ROOT / relative
            self.assertTrue(doc.is_file(), f"{relative} is missing")
            lines = doc.read_text(encoding="utf-8").splitlines()
            accepted_in_doc = 0
            for number, line in enumerate(lines, start=1):
                verdicts = {
                    token: _boundary_verdict(token)
                    for token in _CONFIG_KEY_TOKEN.findall(line)
                }
                if not verdicts:
                    continue
                accepted = [t for t, v in verdicts.items() if v == "accepted"]
                rejected = [t for t, v in verdicts.items() if v == "boundary"]
                accepted_in_doc += len(accepted)
                rejected_seen += len(rejected)
                if rejected and not accepted:
                    offenders.append(f"{relative}:{number} names only {sorted(rejected)}")
            # Each doc must actually carry the canonical key, or the sweep proves nothing
            # about that doc (a doc that says nothing cannot say anything wrong).
            self.assertGreater(
                accepted_in_doc, 0, f"{relative} names no accepted placement config key"
            )
        self.assertEqual(offenders, [])
        # And the contrast form must really be exercised somewhere, so the rule above is
        # not passing merely because no rejected spelling survives anywhere.
        self.assertGreater(rejected_seen, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
