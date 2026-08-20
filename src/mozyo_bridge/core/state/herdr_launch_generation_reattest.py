"""Restored-terminal re-attest CAS for the launch-generation store (Redmine #15769).

Companion to :mod:`mozyo_bridge.core.state.herdr_launch_generation` (the same
module-health split shape as :mod:`.herdr_launch_generation_authority`): the store
class exposes :meth:`HerdrLaunchGenerationStore.reattest_restored_terminal` and
delegates the locked write body here.

The write side of the #15769 restored-pair re-attest (design decision j#108766;
measured deadlock #15631 j#108741): a Herdr/tmux server loss can restore a live,
working slot under a NEW server-owned terminal id (and possibly a new pane locator)
while the store still records the launch-time values, so the read-side
``verified_generation_token`` — deliberately unchanged — refuses forever. Only the
GOVERNED rebind rail calls this, after it has proven the identity join on
server-owned inventory facts (unique live named slot, SLOT_LIVE, exact stamps); the
store performs nothing but the byte-exact CAS.

Fail-closed, mirroring the store's ``finalize``: the exact expected old row is
required — ``(assigned_name, startup_action_id, phase='attested')`` plus the
reserved identity, the expected old ``locator`` / ``terminal_id`` and the recorded
``verdict`` must all match, or zero rows update and this raises. Never an upsert
(an absent / pending / superseded / already-moved row is refused), and a no-op
request (old values equal the live values) is refused rather than reported as a
write. ``observed_at`` / ``attested_at`` are deliberately NOT touched: they remain
the original launch's attestation evidence, which delivery bindings compare
byte-exactly. The store method wraps this body in its SHARED store lock (the same
locked write funnel as reserve / finalize) so maintenance cannot rotate the store
mid-write.

Imports of the store symbols happen inside the function, mirroring the authority
companion's function-level import style and keeping this module import-safe from
either direction.
"""

from __future__ import annotations

import sqlite3


def reattest_restored_terminal_locked(
    store,
    *,
    assigned_name: str,
    startup_action_id: str,
    workspace_id: str,
    role: str,
    lane_id: str,
    verdict: str,
    expected_locator: str,
    expected_terminal_id: str,
    live_locator: str,
    live_terminal_id: str,
):
    """The locked CAS body (the store method already holds the shared store lock)."""
    from mozyo_bridge.core.state.herdr_launch_generation import (
        _TABLE,
        GENERATION_ATTESTED,
        HerdrLaunchGenerationError,
        _decode,
        _rollback_quietly,
        _token,
    )

    fields = {
        "assigned_name": _token(assigned_name, "assigned_name"),
        "startup_action_id": _token(startup_action_id, "startup_action_id"),
        "workspace_id": _token(workspace_id, "workspace_id"),
        "role": _token(role, "role"),
        "lane_id": _token(lane_id, "lane_id"),
        "verdict": _token(verdict, "verdict"),
        "expected_locator": _token(expected_locator, "expected_locator"),
        "expected_terminal_id": _token(expected_terminal_id, "expected_terminal_id"),
        "live_locator": _token(live_locator, "live_locator"),
        "live_terminal_id": _token(live_terminal_id, "live_terminal_id"),
    }
    if (
        fields["expected_locator"] == fields["live_locator"]
        and fields["expected_terminal_id"] == fields["live_terminal_id"]
    ):
        raise HerdrLaunchGenerationError(
            "restored-terminal re-attest refused: the expected and live values are "
            "identical (nothing to re-attest; the caller reports a typed no-op)"
        )
    if not store.path.exists():
        raise HerdrLaunchGenerationError(
            "cannot re-attest a generation: the store does not exist (no attested row)"
        )
    conn = store._connect_existing(readonly=False)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            f"UPDATE {_TABLE} SET locator=?, terminal_id=? "
            "WHERE assigned_name=? AND startup_action_id=? AND phase=? "
            "AND workspace_id=? AND role=? AND lane_id=? "
            "AND locator=? AND terminal_id=? AND verdict=?",
            (
                fields["live_locator"],
                fields["live_terminal_id"],
                fields["assigned_name"],
                fields["startup_action_id"],
                GENERATION_ATTESTED,
                fields["workspace_id"],
                fields["role"],
                fields["lane_id"],
                fields["expected_locator"],
                fields["expected_terminal_id"],
                fields["verdict"],
            ),
        )
        if conn.total_changes != 1:
            raise HerdrLaunchGenerationError(
                "restored-terminal re-attest compare-and-set was refused (no attested "
                "row matches this exact identity, token, and expected old locator / "
                "terminal — the row may be absent, pending, superseded, or already "
                "moved)"
            )
        row = store._row(conn, fields["assigned_name"])
        conn.commit()
        return _decode(row)
    except HerdrLaunchGenerationError:
        _rollback_quietly(conn)
        raise
    except (sqlite3.DatabaseError, OSError) as exc:
        _rollback_quietly(conn)
        raise HerdrLaunchGenerationError(
            "restored-terminal re-attest write failed"
        ) from exc
    except BaseException:
        _rollback_quietly(conn)
        raise
    finally:
        conn.close()


__all__ = ("reattest_restored_terminal_locked",)
