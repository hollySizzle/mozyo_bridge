"""herdr durable-identity mapping seam tests (Redmine #13247).

Pins the naming convention and re-bind contract from
:mod:`mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity`:

- the encode/decode naming convention is **deterministic**, **round-trippable**
  for arbitrary component strings (including ``_`` and non-ASCII), and
  **collision-free** (injective) across distinct slots;
- a malformed / out-of-scheme name fails closed with a structured result (never
  an exception);
- a generated name is pane/terminal-free and is a valid #13245 transport target;
- the restart re-bind procedure recovers a live locator by durable name and
  fails closed on not-found / ambiguous / invalid-name.

No network / tmux / herdr binary is touched here.
"""

from __future__ import annotations

import itertools
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    DECODE_FAILURE_REASONS,
    DEFAULT_LANE,
    NAME_MAX_LENGTH,
    REASON_BAD_ESCAPE,
    REASON_BAD_PREFIX,
    REASON_BAD_SHAPE,
    REASON_EMPTY,
    REASON_ILLEGAL_CHAR,
    REASON_TOO_LONG,
    REBIND_AMBIGUOUS,
    REBIND_INVALID_NAME,
    REBIND_MISSING_LOCATOR,
    REBIND_NOT_FOUND,
    REBIND_OK,
    SCHEME_PREFIX,
    LANE_WORKSPACE_TOKEN_PREFIX,
    HerdrAgentIdentity,
    HerdrIdentityError,
    decode_assigned_name,
    derive_lane_workspace_token,
    encode_assigned_name,
    encode_field,
    occupant_of_locator,
    rebind_by_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.terminal_transport import (  # noqa: E501
    valid_target,
)
from mozyo_bridge.core.state.herdr_native_identity_binding import (
    HerdrNativeIdentityBindingError,
    HerdrNativeIdentityBindingStore,
    NATIVE_NAME_MAX_LENGTH,
    is_native_name,
    logicalize_agent_rows,
    native_name_for,
)
from tests.support.private_path_fixtures import macos_home_path

# A spread of component values that exercise the codec: identifier-safe, the
# delimiter, other punctuation, spaces, non-ASCII, digits, mixed case.
_SLOT_VALUES = (
    "claude",
    "codex",
    "lane_13247",
    "giken-3800-mozyo-bridge",
    "ws.default",
    "a_b_c",
    "MixedCase",
    "Z",  # the escape character itself
    "ZZZ",
    "空間",  # non-ASCII (multi-byte UTF-8)
    "x y",  # space
    "1",
)


class EncodeFieldRoundTripTest(unittest.TestCase):
    """The per-field codec is an exact inverse for arbitrary strings."""

    def test_field_roundtrip_and_alphabet(self) -> None:
        from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
            _decode_field,
        )

        for value in _SLOT_VALUES + ("", "under_score", "%$&*()", "tab\tend"):
            encoded = encode_field(value)
            # Output is drawn only from [A-Za-z0-9] and carries no delimiter.
            self.assertRegex(encoded, r"^[A-Za-z0-9]*$")
            self.assertNotIn("_", encoded)
            self.assertEqual(_decode_field(encoded), value)

    def test_escape_char_self_escapes(self) -> None:
        # 'Z' is 0x5A; it must not pass through literally or decode ambiguity
        # would follow.
        self.assertEqual(encode_field("Z"), "Z5A")

    def test_encode_field_rejects_non_string(self) -> None:
        with self.assertRaises(HerdrIdentityError):
            encode_field(123)  # type: ignore[arg-type]


