"""Fail-closed readback for the offline-rollout supervisor stop phase (#15192).

The scheduler backend owns the mutation.  This module only joins its result to a fresh,
secret-safe status projection before the rollout may migrate shared stores or runtimes.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence


BACKEND_LAUNCHD = "launchd"
BACKEND_SYSTEMD = "systemd_user"
EFFECT_COMPLETE = "complete"
LEGACY_DRAIN_ABSENT = "absent"
LEGACY_DRAIN_OWNED = "owned"


def supervisor_stop_refusal(
    result: object, status: object, *, expected_label: object
) -> str:
    """Return ``""`` only for positive current-and-legacy stop evidence.

    Missing fields never mean ``False``/absent.  In particular, launchd's retired
    ``--drain-only`` registration must be absent in both the uninstall outcome and the fresh
    status row; a partial migration cannot authorize the following offline rollout phase.
    """
    if not isinstance(result, Mapping) or not isinstance(status, Mapping):
        return "supervisor_stop_evidence_invalid"
    if type(expected_label) is not str or not expected_label:
        return "supervisor_stop_label_invalid"
    backend = result.get("backend")
    if backend not in (BACKEND_LAUNCHD, BACKEND_SYSTEMD):
        return "supervisor_stop_backend_invalid"
    if status.get("backend") != backend:
        return "supervisor_stop_backend_drift"
    if result.get("performed") is not True:
        return "supervisor_uninstall_not_performed"
    if result.get("effect_state") != EFFECT_COMPLETE:
        return "supervisor_uninstall_incomplete"

    rows = status.get("agents")
    if (
        not isinstance(rows, Sequence)
        or isinstance(rows, (str, bytes, bytearray))
        or len(rows) != 1
        or not isinstance(rows[0], Mapping)
    ):
        return "supervisor_status_roster_invalid"
    row = rows[0]
    if result.get("label") != expected_label or row.get("label") != expected_label:
        return "supervisor_stop_label_drift"
    if row.get("installed") is not False or row.get("loaded") is not False:
        return "supervisor_current_stop_unverified"

    if backend == BACKEND_LAUNCHD:
        legacy_before = result.get("legacy_drain")
        legacy_removed = result.get("legacy_drain_removed")
        migration_complete = (
            legacy_before == LEGACY_DRAIN_ABSENT and legacy_removed is False
        ) or (legacy_before == LEGACY_DRAIN_OWNED and legacy_removed is True)
        if (
            not migration_complete
            or result.get("legacy_drain_reason") != ""
            or row.get("legacy_drain") != LEGACY_DRAIN_ABSENT
        ):
            return "supervisor_legacy_stop_unverified"
    return ""


__all__ = ("supervisor_stop_refusal",)
