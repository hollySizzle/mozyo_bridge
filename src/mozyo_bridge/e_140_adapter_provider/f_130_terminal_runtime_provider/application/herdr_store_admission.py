"""Store-side admission for a managed launch (Redmine #15520).

Split out of ``herdr_launcher_capability`` when ``doctor`` became a second caller. The
question this module answers — *does the selected home's attestation store admit a
managed launch at all?* — is settled by the store's own shape, with no launcher probed
and no subprocess run, so a diagnostic can ask it as cheaply as the launch preflight
does. Keeping one implementation is the point: doctor previously reported ``herdr: ok``
on a host where every managed launch was already refused by this exact join, and a
second copy of the rule would drift straight back into that.

The dependency runs one way. Nothing here imports the capability module; the verdict type
and the store vocabulary live here and ``herdr_launcher_capability`` imports them back, so
its ``decide_store_compatibility`` stays the single joined decision for callers that DO
hold a launcher observation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    STORE_ABSENT as _STORE_ABSENT_STATE,
    STORE_UNREADABLE as _STORE_UNREADABLE_STATE,
    STORE_UNSUPPORTED as _STORE_UNSUPPORTED_STATE,
    StoreSchemaObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    MIGRATE_HINT as _MIGRATE_HINT,
    epoch_store_admission,
)


@dataclass(frozen=True)
class LauncherCapabilityVerdict:
    """The fail-closed capability verdict. ``ok`` is True only for a full match."""

    ok: bool
    reason: str
    #: An operator-facing, value-free explanation (never a path / secret) suitable for
    #: the fail-closed error the probe raises.
    detail: str


# --- Store-join verdict vocabulary (Redmine #13882; fail-closed). ---------------------
#: The selected store's shape is writable by the probed launcher for this launch kind.
STORE_JOIN_OK = "attestation_store_ok"
#: The store file exists but cannot be opened / queried at all.
STORE_UNREADABLE = "attestation_store_unreadable"
#: The store's recorded version / on-disk shape is not one this runtime recognizes.
STORE_UNSUPPORTED = "attestation_store_unsupported"
#: The store is older than this runtime and the probed launcher cannot prove it writes
#: that shape — the exact live-but-unattested class of #13882.
STORE_LAUNCHER_CANNOT_WRITE = "attestation_store_launcher_cannot_write"
#: Maintenance holds the store exclusively, so admission fails closed at acquisition
#: (Redmine #13882 j#80190 boundary 1) — before any workspace / tab / agent exists.
STORE_MAINTENANCE_IN_PROGRESS = "attestation_store_maintenance_in_progress"
#: A replacement launch was requested against a store whose shape has no
#: ``replacement_action_id`` column.
STORE_REPLACEMENT_UNSUPPORTED = "attestation_store_replacement_unsupported"

#: The store shape that first carried ``replacement_action_id`` (#13806). A replacement
#: launch cannot be attested by anything older.
_REPLACEMENT_MIN_STORE_VERSION = 2

def decide_store_admission(
    store: StoreSchemaObservation,
    *,
    required_schema_version: int,
    replacement_launch: bool = False,
    epoch_launch: bool = False,
) -> Optional[LauncherCapabilityVerdict]:
    """Decide what the SELECTED store's own shape settles, before any launcher is probed.

    Extracted from :func:`decide_store_compatibility` (Redmine #15520) as pure code
    motion: same branches, same order, same reasons and detail text. It exists as its
    own callable because the store-side verdict is a **home-level fact** that a caller
    holding no launcher observation still needs to know — specifically ``doctor``, which
    reported ``herdr: ok`` on a host whose every managed launch was already being refused
    by this exact join. Sharing the function is the point: a doctor that re-implemented
    the rule would drift from the rail that actually stops the launch.

    Returns ``None`` when the store settles nothing and the launcher-side checks must
    still run; otherwise the verdict — including the *approving* verdict for an absent
    store, which the caller returns as-is.
    """
    if store.state == _STORE_UNREADABLE_STATE:
        return LauncherCapabilityVerdict(
            False,
            STORE_UNREADABLE,
            "the selected attestation store could not be read (corrupt, or not a "
            "database); an unreadable store is not an empty one, so no launch may "
            "proceed against it — its attestations could not be verified afterwards",
        )
    if store.state == _STORE_UNSUPPORTED_STATE:
        hint = (
            "it is newer than this runtime understands; use a newer runtime"
            if store.upgrade_required
            else "its recorded version and on-disk shape disagree (partial / corrupt / "
            f"foreign); restore from a backup or rebuild it with "
            f"`mozyo-bridge herdr attestation-store rebuild --write`"
        )
        return LauncherCapabilityVerdict(
            False,
            STORE_UNSUPPORTED,
            f"the selected attestation store has an unsupported schema "
            f"(recorded version {store.version}) — {hint}. Launching would boot a pair "
            f"whose self-attestations this runtime could never read",
        )
    if store.state == _STORE_ABSENT_STATE:
        return LauncherCapabilityVerdict(
            True,
            STORE_JOIN_OK,
            f"no attestation store exists yet; the first self-attestation creates it at "
            f"v{int(required_schema_version)}",
        )
    version = int(store.version or 0)
    if replacement_launch and version < _REPLACEMENT_MIN_STORE_VERSION:
        return LauncherCapabilityVerdict(
            False,
            STORE_REPLACEMENT_UNSUPPORTED,
            f"this is a replacement launch, but the selected attestation store is "
            f"v{version}, whose shape has no `replacement_action_id` column. Attesting "
            f"it would silently drop the replacement binding a recovery matches on "
            f"exactly, so the pair would relaunch unverifiable. Migrate the store first: "
            f"{_MIGRATE_HINT}",
        )
    epoch_refusal = epoch_store_admission(
        epoch_launch=epoch_launch, store_version=version, migrate_hint=_MIGRATE_HINT
    )
    if epoch_refusal is not None:
        return LauncherCapabilityVerdict(False, *epoch_refusal)
    if version != int(required_schema_version):
        return LauncherCapabilityVerdict(
            False,
            STORE_LAUNCHER_CANNOT_WRITE,
            f"the selected attestation store is v{version}, but a managed launch now "
            f"requires the v{int(required_schema_version)} terminal-identity shape. "
            f"Older shapes remain readable only; migrate before launch: {_MIGRATE_HINT}",
        )
    return None



__all__ = (
    "LauncherCapabilityVerdict",
    "STORE_JOIN_OK",
    "STORE_LAUNCHER_CANNOT_WRITE",
    "STORE_MAINTENANCE_IN_PROGRESS",
    "STORE_REPLACEMENT_UNSUPPORTED",
    "STORE_UNREADABLE",
    "STORE_UNSUPPORTED",
    "decide_store_admission",
)
