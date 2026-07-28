"""Redmine #14569 — ``lane_placement.<class>.ratio``: declare a pair's relative split.

Owner intent (issue description, 2026-07-27 coordinator chat): the coordinator and sublane
pairs are vertical since #14568, and their share of the container must be declarable too —
as herdr's own **relative** ratio, never a fixed pixel / column / row count, so a terminal
resize keeps the declared division. Design Answer j#91127 fixed the schema
(``0.1 <= ratio <= 0.9``, product default ``0.5``, ``order[0]``-relative, wholesale
``by_lane_kind`` shadowing) and the actuation seam (``agent start`` has no ``--ratio``, so
one post-launch herdr-native ``pane resize`` + a ``pane layout`` verification).

The four surfaces this pins, one class each:

- **Schema** — the value domain fails closed at CONFIG PARSE time, before any pane is
  touched, and the precedence puts ``by_lane_kind > lane class > product default``.
- **Projection** — ``config status`` reports the effective ratio and whether it was
  ``declared`` or ``default``, from the same resolver a launch reads.
- **Geometry decisions** — the pure identification / verification rules that keep the
  actuation off a divider that is not the pair's own.
- **Launch** — a fresh pair really is divided and MEASURED, an all-adopt run is not
  touched, an order-deferred heal declares its deferral, and a failure is not success.

The herdr facts these assert against are live-measured on 0.7.4 and recorded in j#91140.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from support.herdr_fake import (  # noqa: E402
    HERDR_SPLIT_EXTENT,
    FakeHerdr,
    apply_resize_amount,
    render_pane_layout,
)
from support.agent_provider_binaries import (  # noqa: E402
    FakeAgentBinaries,
    neutralized_overrides,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E402,E501
    LANE_PLACEMENT_CLASS_KEYS,
    LANE_PLACEMENT_RATIO_MAX,
    LANE_PLACEMENT_RATIO_MIN,
    LanePlacementConfig,
    LanePlacementError,
    product_default_placement,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E402,E501
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E402,E501
    PLACEMENT_LEAF_KEYS,
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    classify_config_sources,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E402,E501
    RATIO_APPLIED,
    RATIO_DEFERRED,
    RATIO_FAILED,
    RATIO_MATCHED,
    RATIO_NOT_APPLICABLE,
    RATIO_OUTCOMES,
    RATIO_SUCCESS_OUTCOMES,
    PaneRect,
    find_pair_split,
    governing_split,
    order_pair,
    parse_pane_layout,
    ratio_verdict,
    resize_step,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E402,E501
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_topology import (  # noqa: E402,E501
    HerdrSessionStartError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E402,E501
    StartupProbe,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_result import (  # noqa: E402,E501
    SessionStartResult,
    SlotResult,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.startup_health import (  # noqa: E402,E501
    HEALTH_HEALTHY,
)
from mozyo_bridge.core.state.herdr_identity_attestation import (  # noqa: E402
    IdentityAttestationRecord,
    VERDICT_PRESENT,
)
from mozyo_bridge.core.state.workspace_registry import register_workspace  # noqa: E402
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E402,E501
    decode_assigned_name,
)

#: The launch env's herdr-binary override key, and hermetic provider stubs on its PATH, so
#: no assertion here can resolve (or depend on) the host's real ``claude`` / ``codex``.
HERDR_ENV = "MOZYO_HERDR_BINARY"
PROVIDER_BINS = FakeAgentBinaries(Path(tempfile.mkdtemp(prefix="mzb14569-provider-bins-")))
atexit.register(shutil.rmtree, PROVIDER_BINS.bin_dir.parent, True)
#: The post-launch health probe with no real sleeping (the fake settles instantly).
_FAST_PROBE = StartupProbe(polls=3, interval=0.0, sleeper=lambda _seconds: None)


def _attesting_reader(herdr):
    """An attestation reader that vouches for whatever the fake has live.

    The #13637 adopt gate only ADOPTS a live name-match whose startup self-attestation is
    bound to its current locator; the shared fake models herdr, not the wrapper that writes
    that record. Injecting it here is what lets these tests exercise the adopt / heal
    shapes at all — the axis under test is the pair's geometry, and a run whose slots all
    came back ``unattested`` would never reach it.
    """

    def read(name):
        agent = herdr.agent_named(name)
        decoded = decode_assigned_name(name)
        if agent is None or not decoded.ok or decoded.identity is None:
            return None
        return IdentityAttestationRecord(
            assigned_name=name,
            workspace_id=decoded.identity.workspace_id,
            role=decoded.identity.role,
            lane_id=decoded.identity.lane_id,
            locator=agent["pane_id"],
            verdict=VERDICT_PRESENT,
            observed_at="2026-07-28T00:00:00+00:00",
        )

    return read


class RatioSchemaTest(unittest.TestCase):
    """Close condition 1 / 2: declarable per class and kind, fail-closed value domain."""

    def test_ratio_is_a_recognized_class_key_on_both_axes(self) -> None:
        self.assertIn("ratio", LANE_PLACEMENT_CLASS_KEYS)
        config = LanePlacementConfig.from_record(
            {
                "default": {"ratio": 0.6},
                "sublane": {"ratio": 0.4},
                "by_lane_kind": {"implementation": {"ratio": 0.3}},
            }
        )
        self.assertEqual(config.resolve("default").ratio, 0.6)
        self.assertEqual(config.resolve("sublane").ratio, 0.4)
        self.assertEqual(config.resolve_by_lane_kind("implementation").ratio, 0.3)

    def test_the_accepted_domain_is_herdrs_own_effective_split_domain(self) -> None:
        # The bounds are not a taste: herdr 0.7.4's layout clamps a split ratio into
        # 0.1..0.9 (j#91140), so a value outside them could never BE the geometry. Both
        # endpoints are inclusive and both are reachable.
        for value in (LANE_PLACEMENT_RATIO_MIN, 0.5, LANE_PLACEMENT_RATIO_MAX):
            with self.subTest(value=value):
                config = LanePlacementConfig.from_record({"default": {"ratio": value}})
                self.assertEqual(config.resolve_effective("default").ratio, value)

    def test_out_of_domain_and_non_numeric_values_fail_at_parse_time(self) -> None:
        # Every one of these would otherwise reach a pane: herdr's CLI accepts any finite
        # f32 and silently clamps, so "reject late" means a config that says 0.95 and a
        # pair that renders 0.9 forever.
        rejected = [
            0.95,                       # above the effective domain
            0.05,                       # below it
            1.0,                        # a plausible "100%" that cannot be a split
            0.0,
            -0.5,
            True,                       # bool is an int in Python; a YAML `ratio: yes`
            "0.6",                      # a quoting mistake, never coerced
            [0.5],
            {"value": 0.5},
            float("nan"),
            float("inf"),
            float("-inf"),
        ]
        for value in rejected:
            with self.subTest(value=value):
                with self.assertRaises(LanePlacementError):
                    LanePlacementConfig.from_record({"default": {"ratio": value}})
                with self.assertRaises(LanePlacementError):
                    LanePlacementConfig.from_record(
                        {"by_lane_kind": {"coordinator": {"ratio": value}}}
                    )

    def test_a_bad_ratio_fails_the_whole_repo_local_config_closed(self) -> None:
        # The single fail-closed boundary: the sibling's error surfaces as the loader's,
        # so a bad ratio refuses the CONFIG rather than being dropped from it.
        with self.assertRaises(RepoLocalConfigError):
            RepoLocalConfig.from_record(
                {"version": 2, "lane_placement": {"default": {"ratio": 2.0}}}
            )

    def test_the_product_default_is_an_even_split_on_both_lane_classes(self) -> None:
        # Close condition 6: adopting the axis must not move an undeclared workspace. 0.5
        # is the division a freshly split herdr pair already had, so nothing drifts.
        for lane_class in ("default", "sublane"):
            with self.subTest(lane_class=lane_class):
                self.assertEqual(product_default_placement(lane_class).ratio, 0.5)
                self.assertEqual(
                    LanePlacementConfig.default().resolve_effective(lane_class).ratio, 0.5
                )

    def test_precedence_is_kind_over_class_over_product_default(self) -> None:
        # Close condition 5, with all three layers distinguishable at once.
        config = LanePlacementConfig.from_record(
            {
                "sublane": {"ratio": 0.4},
                "by_lane_kind": {"implementation": {"ratio": 0.8}},
            }
        )
        self.assertEqual(
            config.resolve_effective("sublane", "implementation").ratio, 0.8
        )
        self.assertEqual(config.resolve_effective("sublane", "coordinator").ratio, 0.4)
        self.assertEqual(config.resolve_effective("default", None).ratio, 0.5)

    def test_a_declared_kind_shadows_its_class_wholesale_including_ratio(self) -> None:
        # #13647's wholesale shadowing is UNCHANGED by this issue (j#91127): a kind that
        # declares only `order` does not inherit its class's ratio — it takes the product
        # default. Pinned because a per-field merge is the tempting, wrong generalization.
        config = LanePlacementConfig.from_record(
            {
                "sublane": {"ratio": 0.8, "split": "right"},
                "by_lane_kind": {"implementation": {"order": ["claude", "codex"]}},
            }
        )
        effective = config.resolve_effective("sublane", "implementation")
        self.assertEqual(effective.ratio, 0.5)
        self.assertEqual(effective.split, "down")
        self.assertEqual(effective.order, ("claude", "codex"))

    def test_an_undeclared_ratio_does_not_disturb_the_other_placement_fields(self) -> None:
        config = LanePlacementConfig.from_record({"default": {"split": "right"}})
        effective = config.resolve_effective("default")
        self.assertEqual(effective.split, "right")
        self.assertEqual(effective.order, ("codex", "claude"))
        self.assertEqual(effective.ratio, 0.5)


class RatioStatusProjectionTest(unittest.TestCase):
    """Close condition 9: ``config status`` shows the effective ratio and its source."""

    def _rows(self, record):
        config = RepoLocalConfig.from_record(record or {"version": 2})
        return {
            row.key: row
            for row in classify_config_sources(
                raw_record=record,
                config=config,
                schema_version=2,
                legacy_migratable=False,
            )
        }

    def test_both_lane_classes_expose_a_ratio_leaf_row(self) -> None:
        for lane_class in ("default", "sublane"):
            self.assertIn(f"lane_placement.{lane_class}.ratio", PLACEMENT_LEAF_KEYS)

    def test_an_undeclared_ratio_reads_as_the_product_default(self) -> None:
        rows = self._rows(None)
        for lane_class in ("default", "sublane"):
            row = rows[f"lane_placement.{lane_class}.ratio"]
            self.assertEqual((row.effective_value, row.source), (0.5, SOURCE_DEFAULT))

    def test_a_declared_ratio_reads_as_declared_and_does_not_leak(self) -> None:
        rows = self._rows(
            {"version": 2, "lane_placement": {"default": {"ratio": 0.7}}}
        )
        declared = rows["lane_placement.default.ratio"]
        self.assertEqual((declared.effective_value, declared.source), (0.7, SOURCE_DECLARED))
        other = rows["lane_placement.sublane.ratio"]
        self.assertEqual((other.effective_value, other.source), (0.5, SOURCE_DEFAULT))

    def test_declaring_the_default_value_still_reads_as_declared(self) -> None:
        # Declaring intent counts, exactly as it does for every other leaf on this surface:
        # `0.5 (declared)` and `0.5 (default)` are different operator facts.
        rows = self._rows(
            {"version": 2, "lane_placement": {"sublane": {"ratio": 0.5}}}
        )
        row = rows["lane_placement.sublane.ratio"]
        self.assertEqual((row.effective_value, row.source), (0.5, SOURCE_DECLARED))


class PaneGeometryDecisionTest(unittest.TestCase):
    """The pure rules that keep the actuation on the pair's OWN divider."""

    def _layout(self, direction="down", ratio=0.5, panes=("p1", "p2")):
        snapshot = parse_pane_layout(
            json.dumps(
                render_pane_layout(pane_ids=list(panes), direction=direction, ratio=ratio)
            )
        )
        self.assertIsNotNone(snapshot)
        return snapshot

    def test_parses_the_live_0_7_4_layout_envelope(self) -> None:
        # The exact payload shape measured in j#91140, transcribed rather than paraphrased.
        snapshot = parse_pane_layout(
            json.dumps(
                {
                    "id": "cli:pane:layout",
                    "result": {
                        "type": "pane_layout",
                        "layout": {
                            "area": {"height": 75, "width": 124, "x": 19, "y": 1},
                            "focused_pane_id": "w4C:p1",
                            "tab_id": "w4C:t1",
                            "workspace_id": "w4C",
                            "zoomed": False,
                            "panes": [
                                {
                                    "focused": True,
                                    "pane_id": "w4C:p1",
                                    "rect": {"height": 38, "width": 124, "x": 19, "y": 1},
                                },
                                {
                                    "focused": False,
                                    "pane_id": "w4C:p2",
                                    "rect": {"height": 37, "width": 124, "x": 19, "y": 39},
                                },
                            ],
                            "splits": [
                                {
                                    "direction": "down",
                                    "id": "split_0_root",
                                    "ratio": 0.5,
                                    "rect": {
                                        "height": 75,
                                        "width": 124,
                                        "x": 19,
                                        "y": 1,
                                    },
                                }
                            ],
                        },
                    },
                }
            )
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.tab_id, "w4C:t1")
        self.assertEqual(
            snapshot.panes["w4C:p1"], PaneRect(x=19, y=1, width=124, height=38)
        )
        self.assertEqual(snapshot.splits[0].direction, "down")
        self.assertEqual(snapshot.splits[0].ratio, 0.5)

    def test_a_payload_that_is_not_a_layout_is_refused_not_guessed(self) -> None:
        for payload in (
            "",
            "not json",
            json.dumps({"result": {"type": "ok"}}),
            json.dumps({"result": {"type": "pane_layout"}}),
            # a pane whose rect is not four integers
            json.dumps(
                {
                    "result": {
                        "type": "pane_layout",
                        "layout": {
                            "panes": [{"pane_id": "p1", "rect": {"x": 0, "y": 0}}],
                            "splits": [],
                        },
                    }
                }
            ),
            # the same pane id twice: half the evidence would decide the tiling test
            json.dumps(
                {
                    "result": {
                        "type": "pane_layout",
                        "layout": {
                            "panes": [
                                {
                                    "pane_id": "p1",
                                    "rect": {"x": 0, "y": 0, "width": 1, "height": 1},
                                },
                                {
                                    "pane_id": "p1",
                                    "rect": {"x": 0, "y": 1, "width": 1, "height": 1},
                                },
                            ],
                            "splits": [],
                        },
                    }
                }
            ),
        ):
            with self.subTest(payload=payload[:40]):
                self.assertIsNone(parse_pane_layout(payload))

    def test_the_pairs_split_is_identified_by_exact_tiling_not_a_bounding_box(self) -> None:
        # The live counter-example from j#91140: in a three-pane tab, two panes that are NOT
        # siblings had a union rect identical to the ROOT split's rect. A bounding-box match
        # would have picked the outer divider and rearranged a neighbouring lane.
        payload = json.dumps(
            {
                "result": {
                    "type": "pane_layout",
                    "layout": {
                        "tab_id": "w4C:t1",
                        "panes": [
                            {
                                "pane_id": "p1",
                                "rect": {"x": 19, "y": 1, "width": 124, "height": 45},
                            },
                            {
                                "pane_id": "p2",
                                "rect": {"x": 19, "y": 46, "width": 62, "height": 30},
                            },
                            {
                                "pane_id": "p3",
                                "rect": {"x": 81, "y": 46, "width": 62, "height": 30},
                            },
                        ],
                        "splits": [
                            {
                                "id": "split_0_root",
                                "direction": "down",
                                "ratio": 0.6,
                                "rect": {"x": 19, "y": 1, "width": 124, "height": 75},
                            },
                            {
                                "id": "split_1_1",
                                "direction": "right",
                                "ratio": 0.5,
                                "rect": {"x": 19, "y": 46, "width": 124, "height": 30},
                            },
                        ],
                    },
                }
            }
        )
        snapshot = parse_pane_layout(payload)
        self.assertIsNotNone(snapshot)
        # p1 over p2 spans the root split's rectangle, but does not TILE it: p2 is half as
        # wide. The non-sibling pair therefore has no divider of its own.
        self.assertIsNone(
            find_pair_split(snapshot, snapshot.panes["p1"], snapshot.panes["p2"], "down")
        )
        # The real siblings do tile theirs.
        self.assertEqual(
            find_pair_split(
                snapshot, snapshot.panes["p2"], snapshot.panes["p3"], "right"
            ).split_id,
            "split_1_1",
        )

    def test_governing_split_is_the_nearest_same_axis_ancestor(self) -> None:
        # herdr resolves `pane resize --pane P --direction D` to the nearest ancestor split
        # on D's axis (measured j#91140). Reconstructed from rects as the SMALLEST same-axis
        # split containing the pane — which is what lets the caller refuse when that is not
        # the pair's own divider.
        snapshot = parse_pane_layout(
            json.dumps(
                {
                    "result": {
                        "type": "pane_layout",
                        "layout": {
                            "tab_id": "t",
                            "panes": [
                                {
                                    "pane_id": "inner_a",
                                    "rect": {"x": 0, "y": 0, "width": 50, "height": 20},
                                },
                                {
                                    "pane_id": "inner_b",
                                    "rect": {"x": 0, "y": 20, "width": 50, "height": 20},
                                },
                            ],
                            "splits": [
                                {
                                    "id": "outer",
                                    "direction": "down",
                                    "ratio": 0.5,
                                    "rect": {"x": 0, "y": 0, "width": 50, "height": 80},
                                },
                                {
                                    "id": "inner",
                                    "direction": "down",
                                    "ratio": 0.5,
                                    "rect": {"x": 0, "y": 0, "width": 50, "height": 40},
                                },
                            ],
                        },
                    }
                }
            )
        )
        self.assertIsNotNone(snapshot)
        self.assertEqual(
            governing_split(snapshot, snapshot.panes["inner_a"], "down").split_id, "inner"
        )
        self.assertIsNone(governing_split(snapshot, snapshot.panes["inner_a"], "right"))

    def test_ratio_is_always_the_first_child_share_on_both_axes(self) -> None:
        for direction in ("down", "right"):
            with self.subTest(direction=direction):
                snapshot = self._layout(direction=direction, ratio=0.7)
                first, second = snapshot.panes["p1"], snapshot.panes["p2"]
                self.assertTrue(order_pair(first, second, direction))
                self.assertFalse(order_pair(second, first, direction))
                split = find_pair_split(snapshot, first, second, direction)
                matched, _ = ratio_verdict(split, first, 0.7)
                self.assertTrue(matched)

    def test_the_verdict_rejects_a_ratio_that_disagrees_with_the_rendered_panes(self) -> None:
        snapshot = self._layout(ratio=0.5)
        split = find_pair_split(snapshot, snapshot.panes["p1"], snapshot.panes["p2"], "down")
        self.assertFalse(ratio_verdict(split, snapshot.panes["p1"], 0.7)[0])
        # ...and tolerates herdr's f32 residue, which a live layout really carries
        # (0.40000004 / 0.50000006 / 0.70000005 measured in j#91140).
        residual = type(split)(
            split_id=split.split_id,
            direction=split.direction,
            ratio=0.50000006,
            rect=split.rect,
        )
        self.assertTrue(ratio_verdict(residual, snapshot.panes["p1"], 0.5)[0])

    def test_the_step_direction_carries_the_sign_not_the_amount(self) -> None:
        token, amount = resize_step(0.5, 0.7, "down")
        self.assertEqual(token, "down")
        self.assertAlmostEqual(amount, 0.2, places=9)
        self.assertEqual(resize_step(0.7, 0.5, "down")[0], "up")
        token, amount = resize_step(0.5, 0.7, "right")
        self.assertEqual(token, "right")
        self.assertAlmostEqual(amount, 0.2, places=9)
        self.assertEqual(resize_step(0.7, 0.5, "right")[0], "left")
        for direction in ("down", "right"):
            self.assertGreaterEqual(resize_step(0.9, 0.1, direction)[1], 0.0)


