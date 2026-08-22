"""The operator's stall-watch runtime policy (Redmine #15855).

``stall-watcher-screen-diff.md`` `## 既存正本との境界` places **which units to watch, at
what cadence, and how long to wait before escalating** in operator runtime policy — not in
the shipped product. #15855 j#110121-2 restated that and added the fail-closed half:
"設定不在・不正は typed no-op / refusal とし、暗黙に全 agent を監視しない".

This module is the typed form of that boundary. It resolves the ``stall_watch`` block of
``.mozyo-bridge/config.yaml`` into a :class:`StallWatchPolicy`, and its most important
property is what it does with **nothing**:

    An absent block resolves to a policy that watches nothing at all.

Not "watches everything with defaults" — nothing. Cadence and threshold have portable
defaults because a number has to be defensible when nobody chose one, but *scope* has no
defensible default: a watcher that silently reads every pane it can find on a host is a
surveillance surface nobody asked for, and one that escalates about lanes an operator never
declared is noise they cannot have anticipated. So scope is opt-in by written declaration,
and the breadth of it is always something a reader can point at in a file.

For the same reason there is no wildcard. Watching every managed lane is expressible, but
only as :attr:`all_managed_lanes` — a key an operator had to type — rather than as a
pattern that quietly widens when the cockpit grows.

Invalid is not "fall back to the default"
------------------------------------------
A malformed block is a :class:`StallWatchPolicyError`, which the caller turns into a
*disabled* policy carrying the reason. It is deliberately not repaired into the defaults:
an operator who wrote a cadence and mistyped it should get a watcher that says why it is
off, not one silently running at a cadence they did not choose.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

#: Portable default cadence: how often one stall-watch phase should run, in seconds.
#: 300s is the "about five minutes" #15855 asks for, and it is the watcher's OWN watermark
#: — not an OS timer. The host tick stays at ``DEFAULT_OS_TICK_INTERVAL_SECONDS`` (180s),
#: so a phase gated on this constant runs on roughly every other tick. Tick quantization
#: means the realized period is "about five minutes", never exactly 300s, which is why a
#: status surface reports the last and next due instants rather than claiming a period.
DEFAULT_STALL_WATCH_CADENCE_SECONDS = 300

#: Portable default N: consecutive same-class detections before an escalation.
#: See ``stall_escalation_policy.DEFAULT_ESCALATION_THRESHOLD`` for why two.
DEFAULT_STALL_WATCH_THRESHOLD = 2

#: The lowest cadence this policy accepts. A sub-tick cadence cannot be honoured (the phase
#: only runs when the host tick runs) and would make the watermark meaningless, so it is
#: refused rather than accepted and silently rounded up to whatever the tick happens to be.
MINIMUM_CADENCE_SECONDS = 30

#: Resolution outcomes. ``configured`` is the only one that watches anything.
POLICY_ABSENT = "absent"
POLICY_CONFIGURED = "configured"
POLICY_INVALID = "invalid"
POLICY_NO_SCOPE = "declared_without_scope"

#: The recognized keys of the ``stall_watch`` block (closed).
STALL_WATCH_KEYS: frozenset[str] = frozenset(
    {"cadence_seconds", "threshold", "roles", "lanes", "all_managed_lanes"}
)


class StallWatchPolicyError(ValueError):
    """Raised on a structurally invalid ``stall_watch`` block (never repaired silently)."""


@dataclass(frozen=True)
class StallWatchPolicy:
    """The resolved policy, and where it came from.

    ``enabled`` is the single question every caller asks. It is false for an absent block,
    for an invalid one, and for a declared block that names no scope — three different
    reasons that must stay distinguishable in a status surface, which is what
    :attr:`reason` carries.
    """

    enabled: bool = False
    reason: str = POLICY_ABSENT
    cadence_seconds: int = DEFAULT_STALL_WATCH_CADENCE_SECONDS
    threshold: int = DEFAULT_STALL_WATCH_THRESHOLD
    roles: tuple[str, ...] = ()
    lanes: tuple[str, ...] = ()
    all_managed_lanes: bool = False
    detail: str = ""

    @property
    def source(self) -> str:
        """Where the effective values came from, for the escalation record's ``policy``."""
        return "repo_local_config" if self.reason == POLICY_CONFIGURED else self.reason

    def watches_lane(self, lane_id: str) -> bool:
        """Whether this policy admits ``lane_id`` at all.

        Disabled admits nothing — that is the whole point of the fail-closed default.
        """
        if not self.enabled:
            return False
        if self.all_managed_lanes:
            return True
        return str(lane_id or "") in self.lanes

    def watches_role(self, role: str) -> bool:
        """Whether this policy admits ``role``.

        An empty ``roles`` list admits every role *within an already-admitted lane*. That
        asymmetry is deliberate: the lane list is the scope decision (which work is being
        watched), and role is a refinement inside it, so leaving it unset is a meaningful
        "both sides of this lane" rather than an accidental widening.
        """
        if not self.enabled:
            return False
        return not self.roles or str(role or "") in self.roles

    def admits(self, *, lane_id: str, role: str) -> bool:
        return self.watches_lane(lane_id) and self.watches_role(role)

    def telemetry(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "enabled": self.enabled,
            "reason": self.reason,
            "cadence_seconds": self.cadence_seconds,
            "threshold": self.threshold,
            "all_managed_lanes": self.all_managed_lanes,
            "roles": list(self.roles),
            "lanes": list(self.lanes),
        }
        if self.detail:
            payload["detail"] = self.detail
        return payload

    @classmethod
    def disabled(cls, reason: str, detail: str = "") -> "StallWatchPolicy":
        """A policy that watches nothing, carrying why."""
        return cls(enabled=False, reason=reason, detail=detail)

    @classmethod
    def default(cls) -> "StallWatchPolicy":
        return cls.disabled(POLICY_ABSENT)

    @classmethod
    def from_record(
        cls, record: Optional[Mapping[str, object]] = None
    ) -> "StallWatchPolicy":
        """Resolve the ``stall_watch`` block; raises on a malformed one.

        ``None`` resolves to :meth:`default` (watches nothing). A declared block that names
        no scope resolves to a disabled policy with :data:`POLICY_NO_SCOPE` rather than an
        error: it is a legible operator state ("configured, watching nothing yet"), not a
        mistake, and it must not be repaired into watching everything.
        """
        if record is None:
            return cls.default()
        if not isinstance(record, Mapping):
            raise StallWatchPolicyError(
                "stall_watch must be a mapping of settings, not "
                f"{type(record).__name__}"
            )
        unknown = sorted(set(record) - STALL_WATCH_KEYS)
        if unknown:
            raise StallWatchPolicyError(
                f"unknown stall_watch key(s): {unknown}; "
                f"recognized keys are {sorted(STALL_WATCH_KEYS)}"
            )

        cadence = _positive_int(
            record.get("cadence_seconds"),
            field="cadence_seconds",
            default=DEFAULT_STALL_WATCH_CADENCE_SECONDS,
            minimum=MINIMUM_CADENCE_SECONDS,
        )
        threshold = _positive_int(
            record.get("threshold"),
            field="threshold",
            default=DEFAULT_STALL_WATCH_THRESHOLD,
            minimum=1,
        )
        roles = _string_tuple(record.get("roles"), field="roles")
        lanes = _string_tuple(record.get("lanes"), field="lanes")
        all_managed = record.get("all_managed_lanes", False)
        if not isinstance(all_managed, bool):
            raise StallWatchPolicyError(
                "stall_watch.all_managed_lanes must be true or false, not "
                f"{type(all_managed).__name__}"
            )
        if all_managed and lanes:
            raise StallWatchPolicyError(
                "stall_watch declares both all_managed_lanes and an explicit lanes list; "
                "the intended scope is ambiguous, so it is refused rather than guessed"
            )

        if not all_managed and not lanes:
            return cls(
                enabled=False,
                reason=POLICY_NO_SCOPE,
                cadence_seconds=cadence,
                threshold=threshold,
                roles=roles,
                detail=(
                    "stall_watch is declared but names no lanes and does not set "
                    "all_managed_lanes, so nothing is watched. Scope is opt-in by design: "
                    "a watcher never widens itself to every agent on the host."
                ),
            )

        return cls(
            enabled=True,
            reason=POLICY_CONFIGURED,
            cadence_seconds=cadence,
            threshold=threshold,
            roles=roles,
            lanes=lanes,
            all_managed_lanes=all_managed,
        )

    @classmethod
    def resolve(
        cls, record: Optional[Mapping[str, object]] = None
    ) -> "StallWatchPolicy":
        """:meth:`from_record`, with a malformed block turned into a disabled policy.

        The form a watcher tick uses: a bad config must make the watcher say why it is off,
        never crash a supervisor pass and never quietly run on values nobody chose.
        """
        try:
            return cls.from_record(record)
        except StallWatchPolicyError as exc:
            return cls.disabled(POLICY_INVALID, str(exc))


