"""Redmine #14763 — the managed project coordinator launches on an OWNER-PINNED identity.

Owner intent (#14763 description; disposition j#94798, Gate j#95316): a managed
``delegated_coordinator`` must start on an owner-pinned launch identity, and that has to be
provable from the effective launch argv — not asserted from the config text. What the owner
pins is an owner value and has been re-pinned since #14763 was fixed:

- ``--model gpt-5.6-sol`` at effort ``high`` originally (#14763);
- ``--model claude-fable-5`` when the owner consolidated both profiles onto the claude
  provider (a7e3c7d0, then e9f51af8; #15255 j#104520 / #15418) — the explicit-model era;
- **the #15631 repin (owner decision 2026-08-18; ADR-0011 owner decision, config commit
  bda239d0, CI correction j#107334)**: the coordination profile moved to the ``codex``
  provider and the owner-pinned launch identity became the per-lane-class reasoning-effort
  token — ``model_reasoning_effort=xhigh`` (default) / ``model_reasoning_effort=high``
  (sublane). The owner deliberately pinned NO ``--model`` on these rows: under the codex
  rows the model is the provider CLI's default, and the explicit-model requirement of the
  earlier era is **superseded for the coordination profile**, not silently dropped. The
  implementation profile stays ``claude`` / ``claude-fable-5`` and is out of this file's
  scope (j#95508 finding 2 / j#107334 item 4).

Causing commit: ``ceab289e`` ("Pin Codex coordinator/default lane to gpt-5.6-sol; keep
sublane on Codex default", #13451). It put the pin on the Codex *default* lane class and left
the *sublane* row carrying reasoning effort alone; the v1 -> v2 migration carried that exact
shape into ``agents.profiles.coordination``.

The symptom that shape produces is a *lane-class inheritance illusion*, and it is unchanged
by what the pinned token happens to be. Lane classes do NOT inherit
(``AgentsTopologyConfig.resolve_launch_argv_for_role`` returns the tokens of the matching
lane class or ``[]`` — the #13451 invariant). A ``delegated_coordinator`` is a named lane,
and ``herdr_session_start`` derives ``lane_class = "default" if lane_id == DEFAULT_LANE else
"sublane"``, so a row that lost its own pin launches on whatever the provider CLI defaults
to while the config still reads as though the identity were pinned. Since #15631 the pinned
identity this file re-detects is the per-lane effort token, not ``--model``.

Every test below re-detects that one symptom, at one of two layers, and neither layer
restates the other:

1. **Config resolution** — over the coordination roles and the coordination profile's own
   declared lane classes, derived from what the committed config resolves rather than a
   hand-listed set. Scope is the coordination profile: that is where the symptom lives, and
   whether OTHER profiles must also pin a launch identity is a repo-wide policy question this
   file does not decide (Review j#95508 finding 2; verdict j#95511).
2. **Effective managed launch argv** — the committed config is driven through the real
   ``prepare_session`` launch chain against a fake herdr, and the assertion reads the argv
   herdr was actually asked to start. Layer 1 passing cannot make layer 2 pass: the launch
   path resolves its own lane class, and a config that pins the identity on the wrong row
   lands an argv without it. The #15631 repin pins the two lane classes to DIFFERING
   literals (``xhigh`` vs ``high``), which restores the differential anchor #15418 lost:
   the exact effort each launch carries proves the argv came from that lane class's own row,
   because the other row's token would read differently.

"Exactly one" is counted off the raw token list, in the test that makes the claim. An
earlier revision folded the argv into a SET of ``(flag, value)`` pairs, so a repeated
*identical* pin collapsed to one and the assertion passed on argv that declares it twice
(Review j#95508 finding 1). Routing the count through a separable helper then needed a
synthetic test to guard that helper — a claim about a helper's contract, which is not
re-detection of this symptom and does not belong in this file (Review j#95547 finding 1;
verdicts j#95511, j#95737). Counting positions in the raw list removes the folding container
and the helper at once: nothing here can fold, and no contract is left to guard, so every
test stays on the symptom's own surface.

The owner-pinned provider/effort literals appear here because the owner named them
(ADR-0011 owner decision 2026-08-18 / j#107334); everything else (which roles coordinate,
which lane classes the coordination profile declares) is derived from the config.
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

#: The owner-pinned launch identity of the managed project coordinator per lane class,
#: written as the exact ``(flag, value)`` pair the row must carry (#15631 repin: ADR-0011
#: owner decision 2026-08-18 / config commit bda239d0 / j#107334). The pin is the per-lane
#: reasoning-effort token; the owner deliberately pinned NO ``--model`` on the codex
#: coordination rows. A lane class the committed config declares but this map does not name
#: is not owner-pinned and must fail below.
OWNER_PINNED_COORDINATION_EFFORTS = {
    "default": ("--config", "model_reasoning_effort=xhigh"),
    "sublane": ("--config", "model_reasoning_effort=high"),
}

#: The provider the owner moved the coordination profile to (#15631; the implementation
#: profile stays ``claude`` / ``claude-fable-5`` and is not asserted here — j#107334 item 4).
OWNER_PINNED_COORDINATION_PROVIDER = "codex"

#: The flag and value-prefix that carry the effort pin. Matching both — the flag AND the
#: ``model_reasoning_effort=`` value it binds — is what makes the position count below
#: immune to unrelated future ``--config other=...`` tokens, while a trailing or
#: flag-followed ``--config`` binds nothing and does not count (the same launch outcome as
#: omitting the pin). A lane class that omits the pair inherits nothing.
EFFORT_FLAG = "--config"
EFFORT_VALUE_PREFIX = "model_reasoning_effort="

#: A named lane is any lane that is not ``DEFAULT_LANE``; a managed delegated coordinator is
#: one of them. Derived from the constant the launch path compares against, so a rename of
#: the default-lane token cannot leave this test silently exercising the default class.
DELEGATED_COORDINATOR_LANE = f"issue_14763_{LANE_KIND_DELEGATED_COORDINATOR}"

#: No real agent boots behind the fake herdr, so the startup probe must not wait on one.
_FAST_PROBE = StartupProbe(polls=1, interval=0.0, sleeper=lambda _seconds: None)


def _flag_pairs(argv):
    """The ``(flag, value)`` pairs of ``argv``, so an assertion pins adjacency, not presence.

    Used for membership and for telling two lane classes apart — never for counting. The
    "exactly one pin" claim counts positions in the raw token list where it is made, so no
    caller depends on this returning a container that preserves multiplicity. It returns a
    list anyway: a set here folded a repeated identical pair into one once already (Review
    j#95508 finding 1), and there is no reason to keep a folding container around.
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


def _committed_config():
    return load_repo_local_config_from_path(CONFIG_PATH)


class CommittedCoordinationLaunchArgvTest(unittest.TestCase):
    """Layer 1: what the committed config RESOLVES to (not what its YAML text looks like)."""

    def setUp(self) -> None:
        self.config = _committed_config()
        self.topology = self.config.agents

    def test_every_role_bound_to_coordination_gets_the_pinned_effort_on_a_sublane(
        self,
    ) -> None:
        # Derived, not listed: whichever roles the committed topology binds to the
        # coordination profile are exactly the roles that must launch on the pinned
        # provider + effort. A delegated coordinator resolves through one of them, so none
        # may be a row that appears pinned while resolving to an argv without its own pin.
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
                self.assertEqual(
                    OWNER_PINNED_COORDINATION_PROVIDER,
                    self.topology.resolve_provider_for_role(role),
                    f"role {role!r} does not resolve to the owner-pinned provider",
                )
                argv = self.topology.resolve_launch_argv_for_role(role, "sublane")
                flag, value = OWNER_PINNED_COORDINATION_EFFORTS["sublane"]
                self.assertIn(
                    (flag, value),
                    _flag_pairs(argv),
                    f"role {role!r} on a sublane resolves {argv!r}, which does not pin "
                    f"{flag} {value}",
                )

    def test_the_coordination_profile_pins_its_effort_on_every_lane_class_it_declares(
        self,
    ) -> None:
        # The symptom, stated over the surface it actually occupied. The coordination
        # profile declares TWO lane classes; because lane classes do not inherit
        # (`resolve_launch_argv_for_role` returns the matching row or `[]`), a row that
        # lost its own pin would launch on whatever the provider CLI defaults to while the
        # config still read as though the identity were pinned.
        #
        # Scope is deliberately this profile. Requiring the same of every profile in the
        # repo would be a new repo-wide rule about future provider-default choices, which
        # no owner decision states and which this regression file has no standing to
        # introduce (Review j#95508 finding 2 / verdict j#95511).
        profile = self.topology.resolved_profiles()[DEFAULT_PROFILE_COORDINATION]
        self.assertEqual(
            OWNER_PINNED_COORDINATION_PROVIDER,
            profile.provider,
            f"profile {DEFAULT_PROFILE_COORDINATION!r} is not on the owner-pinned provider",
        )
        declared = sorted(profile.launch_argv)
        self.assertTrue(
            declared,
            f"profile {DEFAULT_PROFILE_COORDINATION!r} declares no launch argv at all",
        )
        for lane_class, tokens in declared:
            with self.subTest(lane_class=lane_class):
                tokens = list(tokens)
                self.assertIn(
                    lane_class,
                    OWNER_PINNED_COORDINATION_EFFORTS,
                    f"profile {DEFAULT_PROFILE_COORDINATION!r} declares lane_class "
                    f"{lane_class!r}, which the owner pinned no effort for",
                )
                # Counted as POSITIONS in the config's own raw token list. Nothing is
                # folded into a set or a dict on the way, so a row declaring the pin twice
                # — with the same value or a different one — is two, and the count cannot
                # go false-green the way it did in 9cdc5e7f (Review j#95508 finding 1).
                # Doing it here rather than through a helper also leaves no helper contract
                # needing a synthetic test of its own, which is what put a non-symptom
                # claim in this file (Review j#95547 finding 1 / verdicts j#95511, j#95737).
                at = [
                    i
                    for i, tok in enumerate(tokens)
                    if tok == EFFORT_FLAG
                    and i + 1 < len(tokens)
                    and tokens[i + 1].startswith(EFFORT_VALUE_PREFIX)
                ]
                self.assertEqual(
                    1,
                    len(at),
                    f"profile {DEFAULT_PROFILE_COORDINATION!r} lane_class {lane_class!r} "
                    f"declares {tokens!r}; it must pin exactly one {EFFORT_FLAG} "
                    f"{EFFORT_VALUE_PREFIX}... as separate tokens (nothing is inherited "
                    f"from another lane class)",
                )
                flag, value = OWNER_PINNED_COORDINATION_EFFORTS[lane_class]
                self.assertEqual(
                    value,
                    tokens[at[0] + 1],
                    f"lane_class {lane_class!r} pins {tokens[at[0] + 1]!r}, not the "
                    f"owner-pinned {value!r}",
                )


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
                    providers=[OWNER_PINNED_COORDINATION_PROVIDER],
                    lane_id=lane_id,
                    env=env,
                    runner=herdr.run,
                    agent_launch=config.agent_launch,
                    probe=_FAST_PROBE,
                )
        starts = herdr.start_argvs
        self.assertEqual(1, len(starts), f"expected exactly one launch, got {starts!r}")
        return starts[0]

    def _provider_command(self, start_argv):
        """The provider arguments after Herdr 0.8's pane-bound launch separator."""
        return list(start_argv[start_argv.index("--") + 1 :])

    def test_delegated_coordinator_lane_launches_on_the_pinned_provider_and_effort(
        self,
    ) -> None:
        start = self._start_argv(lane_id=DELEGATED_COORDINATOR_LANE)
        self.assertEqual(
            OWNER_PINNED_COORDINATION_PROVIDER, start[start.index("--kind") + 1]
        )
        argv = self._provider_command(start)
        flag, value = OWNER_PINNED_COORDINATION_EFFORTS["sublane"]
        self.assertIn(
            (flag, value),
            _flag_pairs(argv),
            f"the effective launch argv {argv!r} does not pin {flag} {value}",
        )

    def test_every_lane_class_launch_carries_its_own_effort_pin(self) -> None:
        # The #13451 inheritance illusion, re-detected at this layer per lane class: lane
        # classes do NOT inherit, so a row that lost its pin would launch an argv with ZERO
        # effort pins below. The #15631 repin pins the two lane classes to DIFFERING
        # literals (`xhigh` vs `high`), so the exact value each launch carries is also a
        # differential anchor again (re-anchoring what #15418 lost when both rows were
        # pinned to the same tokens): an argv inherited from the other row would carry the
        # other row's effort and fail the exact-value assertion.
        for lane_id, lane_class in (
            (DELEGATED_COORDINATOR_LANE, "sublane"),
            (DEFAULT_LANE, "default"),
        ):
            with self.subTest(lane_class=lane_class):
                argv = self._provider_command(self._start_argv(lane_id=lane_id))
                # Positions in the raw launched argv, for the same no-folding / no-helper
                # reasons as layer 1 (Review j#95508 finding 1 / j#95547 finding 1).
                at = [
                    i
                    for i, tok in enumerate(argv)
                    if tok == EFFORT_FLAG
                    and i + 1 < len(argv)
                    and argv[i + 1].startswith(EFFORT_VALUE_PREFIX)
                ]
                self.assertEqual(
                    1,
                    len(at),
                    f"lane_class {lane_class!r} launched {argv!r}; its own row must pin "
                    f"exactly one {EFFORT_FLAG} {EFFORT_VALUE_PREFIX}... (nothing is "
                    f"inherited from another lane class)",
                )
                flag, value = OWNER_PINNED_COORDINATION_EFFORTS[lane_class]
                self.assertEqual(
                    value,
                    argv[at[0] + 1],
                    f"lane_class {lane_class!r} launched with {argv[at[0] + 1]!r}, not "
                    f"its own owner-pinned {value!r}",
                )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
