"""Strict lane-evidence-envelope grammar tests (Redmine #14219 T2b, step 1).

Pins the common envelope every hibernate-basis producer must carry:

- **fail-closed parse** — a missing workspace / lane / generation, a non-positive or non-numeric
  generation, and (for a head-bearing conjunct) a missing head each fold to a distinct typed
  reason; a malformed head is rejected even when the head is optional;
- **strict head** — only a full 40/64-hex lowercase SHA is accepted;
- **round-trip** — render then parse reproduces the envelope;
- **conflict resolution** — zero envelopes -> absent, identical duplicates collapse, any two
  differing envelopes -> conflict (a superseded / cross-lane record is never silently preferred).
"""

from __future__ import annotations

import unittest

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain import (
    hibernate_evidence_envelope as env,
)

WS = "ws-1"
LANE = "lane-abc"
HEAD = "a" * 40
HEAD256 = "b" * 64


def _fields(**over):
    base = {env.FIELD_WORKSPACE: WS, env.FIELD_LANE: LANE, env.FIELD_LANE_GENERATION: "3"}
    base.update(over)
    return base


class ParseTests(unittest.TestCase):
    def test_a_full_envelope_parses(self):
        got = env.parse_lane_envelope(_fields(head=HEAD), require_head=True)
        self.assertIsInstance(got, env.LaneEvidenceEnvelope)
        self.assertEqual((got.workspace, got.lane, got.lane_generation, got.head), (WS, LANE, 3, HEAD))

    def test_lane_only_envelope_parses_without_head(self):
        got = env.parse_lane_envelope(_fields(), require_head=False)
        self.assertIsInstance(got, env.LaneEvidenceEnvelope)
        self.assertEqual(got.head, "")

    def test_missing_workspace_is_typed(self):
        got = env.parse_lane_envelope(_fields(workspace=""), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MISSING_WORKSPACE)

    def test_missing_lane_is_typed(self):
        got = env.parse_lane_envelope(_fields(lane="  "), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MISSING_LANE)

    def test_missing_generation_is_typed(self):
        got = env.parse_lane_envelope(_fields(lane_generation=""), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MISSING_GENERATION)

    def test_non_numeric_generation_is_malformed(self):
        got = env.parse_lane_envelope(_fields(lane_generation="two"), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MALFORMED_GENERATION)

    def test_zero_generation_is_malformed(self):
        got = env.parse_lane_envelope(_fields(lane_generation="0"), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MALFORMED_GENERATION)

    def test_negative_generation_is_malformed(self):
        got = env.parse_lane_envelope(_fields(lane_generation="-1"), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MALFORMED_GENERATION)

    def test_head_required_but_absent_is_typed(self):
        got = env.parse_lane_envelope(_fields(), require_head=True)
        self.assertEqual(got.reason, env.ENVELOPE_MISSING_HEAD)

    def test_a_short_head_is_malformed_even_when_optional(self):
        got = env.parse_lane_envelope(_fields(head="abc123"), require_head=False)
        self.assertEqual(got.reason, env.ENVELOPE_MALFORMED_HEAD)

    def test_an_uppercase_head_is_malformed(self):
        got = env.parse_lane_envelope(_fields(head="A" * 40), require_head=True)
        self.assertEqual(got.reason, env.ENVELOPE_MALFORMED_HEAD)

    def test_sha256_head_is_accepted(self):
        got = env.parse_lane_envelope(_fields(head=HEAD256), require_head=True)
        self.assertIsInstance(got, env.LaneEvidenceEnvelope)
        self.assertEqual(got.head, HEAD256)

    def test_every_parse_reason_is_in_the_closed_vocabulary(self):
        cases = [
            env.parse_lane_envelope(_fields(workspace=""), require_head=False),
            env.parse_lane_envelope(_fields(lane=""), require_head=False),
            env.parse_lane_envelope(_fields(lane_generation=""), require_head=False),
            env.parse_lane_envelope(_fields(lane_generation="x"), require_head=False),
            env.parse_lane_envelope(_fields(), require_head=True),
            env.parse_lane_envelope(_fields(head="zz"), require_head=False),
        ]
        for got in cases:
            self.assertIsInstance(got, env.EnvelopeParseError)
            self.assertIn(got.reason, env.LANE_ENVELOPE_PARSE_REASONS)