class AssignedNameConventionTest(unittest.TestCase):
    """encode/decode determinism, round-trip, and injectivity."""

    def test_deterministic(self) -> None:
        first = encode_assigned_name("ws1", "claude", "laneA")
        second = encode_assigned_name("ws1", "claude", "laneA")
        self.assertEqual(first, second)
        self.assertTrue(first.startswith(SCHEME_PREFIX + "_"))

    def test_documented_example(self) -> None:
        self.assertEqual(
            encode_assigned_name("giken-3800-mozyo-bridge", "claude", "lane_13247"),
            "mzb1_gikenZ2D3800Z2DmozyoZ2Dbridge_claude_laneZ5F13247",
        )

    def test_roundtrip_over_component_matrix(self) -> None:
        # Property-style: enumerate a matrix of (workspace, role, lane) slots and
        # assert encode -> decode recovers each component exactly (lane empty
        # normalises to DEFAULT_LANE).
        for ws, role, lane in itertools.product(
            _SLOT_VALUES, ("claude", "codex", "a_b"), ("", "L1", "lane_x", "Z")
        ):
            name = encode_assigned_name(ws, role, lane)
            decoded = decode_assigned_name(name)
            self.assertTrue(decoded.ok, f"decode failed for {name!r}: {decoded.reason}")
            identity = decoded.identity
            assert identity is not None
            self.assertEqual(identity.workspace_id, ws.strip())
            self.assertEqual(identity.role, role.strip())
            self.assertEqual(identity.lane_id, lane.strip() or DEFAULT_LANE)
            # The identity re-mints the exact same name (stable handle).
            self.assertEqual(identity.assigned_name, name)

    def test_injective_no_collisions(self) -> None:
        # Distinct slots must never share a name. Include slots that differ only
        # by where a delimiter/underscore falls, which a naive join would collide.
        slots = [
            ("a_b", "c", "d"),
            ("a", "b_c", "d"),
            ("a", "b", "c_d"),
            ("a", "b", "c"),
            ("ab", "c", "d"),
            ("a", "bc", "d"),
            ("ws1", "claude", ""),  # -> default lane
            ("ws1", "claude", "default"),  # same normalised slot as above
        ]
        names = [encode_assigned_name(*slot) for slot in slots]
        # The last two normalise to the same slot, so they SHARE a name (correct);
        # every other pair must be distinct.
        self.assertEqual(names[-1], names[-2])
        distinct = names[:-1]
        self.assertEqual(len(set(distinct)), len(distinct))

    def test_generated_name_is_valid_transport_target(self) -> None:
        # A minted name must be usable straight as a #13245 transport target.
        for ws, role, lane in itertools.product(
            _SLOT_VALUES, ("claude", "codex"), ("", "lane_1", "空間")
        ):
            name = encode_assigned_name(ws, role, lane)
            self.assertTrue(valid_target(name), f"{name!r} not a valid target")


class EncodeFailClosedTest(unittest.TestCase):
    """encode_assigned_name raises for empty required components / over-length."""

    def test_empty_required_raises(self) -> None:
        for ws, role in (("", "claude"), ("ws", ""), ("  ", "claude")):
            with self.assertRaises(HerdrIdentityError):
                encode_assigned_name(ws, role, "lane")

    def test_empty_lane_defaults(self) -> None:
        name = encode_assigned_name("ws", "claude", "")
        decoded = decode_assigned_name(name)
        assert decoded.identity is not None
        self.assertEqual(decoded.identity.lane_id, DEFAULT_LANE)

    def test_over_length_raises(self) -> None:
        with self.assertRaises(HerdrIdentityError):
            encode_assigned_name("w" * (NAME_MAX_LENGTH + 10), "claude", "lane")


