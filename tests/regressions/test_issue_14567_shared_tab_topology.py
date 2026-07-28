"""Regression pins for Redmine #14567 — every sublane in ONE shared herdr tab.

Owner intent 2026-07-27: a sublane host workspace shows one tab per lane (#13411), so
seeing every lane means switching tabs. The operator wants all lanes side by side in one
tab — but as a repo-local CHOICE, because per-lane tabs suit other adopters. Design Answer
j#91144 fixed the contract; these pins state it as behaviour:

1. **the choice is a repo-local closed-vocabulary block** — ``sublane_tab_topology.mode``
   is ``per_lane_tab`` or ``shared_tab`` and anything else fails closed, so a typo can
   never silently pick a topology (Decision 1);
2. **the portable default is the #13411 placement** — an undeclared repo launches
   byte-for-byte as before. This block is behavior-preserving when undeclared, unlike its
   ``lane_placement`` neighbour (Decision 2);
3. **the shared tab is identified by a stable LABEL, not by the live inventory** — an
   inventory-only resolver is blind to a tab that was created but not yet launched into,
   so two concurrent clean-slate lanes would each mint one. The label is read back with
   ``tab list`` under a workspace-scoped single-flight fence, re-read inside the lock, and
   ambiguity / unreadability fails closed (Decision 3);
4. **a host mid-transition is refused before anything is created** — an existing live lane
   is never moved, adopted, or relabelled implicitly (Decision 3 / the issue's Non-goals);
5. **two split axes, never one** — a lane's FIRST slot in an occupied shared tab opens the
   lane's column (``right``) and is FOCUSED; its sibling then splits THAT pane on the
   ``lane_placement`` axis (product default ``down``). A single direction cannot express
   both, and without the focus the pair would be torn across another lane's pane
   (Decision 4);
6. **the mixed installed/source contract is inherited, not re-invented** — a launcher
   predating this block rejects the config through the #14258 ``config check-parse``
   preflight, before any workspace / tab / agent write (Decision 5).

Layout assertions run against ``_LayoutHerdr`` (the fake that models active-pane splitting,
implicit ``right``, focus, and split collapse on close) and are taken AFTER the root-pane
reclaim, because that reclaim is what silently discarded a configured direction in #13646
R1-F1. Asserting the argv alone would not catch a repeat.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from mozyo_bridge.core.state.sublane_tab_fence import (
    SUBLANE_TAB_CREATE_LOCK_PREFIX,
    SublaneTabCreateLockUnavailable,
    SublaneTabCreateReleaseError,
    sublane_tab_create_lock,
    sublane_tab_create_lock_path,
)
from mozyo_bridge.core.state.workspace_registry import read_anchor, register_workspace
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.lane_placement import (  # noqa: E501
    LanePlacementConfig,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
    REPO_LOCAL_CONFIG_KEYS,
    RepoLocalConfig,
    RepoLocalConfigError,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config_status import (  # noqa: E501
    SOURCE_DECLARED,
    SOURCE_DEFAULT,
    classify_config_sources,
)
from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.sublane_tab_topology import (  # noqa: E501
    DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE,
    PER_LANE_TAB,
    SHARED_TAB,
    SUBLANE_TAB_TOPOLOGY_KEY,
    SublaneTabTopologyConfig,
    SublaneTabTopologyError,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_lane_geometry import (  # noqa: E501
    INTER_LANE_SPLIT_DIRECTION,
    ContainerPlan,
    initial_container_occupancy,
    resolve_container_plan,
    resolve_focus_first_launch,
    slot_placement,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501
    HerdrSessionStartError,
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_shared_tab import (  # noqa: E501
    SHARED_SUBLANE_TAB_LABEL,
    _parse_tab_list,
    host_lane_slot_tabs,
    resolve_shared_tab_from_labels,
    resolve_shared_tab_target,
    verify_shared_tab_consistency,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    encode_assigned_name,
)

# The herdr fakes live with the unit suite that introduced them (#13646 / R1-F1 / #14567).
# Importing them keeps this file a pin on the behaviour instead of a second, subtly
# different simulation of herdr.
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_session_start import (  # noqa: E501
    _Herdr,
    _LayoutHerdr,
    _launch_env,
)

ISSUE = "14567"

SHARED = SublaneTabTopologyConfig(mode=SHARED_TAB)
PER_LANE = SublaneTabTopologyConfig(mode=PER_LANE_TAB)


def _prepare(
    tmp,
    *,
    herdr,
    providers,
    lane,
    sublane_tab_topology=None,
    lane_placement=None,
    rows=None,
    repo_name="repo",
):
    """Run the real ``prepare_session`` against a fake herdr in an isolated home.

    Returns ``(result, workspace_id, {provider: pane_locator})`` — the pane map is what the
    layout assertions key on, since a pane id is the only handle
    ``_LayoutHerdr.direction_between`` understands.
    """
    repo = Path(tmp) / repo_name
    repo.mkdir(exist_ok=True)
    home = Path(tmp) / "home"
    home.mkdir(exist_ok=True)
    binpath = Path(tmp) / "fake-herdr"
    if not binpath.exists():
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
            sublane_tab_topology=sublane_tab_topology,
        )
    return result, workspace_id, {s.provider: s.locator for s in result.slots}


def _row(workspace_id, role, lane, pane, tab):
    """One live ``agent list`` row for ``role`` of ``lane``, located at ``pane`` in ``tab``."""
    return {
        "name": encode_assigned_name(workspace_id, role, lane),
        "pane_id": pane,
        "workspace_id": pane.split(":", 1)[0],
        "tab_id": tab,
        "agent_status": "idle",
    }


def _separating_subtree_panes(herdr, a, b) -> set:
    """Panes under the split node that separates ``a`` from ``b`` in ``herdr``'s layout.

    ``_LayoutHerdr.direction_between`` reports that node's DIRECTION; this reports its
    CONTENT, which is how "the pair is contiguous" is stated structurally: the separating
    split holds exactly the two panes, and no third pane was inserted between them.
    """

    def walk(node):
        if node is None or node[0] == "leaf":
            return None
        left, right = herdr._panes(node[2]), herdr._panes(node[3])
        if (a in left and b in right) or (b in left and a in right):
            return set(left) | set(right)
        return walk(node[2]) or walk(node[3])

    for container in herdr.containers.values():
        found = walk(container["tree"])
        if found:
            return found
    return set()


def _split_of(start_argv) -> str:
    return start_argv[start_argv.index("--split") + 1] if "--split" in start_argv else ""


def _tab_of(start_argv) -> str:
    return start_argv[start_argv.index("--tab") + 1] if "--tab" in start_argv else ""


class SchemaTest(unittest.TestCase):
    """Decision 1 + 2: a closed vocabulary, and a default that preserves behaviour."""

    def test_absent_block_is_the_pre_14567_placement(self) -> None:
        for record in ({}, {"version": 2}, {"version": 2, SUBLANE_TAB_TOPOLOGY_KEY: {}}):
            with self.subTest(record=record):
                config = RepoLocalConfig.from_record(record)
                self.assertEqual(
                    config.sublane_tab_topology.mode, DEFAULT_SUBLANE_TAB_TOPOLOGY_MODE
                )
                self.assertEqual(config.sublane_tab_topology.mode, PER_LANE_TAB)
                self.assertFalse(config.sublane_tab_topology.shared_tab)

    def test_both_modes_parse_and_the_predicate_follows_the_mode(self) -> None:
        for mode, shared in ((PER_LANE_TAB, False), (SHARED_TAB, True)):
            with self.subTest(mode=mode):
                config = RepoLocalConfig.from_record(
                    {"version": 2, SUBLANE_TAB_TOPOLOGY_KEY: {"mode": mode}}
                )
                self.assertEqual(config.sublane_tab_topology.mode, mode)
                self.assertIs(config.sublane_tab_topology.shared_tab, shared)

    def test_unknown_value_key_version_and_shape_fail_closed(self) -> None:
        # A typo must never silently resolve to a topology — in either direction.
        for block in (
            {"mode": "shared"},  # near-miss value
            {"mode": "SHARED_TAB"},  # case variant: the vocabulary is lowercase literal
            {"mode": ""},
            {"mode": None},
            {"mode": 1},
            {"unknown": "x"},
            {"mode": SHARED_TAB, "extra": 1},
            {"version": 2},
            {"version": "1"},
            {"version": True},
            [],
            "shared_tab",
        ):
            with self.subTest(block=block):
                with self.assertRaises(RepoLocalConfigError):
                    RepoLocalConfig.from_record(
                        {"version": 2, SUBLANE_TAB_TOPOLOGY_KEY: block}
                    )

    def test_direct_construction_is_validated_too(self) -> None:
        # No dataclass back door: a directly-built config is checked as thoroughly as a
        # parsed one (the sibling requirement #14139 review j#83383 F3 pinned).
        for kwargs in ({"mode": "nope"}, {"version": 2}, {"version": True}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(SublaneTabTopologyError):
                    SublaneTabTopologyConfig(**kwargs)

    def test_block_is_valid_under_both_schema_versions(self) -> None:
        # The tab topology is orthogonal to the v1 -> v2 provider-topology migration, so it
        # is not a version-gated key on either side.
        for version in (1, 2):
            with self.subTest(version=version):
                config = RepoLocalConfig.from_record(
                    {"version": version, SUBLANE_TAB_TOPOLOGY_KEY: {"mode": SHARED_TAB}}
                )
                self.assertTrue(config.sublane_tab_topology.shared_tab)

    def test_key_is_in_the_closed_top_level_vocabulary(self) -> None:
        self.assertIn(SUBLANE_TAB_TOPOLOGY_KEY, REPO_LOCAL_CONFIG_KEYS)

    def test_key_survives_the_boundary_token_screen(self) -> None:
        # The repo-local schema rejects any key carrying a `pane` / `target` / `route` /
        # `authority` shape BEFORE the allowed-key check. The block name must therefore be
        # nameable at all — this is the pin that a rename into e.g. `pane_tab_topology`
        # would fail loudly rather than at some later adopter's config load.
        config = RepoLocalConfig.from_record(
            {"version": 2, SUBLANE_TAB_TOPOLOGY_KEY: {"mode": SHARED_TAB}}
        )
        self.assertTrue(config.sublane_tab_topology.shared_tab)


class StatusProjectionTest(unittest.TestCase):
    """The operator can read the effective topology, and whether they chose it."""

    def _rows(self, record):
        config = RepoLocalConfig.from_record(record)
        return {
            row.key: row
            for row in classify_config_sources(
                raw_record=record,
                config=config,
                schema_version=config.schema_version,
                legacy_migratable=False,
            )
        }

    def test_undeclared_reports_the_product_default_as_default(self) -> None:
        row = self._rows({"version": 2})["sublane_tab_topology.mode"]
        self.assertEqual(row.effective_value, PER_LANE_TAB)
        self.assertEqual(row.source, SOURCE_DEFAULT)

    def test_declared_reports_the_declared_mode_as_declared(self) -> None:
        record = {"version": 2, SUBLANE_TAB_TOPOLOGY_KEY: {"mode": SHARED_TAB}}
        row = self._rows(record)["sublane_tab_topology.mode"]
        self.assertEqual(row.effective_value, SHARED_TAB)
        self.assertEqual(row.source, SOURCE_DECLARED)

    def test_this_repo_declares_the_shared_tab(self) -> None:
        # The dogfood close condition: mozyo_bridge itself opts in, visibly.
        import yaml

        root = Path(__file__).resolve().parents[2]
        record = yaml.safe_load((root / ".mozyo-bridge" / "config.yaml").read_text())
        config = RepoLocalConfig.from_record(record)
        self.assertTrue(config.sublane_tab_topology.shared_tab)
        row = self._rows(record)["sublane_tab_topology.mode"]
        self.assertEqual(row.effective_value, SHARED_TAB)
        self.assertEqual(row.source, SOURCE_DECLARED)


class TabListParserTest(unittest.TestCase):
    """Decision 3: the label authority is read fail-closed, and verbatim."""

    def test_envelope_and_tolerant_shapes_parse(self) -> None:
        entries = [
            {"tab_id": "w1:t1", "label": SHARED_SUBLANE_TAB_LABEL},
            {"tab_id": "w1:t2", "label": ""},
        ]
        expected = {"w1:t1": SHARED_SUBLANE_TAB_LABEL, "w1:t2": ""}
        for payload in (
            json.dumps({"result": {"type": "tab_list", "tabs": entries}}),
            json.dumps({"tabs": entries}),
            json.dumps(entries),
        ):
            with self.subTest(payload=payload[:24]):
                self.assertEqual(_parse_tab_list(payload), expected)

    def test_labels_are_verbatim(self) -> None:
        # An EXACT match is the adopt authority, so a padded / case-variant label is a
        # DIFFERENT label and must not be normalised into the authority label.
        payload = json.dumps(
            {
                "result": {
                    "type": "tab_list",
                    "tabs": [
                        {"tab_id": "w1:t1", "label": " sublanes "},
                        {"tab_id": "w1:t2", "label": "Sublanes"},
                    ],
                }
            }
        )
        labels = _parse_tab_list(payload)
        self.assertEqual(labels, {"w1:t1": " sublanes ", "w1:t2": "Sublanes"})
        self.assertEqual(
            resolve_shared_tab_from_labels(
                labels, SHARED_SUBLANE_TAB_LABEL, target_workspace="w1"
            ),
            "",
            "a padded / case-variant label is not the shared tab",
        )

    def test_unreadable_and_conflicting_payloads_are_none(self) -> None:
        for payload in (
            "not json",
            json.dumps({"result": {"type": "something_else"}}),
            json.dumps({"result": {}}),
            None,
            123,
            # A duplicate tab identity in one snapshot: keeping the last-seen label would
            # make the whole authority order-dependent.
            json.dumps(
                {
                    "tabs": [
                        {"tab_id": "w1:t1", "label": SHARED_SUBLANE_TAB_LABEL},
                        {"tab_id": "w1:t1", "label": "other"},
                    ]
                }
            ),
        ):
            with self.subTest(payload=str(payload)[:32]):
                self.assertIsNone(_parse_tab_list(payload))

    def test_a_row_this_parser_cannot_read_makes_the_payload_unreadable(self) -> None:
        # Review j#91241 F1. Skipping unreadable rows let a plainly NON-EMPTY container
        # produce `{}`, which is the positive claim "this workspace has no tabs" — and the
        # resolver acts on that by creating one. Since the live payload shape is still
        # unmeasured, a rows-key-it-differently payload is exactly the realistic case, and
        # it would have minted a duplicate beside the real shared tab.
        for payload in (
            json.dumps({"tabs": [{"id": "w1:t1", "label": SHARED_SUBLANE_TAB_LABEL}]}),
            json.dumps({"tabs": ["not-a-tab-row"]}),
            json.dumps({"tabs": [{"tab_id": "", "label": SHARED_SUBLANE_TAB_LABEL}]}),
            json.dumps({"tabs": [{"tab_id": None}]}),
            json.dumps({"tabs": [{"tab_id": 7}]}),
            json.dumps({"tabs": [{"tab_id": "   "}]}),
            # One good row does not license skipping the bad one beside it.
            json.dumps(
                {
                    "tabs": [
                        {"tab_id": "w1:t1", "label": SHARED_SUBLANE_TAB_LABEL},
                        {"id": "w1:t2", "label": ""},
                    ]
                }
            ),
        ):
            with self.subTest(payload=payload[:44]):
                self.assertIsNone(
                    _parse_tab_list(payload),
                    "an unreadable row must not be reported as 'there are no tabs'",
                )

    def test_the_real_herdr_074_payload_parses(self) -> None:
        # The shape this parser was written against was UNMEASURED through R2 (j#91144 /
        # j#91266 both carried that caveat). These two payloads are verbatim `herdr tab
        # list --workspace <id>` output from herdr 0.7.4, captured read-only, so the
        # envelope, the field names, and the extra keys herdr sends are pinned as fact
        # rather than assumed. If a future herdr renames `tab_id` or moves the container,
        # this fails instead of quietly reporting "there are no tabs".
        single_tab = (
            '{"id":"cli:tab:list","result":{"tabs":[{"agent_status":"idle",'
            '"focused":true,"label":"1","number":1,"pane_count":2,"tab_id":"w45:t1",'
            '"workspace_id":"w45"}],"type":"tab_list"}}'
        )
        self.assertEqual(_parse_tab_list(single_tab), {"w45:t1": "1"})

        # A real sublane host running the per-lane topology: several labelled tabs.
        sublane_host = (
            '{"id":"cli:tab:list","result":{"tabs":['
            '{"agent_status":"done","focused":false,"label":"issue_14567_shared_tab_topology",'
            '"number":2,"pane_count":2,"tab_id":"w4B:t2","workspace_id":"w4B"},'
            '{"agent_status":"idle","focused":false,"label":"issue_14584_quote_safe_work_anchor",'
            '"number":4,"pane_count":2,"tab_id":"w4B:t4","workspace_id":"w4B"}'
            '],"type":"tab_list"}}'
        )
        self.assertEqual(
            _parse_tab_list(sublane_host),
            {
                "w4B:t2": "issue_14567_shared_tab_topology",
                "w4B:t4": "issue_14584_quote_safe_work_anchor",
            },
        )
        # Neither host carries the shared label, so both resolve to "create one" — and the
        # per-lane host is then stopped by the mid-transition guard, not by this parser.
        for payload, workspace in ((single_tab, "w45"), (sublane_host, "w4B")):
            self.assertEqual(
                resolve_shared_tab_from_labels(
                    _parse_tab_list(payload),
                    SHARED_SUBLANE_TAB_LABEL,
                    target_workspace=workspace,
                ),
                "",
            )

    def test_herdr_auto_labels_an_unlabelled_tab_with_its_number(self) -> None:
        # Measured on herdr 0.7.4: a tab created without `--label` comes back labelled
        # "1" (its number), NOT "". So "unlabelled" is not an empty-string case in
        # practice, and the exact-match authority is what keeps such a tab from ever
        # reading as the shared one.
        payload = '{"result":{"type":"tab_list","tabs":[{"tab_id":"w45:t1","label":"1"}]}}'
        self.assertEqual(_parse_tab_list(payload), {"w45:t1": "1"})
        self.assertEqual(
            resolve_shared_tab_from_labels(
                _parse_tab_list(payload),
                SHARED_SUBLANE_TAB_LABEL,
                target_workspace="w45",
            ),
            "",
        )

    def test_empty_list_is_readable_and_means_no_tabs(self) -> None:
        # The one case that legitimately yields `{}`: a readable container that really is
        # empty. This is what keeps the create path reachable at all.
        self.assertEqual(_parse_tab_list(json.dumps({"tabs": []})), {})


class SharedTabResolutionTest(unittest.TestCase):
    """Decision 3: adopt exactly one labelled tab, else create, else fail closed."""

    def test_unreadable_labels_fail_closed(self) -> None:
        with self.assertRaises(HerdrSessionStartError):
            resolve_shared_tab_from_labels(
                None, SHARED_SUBLANE_TAB_LABEL, target_workspace="w1"
            )

    def test_exactly_one_labelled_tab_is_adopted(self) -> None:
        labels = {"w1:t1": "", "w1:t2": SHARED_SUBLANE_TAB_LABEL}
        self.assertEqual(
            resolve_shared_tab_from_labels(
                labels, SHARED_SUBLANE_TAB_LABEL, target_workspace="w1"
            ),
            "w1:t2",
        )

    def test_no_labelled_tab_means_create(self) -> None:
        labels = {"w1:t1": "", "w1:t2": "lane-a"}
        self.assertEqual(
            resolve_shared_tab_from_labels(
                labels, SHARED_SUBLANE_TAB_LABEL, target_workspace="w1"
            ),
            "",
        )

    def test_two_labelled_tabs_fail_closed(self) -> None:
        labels = {
            "w1:t1": SHARED_SUBLANE_TAB_LABEL,
            "w1:t2": SHARED_SUBLANE_TAB_LABEL,
        }
        with self.assertRaises(HerdrSessionStartError):
            resolve_shared_tab_from_labels(
                labels, SHARED_SUBLANE_TAB_LABEL, target_workspace="w1"
            )

    def test_a_labelled_tab_in_another_workspace_is_not_ours(self) -> None:
        # `tab list` may answer for the whole server: another project's shared tab carries
        # the same label and must never be adopted here.
        labels = {"w9:t1": SHARED_SUBLANE_TAB_LABEL}
        self.assertEqual(
            resolve_shared_tab_from_labels(
                labels, SHARED_SUBLANE_TAB_LABEL, target_workspace="w1"
            ),
            "",
        )


class MixedTopologyGuardTest(unittest.TestCase):
    """Decision 3: a host mid-transition is refused, before anything is created."""

    def test_clean_slate_passes(self) -> None:
        verify_shared_tab_consistency(
            (),
            authority_tab="",
            target_workspace="w1",
            shared_label=SHARED_SUBLANE_TAB_LABEL,
        )

    def test_all_slots_in_the_authority_tab_passes(self) -> None:
        verify_shared_tab_consistency(
            (("lane-a", "w1:t1"), ("lane-b", "w1:t1")),
            authority_tab="w1:t1",
            target_workspace="w1",
            shared_label=SHARED_SUBLANE_TAB_LABEL,
        )

    def test_live_lanes_without_a_labelled_tab_fail_closed(self) -> None:
        with self.assertRaises(HerdrSessionStartError) as ctx:
            verify_shared_tab_consistency(
                (("lane-a", "w1:t1"),),
                authority_tab="",
                target_workspace="w1",
                shared_label=SHARED_SUBLANE_TAB_LABEL,
            )
        self.assertIn("mid-transition", str(ctx.exception))

    def test_loose_and_foreign_tab_slots_fail_closed(self) -> None:
        for host_slots in (
            (("lane-a", ""),),  # a pre-#13411 loose pane
            (("lane-a", "w1:t9"),),  # a per-lane tab
            (("lane-a", "w1:t1"), ("lane-b", "w1:t9")),  # spread across tabs
        ):
            with self.subTest(host_slots=host_slots):
                with self.assertRaises(HerdrSessionStartError):
                    verify_shared_tab_consistency(
                        host_slots,
                        authority_tab="w1:t1",
                        target_workspace="w1",
                        shared_label=SHARED_SUBLANE_TAB_LABEL,
                    )

    def test_host_inventory_excludes_the_coordinator_and_other_workspaces(self) -> None:
        ws = "wsA"
        rows = [
            _row(ws, "codex", "", "w1:p1", ""),  # default lane (coordinator)
            _row(ws, "codex", "lane-a", "w2:p1", "w2:t1"),  # the host
            _row(ws, "claude", "lane-a", "w2:p2", "w2:t1"),
            _row(ws, "codex", "lane-b", "w3:p1", "w3:t1"),  # a different workspace
        ]
        self.assertEqual(
            host_lane_slot_tabs(rows, ws, "w2"),
            (("lane-a", "w2:t1"), ("lane-a", "w2:t1")),
        )


class FenceTest(unittest.TestCase):
    """Decision 3: the create/adopt critical section is serialised, per workspace."""

    def test_lock_path_is_workspace_scoped(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            a = sublane_tab_create_lock_path("wsA", home=home)
            b = sublane_tab_create_lock_path("wsB", home=home)
            self.assertNotEqual(a, b)
            self.assertEqual(a.parent, home)
            self.assertTrue(a.name.startswith(SUBLANE_TAB_CREATE_LOCK_PREFIX))

    def test_unnameable_workspace_id_fails_closed(self) -> None:
        # Never sanitise: a value that is not a workspace id this build minted must not be
        # rewritten into "some safe filename" (two ids could collapse onto one lock, or a
        # separator could escape the home directory).
        with tempfile.TemporaryDirectory() as tmp:
            for bad in ("", "../escape", "ws/A", "ws A", "w" * 65, None, 7):
                with self.subTest(bad=bad):
                    with self.assertRaises(SublaneTabCreateLockUnavailable):
                        sublane_tab_create_lock_path(bad, home=Path(tmp))

    def test_lock_is_reentrant_across_sequential_holders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            with sublane_tab_create_lock("wsA", home=home):
                pass
            with sublane_tab_create_lock("wsA", home=home):
                pass
            self.assertTrue(sublane_tab_create_lock_path("wsA", home=home).exists())

    def test_body_exception_propagates_unchanged(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                with sublane_tab_create_lock("wsA", home=Path(tmp)):
                    raise ValueError("body fault")


class LockFailureReachesTheLaunchBoundaryTest(unittest.TestCase):
    """Review j#91241 F2: a fence failure must arrive as the launch's own typed error.

    The launch front doors (`herdr session-start`, the bare `mozyo` launch) catch exactly
    :class:`HerdrSessionStartError`. A raw lock error would escape them as an unformatted
    traceback instead of the fail-closed message they render — the fence would still be
    safe, but the operator would not be told what happened or what to do.
    """

    def _resolve(self, workspace_id, *, home=None):
        calls: list = []
        with self.assertRaises(HerdrSessionStartError) as ctx:
            resolve_shared_tab_target(
                [],
                workspace_id,
                "w1",
                list_tabs=lambda ws: calls.append(("list", ws)),
                create_tab=lambda ws, label: calls.append(("create", ws, label)),
                home=home,
            )
        return ctx.exception, calls

    def test_acquire_failure_is_typed_and_creates_nothing(self) -> None:
        exc, calls = self._resolve("bad/workspace id")
        self.assertEqual(calls, [], "zero herdr commands before the lock is held")
        self.assertIn("could not acquire", str(exc))

    def test_the_acquire_message_does_not_claim_the_workspace_was_untouched(self) -> None:
        # The coordinator fence says "no workspace / tab / agent was created" because it
        # guards the FIRST side effect of the run. This rail does not: the host workspace
        # is resolved earlier in the same run, so a fresh sublane host may already exist.
        # Copying that wider sentence here would make the message false.
        exc, _calls = self._resolve("bad/workspace id")
        message = str(exc)
        self.assertIn("no tab and no agent were created", message)
        self.assertNotIn("no workspace", message)

    def test_release_failure_says_the_tab_may_survive(self) -> None:
        # Release runs AFTER the body, so a tab may have been created. The message must
        # not read like the acquire case, or an operator would look for residue that the
        # acquire path never leaves — and miss the residue this path does.
        import contextlib

        @contextlib.contextmanager
        def _release_boom(workspace_id, *, home=None):
            yield
            raise SublaneTabCreateReleaseError("unlock refused")

        target = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_shared_tab.sublane_tab_create_lock"
        )
        with patch(target, _release_boom):
            with self.assertRaises(HerdrSessionStartError) as ctx:
                resolve_shared_tab_target(
                    [],
                    "wsA",
                    "w1",
                    list_tabs=lambda ws: {},
                    create_tab=lambda ws, label: ("w1:t1", "w1:t1-root"),
                )
        message = str(ctx.exception)
        self.assertIn("could not be released", message)
        self.assertIn("may remain", message)
        self.assertIn("adopt it idempotently", message)
        self.assertNotIn("no tab and no agent were created", message)

    def test_the_launch_front_door_renders_it_instead_of_raising_raw(self) -> None:
        # GUARD BITE at the real boundary: `prepare_session` must let the CLI's
        # `except HerdrSessionStartError` catch this, so the command exits with the
        # formatted message rather than a traceback.
        import contextlib

        @contextlib.contextmanager
        def _unavailable(workspace_id, *, home=None):
            raise SublaneTabCreateLockUnavailable("flock unavailable")
            yield  # pragma: no cover

        target = (
            "mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider."
            "application.herdr_shared_tab.sublane_tab_create_lock"
        )
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            with patch(target, _unavailable):
                with self.assertRaises(HerdrSessionStartError):
                    _prepare(
                        tmp,
                        herdr=herdr,
                        providers=["codex", "claude"],
                        lane="lane-a",
                        sublane_tab_topology=SHARED,
                    )
            self.assertEqual(herdr.tab_creates, [])
            self.assertEqual(herdr.start_argvs, [])

    def test_a_body_failure_is_not_relabelled_as_a_lock_failure(self) -> None:
        # The fence propagates a body exception unchanged; the conversion above must not
        # swallow it into "could not acquire / release the lock".
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(HerdrSessionStartError) as ctx:
                resolve_shared_tab_target(
                    [],
                    "wsA",
                    "w1",
                    list_tabs=lambda ws: None,  # unreadable labels
                    create_tab=lambda ws, label: ("w1:t1", "root"),
                    home=Path(tmp),
                )
            message = str(ctx.exception)
            self.assertIn("unreadable", message)
            self.assertNotIn("single-flight lock", message)


class GeometryDecisionTest(unittest.TestCase):
    """Decision 4: two axes, and the focus that keeps a pair together."""

    def test_container_occupancy_equals_lane_occupancy_outside_shared_tab(self) -> None:
        for lane_occupancy in (0, 1, 2):
            with self.subTest(lane_occupancy=lane_occupancy):
                self.assertEqual(
                    initial_container_occupancy(
                        lane_occupancy,
                        shared_tab=False,
                        target_tab="w1:t1",
                        host_slot_tabs=(("lane-b", "w1:t1"),),
                    ),
                    lane_occupancy,
                    "per_lane_tab must not consult other lanes",
                )

    def test_shared_tab_counts_every_lane_in_the_tab(self) -> None:
        host = (("lane-a", "w1:t1"), ("lane-a", "w1:t1"), ("lane-b", "w1:t9"))
        self.assertEqual(
            initial_container_occupancy(
                0, shared_tab=True, target_tab="w1:t1", host_slot_tabs=host
            ),
            2,
            "the lane's own count is 0 but the container holds two panes",
        )

    def test_a_freshly_minted_shared_tab_is_empty(self) -> None:
        self.assertEqual(
            initial_container_occupancy(
                0, shared_tab=True, target_tab="w1:t5", host_slot_tabs=(("a", "w1:t1"),)
            ),
            0,
        )

    def test_focus_follows_the_lane_not_the_container(self) -> None:
        # The #14567 divergence: a fresh lane joining an occupied shared tab still places
        # the pane its sibling must split, so it must be focused.
        self.assertTrue(
            resolve_focus_first_launch(
                split_direction="down", launch_count=2, lane_occupancy=0
            )
        )
        self.assertFalse(
            resolve_focus_first_launch(
                split_direction="down", launch_count=2, lane_occupancy=1
            ),
            "a heal joins its own live sibling, which is already the split target",
        )
        self.assertFalse(
            resolve_focus_first_launch(
                split_direction="down", launch_count=1, lane_occupancy=0
            ),
            "a single-provider request has no second slot to place",
        )

    def test_first_lane_slot_in_an_occupied_container_opens_a_column(self) -> None:
        split, focus, deferred = slot_placement(
            "launch",
            "codex",
            split_direction="down",
            inter_lane_split=INTER_LANE_SPLIT_DIRECTION,
            occupancy=2,
            lane_occupancy=0,
            config_order=("codex", "claude"),
            focus_first=True,
        )
        self.assertEqual(split, INTER_LANE_SPLIT_DIRECTION)
        self.assertTrue(focus, "the lane's first pane must own its own split target")
        self.assertFalse(
            deferred, "a lane opening its own column launched its primary first"
        )

    def test_second_lane_slot_splits_its_own_sibling_on_the_pair_axis(self) -> None:
        split, focus, _ = slot_placement(
            "launch",
            "claude",
            split_direction="down",
            inter_lane_split=INTER_LANE_SPLIT_DIRECTION,
            occupancy=3,
            lane_occupancy=1,
            config_order=("codex", "claude"),
            focus_first=True,
        )
        self.assertEqual(split, "down")
        self.assertFalse(focus, "only the lane's first launch is ever focused")

    def test_empty_container_still_occupies_without_a_split(self) -> None:
        split, focus, _ = slot_placement(
            "launch",
            "codex",
            split_direction="down",
            inter_lane_split=INTER_LANE_SPLIT_DIRECTION,
            occupancy=0,
            lane_occupancy=0,
            config_order=None,
            focus_first=True,
        )
        self.assertEqual(split, "")
        self.assertTrue(focus)

    def test_non_launch_slots_never_carry_a_placement_flag(self) -> None:
        for kind in ("adopt", "planned", "stale", "unattested"):
            with self.subTest(kind=kind):
                self.assertEqual(
                    slot_placement(
                        kind,
                        "codex",
                        split_direction="down",
                        inter_lane_split=INTER_LANE_SPLIT_DIRECTION,
                        occupancy=2,
                        lane_occupancy=0,
                        config_order=None,
                        focus_first=True,
                    ),
                    ("", False, False),
                )

    def test_the_default_lane_cannot_be_put_in_a_shared_tab(self) -> None:
        # The coordinator pair has no tab, so `shared_tab` is meaningless for it — and
        # actively harmful if honoured: its `target_tab` is always empty, so the shared
        # occupancy branch would report 0 for a HEAL that has a live sibling and the
        # healing slot would emit no `--split` at all. The plan builder ANDs the mode with
        # the lane class itself, so a caller that forgets cannot corrupt the pair.
        plan = resolve_container_plan(
            [],
            "wsA",
            "w1",
            "",
            lane_class="default",
            target_tab="",
            lane_slot_tabs=(),
            config_split="down",
            launch_count=1,
            shared_tab=True,
            host_slot_tabs=(("lane-a", "w1:t1"),),
        )
        self.assertEqual(plan.inter_lane_split, "")
        self.assertEqual(
            plan.occupancy,
            plan.lane_occupancy,
            "the default lane's container is its own workspace, never a shared tab",
        )

    def test_a_coordinator_heal_still_splits_beside_its_live_sibling(self) -> None:
        # The end-to-end shape of the same guard: a shared_tab repo healing ONE coordinator
        # slot must still place it beside the live sibling on the pair axis.
        ws = "wsA"
        rows = [_row(ws, "codex", "", "w1:p2", "")]
        plan = resolve_container_plan(
            rows,
            ws,
            "w1",
            "",
            lane_class="default",
            target_tab="",
            lane_slot_tabs=(),
            config_split="down",
            launch_count=1,
            shared_tab=True,
            host_slot_tabs=(),
        )
        self.assertEqual(plan.occupancy, 1, "the live coordinator sibling occupies")
        split, focus, _ = slot_placement(
            "launch",
            "claude",
            split_direction=plan.split_direction,
            inter_lane_split=plan.inter_lane_split,
            occupancy=plan.occupancy,
            lane_occupancy=plan.lane_occupancy,
            config_order=None,
            focus_first=plan.focus_first,
        )
        self.assertEqual(split, "down")
        self.assertFalse(focus)

    def test_container_plan_requires_both_axes(self) -> None:
        # A default on either #14567 field would let a caller that forgot to wire the new
        # axis build a plan that silently reads as `per_lane_tab`.
        with self.assertRaises(TypeError):
            ContainerPlan(split_direction="down", occupancy=0, focus_first=False)


class PerLaneTabIsUnchangedTest(unittest.TestCase):
    """Decision 2: an undeclared repo, and an explicit ``per_lane_tab``, launch as before."""

    def _run(self, topology):
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-a",
                sublane_tab_topology=topology,
            )
            return herdr, result

    def test_no_tab_list_is_issued_and_the_tab_carries_the_lane_label(self) -> None:
        for topology in (None, PER_LANE):
            with self.subTest(topology=topology):
                herdr, result = self._run(topology)
                self.assertEqual(
                    herdr.tab_lists, [], "per_lane_tab must issue no extra herdr command"
                )
                self.assertEqual(len(herdr.tab_creates), 1)
                created = herdr.tab_creates[0]
                self.assertEqual(created[created.index("--label") + 1], "lane-a")
                self.assertEqual(result.herdr_tab_id, "wZ:t1")

    def test_pair_splits_on_the_lane_placement_axis_only(self) -> None:
        herdr, _result = self._run(PER_LANE)
        self.assertEqual(_split_of(herdr.start_argvs[0]), "")
        self.assertEqual(_split_of(herdr.start_argvs[1]), "down")


class SharedTabLaunchTest(unittest.TestCase):
    """Decision 3 + 4: the first lane mints, the next adopts, and the columns form."""

    def test_first_lane_mints_the_labelled_shared_tab(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
            )
            self.assertEqual(len(herdr.tab_lists), 1, "the labels are read once, in-lock")
            self.assertEqual(len(herdr.tab_creates), 1)
            created = herdr.tab_creates[0]
            self.assertEqual(
                created[created.index("--label") + 1], SHARED_SUBLANE_TAB_LABEL
            )
            self.assertEqual(result.herdr_tab_id, "wZ:t1")
            # A freshly minted tab is empty: the first slot occupies, the second splits on
            # the pair axis. No inter-lane split is asked for.
            self.assertEqual(_split_of(herdr.start_argvs[0]), "")
            self.assertEqual(_split_of(herdr.start_argvs[1]), "down")

    def test_second_lane_adopts_the_same_tab_and_opens_a_column(self) -> None:
        ws_holder = {}

        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            _first, ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
            )
            ws_holder["ws"] = ws
            second, _ws2, _panes2 = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-b",
                sublane_tab_topology=SHARED,
            )
            # Same host workspace, same tab — no second tab was minted.
            self.assertEqual(second.herdr_tab_id, "wZ:t1")
            self.assertEqual(len(herdr.tab_creates), 1, "the shared tab is minted once")
            self.assertTrue(
                all(_tab_of(argv) == "wZ:t1" for argv in herdr.start_argvs),
                "every slot of both lanes lands in the shared tab",
            )
            # lane-b's first slot opens its column; its sibling splits that pane.
            self.assertEqual(_split_of(herdr.start_argvs[2]), INTER_LANE_SPLIT_DIRECTION)
            self.assertIn("--focus", herdr.start_argvs[2])
            self.assertEqual(_split_of(herdr.start_argvs[3]), "down")
            self.assertNotIn("--focus", herdr.start_argvs[3])

    def test_pair_identity_is_preserved_across_lanes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            first, ws, _p = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
            )
            second, _ws, _p2 = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-b",
                sublane_tab_topology=SHARED,
            )
            names = {slot.assigned_name for slot in first.slots + second.slots}
            self.assertEqual(
                names,
                {
                    encode_assigned_name(ws, role, lane)
                    for lane in ("lane-a", "lane-b")
                    for role in ("codex", "claude")
                },
                "sharing a tab must not blur the per-lane assigned identities",
            )

    def test_heal_rejoins_the_shared_tab_on_the_pair_axis(self) -> None:
        # A single-provider heal beside a live sibling splits on the PAIR axis, not the
        # inter-lane one: it is placed next to its own gateway, not next to another lane.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            herdr.tab_labels["wZ:t1"] = SHARED_SUBLANE_TAB_LABEL

            def rows(ws):
                return [
                    _row(ws, "codex", "lane-a", "wZ:p2", "wZ:t1"),
                    _row(ws, "codex", "lane-b", "wZ:p4", "wZ:t1"),
                    _row(ws, "claude", "lane-b", "wZ:p5", "wZ:t1"),
                ]

            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
                rows=rows,
            )
            self.assertEqual(result.herdr_tab_id, "wZ:t1")
            self.assertEqual(len(herdr.tab_creates), 0, "an existing tab is adopted")
            self.assertEqual(_split_of(herdr.start_argvs[0]), "down")
            self.assertNotIn(
                "--focus", herdr.start_argvs[0], "a heal never focuses / moves a pane"
            )

    def test_unreadable_tab_labels_create_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            herdr.tab_list_unreadable = True
            with self.assertRaises(HerdrSessionStartError):
                _prepare(
                    tmp,
                    herdr=herdr,
                    providers=["codex", "claude"],
                    lane="lane-a",
                    sublane_tab_topology=SHARED,
                )
            self.assertEqual(herdr.tab_creates, [])
            self.assertEqual(herdr.start_argvs, [])

    def test_a_malformed_tab_row_creates_nothing(self) -> None:
        # Review j#91241 F1, end to end: the whole launch must refuse rather than mint a
        # second shared tab beside one it failed to recognise.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            herdr.tab_list_rows = [{"id": "wZ:t1", "label": SHARED_SUBLANE_TAB_LABEL}]
            with self.assertRaises(HerdrSessionStartError):
                _prepare(
                    tmp,
                    herdr=herdr,
                    providers=["codex", "claude"],
                    lane="lane-a",
                    sublane_tab_topology=SHARED,
                )
            self.assertEqual(herdr.tab_creates, [])
            self.assertEqual(herdr.start_argvs, [])

    def test_ambiguous_shared_label_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            herdr.tab_labels = {
                "wZ:t1": SHARED_SUBLANE_TAB_LABEL,
                "wZ:t2": SHARED_SUBLANE_TAB_LABEL,
            }
            with self.assertRaises(HerdrSessionStartError):
                _prepare(
                    tmp,
                    herdr=herdr,
                    providers=["codex", "claude"],
                    lane="lane-a",
                    sublane_tab_topology=SHARED,
                )
            self.assertEqual(herdr.tab_creates, [])
            self.assertEqual(herdr.start_argvs, [])

    def test_mid_transition_host_creates_nothing(self) -> None:
        # Live per-lane tabs + a config flipped to shared_tab: refuse rather than create a
        # second topology beside the live one, or move what is live.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")

            def rows(ws):
                return [
                    _row(ws, "codex", "lane-a", "wZ:p2", "wZ:t7"),
                    _row(ws, "claude", "lane-a", "wZ:p3", "wZ:t7"),
                ]

            with self.assertRaises(HerdrSessionStartError) as ctx:
                _prepare(
                    tmp,
                    herdr=herdr,
                    providers=["codex", "claude"],
                    lane="lane-b",
                    sublane_tab_topology=SHARED,
                    rows=rows,
                )
            self.assertIn("mid-transition", str(ctx.exception))
            self.assertEqual(herdr.tab_creates, [])
            self.assertEqual(herdr.start_argvs, [])

    def test_default_lane_is_untouched_by_the_mode(self) -> None:
        # The coordinator pair has no tab at all, so the topology cannot reach it: no
        # `tab list`, no `tab create`, and the pre-#14567 geometry.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")
            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="",
                sublane_tab_topology=SHARED,
            )
            self.assertEqual(herdr.tab_lists, [])
            self.assertEqual(herdr.tab_creates, [])
            self.assertEqual(result.herdr_tab_id, "")
            self.assertEqual(_split_of(herdr.start_argvs[1]), "down")


class SharedTabLayoutTest(unittest.TestCase):
    """What the operator SEES, taken after the root-pane reclaim (#13646 R1-F1 shape)."""

    def _two_lanes(self, tmp, *, lane_placement=None):
        herdr = _LayoutHerdr(created_workspace="wZ")
        first, _ws, first_panes = _prepare(
            tmp,
            herdr=herdr,
            providers=["codex", "claude"],
            lane="lane-a",
            sublane_tab_topology=SHARED,
            lane_placement=lane_placement,
        )
        second, _ws2, second_panes = _prepare(
            tmp,
            herdr=herdr,
            providers=["codex", "claude"],
            lane="lane-b",
            sublane_tab_topology=SHARED,
            lane_placement=lane_placement,
        )
        return herdr, first_panes, second_panes

    def test_each_pair_is_vertical_and_the_lanes_are_side_by_side(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            herdr, a_panes, b_panes = self._two_lanes(tmp)
            # Within a lane: the product-default vertical pair (#14568) survives.
            self.assertEqual(
                herdr.direction_between(a_panes["codex"], a_panes["claude"]), "down"
            )
            self.assertEqual(
                herdr.direction_between(b_panes["codex"], b_panes["claude"]), "down"
            )
            # Between lanes: a column.
            self.assertEqual(
                herdr.direction_between(a_panes["codex"], b_panes["codex"]),
                INTER_LANE_SPLIT_DIRECTION,
            )

    def test_the_arriving_lane_lands_as_one_contiguous_pair(self) -> None:
        # The property the focus exists for: a lane joining an occupied tab splits a
        # FOREIGN pane to open its column, so if its own first pane were not focused its
        # sibling would split that foreign pane and the arriving pair would be built around
        # someone else's. Pinned structurally — the split separating the arriving lane's
        # two panes must hold exactly those two panes and nothing else.
        with tempfile.TemporaryDirectory() as tmp:
            herdr, _a_panes, b_panes = self._two_lanes(tmp)
            self.assertEqual(
                _separating_subtree_panes(herdr, b_panes["codex"], b_panes["claude"]),
                {b_panes["codex"], b_panes["claude"]},
                "the arriving lane's pair must be contiguous, not built around a "
                "foreign lane's pane",
            )

    def test_column_purity_for_already_placed_lanes_is_explicitly_not_claimed(
        self,
    ) -> None:
        # Honest scope pin, so a reader does not mistake this for a defect. `agent start`
        # has no pane-target flag and no command focuses an arbitrary pane, so a new lane
        # can only split ONE existing pane — which lands its column inside one half of a
        # lane already there. Owner clarification 2026-07-28 waives strict adjacency /
        # equal width / append-at-the-right for this US, and Design Answer j#91144
        # Decision 4 rejects the `pane move --target-pane` bounce that would be needed
        # (it belongs to #14605's live-placement responsibility). What IS guaranteed is
        # the two pins above: one shared tab, and each pair placed relative to itself.
        with tempfile.TemporaryDirectory() as tmp:
            herdr, a_panes, b_panes = self._two_lanes(tmp)
            self.assertNotEqual(
                _separating_subtree_panes(herdr, a_panes["codex"], a_panes["claude"]),
                {a_panes["codex"], a_panes["claude"]},
                "if this becomes contiguous the layout guarantee GREW; re-read #14604 "
                "before relaxing the documented best-effort boundary",
            )

    def test_pair_axis_rollback_still_applies_inside_the_shared_tab(self) -> None:
        # The two axes are independent: `split: right` on the sublane class rolls the PAIR
        # back to horizontal without changing the inter-lane placement.
        with tempfile.TemporaryDirectory() as tmp:
            placement = LanePlacementConfig.from_record({"sublane": {"split": "right"}})
            herdr, a_panes, b_panes = self._two_lanes(tmp, lane_placement=placement)
            self.assertEqual(
                herdr.direction_between(a_panes["codex"], a_panes["claude"]), "right"
            )
            self.assertEqual(
                herdr.direction_between(a_panes["codex"], b_panes["codex"]),
                INTER_LANE_SPLIT_DIRECTION,
            )


class LauncherConfigParsePreflightTest(unittest.TestCase):
    """Decision 5: an old launcher refuses the new block before any herdr write."""

    def test_a_launcher_predating_the_block_rejects_the_config(self) -> None:
        # The #14258 preflight runs the LAUNCHER's own parser against the exact target
        # config. A build that predates this block has `sublane_tab_topology` outside its
        # closed top-level vocabulary, so it rejects the document — which is what turns a
        # runtime skew into a zero-side-effect refusal rather than a half-built lane.
        legacy_keys = REPO_LOCAL_CONFIG_KEYS - {SUBLANE_TAB_TOPOLOGY_KEY}
        record = {"version": 2, SUBLANE_TAB_TOPOLOGY_KEY: {"mode": SHARED_TAB}}
        offending = [key for key in record if key != "version" and key not in legacy_keys]
        self.assertEqual(
            offending,
            [SUBLANE_TAB_TOPOLOGY_KEY],
            "the new block must be an unknown top-level key to any older parser, which is "
            "exactly what the #14258 config-parse conjunct detects",
        )

    def test_the_preflight_conjunct_is_still_wired_ahead_of_every_write(self) -> None:
        # GUARD BITE: Decision 5 delegates to #14258 instead of adding a capability token,
        # so this pins that the delegation target is actually on the launch path — and
        # ahead of the tab write this US adds, not only the workspace write.
        import inspect

        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application import (  # noqa: E501
            herdr_session_start,
        )

        src = inspect.getsource(herdr_session_start._prepare_session_locked)
        for later in ("resolve_lane_tab", "_create_tab", "_list_tab_labels"):
            self.assertLess(
                src.index("preflight_launcher_compatibility"),
                src.index(later),
                f"the launcher compatibility conjunction must precede {later}",
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
