"""Redmine #14763 — the managed project coordinator launches on an EXPLICIT model.

Owner intent (#14763 description; disposition j#94798, Gate j#95316): a managed
``delegated_coordinator`` must start as ``gpt-5.6-sol`` at reasoning effort ``high``, and
that has to be provable from the effective launch argv — not asserted from the config text.

Causing commit: ``ceab289e`` ("Pin Codex coordinator/default lane to gpt-5.6-sol; keep
sublane on Codex default", #13451). It put ``--model gpt-5.6-sol`` on the Codex *default*
lane class and left the *sublane* row carrying reasoning effort alone; the v1 -> v2 migration
carried that exact shape into ``agents.profiles.coordination``.

The symptom that shape produces is a *lane-class inheritance illusion*. Lane classes do NOT
inherit (``AgentsTopologyConfig.resolve_launch_argv_for_role`` returns the tokens of the
matching lane class or ``[]`` — the #13451 invariant). A ``delegated_coordinator`` is a named
lane, and ``herdr_session_start`` derives ``lane_class = "default" if lane_id == DEFAULT_LANE
else "sublane"``, so it resolved the *sublane* row: effort only, no model. The model the
config appeared to state was never on that launch.

Every test below re-detects that one symptom, at one of two layers, and neither layer
restates the other:

1. **Config resolution** — over the coordination roles and the coordination profile's own
   declared lane classes, derived from what the committed config resolves rather than a
   hand-listed set. Scope is the coordination profile: that is where the symptom lives, and
   whether OTHER profiles must also pin a model is a repo-wide policy question this file does
   not decide (Review j#95508 finding 2; verdict j#95511).
2. **Effective managed launch argv** — the committed config is driven through the real
   ``prepare_session`` launch chain against a fake herdr, and the assertion reads the argv
   herdr was actually asked to start. Layer 1 passing cannot make layer 2 pass: the launch
   path resolves its own lane class, and a config that pins the model on the wrong row lands
   an argv without it.

The counting oracle carries its own teeth test. An earlier revision folded the argv into a
SET of ``(flag, value)`` pairs, so a repeated *identical* ``--model`` collapsed to one and the
"exactly one" assertion passed on argv that declares the flag twice — the detector for this
symptom could go false-green (Review j#95508 finding 1; verdict j#95511). Multiplicity is
preserved now, and a test pins that it is.

The owner-pinned model/effort literals appear here because the owner named them; everything
else (which roles coordinate, which lane classes the coordination profile declares) is
derived from the config.
"""

from __future__ import annotations

import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]  # tests/regressions/<file> -> repo root
sys.path.insert(0, str(ROOT / "src"))
_TESTS_ROOT = Path(__file__).resolve().parents[1]
if str(_TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_TESTS_ROOT))

from support.agent_provider_binaries import (  # noqa: E402
    provider_bin_path,
    with_provider_path,
)
from support.herdr_fake import FakeHerdr  # noqa: E402