class DecodeFailClosedTest(unittest.TestCase):
    """decode_assigned_name returns a structured failure, never raises."""

    def test_reason_is_from_closed_vocabulary(self) -> None:
        for bad in ("", None, 123, "not-a-scheme", "mzb1_a", "mzb1_a_b_c_d"):
            decoded = decode_assigned_name(bad)
            self.assertFalse(decoded.ok)
            self.assertIn(decoded.reason, DECODE_FAILURE_REASONS)
            self.assertIsNone(decoded.identity)

    def test_specific_reasons(self) -> None:
        self.assertEqual(decode_assigned_name("").reason, REASON_EMPTY)
        self.assertEqual(decode_assigned_name(None).reason, REASON_EMPTY)
        # Illegal char (a shell metacharacter can never survive the guard).
        self.assertEqual(
            decode_assigned_name("mzb1_a_b_c;rm").reason, REASON_ILLEGAL_CHAR
        )
        # Right alphabet, wrong prefix.
        self.assertEqual(decode_assigned_name("zzz1_a_b_c").reason, REASON_BAD_PREFIX)
        # Right prefix, wrong number of fields.
        self.assertEqual(decode_assigned_name("mzb1_a_b").reason, REASON_BAD_SHAPE)
        # Malformed escape: 'Z' not followed by two hex digits.
        self.assertEqual(decode_assigned_name("mzb1_aZ_b_c").reason, REASON_BAD_ESCAPE)
        self.assertEqual(decode_assigned_name("mzb1_aZ2G_b_c").reason, REASON_BAD_ESCAPE)
        # Over the length cap.
        self.assertEqual(
            decode_assigned_name("mzb1_" + "a" * NAME_MAX_LENGTH).reason,
            REASON_TOO_LONG,
        )

    def test_from_assigned_name_alias(self) -> None:
        name = encode_assigned_name("ws", "claude", "lane")
        via_alias = HerdrAgentIdentity.from_assigned_name(name)
        self.assertTrue(via_alias.ok)
        assert via_alias.identity is not None
        self.assertEqual(via_alias.identity.assigned_name, name)


class IdentityTypeTest(unittest.TestCase):
    """HerdrAgentIdentity holds no pane/terminal locator and normalises the slot."""

    def test_no_pane_or_terminal_field(self) -> None:
        identity = HerdrAgentIdentity(workspace_id="ws", role="claude", lane_id="l1")
        fields = set(vars(identity))
        self.assertNotIn("pane_id", fields)
        self.assertNotIn("terminal_id", fields)
        self.assertEqual(fields, {"workspace_id", "role", "lane_id"})

    def test_slot_and_record_are_durable_only(self) -> None:
        identity = HerdrAgentIdentity(workspace_id="ws", role="claude", lane_id="")
        self.assertEqual(identity.identity_slot, ("ws", DEFAULT_LANE, "claude"))
        record = identity.to_record()
        self.assertEqual(
            set(record), {"assigned_name", "workspace_id", "lane_id", "role"}
        )
        # public_pointer / record carry no session-local locator token.
        self.assertNotIn("pane", identity.public_pointer())

    def test_construction_fails_closed_on_empty_required(self) -> None:
        with self.assertRaises(HerdrIdentityError):
            HerdrAgentIdentity(workspace_id="", role="claude")



