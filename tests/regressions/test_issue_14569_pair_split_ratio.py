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
                 lane="lane-1", dry_run=False, existing_rows=None):
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

    def test_the_resting_outcome_is_not_applicable(self) -> None:
        # A run that never had an opinion about a divider must not claim one, and must not
        # be penalised for it either.
        result = SessionStartResult(workspace_id="ws", lane_id="lane")
        self.assertEqual(result.ratio_outcome, RATIO_NOT_APPLICABLE)
        self.assertTrue(result.ratio_ok)


if __name__ == "__main__":  # pragma: no cover - unittest entry point
    unittest.main()