from mozyo_bridge.application.repo_local_config_loader import (  # noqa: E402
    load_repo_local_config_from_path,
)
from mozyo_bridge.core.state.lane_kind import (  # noqa: E402
    LANE_KIND_DELEGATED_COORDINATOR,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.role_provider_binding import (  # noqa: E501,E402
    DEFAULT_PROFILE_COORDINATION,
)
from mozyo_bridge.e_120_operations_cockpit.f_140_presentation_grouping_layout.domain.cockpit_layout import (  # noqa: E501,E402
    DEFAULT_LANE,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_session_start import (  # noqa: E501,E402
    prepare_session,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_startup_health import (  # noqa: E501,E402
    StartupProbe,
)

CONFIG_PATH = ROOT / ".mozyo-bridge" / "config.yaml"

#: The owner-pinned launch identity of the managed project coordinator (#14763 description).
#: Written as flag/value PAIRS because a bare token search would pass on
#: ``--model <something else> ... gpt-5.6-sol`` appearing anywhere in the argv.
OWNER_PINNED_COORDINATION_SUBLANE = (
    ("--model", "gpt-5.6-sol"),
    ("--config", "model_reasoning_effort=high"),
)

#: The flag that carries the model. A lane class that omits it inherits nothing.
MODEL_FLAG = "--model"

#: A named lane is any lane that is not ``DEFAULT_LANE``; a managed delegated coordinator is
#: one of them. Derived from the constant the launch path compares against, so a rename of
#: the default-lane token cannot leave this test silently exercising the default class.
DELEGATED_COORDINATOR_LANE = f"issue_14763_{LANE_KIND_DELEGATED_COORDINATOR}"

#: No real agent boots behind the fake herdr, so the startup probe must not wait on one.
_FAST_PROBE = StartupProbe(polls=1, interval=0.0, sleeper=lambda _seconds: None)


def _flag_pairs(argv):
    """The ``(flag, value)`` pairs of ``argv``, so an assertion pins adjacency, not presence.

    Returns a LIST, in argv order, with multiplicity preserved. A set would silently fold a
    repeated identical pair into one, which is how the "exactly one ``--model``" check below
    went false-green before Review j#95508 finding 1: an argv declaring the same model flag
    twice counted as one. Membership assertions read the same either way; only counting
    depends on multiplicity, and counting is what the symptom detector rests on.
    """
    return [
        (tok, argv[i + 1])
        for i, tok in enumerate(argv)
        # `--` is the herdr/provider separator, not a flag; pairing it would make the
        # hermetic stub path look like a flag value in every assertion message.
        if tok.startswith("--")
        and tok != "--"
        and i + 1 < len(argv)
        and not argv[i + 1].startswith("--")
    ]


def _model_tokens(argv):
    """Every value bound to ``--model`` in ``argv``, with repeats kept.

    A bare trailing ``--model`` binds no value and so contributes nothing: a flag without a
    value pins no model, which is the condition this file exists to catch.
    """
    return [value for flag, value in _flag_pairs(argv) if flag == MODEL_FLAG]


def _committed_config():
    return load_repo_local_config_from_path(CONFIG_PATH)


class CommittedCoordinationLaunchArgvTest(unittest.TestCase):
    """Layer 1: what the committed config RESOLVES to (not what its YAML text looks like)."""

    def setUp(self) -> None:
        self.config = _committed_config()
        self.topology = self.config.agents

    def test_every_role_bound_to_coordination_gets_the_pinned_model_on_a_sublane(self) -> None:
        # Derived, not listed: whichever roles the committed topology binds to the
        # coordination profile are exactly the roles that must launch on the pinned model.
        # A delegated coordinator resolves through one of them, so none may be the row that
        # carries effort without a model.
        coordinating = [
            role
            for role, profile in self.topology.resolved_role_profiles().items()
            if profile == DEFAULT_PROFILE_COORDINATION
        ]
        self.assertTrue(
            coordinating, "committed config binds no role to the coordination profile"
        )
        for role in sorted(coordinating):
            with self.subTest(role=role):
                argv = self.topology.resolve_launch_argv_for_role(role, "sublane")
                pairs = _flag_pairs(argv)
                for flag, value in OWNER_PINNED_COORDINATION_SUBLANE:
                    self.assertIn(
                        (flag, value),
                        pairs,
                        f"role {role!r} on a sublane resolves {argv!r}, which does not pin "
                        f"{flag} {value}",
                    )

    def test_the_coordination_profile_pins_a_model_on_every_lane_class_it_declares(
        self,
    ) -> None:
        # The symptom, stated over the surface it actually occupied. The coordination
        # profile declared TWO lane classes and put `--model` on only one of them; because
        # lane classes do not inherit (`resolve_launch_argv_for_role` returns the matching
        # row or `[]`), the other row launched on whatever the provider CLI defaults to
        # while the config still read as though a model were pinned.
        #
        # Scope is deliberately this profile. Requiring the same of every profile in the
        # repo would be a new repo-wide rule about future provider-default choices, which
        # no owner decision states and which this regression file has no standing to
        # introduce (Review j#95508 finding 2 / verdict j#95511).
        profile = self.topology.resolved_profiles()[DEFAULT_PROFILE_COORDINATION]
        declared = sorted(profile.launch_argv)
        self.assertTrue(
            declared,
            f"profile {DEFAULT_PROFILE_COORDINATION!r} declares no launch argv at all",
        )
        for lane_class, tokens in declared:
            with self.subTest(lane_class=lane_class):
                models = _model_tokens(list(tokens))
                self.assertEqual(
                    1,
                    len(models),
                    f"profile {DEFAULT_PROFILE_COORDINATION!r} lane_class {lane_class!r} "
                    f"declares {list(tokens)!r}; it must pin exactly one {MODEL_FLAG} "
                    f"(nothing is inherited from another lane class)",
                )
                self.assertTrue(models[0], "the pinned model token must not be empty")

    def test_the_model_count_oracle_does_not_fold_a_repeated_identical_flag(self) -> None:
        # Teeth for the counter the test above rests on. Folding the argv into a SET of
        # (flag, value) pairs made a repeated IDENTICAL `--model` count as one, so argv
        # declaring the flag twice satisfied "exactly one" — the symptom detector itself
        # could go false-green (Review j#95508 finding 1 / verdict j#95511). A repeat with
        # a DIFFERENT value was already counted twice, which is why the gap survived: only
        # the identical case folded.
        repeated_identical = ["--model", "gpt-5.6-sol", "--model", "gpt-5.6-sol"]
        self.assertEqual(
            ["gpt-5.6-sol", "gpt-5.6-sol"],
            _model_tokens(repeated_identical),
            "a repeated identical --model must be counted twice, not folded to one",
        )
        repeated_conflicting = ["--model", "gpt-5.6-sol", "--model", "some-other-model"]
        self.assertEqual(2, len(_model_tokens(repeated_conflicting)))
        # A flag with no value binds no model, so it must not be counted as pinning one.
        self.assertEqual([], _model_tokens(["--config", "x=1", "--model"]))


class EffectiveManagedLaunchArgvTest(unittest.TestCase):
    """Layer 2: the argv herdr is actually asked to start, from the committed config."""

    def _start_argv(self, *, lane_id):
        """Drive the real launch chain for ``lane_id``; return the launched ``agent start`` argv.

        The committed config's own ``agent_launch`` record is passed — the same object the
        launch site resolves from ``.mozyo-bridge/config.yaml`` — so this exercises the
        deployed value, not a synthetic one. Everything else (herdr, provider binaries, home)
        is hermetic: no host binary is resolved and no live agent is started.
        """
        config = _committed_config()
        herdr = FakeHerdr()
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "repo"
            repo.mkdir()
            home = Path(tmp) / "home"
            home.mkdir()
            herdr_bin = Path(tmp) / "herdr"
            herdr_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            herdr_bin.chmod(herdr_bin.stat().st_mode | stat.S_IEXEC)
            env = with_provider_path({"MOZYO_HERDR_BINARY": str(herdr_bin)})
            with patch.dict(os.environ, {"MOZYO_BRIDGE_HOME": str(home)}, clear=False):
                prepare_session(
                    repo_root=repo,
                    providers=["codex"],
                    lane_id=lane_id,
                    env=env,
                    runner=herdr.run,
                    agent_launch=config.agent_launch,
                    probe=_FAST_PROBE,
                )
        starts = [call for call in herdr.calls if call[:2] == ["agent", "start"]]
        self.assertEqual(1, len(starts), f"expected exactly one launch, got {starts!r}")
        return starts[0]

    def _provider_command(self, start_argv):
        """The tokens after ``--``: what the pane runs, as opposed to what herdr is told."""
        return list(start_argv[start_argv.index("--") :])

    def test_delegated_coordinator_lane_launches_on_the_pinned_model_and_effort(self) -> None:
        argv = self._provider_command(
            self._start_argv(lane_id=DELEGATED_COORDINATOR_LANE)
        )
        self.assertEqual(
            provider_bin_path("codex"),
            argv[1],
            "argv[0] of the provider command must be the hermetic codex stub",
        )
        pairs = _flag_pairs(argv)
        for flag, value in OWNER_PINNED_COORDINATION_SUBLANE:
            self.assertIn(
                (flag, value),
                pairs,
                f"the effective launch argv {argv!r} does not pin {flag} {value}",
            )

    def test_the_model_is_pinned_on_the_sublane_row_not_borrowed_from_the_default_lane(
        self,
    ) -> None:
        # The differential that makes layer 2 independent of layer 1: the two lane classes
        # resolve DIFFERENT effort, so observing the sublane effort proves the sublane row
        # is the one that produced this argv — and the model rides on that same row.
        sublane = _flag_pairs(
            self._provider_command(self._start_argv(lane_id=DELEGATED_COORDINATOR_LANE))
        )
        default = _flag_pairs(
            self._provider_command(self._start_argv(lane_id=DEFAULT_LANE))
        )
        sublane_effort = {v for f, v in sublane if f == "--config"}
        default_effort = {v for f, v in default if f == "--config"}
        self.assertNotEqual(
            sublane_effort,
            default_effort,
            "the two lane classes resolve the same effort, so this test can no longer tell "
            "which row produced the argv; re-anchor it on a token that still differs",
        )
        self.assertIn(("--config", "model_reasoning_effort=high"), sublane)
        self.assertIn((MODEL_FLAG, "gpt-5.6-sol"), sublane)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