class OccupantOfLocatorTest(unittest.TestCase):
    """occupant_of_locator answers "who holds this pane now", fail-closed (#15202).

    The inverse of :func:`rebind_by_name`, and the identity revalidation the turn-start
    rail's WAIT_ERROR Enter-resend compares before and after the wait (audit j#102755
    finding 3). A locator is transient, so anything short of exactly one unambiguous
    answer must be ``None`` — the rail reads ``None`` as "unconfirmed" and withholds
    the extra Enter rather than press it into a pane whose occupant it cannot name.
    """

    def setUp(self) -> None:
        self.name = encode_assigned_name("ws", "claude", "lane15202")

    def test_resolves_the_holder_of_a_locator(self) -> None:
        agents = [
            {"name": "someone_else", "pane_id": "w0:p0"},
            {"name": self.name, "pane_id": "w1:p1"},
        ]
        self.assertEqual(occupant_of_locator("w1:p1", agents), self.name)

    def test_locator_aliases_resolve(self) -> None:
        # Same alias precedence rebind_by_name uses: pane_id, then pane, then location.
        for key in ("pane_id", "pane", "location"):
            with self.subTest(key=key):
                self.assertEqual(
                    occupant_of_locator("w1:p1", [{"name": self.name, key: "w1:p1"}]),
                    self.name,
                )

    def test_unknown_locator_is_none(self) -> None:
        agents = [{"name": self.name, "pane_id": "w1:p1"}]
        self.assertIsNone(occupant_of_locator("w9:p9", agents))
        self.assertIsNone(occupant_of_locator("w1:p1", []))

    def test_ambiguous_locator_is_none(self) -> None:
        # Two rows claiming one pane is a herdr uniqueness violation. Refuse to guess,
        # exactly as REBIND_AMBIGUOUS does — picking either would fabricate an identity.
        agents = [
            {"name": self.name, "pane_id": "w1:p1"},
            {"name": "other_agent", "pane_id": "w1:p1"},
        ]
        self.assertIsNone(occupant_of_locator("w1:p1", agents))

    def test_blank_locator_or_blank_name_is_none(self) -> None:
        # A blank identity would compare equal to another blank one and pass a drift
        # check that never actually confirmed anything.
        self.assertIsNone(occupant_of_locator("", [{"name": self.name, "pane_id": ""}]))
        self.assertIsNone(occupant_of_locator("   ", []))
        self.assertIsNone(occupant_of_locator("w1:p1", [{"name": "  ", "pane_id": "w1:p1"}]))
        self.assertIsNone(occupant_of_locator(None, [{"name": self.name, "pane_id": "w1:p1"}]))

    def test_malformed_rows_are_skipped_not_raised(self) -> None:
        agents = ["not-a-row", 42, None, {"name": self.name, "pane_id": "w1:p1"}]
        self.assertEqual(occupant_of_locator("w1:p1", agents), self.name)