class LaunchRatioTest(unittest.TestCase):
    """Close conditions 3 / 4 / 7 / 8: what a real launch does with the declared ratio.

    Driven through the shared stateful fake herdr at the subprocess ``Runner`` boundary, so
    ``prepare_session`` runs for real end to end: the divider is created by an
    ``agent start --split``, the root pane is reclaimed, and only then does the ratio rail
    read and move herdr's own layout. The fake's ``pane resize`` reproduces herdr's measured
    arithmetic (0.5 amount cap, 0.1..0.9 clamp), so a pass here means the code converged
    against the real clamps rather than against a compliant stub.
    """

    def _prepare(self, tmp, *, herdr, lane_placement, providers=("codex", "claude"),
                 lane="lane-1", dry_run=False, pair_order=None):
        repo = Path(tmp) / "repo"
        repo.mkdir(exist_ok=True)
        home = Path(tmp) / "home"
        home.mkdir(exist_ok=True)
        binpath = Path(tmp) / "fake-herdr"
        binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
        env = {
            HERDR_ENV: str(binpath),
            "PATH": str(PROVIDER_BINS.bin_dir),
            **neutralized_overrides(),
        }
        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            if dry_run:
                # A dry run refuses to register a workspace (it has no side effect), so the
                # identity has to exist first — otherwise the run fails closed before the
                # geometry rail is even reached and the test would prove nothing.
                register_workspace(repo)
            return prepare_session(
                repo_root=repo,
                providers=list(providers),
                lane_id=lane,
                env=env,
                runner=herdr.run,
                dry_run=dry_run,
                lane_placement=lane_placement,
                pair_order=pair_order,
                attestation_reader=_attesting_reader(herdr),
                probe=_FAST_PROBE,
            )

    def _resize_calls(self, herdr):
        return [c for c in herdr.calls if c[:2] == ["pane", "resize"]]

    def test_a_fresh_pair_is_divided_at_the_declared_ratio_and_measured(self) -> None:
        # Close condition 3, on the sublane class. herdr splits a fresh pair evenly, so a
        # declared 0.7 is only true if the run actually moved the divider AND read it back.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.7}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config)
        self.assertTrue(result.ratio_ok, result.ratio_detail)
        self.assertEqual(result.ratio_outcome, RATIO_APPLIED)
        self.assertTrue(self._resize_calls(herdr))
        # The claim is about herdr's state, not about what we asked for.
        live = herdr.split_ratio_of(result.slots[0].locator)
        self.assertAlmostEqual(live, 0.7, places=6)

    def test_both_domain_boundaries_are_reachable_on_a_fresh_pair(self) -> None:
        # Close condition 2's endpoints, end to end: 0.1 and 0.9 are exactly where herdr's
        # own clamp sits, so they are the two values most likely to come back off-target.
        for declared in (LANE_PLACEMENT_RATIO_MIN, LANE_PLACEMENT_RATIO_MAX):
            with self.subTest(ratio=declared):
                herdr = FakeHerdr()
                config = LanePlacementConfig.from_record({"sublane": {"ratio": declared}})
                with tempfile.TemporaryDirectory() as tmp:
                    result = self._prepare(tmp, herdr=herdr, lane_placement=config)
                self.assertEqual(result.ratio_outcome, RATIO_APPLIED, result.ratio_detail)
                self.assertAlmostEqual(
                    herdr.split_ratio_of(result.slots[0].locator), declared, places=6
                )

    def test_a_ratio_that_needs_more_than_one_pass_still_converges(self) -> None:
        # herdr caps `--amount` at 0.5 per call (j#91140), so a divider already at 0.1 that
        # must reach 0.9 cannot get there in one resize. Drive exactly that: the loop
        # recomputes from the MEASURED ratio, so it converges instead of reporting the
        # clamped intermediate as done.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.9}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config)
            herdr_first = herdr
        self.assertEqual(result.ratio_outcome, RATIO_APPLIED, result.ratio_detail)
        # A single 0.5-capped step from 0.5 lands on 0.9 here; prove the CAP itself is what
        # the loop is robust to by walking the shared model directly from the far endpoint.
        ratio = 0.1
        passes = 0
        while abs(ratio - 0.9) > 1e-3 and passes < 4:
            ratio = apply_resize_amount(ratio, "down", 0.9 - ratio)
            passes += 1
        self.assertEqual((round(ratio, 6), passes), (0.9, 2))
        self.assertTrue(self._resize_calls(herdr_first))

    def test_the_default_ratio_on_an_even_pair_is_a_verified_no_op(self) -> None:
        # Design Answer j#91127: 0.5 must be confirmed read-only and a no-op must NOT be a
        # failure. It must also not be silent — `matched` says the geometry was measured.
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(
                tmp, herdr=herdr, lane_placement=LanePlacementConfig.default()
            )
        self.assertEqual(result.ratio_outcome, RATIO_MATCHED, result.ratio_detail)
        self.assertEqual(self._resize_calls(herdr), [])
        self.assertTrue(result.ratio_ok)

    def test_a_dry_run_reads_and_resizes_nothing(self) -> None:
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config, dry_run=True)
        self.assertEqual(result.ratio_outcome, RATIO_NOT_APPLICABLE)
        self.assertEqual(
            [c for c in herdr.calls if c[:2] in (["pane", "resize"], ["pane", "layout"])],
            [],
        )

    def test_an_all_adopt_run_never_resizes_the_live_pair(self) -> None:
        # Close condition 8 / the module's whole boundary: changing the config must not
        # rearrange a pair that is already up. The second run adopts both slots — it creates
        # no divider, so it touches none.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.5}})
        with tempfile.TemporaryDirectory() as tmp:
            first = self._prepare(tmp, herdr=herdr, lane_placement=config)
            self.assertTrue(first.ratio_ok, first.ratio_detail)
            wider = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
            before = len(self._resize_calls(herdr))
            second = self._prepare(tmp, herdr=herdr, lane_placement=wider)
        self.assertEqual(
            [s.outcome for s in second.slots], ["adopted", "adopted"], second.slots
        )
        self.assertEqual(second.ratio_outcome, RATIO_NOT_APPLICABLE)
        self.assertEqual(len(self._resize_calls(herdr)), before)
        self.assertAlmostEqual(
            herdr.split_ratio_of(second.slots[0].locator), 0.5, places=6
        )

    def test_an_order_deferred_heal_declares_the_deferral_and_moves_nothing(self) -> None:
        # The configured primary can only be launched BESIDE a live sibling, so it lands on
        # the second side. Applying the declared share there would hand it to the other
        # provider, and swapping a live pane is forbidden — so the run says so.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record(
            {"sublane": {"ratio": 0.8, "order": ["codex", "claude"]}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = self._prepare(tmp, herdr=herdr, lane_placement=config)
            self.assertTrue(first.ratio_ok, first.ratio_detail)
            # Kill the CONFIGURED PRIMARY's pane, then heal: its replacement must split
            # beside the surviving sibling, i.e. land second.
            primary = next(s for s in first.slots if s.provider == "codex")
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", primary.locator])
            before = len(self._resize_calls(herdr))
            healed = self._prepare(tmp, herdr=herdr, lane_placement=config)
        self.assertEqual(healed.ratio_outcome, RATIO_DEFERRED, healed.ratio_detail)
        self.assertIn("codex", healed.ratio_detail)
        self.assertEqual(len(self._resize_calls(herdr)), before)
        # A deferral is an honest outcome, not a failure: the pair is usable.
        self.assertTrue(healed.ratio_ok, healed.ratio_detail)

    def test_a_target_only_secondary_heal_divides_the_divider_it_just_created(self) -> None:
        # Review j#91217 R1-F1. The production `replacement_target_only` path calls
        # `prepare_session(providers=(one,))`, so the run's ONLY slot is the replacement and
        # the surviving sibling belongs to an earlier run. That run still emits `--split`
        # and therefore creates the pair's divider — the earlier cut skipped it as
        # `not_applicable`, dropping the declared ratio while reporting success.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            first = self._prepare(tmp, herdr=herdr, lane_placement=config)
            self.assertEqual(first.ratio_outcome, RATIO_APPLIED, first.ratio_detail)
            secondary = first.slots[-1]
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", secondary.locator])
            before = len(self._resize_calls(herdr))
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(secondary.provider,),
                pair_order=("codex", "claude"),
            )
        self.assertEqual([s.provider for s in healed.slots], [secondary.provider])
        self.assertEqual(healed.ratio_outcome, RATIO_APPLIED, healed.ratio_detail)
        self.assertGreater(len(self._resize_calls(herdr)), before)
        self.assertAlmostEqual(
            herdr.split_ratio_of(healed.slots[0].locator), 0.8, places=6
        )

    def test_a_target_only_primary_heal_defers_instead_of_dividing_the_wrong_side(self) -> None:
        # The other half of R1-F1's required behaviour: when the single provider being
        # healed IS the configured `order[0]`, it lands on the second side, so applying the
        # declared share there would give it to the other provider. Deferred, not applied,
        # and not silently skipped either.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record(
            {"sublane": {"ratio": 0.8, "order": ["codex", "claude"]}}
        )
        with tempfile.TemporaryDirectory() as tmp:
            first = self._prepare(tmp, herdr=herdr, lane_placement=config)
            primary = next(s for s in first.slots if s.provider == "codex")
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", primary.locator])
            before = len(self._resize_calls(herdr))
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=("codex",),
                pair_order=("codex", "claude"),
            )
        self.assertEqual(healed.ratio_outcome, RATIO_DEFERRED, healed.ratio_detail)
        self.assertIn("codex", healed.ratio_detail)
        self.assertEqual(len(self._resize_calls(herdr)), before)
        self.assertTrue(healed.ratio_ok)

    def _first_side_provider(self, herdr, anchor_locator):
        """The provider physically holding the FIRST (top) side, read from the live layout."""
        out = herdr.run(
            ["herdr", "pane", "layout", "--pane", anchor_locator],
            capture_output=True, text=True,
        )
        snapshot = parse_pane_layout(out.stdout)
        first_pane = min(snapshot.panes.items(), key=lambda kv: kv[1].y)[0]
        name = next(a["name"] for a in herdr.agents if a["pane_id"] == first_pane)
        return decode_assigned_name(name).identity.role

    def test_an_undeclared_order_still_means_the_binding_order_on_a_shrunk_request(self) -> None:
        # Review j#91263 R2-F1. The `sublane` product default leaves `order` UNDECLARED on
        # purpose — so the repo-local binding's `(gateway, worker)` order is respected, not
        # overridden. That is a positive claim, not the absence of one. A target-only
        # replacement shrinks the request to a single provider, so the request can no longer
        # carry it; the caller's stable pair order must. Healing the GATEWAY previously made
        # the surviving worker the first side and handed it the gateway's declared 0.8.
        gateway, worker = "codex", "claude"
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(gateway, worker)
            )
            self.assertEqual(fresh.ratio_outcome, RATIO_APPLIED, fresh.ratio_detail)
            self.assertEqual(
                self._first_side_provider(herdr, fresh.slots[0].locator), gateway
            )
            victim = next(s for s in fresh.slots if s.provider == gateway)
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", victim.locator])
            before = len(self._resize_calls(herdr))
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(gateway,),
                pair_order=(gateway, worker),
            )
        # The gateway could only be launched as the split (second side), so its declared
        # share cannot be honoured without moving a live pane. Say so; divide nothing.
        self.assertEqual(healed.ratio_outcome, RATIO_DEFERRED, healed.ratio_detail)
        self.assertEqual(len(self._resize_calls(herdr)), before)
        self.assertAlmostEqual(
            herdr.split_ratio_of(healed.slots[0].locator), 0.5, places=6
        )

    def test_an_undeclared_order_worker_heal_keeps_the_share_on_the_gateway(self) -> None:
        # The other side of R2-F1: healing the WORKER leaves the gateway on the first side,
        # so the declared share is honoured and really does land on the gateway.
        gateway, worker = "codex", "claude"
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(gateway, worker)
            )
            victim = next(s for s in fresh.slots if s.provider == worker)
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", victim.locator])
            before = len(self._resize_calls(herdr))
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(worker,),
                pair_order=(gateway, worker),
            )
            side = self._first_side_provider(herdr, healed.slots[0].locator)
            live = herdr.split_ratio_of(healed.slots[0].locator)
        self.assertEqual(healed.ratio_outcome, RATIO_APPLIED, healed.ratio_detail)
        self.assertGreater(len(self._resize_calls(herdr)), before)
        self.assertEqual(side, gateway)
        self.assertAlmostEqual(live, 0.8, places=6)

    def test_a_rebound_binding_keeps_the_share_on_ITS_first_role(self) -> None:
        # The order that matters is the LANE's, not a hard-coded `(codex, claude)`. With a
        # rebound binding the worker leads, so a target-only heal of the now-second provider
        # must put the declared share on the rebound first role.
        first, second = "claude", "codex"
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(first, second)
            )
            victim = next(s for s in fresh.slots if s.provider == second)
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", victim.locator])
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(second,),
                pair_order=(first, second),
            )
            side = self._first_side_provider(herdr, healed.slots[0].locator)
            live = herdr.split_ratio_of(healed.slots[0].locator)
        self.assertEqual(healed.ratio_outcome, RATIO_APPLIED, healed.ratio_detail)
        self.assertEqual(side, first)
        self.assertAlmostEqual(live, 0.8, places=6)

    def test_a_shrunk_request_with_no_stable_order_defers_rather_than_guessing(self) -> None:
        # The fail-safe end of R2-F1: a caller that shrinks the request and supplies NO
        # stable pair order has left the run unable to attribute the first side. The single
        # provider it holds is trivially "first" in its own shrunk list — which is exactly
        # the false attribution the finding was. Defer; touch nothing.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._prepare(tmp, herdr=herdr, lane_placement=config)
            victim = fresh.slots[0]
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", victim.locator])
            before = len(self._resize_calls(herdr))
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(victim.provider,)
            )
        self.assertEqual(healed.ratio_outcome, RATIO_DEFERRED, healed.ratio_detail)
        self.assertEqual(len(self._resize_calls(herdr)), before)

    def test_a_malformed_pair_order_is_refused_before_any_side_effect(self) -> None:
        # Review j#91284 R3-F1. `pair_order` is ratio authority, so it is held to the same
        # domain the declared `order` already is: an exact permutation of the canonical
        # providers. Coercion was not theoretical — `("unknown", "codex")` made `codex` NOT
        # the primary, so a gateway heal resized the pair and gave the gateway's declared
        # share to the surviving worker while reporting `applied` (j#91299).
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        malformed = [
            ("unknown", "codex"),      # an unknown provider
            ("codex", "codex"),        # a duplicate
            ("codex",),                # a partial pair
            (),                        # empty
            (None, "codex"),           # a non-string element (used to become "None")
            ("codex", "claude", "x"),  # a superset
            "codex",                   # a bare string is not a list of providers
            7,                         # not a sequence at all
        ]
        for bad in malformed:
            with self.subTest(pair_order=bad):
                herdr = FakeHerdr()
                with tempfile.TemporaryDirectory() as tmp:
                    with self.assertRaises(HerdrSessionStartError):
                        self._prepare(
                            tmp, herdr=herdr, lane_placement=config, pair_order=bad
                        )
                # Zero side effect: the refusal happens at the argument boundary, so herdr
                # was never asked to create, launch, resize or close anything.
                self.assertEqual(herdr.calls, [], f"{bad!r} reached herdr")

    def test_a_malformed_pair_order_costs_no_filesystem_side_effect(self) -> None:
        """Review j#91331 R4-F1: refused BEFORE the first side effect, not merely early.

        The public entry takes the attestation-store lock before delegating, and taking it
        creates the mozyo home directory and a lock file. Validating inside the locked
        callee therefore refused the argument only *after* touching the filesystem — the
        exact "side effect ahead of validation" this entry point's signature is spelled out
        to prevent. Asserting ``herdr.calls == []`` did not see it, because the residue is
        not a herdr call.
        """
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"  # deliberately NOT created
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            env = {
                HERDR_ENV: str(binpath),
                "PATH": str(PROVIDER_BINS.bin_dir),
                **neutralized_overrides(),
            }
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with self.assertRaises(HerdrSessionStartError):
                    prepare_session(
                        repo_root=repo, providers=["codex"], lane_id="lane-1", env=env,
                        runner=herdr.run, lane_placement=config,
                        pair_order=("unknown", "codex"), probe=_FAST_PROBE,
                    )
            self.assertEqual(herdr.calls, [])
            # Neither the home directory nor the lock file inside it may exist: the argument
            # was rejected before anything was created.
            self.assertFalse(home.exists(), sorted(p.name for p in home.rglob("*")))

    def test_a_malformed_pair_order_is_refused_before_lock_contention_matters(self) -> None:
        # Ordering, not just absence: a malformed argument must lose to nothing — including
        # a busy store. If validation ran after acquisition, a contended lock would decide
        # the error instead, and the caller would be told to retry a call that can never work.
        from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
            AttestationStoreLockBusy,
        )

        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            binpath = Path(tmp) / "fake-herdr"
            binpath.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
            env = {
                HERDR_ENV: str(binpath),
                "PATH": str(PROVIDER_BINS.bin_dir),
                **neutralized_overrides(),
            }

            def busy(*_args, **_kwargs):
                raise AttestationStoreLockBusy("probe: the store is held exclusively")

            module = "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                with patch(
                    module + "application.herdr_session_start.attestation_store_lock", busy
                ):
                    with self.assertRaises(HerdrSessionStartError):
                        prepare_session(
                            repo_root=repo, providers=["codex"], lane_id="lane-1", env=env,
                            runner=herdr.run, lane_placement=config,
                            pair_order=("unknown", "codex"), probe=_FAST_PROBE,
                        )

    def test_a_pair_order_that_excludes_the_requested_provider_is_refused(self) -> None:
        # A caller naming a stable order that does not contain what it is launching has
        # contradicted itself; the side the ratio would pick from that is meaningless.
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError):
                self._prepare(
                    tmp, herdr=herdr, lane_placement=config,
                    providers=("codex",), pair_order=("claude", "claude"),
                )
        self.assertEqual(herdr.calls, [])

    def test_a_well_formed_pair_order_is_accepted_in_either_direction(self) -> None:
        # The validator must not be so strict that the real orders stop working: both
        # permutations are legitimate lane orders (a rebound binding is the second).
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        for order in (("codex", "claude"), ("claude", "codex")):
            with self.subTest(pair_order=order):
                herdr = FakeHerdr()
                with tempfile.TemporaryDirectory() as tmp:
                    result = self._prepare(
                        tmp, herdr=herdr, lane_placement=config,
                        providers=order, pair_order=order,
                    )
                self.assertEqual(result.ratio_outcome, RATIO_APPLIED, result.ratio_detail)

    def test_a_shrunk_request_reports_an_unattributable_effective_order(self) -> None:
        # R3-F1's third part: the spec's layer 3 says a request that is not a full pair
        # contributes nothing, so the effective order is EMPTY. Previously the singleton
        # request became a one-element "order" whose deferral was a coincidence, and whose
        # detail claimed an effective order of `['codex']`.
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._prepare(tmp, herdr=herdr, lane_placement=config)
            victim = fresh.slots[0]
            herdr.run([str(Path(tmp) / "fake-herdr"), "pane", "close", victim.locator])
            healed = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=(victim.provider,)
            )
        self.assertEqual(healed.ratio_outcome, RATIO_DEFERRED)
        self.assertIn("unattributable", healed.ratio_detail)
        self.assertNotIn(f"['{victim.provider}']", healed.ratio_detail)

    def test_a_full_pair_request_needs_no_caller_supplied_order(self) -> None:
        # The fallback must not make the ordinary path depend on a new argument: an
        # unshrunk request IS the pair order, so a fresh pair still divides with no
        # `pair_order` supplied at all.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.7}})
        with tempfile.TemporaryDirectory() as tmp:
            fresh = self._prepare(tmp, herdr=herdr, lane_placement=config)
            side = self._first_side_provider(herdr, fresh.slots[0].locator)
        self.assertEqual(fresh.ratio_outcome, RATIO_APPLIED, fresh.ratio_detail)
        self.assertEqual(side, "codex")

    def test_a_target_only_run_into_an_empty_container_stays_not_applicable(self) -> None:
        # The boundary the R1-F1 fix must NOT cross: a single launch that only OCCUPIES a
        # container creates no divider, so it still measures and resizes nothing. Otherwise
        # "measure whenever one slot launched" would reach panes this run never split.
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.8}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(
                tmp, herdr=herdr, lane_placement=config, providers=("codex",)
            )
        self.assertEqual(result.ratio_outcome, RATIO_NOT_APPLICABLE, result.ratio_detail)
        self.assertEqual(
            [c for c in herdr.calls if c[:2] in (["pane", "resize"], ["pane", "layout"])],
            [],
        )

    def test_an_unreadable_layout_is_a_failure_not_a_silent_success(self) -> None:
        # Close condition 7: a ratio that could not be established is never reported as
        # applied, and it costs the run its exit-code success — while closing no agent.
        herdr = FakeHerdr()
        herdr.layout_fails = True
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.7}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config)
        self.assertEqual(result.ratio_outcome, RATIO_FAILED)
        self.assertFalse(result.ratio_ok)
        # ...and no agent was closed over a geometry miss: the only reclaimed panes are the
        # empty roots this run created, never a live slot's locator.
        closed = {c[2] for c in herdr.calls if c[:2] == ["pane", "close"]}
        self.assertFalse(closed & {s.locator for s in result.slots})

    def test_a_refused_resize_is_a_failure(self) -> None:
        herdr = FakeHerdr()
        herdr.resize_fails = True
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.7}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config)
        self.assertEqual(result.ratio_outcome, RATIO_FAILED)
        self.assertIn("refused", result.ratio_detail)
        self.assertFalse(result.ratio_ok)

    def test_a_layout_payload_the_parser_refuses_is_a_failure(self) -> None:
        herdr = FakeHerdr()
        herdr.layout_bad_payload = True
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.7}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config)
        self.assertEqual(result.ratio_outcome, RATIO_FAILED)
        self.assertFalse(result.ratio_ok)

    def test_the_ratio_outcome_is_reported_on_the_run_payload(self) -> None:
        herdr = FakeHerdr()
        config = LanePlacementConfig.from_record({"sublane": {"ratio": 0.6}})
        with tempfile.TemporaryDirectory() as tmp:
            result = self._prepare(tmp, herdr=herdr, lane_placement=config)
        payload = result.as_payload()
        self.assertEqual(payload["ratio_outcome"], RATIO_APPLIED)
        self.assertIn("declared=0.6", payload["ratio_detail"])

    def test_the_split_extent_used_by_the_model_is_the_shared_one(self) -> None:
        # Guards the fixture, not the code: if the shared fake's extent stopped being a
        # round number, `round(extent * ratio)` would carry rounding error and the geometry
        # assertions above would start being about arithmetic instead of about the rail.
        self.assertEqual(HERDR_SPLIT_EXTENT % 10, 0)


