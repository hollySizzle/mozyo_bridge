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
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

# The shared herdr fakes live under `tests/support`; make them importable the same way the
# sibling regression suites do rather than relying on another test module's import order.
_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from mozyo_bridge.core.state.sublane_tab_fence import (  # noqa: E402
    SUBLANE_TAB_CREATE_LOCK_PREFIX,
    SublaneTabCreateLockUnavailable,
    SublaneTabCreateReleaseError,
    sublane_tab_create_lock,
    sublane_tab_create_lock_path,
)
from support.herdr_fake import (  # noqa: E402
    apply_resize_amount,
    render_shared_tab_layout,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_pair_split_ratio import (  # noqa: E501
    RATIO_APPLIED,
    RATIO_MATCHED,
    RATIO_NOT_APPLICABLE,
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
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501
    StartupProbe,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    decode_assigned_name,
    encode_assigned_name,
)

# The herdr fakes live with the unit suite that introduced them (#13646 / R1-F1 / #14567).
# Importing them keeps this file a pin on the behaviour instead of a second, subtly
# different simulation of herdr.
from tests.unit.e_140_adapter_provider.f_130_terminal_runtime_provider.test_herdr_session_start import (  # noqa: E501
    PROVIDER_BINS,
    _Herdr,
    _LayoutHerdr,
    _launch_env,
)

ISSUE = "14567"

SHARED = SublaneTabTopologyConfig(mode=SHARED_TAB)
PER_LANE = SublaneTabTopologyConfig(mode=PER_LANE_TAB)


_FAST_PROBE = StartupProbe(polls=3, interval=0.0, sleeper=lambda _seconds: None)


def _wrapping_launch_env(tmp, binpath):
    """A launch env whose PATH carries a resolvable ``mozyo-bridge`` (the #13637 wrapper).

    Without it the launcher cannot wrap, every launched slot comes back
    ``attestation_unavailable``, and ``SessionStartResult.ok`` is False for a reason that
    has nothing to do with the axis under test.

    That is not a cosmetic detail here. Review j#92057 F1: the first cut of the ratio pins
    below hit that False ``result.ok``, and instead of asking why, the assertion was
    narrowed to ``ratio_ok`` — discarding the one aggregate signal that would have shown
    the fixture was measuring a single two-pane container while claiming two lanes. Making
    a genuinely healthy run reachable is what lets those pins assert ``result.ok`` honestly,
    with the attestation written by the fake wrapper and read back through the real rail
    (rather than an injected reader that would vouch for anything).

    The PATH replaces the default one, so it must also carry the #13441 provider stubs or
    argv[0] would not resolve. Both components are absolute; only this dir holds
    ``mozyo-bridge`` and only the shared dir holds the providers, so neither lookup is
    ambiguous.
    """
    bindir = Path(tmp) / "bin"
    bindir.mkdir(exist_ok=True)
    launcher = bindir / "mozyo-bridge"
    if not launcher.exists():
        launcher.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        launcher.chmod(
            launcher.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH
        )
    env = _launch_env(binpath)
    env["PATH"] = os.pathsep.join([str(bindir), str(PROVIDER_BINS.bin_dir)])
    return env


def _prepare(
    tmp,
    *,
    herdr,
    providers,
    lane,
    sublane_tab_topology=None,
    lane_placement=None,
    pair_order=None,
    rows=None,
    repo_name="repo",
    attested=False,
):
    """Run the real ``prepare_session`` against a fake herdr in an isolated home.

    Returns ``(result, workspace_id, {provider: pane_locator})`` — the pane map is what the
    layout assertions key on, since a pane id is the only handle
    ``_LayoutHerdr.direction_between`` understands.

    ``attested`` wraps the launch (:func:`_wrapping_launch_env`) and points the fake
    wrapper's attestation write at the home the launcher resolves, so the run can actually
    come up healthy — what a case asserting ``result.ok`` needs. It defaults to False so
    every pre-existing case keeps its exact previous behaviour.
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
        env = _launch_env(binpath)
        extra: dict = {}
        if attested:
            env = _wrapping_launch_env(tmp, binpath)
            # Point the fake wrapper's attestation write at the SAME home the launcher
            # resolves, so a wrapped launch in a test attests where the real one does.
            herdr.attest_home = home
            extra["probe"] = _FAST_PROBE
        if pair_order is not None:
            extra["pair_order"] = pair_order
        result = prepare_session(
            repo_root=repo,
            providers=providers,
            lane_id=lane,
            env=env,
            runner=herdr.run,
            lane_placement=lane_placement,
            sublane_tab_topology=sublane_tab_topology,
            **extra,
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
        #
        # Runs on `_SharedTabHerdr` because the flat fake could not represent this tab
        # (review j#92057 F1): it mints `wZ:p2`/`wZ:p3` for launches, so the seeded lane-a
        # sibling and the healed slot collapsed into one pane, and its layout renders any
        # container that is not exactly two panes with NO splits. The run therefore came
        # back `ratio_outcome=failed` while this test — asserting only the argv — passed,
        # which is precisely the state in which a fixture gap and a production gap are
        # indistinguishable. The ratio axis is asserted below so that can no longer hide.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _SharedTabHerdr(created_workspace="wZ")
            herdr.tab_labels["wZ:t1"] = SHARED_SUBLANE_TAB_LABEL

            def rows(ws):
                return [
                    _row(ws, "codex", "lane-a", "wZ:p90", "wZ:t1"),
                    _row(ws, "codex", "lane-b", "wZ:p94", "wZ:t1"),
                    _row(ws, "claude", "lane-b", "wZ:p95", "wZ:t1"),
                ]

            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
                pair_order=("codex", "claude"),
                rows=rows,
                attested=True,
            )
            herdr.assert_no_locator_collision(self, result)
            self.assertEqual(result.herdr_tab_id, "wZ:t1")
            self.assertEqual(len(herdr.tab_creates), 0, "an existing tab is adopted")
            self.assertEqual(_split_of(herdr.start_argvs[0]), "down")
            self.assertNotIn(
                "--focus", herdr.start_argvs[0], "a heal never focuses / moves a pane"
            )
            self.assertTrue(
                result.ratio_ok,
                f"a heal that rejoined its own pair must not leave the ratio axis "
                f"unresolved: {result.ratio_outcome} / {result.ratio_detail}",
            )
            self.assertTrue(result.ok, result.ratio_detail)

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


class _SharedTabHerdr(_Herdr):
    """A herdr fake whose tab holds SEVERAL lanes, each as its own column.

    ``_Herdr`` models a container as at most one divider, which is everything a per-lane
    tab has. Review j#92057 F1 showed that is not enough to state the #14567 x #14569
    composition as behaviour, in two independent ways:

    1. **locator collision.** ``_Herdr`` mints ``<ws>:p2``, ``<ws>:p3``, ... in launch
       order, so a test seeding an "existing" lane at those ids gets the freshly launched
       pane and the pre-existing one as the SAME pane. A ratio then measured "successfully"
       against a two-pane tab that was supposed to hold four. This fake namespaces its
       launches (``:pL<n>``) so a seeded pane and a launched one can never coincide, and
       :meth:`assert_no_locator_collision` states that as an assertion rather than a
       convention.
    2. **flat layout.** ``render_pane_layout`` renders anything other than exactly two
       panes with NO splits, so every divider in a populated shared tab is unidentifiable
       and the rail reports ``failed`` for fixture reasons. This fake tracks per-lane
       columns and renders the real tree through
       :func:`support.herdr_fake.render_shared_tab_layout`.

    Columns are recovered the way production does it — from the lane segment of each live
    slot's durable name — so the fake never needs to be told which lane a seeded pane
    belongs to.
    """

    #: Locator namespace for panes THIS fake launches. Disjoint from any `:p<n>` a test
    #: seeds, which is the whole point (see the class docstring).
    LAUNCH_PREFIX = "pL"

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        #: ``{container key: [[pane, ...] per lane column, in arrival order]}``
        self.columns: dict = {}
        #: ``{container key: [lane id per column]}`` — which column belongs to which lane.
        self.column_lanes: dict = {}
        #: ``{container key: [pair ratio per column]}``
        self.column_ratios: dict = {}
        #: ``{container key: [pair split axis per column]}`` — the direction the lane's own
        #: `--split` used. Recorded per column rather than assumed, because
        #: `lane_placement: {split: right}` makes the pair axis the SAME as the inter-lane
        #: one and the rail then has to pick the nearer divider of two same-axis splits.
        self.column_directions: dict = {}
        #: Columns whose PAIR divider a ``pane resize`` actually moved, in call order.
        self.resized_columns: list = []
        #: Inter-lane dividers a ``pane resize`` would have moved — the outcome that
        #: re-lays-out a neighbouring lane. Must stay empty on every path.
        self.inter_lane_resizes: list = []
        self._launch_seq = 0

    # -- identity ------------------------------------------------------------------

    def _mint_pane_id(self, workspace_id):
        self._launch_seq += 1
        return f"{workspace_id}:{self.LAUNCH_PREFIX}{self._launch_seq}"

    def assert_no_locator_collision(self, case, result) -> None:
        """Fail loudly if a launched slot reused a seeded pane's locator.

        The defect this class exists for was invisible precisely because it looked like a
        pass. Asserting the disjointness makes a future change to either numbering scheme
        break here, where the reason is written down, instead of in a ratio assertion whose
        message would blame the production code.
        """
        seeded = {str(row.get("pane_id") or "") for row in self.existing_rows}
        launched = {slot.locator for slot in result.slots if slot.locator}
        case.assertEqual(
            seeded & launched,
            set(),
            "a launched slot reused a seeded pane's locator; the fixture is measuring "
            "one pane while claiming two",
        )

    # -- geometry ------------------------------------------------------------------

    @staticmethod
    def _lane_of(name: str) -> str:
        decoded = decode_assigned_name(name)
        if not decoded.ok or decoded.identity is None:
            return ""
        return decoded.identity.lane_id

    def _seed_columns(self, key, workspace_id, tab_id):
        """Recover the pre-existing lanes' columns from the live inventory, in row order."""
        order: list = []
        grouped: dict = {}
        for row in self.existing_rows:
            locator = str(row.get("pane_id") or "")
            same_tab = (not tab_id) or str(row.get("tab_id") or "") == tab_id
            if not locator.startswith(f"{workspace_id}:") or not same_tab:
                continue
            lane = self._lane_of(str(row.get("name") or ""))
            if lane not in grouped:
                grouped[lane] = []
                order.append(lane)
            grouped[lane].append(locator)
        self.columns[key] = [grouped[lane] for lane in order]
        self.column_lanes[key] = list(order)
        self.column_ratios[key] = [0.5 for _ in order]
        self.column_directions[key] = ["down" for _ in order]

    def _place_pane(self, rest, pane_id, workspace_id, tab_id):
        """Place the launch in ITS OWN lane's column — a new one if the lane has none yet.

        Keyed on the lane decoded from the launch's assigned name, never on position. An
        earlier cut of this fake appended a pair-axis launch to the LAST column, which is
        the launching lane's only when that lane happens to be the rightmost; with the
        neighbour seeded after it, a heal silently joined the neighbour's column and the
        pair became unidentifiable. That is the same class of defect review j#92057 F1
        found in the fixture it replaced, so it is decided from identity here.
        """
        key = self._container_key(workspace_id, tab_id)
        if key not in self.columns:
            self._seed_columns(key, workspace_id, tab_id)
        columns = self.columns[key]
        lanes = self.column_lanes[key]
        ratios = self.column_ratios[key]
        directions = self.column_directions[key]
        split = rest[rest.index("--split") + 1] if "--split" in rest else ""
        if split:
            self.split_direction = split
        lane = self._lane_of(rest[2]) if len(rest) > 2 else ""
        if lane in lanes:
            # Splitting beside this lane's own sibling: the pair axis, inside its column.
            # The axis is whatever this launch actually asked for, not an assumption.
            index = lanes.index(lane)
            if pane_id not in columns[index]:
                columns[index].append(pane_id)
                if split:
                    directions[index] = split
            return
        # This lane has no column yet: opening one (or occupying an empty tab).
        columns.append([pane_id])
        lanes.append(lane)
        ratios.append(0.5)
        directions.append("down")

    def column_of_lane(self, lane, key="wZ:t1"):
        """The panes of ``lane``'s column — so a test can name the column it means."""
        lanes = self.column_lanes.get(key, [])
        return list(self.columns[key][lanes.index(lane)]) if lane in lanes else []

    def _column_of(self, pane_id):
        for key, columns in self.columns.items():
            for index, panes in enumerate(columns):
                if pane_id in panes:
                    return key, index
        return "", -1

    def _layout_payload(self, pane_id):
        key, _index = self._column_of(pane_id)
        if not key:
            return render_shared_tab_layout(columns=[], pair_ratios=[])
        return render_shared_tab_layout(
            columns=self.columns[key],
            pair_ratios=self.column_ratios[key],
            pair_directions=self.column_directions[key],
            width=self.split_extent,
            height=self.split_cross,
        )

    #: The canonical AXIS each ``pane resize --direction`` token addresses. herdr carries the
    #: sign of the move in the token (:func:`herdr_pair_split_ratio.resize_step`), so
    #: shrinking a divider issues ``left`` / ``up`` while the layout only ever labels a split
    #: ``right`` / ``down``. Matching the token against the label directly finds no split at
    #: all and silently applies nothing — every ``ratio < 0.5`` run then fails for a fixture
    #: reason (review j#92117 R3-F1). The token selects the axis; it does not name it.
    RESIZE_AXIS = {"right": "right", "left": "right", "down": "down", "up": "down"}

    def _apply_resize(self, direction, amount, pane=""):
        """Resolve the resize the way herdr does: the NEAREST same-axis ancestor split.

        Deliberately not "the addressed pane's own column divider". Assuming that would
        make the fake grant the very property `governing_split` exists to guarantee — the
        production rail refuses precisely when the divider herdr would move is NOT the
        pair's, so a fake that always moved the pair's could never show that failure, and
        the guard's pin would be vacuous.

        Modelled as the smallest split ON THE ADDRESSED AXIS whose rect contains the pane,
        read off the rendered layout so the fake and the code agree on one geometry. The
        axis comes from :data:`RESIZE_AXIS`, never from the raw token: the token also
        carries the direction of travel, which :func:`apply_resize_amount` (not the
        candidate search) is what consumes. Whether the winner was the lane's own pair
        divider or the INTER-LANE one is recorded separately: the latter is the outcome that
        re-lays-out a neighbouring lane, and a test asserts it never happens rather than
        trusting the argv.
        """
        key, index = self._column_of(pane)
        if index < 0:
            return
        axis = self.RESIZE_AXIS.get(direction)
        if axis is None:
            return
        layout = self._layout_payload(pane)["result"]["layout"]
        rect = next(
            (p["rect"] for p in layout["panes"] if p["pane_id"] == pane), None
        )
        if rect is None:
            return

        def contains(outer, inner):
            return (
                outer["x"] <= inner["x"]
                and outer["y"] <= inner["y"]
                and outer["x"] + outer["width"] >= inner["x"] + inner["width"]
                and outer["y"] + outer["height"] >= inner["y"] + inner["height"]
            )

        candidates = [
            split
            for split in layout["splits"]
            if split["direction"] == axis and contains(split["rect"], rect)
        ]
        if not candidates:
            return
        nearest = min(
            candidates, key=lambda s: s["rect"]["width"] * s["rect"]["height"]
        )
        if nearest["id"].startswith("split_lane_"):
            # herdr would move the divider BETWEEN lanes. Record it; do not pretend the
            # pair moved.
            self.inter_lane_resizes.append(int(nearest["id"].rsplit("_", 1)[1]))
            return
        moved = int(nearest["id"].rsplit("_", 1)[1])
        self.column_ratios[key][moved] = apply_resize_amount(
            self.column_ratios[key][moved], direction, amount
        )
        self.resized_columns.append(moved)

    def pair_ratio_of(self, pane_id) -> float:
        """The live ratio of the column holding ``pane_id`` — herdr's state, not the ask."""
        key, index = self._column_of(pane_id)
        return self.column_ratios[key][index] if index >= 0 else -1.0


class SharedTabFakeModelTest(unittest.TestCase):
    """Executable pins on :class:`_SharedTabHerdr` itself — the model the claims below rest on.

    Review j#92117 F2: the composition pins assert ``inter_lane_resizes == []``, which proves
    nothing unless the recorder can fire. j#92111 showed it could with a one-off manual
    probe, but a mutation clearing the list on every call left all five composition pins
    green — so nothing that RUNS pinned the observable's liveness, and a future change could
    make every one of those assertions vacuous without a single test noticing. A manual probe
    is evidence about a moment; only a test is evidence about the repository.

    These drive the fake directly (constructing the container state rather than launching
    into it) because the property under test is the fake's own resolution rule, and the
    launch path cannot reach a one-pane column at measurement time — that is exactly the
    structural argument recorded in j#92111, and it is why the liveness has to be pinned
    here instead.
    """

    KEY = "wZ:t1"

    def _herdr(self, columns, lanes, directions=None):
        herdr = _SharedTabHerdr(created_workspace="wZ")
        herdr.columns[self.KEY] = [list(column) for column in columns]
        herdr.column_lanes[self.KEY] = list(lanes)
        herdr.column_ratios[self.KEY] = [0.5 for _ in columns]
        herdr.column_directions[self.KEY] = list(directions or ["down"] * len(columns))
        return herdr

    def test_a_one_pane_column_resolves_to_the_inter_lane_divider(self) -> None:
        # A column with no pair divider of its own has no same-axis split inside it, so the
        # nearest one containing the pane is the divider BETWEEN lanes. This is the input
        # that makes `inter_lane_resizes` non-empty; without it, every `== []` assertion in
        # this file passes for a recorder that never fires.
        # Both tokens of the axis are driven, which also pins the R3-F1 normalization.
        for token in ("right", "left"):
            with self.subTest(token=token):
                herdr = self._herdr(
                    [["wZ:pA"], ["wZ:pB1", "wZ:pB2"]], ["lane-a", "lane-b"]
                )
                herdr._apply_resize(token, 0.2, pane="wZ:pA")
                self.assertEqual(
                    herdr.inter_lane_resizes,
                    [0],
                    "the observable the composition pins rely on must actually fire",
                )
                self.assertEqual(
                    herdr.resized_columns,
                    [],
                    "resolving to the inter-lane divider must not report a pair as moved",
                )
                self.assertEqual(
                    herdr.column_ratios[self.KEY],
                    [0.5, 0.5],
                    "no pair ratio may change when no pair divider was addressed",
                )

    def test_a_pair_column_resolves_to_its_own_divider_on_both_tokens(self) -> None:
        # R3-F1: the token carries the direction of TRAVEL, the axis is what selects the
        # split. `up` must find the same `down` divider that `down` does, and move it the
        # other way. Matching the token against the layout's label instead finds nothing and
        # silently applies no resize at all.
        for token, expected in (("down", 0.7), ("up", 0.3)):
            with self.subTest(token=token):
                herdr = self._herdr(
                    [["wZ:pA1", "wZ:pA2"], ["wZ:pB1", "wZ:pB2"]], ["lane-a", "lane-b"]
                )
                herdr._apply_resize(token, 0.2, pane="wZ:pA1")
                self.assertEqual(herdr.inter_lane_resizes, [])
                self.assertEqual(herdr.resized_columns, [0])
                self.assertAlmostEqual(
                    herdr.column_ratios[self.KEY][0], expected, places=6
                )
                self.assertAlmostEqual(
                    herdr.column_ratios[self.KEY][1],
                    0.5,
                    places=6,
                    msg="the neighbouring lane's divider must not move",
                )

    def test_an_unknown_direction_token_moves_nothing(self) -> None:
        # `RESIZE_AXIS` is a closed map. An unrecognised token must resolve to no axis and
        # actuate nothing, rather than defaulting to one and moving a divider by guess.
        herdr = self._herdr(
            [["wZ:pA1", "wZ:pA2"], ["wZ:pB1", "wZ:pB2"]], ["lane-a", "lane-b"]
        )
        herdr._apply_resize("sideways", 0.2, pane="wZ:pA1")
        self.assertEqual(herdr.resized_columns, [])
        self.assertEqual(herdr.inter_lane_resizes, [])
        self.assertEqual(herdr.column_ratios[self.KEY], [0.5, 0.5])


class SharedTabPairRatioCompositionTest(unittest.TestCase):
    """The #14569 x #14567 seam: which occupancy decides that a PAIR divider was created.

    #14569's ratio rail actuates only on a divider the run itself created, and it decided
    that from the CONTAINER's initial occupancy — correct while a lane owned its container.
    #14567 breaks that identity: in a shared tab a lane's first slot splits beside ANOTHER
    LANE on :data:`INTER_LANE_SPLIT_DIRECTION`, so the container is occupied while this
    pair still has no divider. The predicate is therefore scoped to the LANE's occupancy
    (``_created_pair_split``).

    Every case runs on :class:`_SharedTabHerdr`, which models the populated tab as columns
    with their own dividers. The first cut of these pins ran on the flat fake and was a
    false positive (review j#92057 F1): the "existing" lane and the launched pair were the
    same two panes, so ``applied`` proved nothing about a lane sharing a tab with another.
    """

    #: The neighbouring lane already in the shared tab, seeded where the fake's own
    #: launch numbering cannot reach.
    FOREIGN = ("wZ:p90", "wZ:p91")

    @staticmethod
    def _resizes(herdr):
        return [call for call in herdr.calls if call[:2] == ["pane", "resize"]]

    @staticmethod
    def _placement():
        # A declared ratio, so `config_ratio` is not None and the rail is genuinely armed;
        # a test reaching `not_applicable` with no ratio would prove it for the wrong reason.
        return LanePlacementConfig.from_record(
            {"sublane": {"split": "down", "ratio": 0.7}}
        )

    def _foreign_lane_rows(self):
        def rows(ws):
            return [
                _row(ws, "codex", "lane-b", self.FOREIGN[0], "wZ:t1"),
                _row(ws, "claude", "lane-b", self.FOREIGN[1], "wZ:t1"),
            ]

        return rows

    def _shared_herdr(self):
        herdr = _SharedTabHerdr(created_workspace="wZ")
        herdr.tab_labels["wZ:t1"] = SHARED_SUBLANE_TAB_LABEL
        return herdr

    def test_a_lane_opening_its_column_owes_no_pair_ratio(self) -> None:
        # lane-a has no live slot; lane-b already holds the shared tab. lane-a's single
        # launch opens its own column — a divider, but the INTER-LANE one (#14604's axis).
        # Reading the container's occupancy here claims a pair divider that does not exist,
        # then cannot find it in `config_split`'s direction and reports RATIO_FAILED.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = self._shared_herdr()
            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
                lane_placement=self._placement(),
                rows=self._foreign_lane_rows(),
                attested=True,
            )
            herdr.assert_no_locator_collision(self, result)
            # It really did open a column — otherwise this pins nothing.
            self.assertEqual(
                _split_of(herdr.start_argvs[0]), INTER_LANE_SPLIT_DIRECTION
            )
            self.assertEqual(
                result.ratio_outcome, RATIO_NOT_APPLICABLE, result.ratio_detail
            )
            self.assertEqual(
                self._resizes(herdr),
                [],
                "no divider is resized when this run created no pair divider of its own",
            )
            self.assertTrue(result.ok, result.ratio_detail)

    def test_a_pair_launched_into_an_occupied_shared_tab_gets_only_its_own_ratio(self) -> None:
        # The other direction of the same predicate, so the fix cannot be "switch the ratio
        # rail off under shared_tab" — and, unlike the first cut of this pin, with a real
        # neighbouring lane present. The first slot opens the column, the SECOND creates
        # this pair's own divider, which is owed the declared ratio.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = self._shared_herdr()
            result, _ws, panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
                lane_placement=self._placement(),
                rows=self._foreign_lane_rows(),
                attested=True,
            )
            herdr.assert_no_locator_collision(self, result)
            # Four panes really are live in the one tab: the neighbour's two and ours —
            # named by LANE, because a column identified by position passes even when a
            # launch joined the wrong lane's column.
            self.assertEqual(
                herdr.column_of_lane("lane-b"), list(self.FOREIGN),
                "the neighbouring lane's column must be untouched by this run",
            )
            self.assertEqual(
                sorted(herdr.column_of_lane("lane-a")),
                sorted(slot.locator for slot in result.slots),
                "this run's slots must form this lane's own column",
            )
            self.assertEqual(
                _split_of(herdr.start_argvs[0]), INTER_LANE_SPLIT_DIRECTION
            )
            self.assertEqual(_split_of(herdr.start_argvs[1]), "down")
            self.assertIn(
                result.ratio_outcome,
                (RATIO_APPLIED, RATIO_MATCHED),
                result.ratio_detail,
            )
            self.assertTrue(result.ok, result.ratio_detail)
            # The claim is about herdr's state: OUR column moved to the declared ratio...
            self.assertAlmostEqual(herdr.pair_ratio_of(panes["codex"]), 0.7, places=6)
            # ...and the neighbour's did not move at all.
            self.assertAlmostEqual(herdr.pair_ratio_of(self.FOREIGN[0]), 0.5, places=6)
            self.assertEqual(
                set(herdr.resized_columns),
                {herdr.column_lanes["wZ:t1"].index("lane-a")},
                "only this lane's own column divider may be resized",
            )
            self.assertEqual(
                herdr.inter_lane_resizes,
                [],
                "no resize may resolve to the divider BETWEEN lanes",
            )

    def test_a_shared_tab_heal_beside_its_own_sibling_is_owed_the_ratio(self) -> None:
        # Decision 4's heal shape, stated as a ratio disposition rather than only as argv.
        # lane-a already holds one slot in the shared tab, so its second launch splits on
        # the PAIR axis beside its own sibling and therefore DOES create the pair divider —
        # `not_applicable` here would mean a shared-tab lane silently never gets its ratio.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = self._shared_herdr()

            def rows(ws):
                return [
                    _row(ws, "codex", "lane-b", self.FOREIGN[0], "wZ:t1"),
                    _row(ws, "claude", "lane-b", self.FOREIGN[1], "wZ:t1"),
                    _row(ws, "codex", "lane-a", "wZ:p92", "wZ:t1"),
                ]

            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
                lane_placement=self._placement(),
                pair_order=("codex", "claude"),
                rows=rows,
                attested=True,
            )
            herdr.assert_no_locator_collision(self, result)
            self.assertEqual(
                _split_of(herdr.start_argvs[0]),
                "down",
                "a heal rejoins its own pair, not the inter-lane axis",
            )
            self.assertIn(
                result.ratio_outcome,
                (RATIO_APPLIED, RATIO_MATCHED),
                result.ratio_detail,
            )
            self.assertTrue(result.ok, result.ratio_detail)
            self.assertAlmostEqual(herdr.pair_ratio_of("wZ:p92"), 0.7, places=6)
            self.assertAlmostEqual(herdr.pair_ratio_of(self.FOREIGN[0]), 0.5, places=6)

    def test_a_right_pair_axis_resizes_its_own_divider_not_the_inter_lane_one(self) -> None:
        # The adversarial edge of this whole composition, surfaced by the full-surface sweep
        # the escalation gate (j#92090) put this round into.
        #
        # `INTER_LANE_SPLIT_DIRECTION` is `right`, and `lane_placement` may declare the PAIR
        # axis as `right` too (the #14568 rollback). Then the tab holds TWO same-axis splits
        # containing this lane's panes — the inter-lane one and the pair's — and
        # `pane resize --direction right` moves whichever herdr resolves as nearest. If the
        # rail addressed the outer one it would re-lay-out the neighbouring lane, which is
        # exactly what `governing_split` exists to refuse. Nothing else in this file drives
        # that collision: the other cases leave the pair on `down`, where the two axes differ
        # and the ambiguity cannot arise.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = self._shared_herdr()
            result, _ws, panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["codex", "claude"],
                lane="lane-a",
                sublane_tab_topology=SHARED,
                lane_placement=LanePlacementConfig.from_record(
                    {"sublane": {"split": "right", "ratio": 0.7}}
                ),
                rows=self._foreign_lane_rows(),
                attested=True,
            )
            herdr.assert_no_locator_collision(self, result)
            # Both axes really are `right` here — otherwise the collision is not exercised.
            self.assertEqual(
                _split_of(herdr.start_argvs[0]), INTER_LANE_SPLIT_DIRECTION
            )
            self.assertEqual(_split_of(herdr.start_argvs[1]), "right")
            self.assertEqual(
                herdr.column_directions["wZ:t1"][
                    herdr.column_lanes["wZ:t1"].index("lane-a")
                ],
                INTER_LANE_SPLIT_DIRECTION,
                "the fixture must model the pair on the SAME axis as the inter-lane split",
            )
            self.assertTrue(result.ratio_ok, result.ratio_detail)
            self.assertTrue(result.ok, result.ratio_detail)
            # The decisive observation: our column moved, the neighbour's did not.
            self.assertAlmostEqual(herdr.pair_ratio_of(panes["codex"]), 0.7, places=6)
            self.assertAlmostEqual(herdr.pair_ratio_of(self.FOREIGN[0]), 0.5, places=6)
            self.assertEqual(
                set(herdr.resized_columns),
                {herdr.column_lanes["wZ:t1"].index("lane-a")},
                "a `right` pair must resize its own divider, never the inter-lane one",
            )
            self.assertEqual(
                herdr.inter_lane_resizes,
                [],
                "no resize may resolve to the divider BETWEEN lanes",
            )

    def test_a_ratio_below_half_shrinks_only_this_lanes_divider(self) -> None:
        # `ratio < 0.5` is the half of the declared domain (0.1..0.9) that makes production
        # issue the SHRINK token — `left` on the `right` axis, `up` on `down`
        # (`resize_step`). Every other pin in this file declares 0.7 and therefore only ever
        # drives the grow tokens, so the shrink direction reached the shared tab untested
        # (review j#92117 R3-F1).
        #
        # Driven on BOTH axes: `right` is the collision case where the pair shares the
        # inter-lane axis, `down` is the control where they differ. The defect was not
        # specific to the collision — every `ratio < 0.5` run was affected.
        for axis in ("right", "down"):
            with self.subTest(axis=axis):
                with tempfile.TemporaryDirectory() as tmp:
                    herdr = self._shared_herdr()
                    result, _ws, panes = _prepare(
                        tmp,
                        herdr=herdr,
                        providers=["codex", "claude"],
                        lane="lane-a",
                        sublane_tab_topology=SHARED,
                        lane_placement=LanePlacementConfig.from_record(
                            {"sublane": {"split": axis, "ratio": 0.3}}
                        ),
                        rows=self._foreign_lane_rows(),
                        attested=True,
                    )
                    herdr.assert_no_locator_collision(self, result)
                    # The run really did issue a shrink token — otherwise this pins nothing.
                    shrink = {"right": "left", "down": "up"}[axis]
                    self.assertIn(
                        shrink,
                        [
                            call[call.index("--direction") + 1]
                            for call in self._resizes(herdr)
                            if "--direction" in call
                        ],
                        f"a declared 0.3 on the {axis} axis must issue `{shrink}`",
                    )
                    self.assertIn(
                        result.ratio_outcome,
                        (RATIO_APPLIED, RATIO_MATCHED),
                        result.ratio_detail,
                    )
                    self.assertTrue(result.ok, result.ratio_detail)
                    self.assertAlmostEqual(
                        herdr.pair_ratio_of(panes["codex"]), 0.3, places=6
                    )
                    self.assertAlmostEqual(
                        herdr.pair_ratio_of(self.FOREIGN[0]), 0.5, places=6
                    )
                    self.assertEqual(
                        herdr.inter_lane_resizes,
                        [],
                        "shrinking must not resolve to the divider BETWEEN lanes",
                    )

    def test_per_lane_tab_keeps_the_pre_14567_predicate(self) -> None:
        # Under `per_lane_tab` the lane occupancy IS the container occupancy, so a
        # single-slot heal beside a live sibling still owns its divider and is still owed
        # the ratio. This is the path the whole pre-#14567 rail runs on.
        with tempfile.TemporaryDirectory() as tmp:
            herdr = _Herdr(created_workspace="wZ")

            def rows(ws):
                return [_row(ws, "codex", "lane-a", "wZ:p90", "wZ:t1")]

            result, _ws, _panes = _prepare(
                tmp,
                herdr=herdr,
                providers=["claude"],
                lane="lane-a",
                sublane_tab_topology=PER_LANE,
                lane_placement=self._placement(),
                rows=rows,
                attested=True,
            )
            self.assertEqual(_split_of(herdr.start_argvs[0]), "down")
            self.assertNotEqual(
                result.ratio_outcome,
                RATIO_NOT_APPLICABLE,
                "a heal that split beside its live sibling DID create the pair divider; "
                "scoping the predicate to the lane must not skip it",
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