class RebindTest(unittest.TestCase):
    """rebind_by_name recovers a transient locator by durable name, fail-closed."""

    def setUp(self) -> None:
        self.name = encode_assigned_name("ws", "claude", "lane13247")

    def test_rebind_ok_recovers_fresh_locator(self) -> None:
        # Representative rebind path over the REAL herdr `agent list` row schema:
        # the transient locator rides on `pane_id` (AGENT_KEY_LOCATOR, PoC #13175
        # E10 実測). Simulate a post-restart agent list: the pane locator is a
        # *new* value, but the durable name still matches.
        agents = [
            {"name": "someone_else", "pane_id": "w0:p0"},
            {"name": self.name, "pane_id": "w1:p1"},
        ]
        result = rebind_by_name(self.name, agents)
        self.assertEqual(result.status, REBIND_OK)
        self.assertTrue(result.is_rebound)
        self.assertEqual(result.locator, "w1:p1")
        self.assertEqual(result.considered, 2)
        assert result.identity is not None
        self.assertEqual(result.identity.assigned_name, self.name)

    def test_rebind_locator_alias_pane(self) -> None:
        # Alias regression: a row that carries the transient locator under the
        # `pane` alias (AGENT_KEY_LOCATOR_ALIAS) instead of the real `pane_id`
        # primary key still resolves.
        agents = [{"name": self.name, "pane": "w1:p1"}]
        result = rebind_by_name(self.name, agents)
        self.assertEqual(result.status, REBIND_OK)
        self.assertEqual(result.locator, "w1:p1")

    def test_rebind_locator_alias_location(self) -> None:
        # Alias regression: the `location` alias (AGENT_KEY_LOCATOR_ALIAS_2) is the
        # last fallback after `pane_id` and `pane`.
        agents = [{"name": self.name, "location": "w2:p3"}]
        result = rebind_by_name(self.name, agents)
        self.assertEqual(result.status, REBIND_OK)
        self.assertEqual(result.locator, "w2:p3")

    def test_rebind_pane_id_primary_wins_over_aliases(self) -> None:
        # When a row carries all three keys, the real `pane_id` primary is the one
        # recovered — the aliases never shadow it.
        agents = [
            {
                "name": self.name,
                "pane_id": "wPRIMARY:p0",
                "pane": "wALIAS:p1",
                "location": "wALIAS2:p2",
            }
        ]
        result = rebind_by_name(self.name, agents)
        self.assertEqual(result.status, REBIND_OK)
        self.assertEqual(result.locator, "wPRIMARY:p0")

    def test_rebind_not_found(self) -> None:
        result = rebind_by_name(self.name, [{"name": "other", "pane": "w0:p0"}])
        self.assertEqual(result.status, REBIND_NOT_FOUND)
        self.assertTrue(result.is_fail)
        self.assertEqual(result.locator, "")

    def test_rebind_ambiguous(self) -> None:
        agents = [
            {"name": self.name, "pane_id": "w1:p1"},
            {"name": self.name, "pane_id": "w2:p2"},
        ]
        result = rebind_by_name(self.name, agents)
        self.assertEqual(result.status, REBIND_AMBIGUOUS)
        self.assertEqual(result.locator, "")

    def test_rebind_invalid_name(self) -> None:
        result = rebind_by_name("not-a-scheme", [{"name": "not-a-scheme", "pane": "x"}])
        self.assertEqual(result.status, REBIND_INVALID_NAME)
        self.assertTrue(result.is_fail)

    def test_rebind_missing_locator_no_key(self) -> None:
        # A single name match whose row carries no pane/location key must not be
        # reported as a success with a blank target (fail-open); fail closed.
        result = rebind_by_name(self.name, [{"name": self.name}])
        self.assertEqual(result.status, REBIND_MISSING_LOCATOR)
        self.assertFalse(result.is_rebound)
        self.assertTrue(result.is_fail)
        self.assertEqual(result.locator, "")
        self.assertEqual(result.considered, 1)
        assert result.identity is not None
        self.assertEqual(result.identity.assigned_name, self.name)

    def test_rebind_missing_locator_blank_values(self) -> None:
        # An empty / whitespace-only pane with no location alias is unusable too.
        for row in (
            {"name": self.name, "pane": ""},
            {"name": self.name, "pane": "   "},
            {"name": self.name, "pane": "\t"},
        ):
            result = rebind_by_name(self.name, [row])
            self.assertEqual(result.status, REBIND_MISSING_LOCATOR)
            self.assertFalse(result.is_rebound)
            self.assertEqual(result.locator, "")

    def test_rebind_public_pointer_has_no_locator(self) -> None:
        agents = [{"name": self.name, "pane_id": "w1:p1"}]
        result = rebind_by_name(self.name, agents)
        self.assertNotIn("w1:p1", result.public_pointer())


class LaneWorkspaceTokenTest(unittest.TestCase):
    """Redmine #13331 (design j#73357): the lane-scoped herdr workspace token for a
    linked git worktree — deterministic, unique per path, private-path-safe, and a valid
    mzb1 workspace segment."""

    def test_deterministic_and_prefixed(self) -> None:
        a = derive_lane_workspace_token("/work/mozyo_bridge_issue_101")
        b = derive_lane_workspace_token("/work/mozyo_bridge_issue_101")
        self.assertEqual(a, b)
        self.assertTrue(a.startswith(LANE_WORKSPACE_TOKEN_PREFIX + "_"))

    def test_distinct_paths_distinct_tokens(self) -> None:
        self.assertNotEqual(
            derive_lane_workspace_token("/work/lane_a"),
            derive_lane_workspace_token("/work/lane_b"),
        )

    def test_does_not_leak_the_path(self) -> None:
        # Only a hash — never the (private) absolute path — is emitted.
        path = macos_home_path("someone", "secret", "mozyo_bridge_issue_101")
        token = derive_lane_workspace_token(path)
        self.assertNotIn("someone", token)
        self.assertNotIn("secret", token)
        self.assertNotIn("/", token)

    def test_round_trips_through_mzb1_encoding(self) -> None:
        token = derive_lane_workspace_token("/work/mozyo_bridge_issue_101")
        name = encode_assigned_name(token, "codex", "")
        decoded = decode_assigned_name(name)
        self.assertTrue(decoded.ok)
        self.assertEqual(decoded.identity.workspace_id, token)
        self.assertEqual(decoded.identity.role, "codex")
        self.assertEqual(decoded.identity.lane_id, DEFAULT_LANE)
        self.assertLessEqual(len(name), NAME_MAX_LENGTH)


