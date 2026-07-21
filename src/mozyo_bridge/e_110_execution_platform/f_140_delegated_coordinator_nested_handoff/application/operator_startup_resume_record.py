"""Durable gate-transition append port for the resume leg (Redmine #13813 F2, j#79332).

The Design Answer (j#79332 §5) requires the append of the advanced gate journal to be a
**dedicated typed ticket-provider port** (not the delivery-record sink, which is a
notification pointer), with the trusted base URL / explicit write opt-in / credential
**preflighted BEFORE the reserve** (an unset write path means reserve/send 0), and a
post-send append failure surfaced as a typed ``record_failed / operator_reconcile`` rather
than silently swallowed. The fence stays the sole exactly-once authority — a failed append
never re-sends, because a delivered fence row already refuses a re-reserve.

No credential / login method / pane body / hash / absolute path ever enters the payload; the
note is the pasteable-safe :func:`render_gate_journal` output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional

from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.domain.operator_startup_gate import (
    OperatorStartupGate,
)

#: A write transport: ``post_issue_note(issue_id, notes) -> str``, or None when the write
#: path is not opted-in / configured. Injectable for hermetic tests.
TransportFactory = Callable[[Mapping[str, str]], Optional[object]]
#: Resolves redmine credentials (base_url / api_key) for the preflight. Injectable.
CredentialsResolver = Callable[[Mapping[str, str]], object]


def _default_transport_factory(env: Mapping[str, str]) -> Optional[object]:
    """A live credentialed note transport iff the write opt-in is set, else None (preflight)."""
    try:
        from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_note_transport import (
            redmine_delivery_transport_from_env,
        )
    except Exception:  # noqa: BLE001 - adapter unavailable -> no transport (preflight fail)
        return None
    try:
        return redmine_delivery_transport_from_env(env)  # type: ignore[call-arg]
    except TypeError:
        try:
            return redmine_delivery_transport_from_env()
        except Exception:  # noqa: BLE001
            return None
    except Exception:  # noqa: BLE001
        return None


def _default_credentials_resolver(env: Mapping[str, str]) -> object:
    from mozyo_bridge.e_140_adapter_provider.f_120_redmine_adapter.infrastructure.redmine_credentials import (
        resolve_redmine_credentials,
    )

    return resolve_redmine_credentials(environ=env)


@dataclass(frozen=True)
class ResumeGateRecorder:
    """Preflight + append the advanced gate journal via the credentialed ticket-provider write.

    :meth:`preflight` (called BEFORE the reserve) is True only when the write opt-in transport
    is available AND the trusted base URL + credential resolve — otherwise the leg zero-sends.
    :meth:`record` appends :func:`render_gate_journal` to the issue and returns True on a
    confirmed write, False on any transport failure (a typed record-failed the leg maps to
    operator reconcile, fence still authoritative).
    """

    issue: str
    env: Mapping[str, str]
    transport_factory: TransportFactory = field(default=_default_transport_factory)
    credentials_resolver: CredentialsResolver = field(default=_default_credentials_resolver)

    def preflight(self) -> bool:
        try:
            credentials = self.credentials_resolver(self.env)
        except Exception:  # noqa: BLE001 - unresolved credentials -> preflight fail (zero-send)
            return False
        base_url = getattr(credentials, "base_url", None)
        api_key = getattr(credentials, "api_key", None)
        if not base_url or not api_key:
            return False
        return self.transport_factory(self.env) is not None

    def record(self, gate: OperatorStartupGate) -> bool:
        return self._append(gate, supersedes_note="")

    def record_reissue(self, gate: OperatorStartupGate, supersedes_note: str) -> bool:
        """Append a fresh v3 gate journal carrying a ``supersedes`` pointer (legacy reapproval)."""
        return self._append(gate, supersedes_note=supersedes_note)

    def _append(self, gate: OperatorStartupGate, *, supersedes_note: str) -> bool:
        transport = self.transport_factory(self.env)
        if transport is None:
            return False
        # Lazy import avoids a leg <-> recorder import cycle (the leg owns the serializer).
        from mozyo_bridge.e_110_execution_platform.f_140_delegated_coordinator_nested_handoff.application.operator_startup_resume_leg import (
            render_gate_journal,
        )

        try:
            transport.post_issue_note(  # type: ignore[attr-defined]
                self.issue, render_gate_journal(gate, supersedes_note=supersedes_note)
            )
            return True
        except Exception:  # noqa: BLE001 - transport failure -> record failed (operator reconcile)
            return False


__all__ = (
    "TransportFactory",
    "CredentialsResolver",
    "ResumeGateRecorder",
)