class TargetOnlyProductionSeamTest(unittest.TestCase):
    """Review j#91217 R1-F1 at the seam that actually reaches production.

    ``sublane_hibernated_pair_recovery_live`` heals a single leg with
    ``replacement_target_only=True``; that flag is what collapses ``prepare_session``'s
    providers to one (``herdr_session_start_v1_replacement_binding``:
    ``startup_providers = (provider,) if target_only else managed_pair``). The class above
    pins the behaviour at ``prepare_session``; this one drives the REAL actuator ->
    ``heal_lane_column`` -> ``prepare_session`` chain so the two cannot agree while the wiring
    between them says something else.

    The heavy v1 lane fixture is reused from the #13933 convergence regression rather than
    re-created here: it already stands up a registered lane, a self-attesting launcher and a
    live v1-bound pair over the shared FakeHerdr, and a second copy of it would drift.
    """

    LANE = "issue_14569_target_only"
    ISSUE = "14569"

    @staticmethod
    def _runner_answering_the_launcher_config_probe(inner):
        """Wrap ``inner`` so the launcher's ``config check-parse`` probe is answered.

        Declaring a ``lane_placement`` at all turns on the #14258 launcher config-parse
        preflight, and the v1 fixture's fake launcher does not model that subcommand — so
        without this the seam refuses before it ever launches. The answer comes from the ONE
        canonical responder the installed-fault harness already uses (which runs this build's
        real loader), never a second hand-written ``exit 0``: a canned success would report a
        launcher as config-compatible even for a document this build rejects.
        """
        from support.installed_fault_harness import _config_check_parse_result

        def run(argv, *args, **kwargs):
            if list(argv[1:])[:2] == ["config", "check-parse"]:
                return _config_check_parse_result(list(argv))
            return inner(argv, *args, **kwargs)

        return run

    def test_a_target_only_replacement_measures_the_divider_it_created(self) -> None:
        from tests.regressions.test_issue_13933_bound_stale_pair_convergence import (
            _append_v1_lane,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )

        with tempfile.TemporaryDirectory() as tmp:
            home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
                _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)
            )
            # Close the worker leg, then heal ONLY it — the exact production shape.
            fake.run([str(Path(tmp) / "fake-herdr"), "pane", "close", wk_old])
            layouts_before = len([c for c in fake.calls if c[:2] == ["pane", "layout"]])
            ops = HerdrSublaneActuatorOps(
                repo_root=coord, lane_label=self.LANE, issue=self.ISSUE, journal="91217",
                env=env, runner=fake.run,
                replacement_action_id="target-only-ratio-1",
                replacement_assigned_name=wk_name,
                replacement_old_locator=wk_old,
                replacement_target_only=True,
            )
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                ops.heal_lane_column(str(worktree), target_provider="claude")
            layouts_after = len([c for c in fake.calls if c[:2] == ["pane", "layout"]])
            live_ratio = fake.split_ratio_of(fake.agent_named(wk_name)["pane_id"])

        # The discriminator is the LAYOUT READ, deliberately, and not a resize: this fixture
        # declares no `lane_placement`, so the effective ratio is the product default and a
        # freshly split pair already matches it — a correct run issues no resize here. Under
        # the defect the rail bailed BEFORE reading anything, so zero layout reads is exactly
        # the failure being pinned. (What a declared, non-default ratio does to a
        # single-provider run is pinned at the `prepare_session` level above, where the
        # launcher config-parse preflight this fixture cannot answer is out of the way.)
        self.assertGreater(
            layouts_after, layouts_before,
            "the target-only replacement never reached the ratio rail (no pane layout read)",
        )
        self.assertAlmostEqual(live_ratio, 0.5, places=6)

    def _drive_target_only_heal(self, tmp, *, healed_provider, spy, results):
        """Run the REAL actuator -> heal_lane_column -> prepare_session chain, target-only.

        Returns ``(fake, healed pane id, resize count delta)`` and fills ``spy`` with the
        ``pair_order`` that crossed the last hop and ``results`` with the
        :class:`SessionStartResult` that hop RETURNED. Both are needed: geometry says the
        pair ended up right, the result says the run knows it did. Review j#91331 R4-F2 —
        keeping only the geometry left the seam green when the production path was forced to
        answer ``not_applicable`` (gateway) or to mislabel a real resize as ``failed``.

        The lane declares a NON-default ``ratio`` and no ``order`` — the product default that
        exists precisely to respect the binding's ``(gateway, worker)`` order — so nothing
        here can pass by coinciding with the even default.
        """
        from tests.regressions.test_issue_13933_bound_stale_pair_convergence import (
            _append_v1_lane,
        )
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.sublane_actuator_herdr_ops import (  # noqa: E501
            HerdrSublaneActuatorOps,
        )
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start as _session,
        )

        home, coord, worktree, env, fake, ws, gw_name, wk_name, gw_old, wk_old = (
            _append_v1_lane(tmp, lane=self.LANE, issue=self.ISSUE)
        )
        names = {"codex": (gw_name, gw_old), "claude": (wk_name, wk_old)}
        assigned_name, old_locator = names[healed_provider]
        for root in (coord, worktree):
            (root / ".mozyo-bridge").mkdir(parents=True, exist_ok=True)
            (root / ".mozyo-bridge" / "config.yaml").write_text(
                "version: 2\nlane_placement:\n  sublane:\n    ratio: 0.8\n",
                encoding="utf-8",
            )
        runner = self._runner_answering_the_launcher_config_probe(fake.run)
        fake.run([str(Path(tmp) / "fake-herdr"), "pane", "close", old_locator])
        before = len([c for c in fake.calls if c[:2] == ["pane", "resize"]])
        ops = HerdrSublaneActuatorOps(
            repo_root=coord, lane_label=self.LANE, issue=self.ISSUE, journal="91284",
            env=env, runner=runner,
            replacement_action_id=f"target-only-{healed_provider}",
            replacement_assigned_name=assigned_name,
            replacement_old_locator=old_locator,
            replacement_target_only=True,
        )
        # BOTH entry points, because the v1 replacement path runs under a caller-held
        # admission lock and therefore composes `_prepare_session_locked`, not the public
        # wrapper — spying on only one would silently observe nothing.
        real_public = _session.prepare_session
        real_locked = _session._prepare_session_locked

        def watch(inner):
            def watched(**kwargs):
                spy.append(kwargs.get("pair_order"))
                result = inner(**kwargs)
                results.append(result)
                return result

            return watched

        with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
            with patch.object(_session, "prepare_session", watch(real_public)), \
                    patch.object(_session, "_prepare_session_locked", watch(real_locked)):
                ops.heal_lane_column(str(worktree), target_provider=healed_provider)
        after = len([c for c in fake.calls if c[:2] == ["pane", "resize"]])
        return fake, fake.agent_named(assigned_name)["pane_id"], after - before

    def test_the_production_seam_carries_the_stable_pair_order_to_a_worker_heal(self) -> None:
        """Review j#91284 R3-F2: pin the FORWARDING, not just the rail it feeds.

        The R2-F1 fix routes the lane's stable ``(gateway, worker)`` order from
        ``heal_lane_column``'s ``managed_pair`` through ``_prepare_lane_session`` ->
        ``prepare_actuator_lane_session`` -> ``prepare_session``. Every test asserting that
        fix injected ``pair_order`` at the LAST of those hops, so deleting the first left all
        295 of them green (measured, j#91299).

        A WORKER target-only heal is the geometry discriminator: with the stable order
        delivered the gateway is the effective primary and holds the first side, so the
        declared 0.8 is applied; without it the shrunk request is unattributable and the run
        defers, leaving herdr's even default. The two outcomes differ in the live layout, not
        merely in a detail string.
        """
        spy: list = []
        results: list = []
        with tempfile.TemporaryDirectory() as tmp:
            fake, pane, resized = self._drive_target_only_heal(
                tmp, healed_provider="claude", spy=spy, results=results
            )
            live_ratio = fake.split_ratio_of(pane)
            starts = [c for c in fake.calls if c[:2] == ["agent", "start"]]
        # The heal really created the divider — otherwise the rail is out of scope and this
        # would prove nothing about forwarding.
        self.assertIn("--split", starts[-1])
        # The exact value that crossed the last hop, so a silently dropped forwarding is
        # named rather than merely inferred from the geometry.
        self.assertEqual([list(o or ()) for o in spy[-1:]], [["codex", "claude"]])
        self.assertGreater(resized, 0)
        self.assertAlmostEqual(live_ratio, 0.8, places=6)
        # ...and the run REPORTS what it did. Geometry alone left this green when the
        # production path mislabelled a completed resize as `failed` (j#91331 R4-F2).
        self.assertEqual(results[-1].ratio_outcome, RATIO_APPLIED, results[-1].ratio_detail)
        self.assertTrue(results[-1].ratio_ok)

    def test_the_production_seam_defers_a_gateway_target_only_heal(self) -> None:
        """The case j#91284 names: the gateway can only be placed second, so nothing is
        divided — and the stable order still has to arrive for that verdict to be the
        *attributed* one rather than the unattributable fallback."""
        spy: list = []
        results: list = []
        with tempfile.TemporaryDirectory() as tmp:
            fake, pane, resized = self._drive_target_only_heal(
                tmp, healed_provider="codex", spy=spy, results=results
            )
            live_ratio = fake.split_ratio_of(pane)
        self.assertEqual([list(o or ()) for o in spy[-1:]], [["codex", "claude"]])
        self.assertEqual(resized, 0)
        # herdr's fresh divider, untouched: the run neither applied 0.8 nor claimed to.
        self.assertAlmostEqual(live_ratio, 0.5, places=6)
        # The typed verdict, which "0 resizes" alone does not distinguish from a run that
        # decided nothing at all — forcing `not_applicable` used to leave this green.
        outcome = results[-1]
        self.assertEqual(outcome.ratio_outcome, RATIO_DEFERRED, outcome.ratio_detail)
        # ...and it is the ATTRIBUTED deferral (the stable order arrived and named the
        # gateway), not the unattributable fallback a missing forwarding would produce.
        self.assertIn("codex", outcome.ratio_detail)
        self.assertNotIn("unattributable", outcome.ratio_detail)


