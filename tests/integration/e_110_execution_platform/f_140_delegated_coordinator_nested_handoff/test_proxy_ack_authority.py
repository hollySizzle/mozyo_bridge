"""``resolve_ack_authority`` tests driven by a REAL ``SenderIdentity`` (Redmine #14546, j#90142).

The live coordinator's first acknowledgement raised ``AttributeError``: the code correlated the
acknowledging process with the live slot through ``identity.assigned_name``, and ``SenderIdentity``
has no such field — it carries exactly ``workspace_id`` / ``role`` / ``lane_id``. The attribute was
verified on a *different* class (``HerdrAgentIdentity``, which does have it) and never on the one
the resolver actually returns.

No test caught it because the CLI regression stubbed ``resolve_ack_authority`` wholesale: it pinned
what the command does with an authority verdict, and never that the authority itself can be computed.
So these drive the real function with a real resolved identity, from real env values through
``resolve_sender_identity``, with only the inventory and attestation store injected. Anything that
touches a field the identity does not have fails here rather than in production.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application import (  # noqa: E501
    coordinator_proxy_send as send_mod,
)
from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
    resolve_ack_authority,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_identity import (  # noqa: E501
    AGENT_KEY_LOCATOR,
    AGENT_KEY_NAME,
    encode_assigned_name,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution import (  # noqa: E501
    SenderIdentity,
    resolve_sender_identity,
)

WS = "e1487dcb1f2d4412b28e825fdeccf9e8"
FOREIGN_WS = "ffffffffffffffffffffffffffffffff"
PROVIDER = "codex"
LOCATOR = "w3C:p3"


def _env(workspace_id=WS, role=PROVIDER, lane="default"):
    return {
        "MOZYO_WORKSPACE_ID": workspace_id,
        "MOZYO_AGENT_ROLE": role,
        "MOZYO_LANE_ID": lane,
    }


def _row(workspace_id=WS, provider=PROVIDER, lane="default", locator=LOCATOR):
    return {
        AGENT_KEY_NAME: encode_assigned_name(workspace_id, provider, lane),
        AGENT_KEY_LOCATOR: locator,
    }


class SenderIdentityShapeTest(unittest.TestCase):
    """Pin the shape the correlation is allowed to depend on."""

    def test_sender_identity_has_no_assigned_name(self):
        # The exact live failure: reading this field raised AttributeError in the coordinator.
        identity = SenderIdentity(workspace_id=WS, role=PROVIDER, lane_id="default")
        self.assertFalse(hasattr(identity, "assigned_name"))
        self.assertEqual(
            sorted(SenderIdentity.__dataclass_fields__), ["lane_id", "role", "workspace_id"]
        )

    def test_a_real_resolution_yields_that_shape(self):
        resolution = resolve_sender_identity(_env(), anchor_workspace_id=WS)
        self.assertTrue(resolution.ok)
        self.assertFalse(hasattr(resolution.identity, "assigned_name"))


class AckAuthorityWithRealIdentityTest(unittest.TestCase):
    def setUp(self) -> None:
        self._patch("live_workspace_id", lambda _root: WS)
        self._patch("live_agent_rows", lambda _env: [_row()])
        self._patch(
            "live_attestation_join",
            lambda _name, *, locator, workspace_id, provider: (True, "ok", "attested"),
        )
        self._patch("resolve_default_lane_authority", lambda _root: ("resolved", "coordinator", "s", ""))
        self._patch("resolve_expected_provider", lambda _root, _role: PROVIDER)

    def _patch(self, name, value):
        original = getattr(send_mod, name)
        setattr(send_mod, name, value)
        self.addCleanup(setattr, send_mod, name, original)

    def _run(self, env):
        return resolve_ack_authority(str(ROOT), env=env)

    def test_the_live_coordinator_is_admitted(self):
        # The production path end to end: real env -> real SenderIdentity -> slot correlation.
        ok, reason, _detail = self._run(_env())
        self.assertTrue(ok, reason)
        self.assertEqual(reason, "")

    def test_an_unattested_shell_is_refused(self):
        ok, reason, _detail = self._run({})
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_unattested")

    def test_a_foreign_workspace_env_is_refused(self):
        # `resolve_sender_identity` itself rejects an env/anchor workspace mismatch.
        ok, reason, _detail = self._run(_env(workspace_id=FOREIGN_WS))
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_unattested")

    def test_a_non_default_lane_agent_is_refused(self):
        ok, reason, _detail = self._run(_env(lane="issue_14546"))
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_not_default_lane")

    def test_a_provider_that_is_not_the_bound_one_is_refused(self):
        ok, reason, _detail = self._run(_env(role="claude"))
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_provider_mismatch")

    def test_a_slot_whose_canonical_name_is_not_the_sender_s_is_refused(self):
        # A defensive branch: the earlier links already exclude a foreign workspace / lane /
        # provider, so this is reached only if target resolution ever yields a slot the sender's
        # own identity does not derive. It must still refuse rather than admit on "close enough".
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.coordinator_proxy_send import (  # noqa: E501
            ProxyTarget,
        )

        self._patch(
            "resolve_proxy_target",
            lambda _rows, *, workspace_id, provider, attestation_join=None: ProxyTarget(
                status="ok",
                assigned_name=encode_assigned_name(FOREIGN_WS, PROVIDER, "default"),
                locator=LOCATOR,
                live=1,
                with_locator=1,
            ),
        )
        ok, reason, _detail = self._run(_env())
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_unattested")

    def test_an_unattested_live_slot_is_refused(self):
        self._patch(
            "live_attestation_join",
            lambda _name, *, locator, workspace_id, provider: (False, "stale", "stale record"),
        )
        ok, reason, _detail = self._run(_env())
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_ack_unattested")

    def test_an_unresolvable_workspace_is_refused(self):
        self._patch("live_workspace_id", lambda _root: "")
        ok, reason, _detail = self._run(_env())
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_workspace_unresolved")

    def test_a_missing_role_authority_is_refused(self):
        self._patch("resolve_default_lane_authority", lambda _root: ("missing", "", "", ""))
        ok, reason, _detail = self._run(_env())
        self.assertFalse(ok)
        self.assertEqual(reason, "proxy_coordinator_authority_missing")

    def test_the_correlation_uses_only_fields_the_identity_has(self):
        # A structural guard against the recurrence: the resolved identity is passed through a
        # namespace that raises on ANY attribute the real dataclass does not declare, so reading a
        # field off the wrong class fails here.
        declared = set(SenderIdentity.__dataclass_fields__)

        class _StrictIdentity:
            def __init__(self, inner):
                self._inner = inner

            def __getattr__(self, name):
                if name not in declared:
                    raise AssertionError(
                        f"the ack correlation read {name!r}, which SenderIdentity does not have"
                    )
                return getattr(self._inner, name)

        original = send_mod.resolve_sender_identity if hasattr(send_mod, "resolve_sender_identity") else None
        self.assertIsNone(original)  # it is imported inside the function, so patch the source

        import mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.domain.herdr_target_resolution as target_mod  # noqa: E501

        real = target_mod.resolve_sender_identity

        def _strict(env, **kwargs):
            resolution = real(env, **kwargs)
            if resolution.ok and resolution.identity is not None:
                object.__setattr__(resolution, "identity", _StrictIdentity(resolution.identity))
            return resolution

        target_mod.resolve_sender_identity = _strict
        self.addCleanup(setattr, target_mod, "resolve_sender_identity", real)

        ok, reason, _detail = self._run(_env())
        self.assertTrue(ok, reason)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