class HerdrNativeIdentityBindingTest(unittest.TestCase):
    """Redmine #15101: Herdr 0.8's short name is an adapter-only identity."""

    def test_native_name_is_deterministic_and_herdr_080_safe(self) -> None:
        logical = encode_assigned_name("a" * 32, "claude", "default")
        first = native_name_for(logical)
        self.assertEqual(first, native_name_for(logical))
        self.assertEqual(len(first), NATIVE_NAME_MAX_LENGTH)
        self.assertTrue(is_native_name(first))
        self.assertRegex(first, r"^mza1_[a-z2-7]{27}$")

    def test_native_name_codec_rejects_noncanonical_managed_shapes(self) -> None:
        self.assertFalse(is_native_name("mza1_" + "0" * 27))
        self.assertFalse(is_native_name("mza1_" + "a" * 26))
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(HerdrNativeIdentityBindingError):
                logicalize_agent_rows(
                    ({"name": "mza1_" + "0" * 27, "pane_id": "w1:p2"},),
                    store=HerdrNativeIdentityBindingStore(home=Path(td)),
                )

    def test_bind_is_idempotent_and_inventory_restores_logical_name(self) -> None:
        logical = encode_assigned_name("a" * 32, "codex", "default")
        with tempfile.TemporaryDirectory() as td:
            store = HerdrNativeIdentityBindingStore(home=Path(td))
            first = store.bind(logical)
            second = store.bind(logical)
            self.assertEqual(first, second)
            rows = logicalize_agent_rows(
                ({"name": first.native_name, "pane_id": "w1:p2"},), store=store
            )
            self.assertEqual(rows[0]["name"], logical)
            self.assertEqual(rows[0]["native_name"], first.native_name)
            self.assertEqual(rows[0]["pane_id"], "w1:p2")
            self.assertEqual(store.path.stat().st_mode & 0o777, 0o600)

    def test_unbound_native_inventory_fails_closed_without_creating_store(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = HerdrNativeIdentityBindingStore(home=Path(td))
            with self.assertRaises(HerdrNativeIdentityBindingError):
                logicalize_agent_rows(
                    ({"name": native_name_for("mzb1_ws_codex_default")},),
                    store=store,
                )
            self.assertFalse(store.path.exists())

    def test_digest_collision_is_explicitly_refused(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = HerdrNativeIdentityBindingStore(home=Path(td))
            native = native_name_for("mzb1_ws_codex_default")
            with mock.patch(
                "mozyo_bridge.core.state.herdr_native_identity_binding.native_name_for",
                return_value=native,
            ):
                store.bind("mzb1_ws_codex_default")
                with self.assertRaises(HerdrNativeIdentityBindingError):
                    store.bind("mzb1_ws_claude_default")

    def test_whole_launch_collision_is_refused_before_store_creation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            store = HerdrNativeIdentityBindingStore(home=Path(td))
            collision = "mza1_" + "a" * 27
            with mock.patch(
                "mozyo_bridge.core.state.herdr_native_identity_binding.native_name_for",
                return_value=collision,
            ):
                with self.assertRaises(HerdrNativeIdentityBindingError):
                    store.bind_many(
                        ("mzb1_ws_codex_default", "mzb1_ws_claude_default")
                    )
            self.assertFalse(store.path.exists())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