class RunSuccessAxisTest(unittest.TestCase):
    """Close condition 7 at the result model: a failed ratio is not exit-code success.

    Pinned on :class:`SessionStartResult` directly rather than through a launch, because the
    launch fixture above deliberately runs UNWRAPPED (no ``mozyo-bridge`` launcher on the
    env's PATH), so its slots never reach ``healthy`` and could not isolate the new
    conjunct from the pre-existing health one.
    """

    def _slot(self):
        return SlotResult(
            provider="codex",
            assigned_name="mzb1_ws_codex_lane",
            outcome="launched",
            locator="w1:p2",
            health=HEALTH_HEALTHY,
        )

    def test_a_healthy_pair_with_a_failed_ratio_is_not_ok(self) -> None:
        result = SessionStartResult(workspace_id="ws", lane_id="lane")
        result.slots = [self._slot()]
        result.ratio_outcome = RATIO_FAILED
        self.assertFalse(result.ratio_ok)
        self.assertFalse(result.ok)
        # ...but it owes no rollback: a mis-divided pair is fully usable, and tearing an
        # agent down over its geometry would be a worse outcome than reporting it.
        self.assertFalse(result.owes_rollback)

    def test_every_non_failure_outcome_leaves_a_healthy_run_ok(self) -> None:
        for outcome in (RATIO_NOT_APPLICABLE, RATIO_MATCHED, RATIO_APPLIED, RATIO_DEFERRED):
            with self.subTest(outcome=outcome):
                result = SessionStartResult(workspace_id="ws", lane_id="lane")
                result.slots = [self._slot()]
                result.ratio_outcome = outcome
                self.assertTrue(result.ratio_ok)
                self.assertTrue(result.ok)

    def test_a_dry_run_is_ok_regardless_of_the_ratio_axis(self) -> None:
        result = SessionStartResult(workspace_id="ws", lane_id="lane", dry_run=True)
        result.ratio_outcome = RATIO_FAILED
        self.assertTrue(result.ok)

    def test_an_unrecognised_outcome_is_never_success(self) -> None:
        """Review j#91418 R5-F1: an outcome this axis cannot read is not evidence of success.

        ``ratio_ok`` used to ask ``!= RATIO_FAILED``, so EVERY token outside the closed
        vocabulary reported the run as successful — a producer typo, a case variant, a
        truncation, an empty string. Declaring a closed vocabulary and then judging by one
        negative comparison means the declaration decides nothing.
        """
        unrecognised = [
            "appllied",                     # a producer typo
            "APPLIED",                      # a case variant of a real token
            "deferred_until_full_relaunc",  # a truncation of a real token
            "",                             # never assigned / cleared
            "definitely_fine",              # an unrelated token
        ]
        for token in unrecognised:
            with self.subTest(ratio_outcome=token):
                self.assertNotIn(token, RATIO_OUTCOMES)
                result = SessionStartResult(workspace_id="ws", lane_id="lane")
                result.slots = [self._slot()]
                result.ratio_outcome = token
                self.assertFalse(result.ratio_ok)
                self.assertFalse(result.ok)
                self.assertFalse(result.as_payload()["ok"])
                # ...and the unreadable token survives into the payload, so a reader can see
                # WHICH one it was rather than only that something was wrong.
                self.assertEqual(result.as_payload()["ratio_outcome"], token)

    def test_the_success_set_and_the_vocabulary_cannot_drift_apart(self) -> None:
        # Two guards, because either one alone can be satisfied by the drift it should
        # catch. Measured, not assumed: growing BOTH sets together keeps the partition
        # equality true, so the equality alone would let a new token join the success side
        # silently — the same shape R5-F1 was.
        #
        #  (a) the success half, literally. Growing it (or swapping the enumeration for a
        #      subtraction that later absorbs a newcomer) changes this value and goes red.
        self.assertEqual(
            RATIO_SUCCESS_OUTCOMES,
            frozenset({"not_applicable", "matched", "applied", "deferred_until_full_relaunch"}),
        )
        #  (b) the partition. Adding a token to the vocabulary alone — the natural way to
        #      extend it — leaves it unclassified and goes red here.
        self.assertEqual(set(RATIO_OUTCOMES), RATIO_SUCCESS_OUTCOMES | {RATIO_FAILED})
        self.assertNotIn(RATIO_FAILED, RATIO_SUCCESS_OUTCOMES)
        for outcome in RATIO_OUTCOMES:
            with self.subTest(outcome=outcome):
                result = SessionStartResult(workspace_id="ws", lane_id="lane")
                result.slots = [self._slot()]
                result.ratio_outcome = outcome
                self.assertEqual(result.ratio_ok, outcome in RATIO_SUCCESS_OUTCOMES)

    def test_the_resting_outcome_is_not_applicable(self) -> None:
        # A run that never had an opinion about a divider must not claim one, and must not
        # be penalised for it either.
        result = SessionStartResult(workspace_id="ws", lane_id="lane")
        self.assertEqual(result.ratio_outcome, RATIO_NOT_APPLICABLE)
        self.assertTrue(result.ratio_ok)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