class RenderRoundTripTests(unittest.TestCase):
    def test_render_then_parse_reproduces_a_head_envelope(self):
        original = env.LaneEvidenceEnvelope(WS, LANE, 3, HEAD)
        rendered = env.render_lane_envelope(original)
        fields = dict(part.split("=", 1) for part in rendered.split(":"))
        self.assertEqual(env.parse_lane_envelope(fields, require_head=True), original)

    def test_render_omits_an_empty_head(self):
        rendered = env.render_lane_envelope(env.LaneEvidenceEnvelope(WS, LANE, 2))
        self.assertNotIn("head=", rendered)
        fields = dict(part.split("=", 1) for part in rendered.split(":"))
        self.assertEqual(env.parse_lane_envelope(fields, require_head=False).head, "")


class ResolveTests(unittest.TestCase):
    def _env(self, **over):
        base = dict(workspace=WS, lane=LANE, lane_generation=3, head=HEAD)
        base.update(over)
        return env.LaneEvidenceEnvelope(**base)

    def test_zero_is_absent(self):
        self.assertEqual(env.resolve_lane_envelope([]).reason, env.ENVELOPE_ABSENT)

    def test_one_resolves_to_itself(self):
        e = self._env()
        self.assertEqual(env.resolve_lane_envelope([e]), e)

    def test_identical_duplicates_collapse(self):
        e = self._env()
        self.assertEqual(env.resolve_lane_envelope([e, e, e]), e)

    def test_a_differing_generation_conflicts(self):
        got = env.resolve_lane_envelope([self._env(), self._env(lane_generation=4)])
        self.assertEqual(got.reason, env.ENVELOPE_CONFLICT)

    def test_a_differing_lane_conflicts(self):
        got = env.resolve_lane_envelope([self._env(), self._env(lane="lane-other")])
        self.assertEqual(got.reason, env.ENVELOPE_CONFLICT)

    def test_a_differing_head_conflicts(self):
        got = env.resolve_lane_envelope([self._env(), self._env(head="c" * 40)])
        self.assertEqual(got.reason, env.ENVELOPE_CONFLICT)


class RendererValidationTests(unittest.TestCase):
    """Checkpoint review j#86389 F4: the renderer must refuse what the parser refuses.

    Rendering an envelope the parser would reject produces durable evidence that silently does not
    count; rendering a separator-bearing id is worse — it splits into a different field set.
    """

    def _envelope(self, **over):
        base = dict(workspace=WS, lane=LANE, lane_generation=3, head=HEAD)
        base.update(over)
        return env.LaneEvidenceEnvelope(**base)

    def test_valid_envelope_still_round_trips(self):
        # Negative control: the guard rejects the invalid, not the valid.
        rendered = env.render_lane_envelope(self._envelope())
        fields = dict(part.split("=", 1) for part in rendered.split(":"))
        self.assertEqual(env.parse_lane_envelope(fields, require_head=True), self._envelope())

    def test_non_positive_generation_is_refused(self):
        for generation in (0, -1):
            with self.subTest(generation=generation):
                with self.assertRaises(ValueError):
                    env.render_lane_envelope(self._envelope(lane_generation=generation))

    def test_malformed_head_is_refused(self):
        for head in ("not-a-sha", "A" * 40, "abc123"):
            with self.subTest(head=head):
                with self.assertRaises(ValueError):
                    env.render_lane_envelope(self._envelope(head=head))

    def test_empty_workspace_or_lane_is_refused(self):
        with self.assertRaises(ValueError):
            env.render_lane_envelope(self._envelope(workspace=""))
        with self.assertRaises(ValueError):
            env.render_lane_envelope(self._envelope(lane="  "))

    def test_separator_bearing_identity_is_refused(self):
        # Rendered unchecked, `ws:evil]x` became `workspace=ws` + a bogus `evil]x` field and
        # truncated the marker there — field injection from a producer-supplied id.
        for bad in ("ws:evil]x", "ws]x", "ws x", "ws[x"):
            with self.subTest(value=bad):
                with self.assertRaises(ValueError):
                    env.render_lane_envelope(self._envelope(workspace=bad))
                with self.assertRaises(ValueError):
                    env.render_lane_envelope(self._envelope(lane=bad))


if __name__ == "__main__":
    unittest.main()