def _positive_int(value: object, *, field: str, default: int, minimum: int) -> int:
    if value is None:
        return default
    # bool is an int subclass; `threshold: true` is a mistake, not the number 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise StallWatchPolicyError(
            f"stall_watch.{field} must be an integer, not {type(value).__name__}"
        )
    if value < minimum:
        raise StallWatchPolicyError(
            f"stall_watch.{field} must be at least {minimum}; got {value}"
        )
    return int(value)


def _string_tuple(value: object, *, field: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise StallWatchPolicyError(
            f"stall_watch.{field} must be a list of strings, not {type(value).__name__}"
        )
    out: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise StallWatchPolicyError(
                f"stall_watch.{field} entries must be non-empty strings; got {item!r}"
            )
        out.append(item.strip())
    if len(set(out)) != len(out):
        raise StallWatchPolicyError(
            f"stall_watch.{field} contains duplicate entries: {sorted(out)}"
        )
    return tuple(out)


__all__ = (
    "DEFAULT_STALL_WATCH_CADENCE_SECONDS",
    "DEFAULT_STALL_WATCH_THRESHOLD",
    "MINIMUM_CADENCE_SECONDS",
    "POLICY_ABSENT",
    "POLICY_CONFIGURED",
    "POLICY_INVALID",
    "POLICY_NO_SCOPE",
    "STALL_WATCH_KEYS",
    "StallWatchPolicy",
    "StallWatchPolicyError",
)
