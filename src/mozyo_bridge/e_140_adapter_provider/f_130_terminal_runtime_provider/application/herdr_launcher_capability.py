"""Launcher attestation-schema capability contract — the pure decision half (Redmine #13847).

The #13748 launcher preflight (:func:`...herdr_pane_lifecycle
.preflight_attest_launcher_capability`) proved the selected launcher *carries* the
``herdr agent-attest`` subcommand by matching :data:`...herdr_launch_argv
.ATTEST_CAPABILITY_MARKER` (``--assigned-name``) in its ``--help`` output. That is a
**subcommand-marker** check only. It cannot see the failure #13847 closes: the source
runtime's startup self-attestation store is schema v2
(:data:`...herdr_identity_attestation.HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION`), but a
managed launch may be wrapped through an *older installed* launcher whose attestation
store is v1. Both launchers carry ``agent-attest`` and ``--assigned-name`` — so the
subcommand-marker probe passes — yet the v1 launcher, injected with the source runtime's
shared ``MOZYO_BRIDGE_HOME``, opens the v2 store, hits the exact-version write guard
(``_connect_rw``), silently drops the attestation, and ``exec``s the provider anyway. The
pair boots **live but unattested / stale**, and every downstream verify (adopt, resume,
recover) fails closed with no public recovery — the live evidence in the issue.

This module is the **pure decision** for the schema/capability contract, split from the
subprocess **probe** (which stays in :mod:`herdr_pane_lifecycle`) and from the
**orchestration** (the session-start / sublane-create callers) so the three concerns are
separately testable (Redmine #13847 required implementation 6). It owns:

- the machine-readable capability contract token the source ``agent-attest --help``
  advertises (:func:`build_attest_capability_contract_line`), whitespace-free so
  argparse's help wrapping can never split it;
- a pure parse of a launcher's probe output into observed facts
  (:func:`parse_launcher_capability_output`);
- the fail-closed verdict that compares those facts to the source runtime's required
  attestation schema version (:func:`decide_launcher_capability`).

A launcher that predates the contract token (any pre-#13847 build, incl. the v1 installed
launcher) advertises no schema and is rejected ``schema_contract_absent`` — it cannot be
proven compatible, so it fails closed. A launcher advertising a *different* exact schema
is rejected ``schema_version_mismatch`` (the shared store's write guard requires an exact
match, so newer and older are both incompatible). Only an exact schema match — with the
subcommand marker still present — is compatible.

Redmine #13882 extends this module with the other half of the join. The decision above is
still **code vs code** — a launcher's advertised schema against the source runtime's
required schema — so two v2 runtimes agree while the *selected shared home* holds a v1
store on disk, the probe passes, and the pair boots live but unattested exactly as
described above. :func:`decide_store_compatibility` joins the same launcher observation
against the real store's probed shape, and
:func:`build_attest_capability_stores_line` / :attr:`LauncherCapabilityObservation
.writable_store_versions` let a launcher advertise the store shapes it can *write* —
without which a pre-#13882 build and a v1-compatible one are indistinguishable.

Pure: no I/O, no subprocess, no store access. It imports the sibling capability marker
constant plus the core store's schema vocabulary (state tokens and the probe's
observation type — constants and a frozen dataclass, no I/O), so the dependency points
only at a core leaf, never provider -> provider.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    STORE_ABSENT as _STORE_ABSENT_STATE,
    STORE_UNREADABLE as _STORE_UNREADABLE_STATE,
    STORE_UNSUPPORTED as _STORE_UNSUPPORTED_STATE,
    StoreSchemaObservation,
)
from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_argv import (
    ATTEST_CAPABILITY_MARKER,
)

#: The stable prefix the source ``herdr agent-attest --help`` advertises to declare its
#: attestation-store schema/capability contract (Redmine #13847). Two properties keep the
#: probed launcher's rendering of it stable: the ``agent-attest`` subparser uses a
#: ``RawDescriptionHelpFormatter`` so its epilog is emitted verbatim (no reflow), and the
#: token itself is hyphen- and whitespace-FREE (measured: argparse's default help wrapping
#: breaks a long token on hyphens AND on width, which split an earlier hyphenated form
#: mid-token so a capable launcher read as incapable). Underscores are not break points,
#: and the raw formatter means it is never wrapped in the first place.
ATTEST_CAPABILITY_CONTRACT_PREFIX = "mozyo_attest_capability_schema="

#: Matches the advertised schema token in probe output. Anchored on the exact prefix so a
#: stray digit elsewhere in the help can never be misread as the advertised schema, and —
#: since review j#80000 finding 3 — bounded on BOTH sides so only a **whole, canonical**
#: token is credited. Without the boundaries ``…schema=2x`` matched its leading ``2`` and
#: was credited as a clean v2 advertisement: a malformed advertisement is *unprovable*,
#: and an admission contract must not credit what a launcher did not clearly say.
_CONTRACT_RE = re.compile(
    r"(?:^|\s)" + re.escape(ATTEST_CAPABILITY_CONTRACT_PREFIX) + r"(\d+)(?=\s|$)"
)

#: The stable prefix advertising the set of **store shapes this launcher can write**
#: (Redmine #13882). The #13847 token above advertises a single *native* schema, which
#: cannot distinguish two launchers that both say ``2``: a pre-#13882 build that can only
#: write a v2 store, and a #13882 build that can also write a v1 store conservatively.
#: Against a v1 shared home the first silently drops its attestation and the second is
#: safe — so the writable SET, not the native version, is what a store join must consult.
#: Underscore-separated for the same reason the sibling token is hyphen-free: argparse's
#: help wrapping breaks on hyphens and width, and underscores are not break points.
ATTEST_CAPABILITY_STORES_PREFIX = "mozyo_attest_capability_stores="

#: The canonical writable-set grammar: ``<int>(_<int>)*``, bounded on both sides. It
#: admits ``1_2`` and rejects every malformed spelling outright rather than salvaging a
#: capability from it (review j#80000 finding 3): ``1__2`` / ``_1_2_`` (empty segments)
#: and ``1_2junk`` (trailing garbage) previously yielded ``{1, 2}``, crediting a launcher
#: with the v1-write capability that admits a v1 store — re-opening the very
#: live-but-unattested launch #13882 exists to refuse.
_STORES_RE = re.compile(
    r"(?:^|\s)" + re.escape(ATTEST_CAPABILITY_STORES_PREFIX) + r"(\d+(?:_\d+)*)(?=\s|$)"
)

# --- Verdict vocabulary (fail-closed; only LAUNCHER_CAPABILITY_OK proceeds). ----------
#: Subcommand marker present AND advertised schema == the required source schema.
LAUNCHER_CAPABILITY_OK = "launcher_capability_ok"
#: The ``agent-attest`` subcommand marker (``--assigned-name``) is absent — the launcher
#: does not carry the wrapper subcommand at all (the pre-#13748 failure class).
LAUNCHER_SUBCOMMAND_ABSENT = "launcher_subcommand_absent"
#: The subcommand is present but the launcher advertises NO attestation-schema contract —
#: it predates the schema-versioned contract (e.g. the v1 installed launcher). Unprovable
#: compatibility fails closed.
LAUNCHER_SCHEMA_CONTRACT_ABSENT = "launcher_schema_contract_absent"
#: The launcher advertises a schema that is not the exact source-required version. The
#: shared attestation store's write guard requires an exact version, so both older and
#: newer are incompatible.
LAUNCHER_SCHEMA_VERSION_MISMATCH = "launcher_schema_version_mismatch"


@dataclass(frozen=True)
class LauncherCapabilityObservation:
    """The value-free facts a launcher's capability probe output carries (pure).

    ``subcommand_marker_present`` — the ``--assigned-name`` marker (proof the
    ``agent-attest`` subcommand exists, the #13748 check). ``advertised_schema_version`` —
    the attestation-store schema the launcher declares via the #13847 contract token, or
    ``None`` when the launcher advertises no contract (a pre-#13847 build).
    ``advertised_store_versions`` — the store shapes the launcher declares it can WRITE
    (Redmine #13882), or ``None`` when it advertises no such set (a pre-#13882 build).
    """

    subcommand_marker_present: bool
    advertised_schema_version: Optional[int]
    advertised_store_versions: Optional[frozenset] = None

    @property
    def writable_store_versions(self) -> frozenset:
        """The store shapes this launcher can be *proven* to write (fail-closed).

        A launcher advertising no #13882 set is credited with its native schema **only**:
        that is exactly the pre-#13882 build whose write guard is an exact-version match,
        so crediting it with anything more would re-admit the silent-drop it cannot avoid.
        """
        if self.advertised_store_versions is not None:
            return self.advertised_store_versions
        if self.advertised_schema_version is None:
            return frozenset()
        return frozenset({self.advertised_schema_version})


@dataclass(frozen=True)
class LauncherCapabilityVerdict:
    """The fail-closed capability verdict. ``ok`` is True only for a full match."""

    ok: bool
    reason: str
    #: An operator-facing, value-free explanation (never a path / secret) suitable for
    #: the fail-closed error the probe raises.
    detail: str


def build_attest_capability_contract_line(schema_version: int) -> str:
    """The capability contract token the source ``agent-attest --help`` advertises (pure).

    Rendered into the ``agent-attest`` subparser's help so a launcher's ``--help`` output
    carries its attestation-store schema version. Built from the store's schema constant
    at the call site so the advertised number can never drift from the store it gates.
    Whitespace-free (one token) so ``--help`` wrapping cannot split it.
    """
    return f"{ATTEST_CAPABILITY_CONTRACT_PREFIX}{int(schema_version)}"


def build_attest_capability_stores_line(store_versions) -> str:
    """The writable-store-set token the source ``agent-attest --help`` advertises (pure).

    Built from the store module's recognized-version set at the call site so the
    advertised set can never drift from the shapes the writer actually accepts.
    Underscore-separated and sorted for a stable, wrap-proof rendering.
    """
    joined = "_".join(str(int(v)) for v in sorted(store_versions))
    return f"{ATTEST_CAPABILITY_STORES_PREFIX}{joined}"


def parse_launcher_capability_output(text: str) -> LauncherCapabilityObservation:
    """Parse a launcher's capability probe output into observed facts (pure).

    Reads the combined stdout+stderr of ``<launcher> herdr agent-attest --help``. The
    subcommand marker, the advertised schema, and the advertised writable store set are
    looked up independently: a launcher can carry the subcommand yet advertise no schema
    (the pre-#13847 installed launcher), or advertise a schema but no store set (a
    pre-#13882 build). Each unprovable fact stays ``None`` → fail closed, never a guessed
    default.

    "Unprovable" is strict (review j#80000 finding 3). Only a **whole canonical token**
    counts; a malformed spelling is not salvaged into a capability, and **conflicting**
    advertisements of the same fact are not arbitrated — a launcher declaring two
    different schemas has not clearly declared either, so the fact stays ``None`` and the
    admission fails closed rather than picking whichever came first.
    """
    haystack = text or ""
    advertised: Optional[int] = None
    schema_values = {int(m) for m in _CONTRACT_RE.findall(haystack)}
    if len(schema_values) == 1:
        advertised = schema_values.pop()
    stores: Optional[frozenset] = None
    store_sets = {
        frozenset(int(p) for p in m.split("_")) for m in _STORES_RE.findall(haystack)
    }
    if len(store_sets) == 1:
        stores = store_sets.pop()
    return LauncherCapabilityObservation(
        subcommand_marker_present=ATTEST_CAPABILITY_MARKER in haystack,
        advertised_schema_version=advertised,
        advertised_store_versions=stores,
    )


def decide_launcher_capability(
    observation: LauncherCapabilityObservation,
    *,
    required_schema_version: int,
) -> LauncherCapabilityVerdict:
    """Decide whether a probed launcher is attestation-schema compatible (pure).

    Fail-closed precedence:

    1. subcommand marker absent -> :data:`LAUNCHER_SUBCOMMAND_ABSENT` (no wrapper
       subcommand at all — the #13748 class, reported first);
    2. no advertised schema -> :data:`LAUNCHER_SCHEMA_CONTRACT_ABSENT` (a pre-#13847
       launcher whose attestation store schema cannot be proven; unprovable fails closed);
    3. advertised schema != the required source schema ->
       :data:`LAUNCHER_SCHEMA_VERSION_MISMATCH` (the shared store's write guard is an
       exact-version match, so newer and older are both incompatible);
    4. otherwise :data:`LAUNCHER_CAPABILITY_OK`.
    """
    required = int(required_schema_version)
    if not observation.subcommand_marker_present:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_SUBCOMMAND_ABSENT,
            "the launcher's `herdr agent-attest --help` did not carry the wrapper "
            f"subcommand marker {ATTEST_CAPABILITY_MARKER!r}; it does not provide the "
            "managed-launch self-attestation wrapper at all",
        )
    advertised = observation.advertised_schema_version
    if advertised is None:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_SCHEMA_CONTRACT_ABSENT,
            "the launcher carries the `herdr agent-attest` subcommand but advertises no "
            f"attestation-schema capability contract; this build requires attestation "
            f"schema v{required}, and a launcher that cannot prove its store schema would "
            "write attestations the source runtime rejects — the pair would boot live "
            "but unattested",
        )
    if advertised != required:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_SCHEMA_VERSION_MISMATCH,
            f"the launcher advertises attestation schema v{advertised} but this runtime "
            f"requires exactly v{required}; the shared attestation store's write guard is "
            "an exact-version match, so its self-attestations would be rejected and the "
            "pair would boot live but unattested",
        )
    return LauncherCapabilityVerdict(
        True,
        LAUNCHER_CAPABILITY_OK,
        f"launcher carries the agent-attest wrapper and advertises the required "
        f"attestation schema v{required}",
    )


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
#: A replacement launch was requested against a store whose shape has no
#: ``replacement_action_id`` column.
STORE_REPLACEMENT_UNSUPPORTED = "attestation_store_replacement_unsupported"

#: The store shape that first carried ``replacement_action_id`` (#13806). A replacement
#: launch cannot be attested by anything older.
_REPLACEMENT_MIN_STORE_VERSION = 2

_MIGRATE_HINT = "`mozyo-bridge herdr attestation-store migrate --write`"


def decide_store_compatibility(
    observation: LauncherCapabilityObservation,
    store: StoreSchemaObservation,
    *,
    required_schema_version: int,
    replacement_launch: bool,
) -> LauncherCapabilityVerdict:
    """Join the launcher's advertised capability with the SELECTED store's real shape.

    The check #13882 adds. The #13847 decision compares the launcher's advertised schema
    to the source runtime's required schema — both code — so two v2 runtimes agree while
    the shared home on disk holds v1, the probe passes, and the pair boots live but
    unattested. This one opens the store that will actually be written.

    Fail-closed precedence:

    1. store unreadable -> :data:`STORE_UNREADABLE` (nothing about it is knowable);
    2. store shape unrecognized -> :data:`STORE_UNSUPPORTED` (upgrade vs corrupt named
       honestly from ``store.upgrade_required``);
    3. an absent store is fine — the first write creates it at the required version;
    4. replacement launch onto a pre-``replacement_action_id`` shape ->
       :data:`STORE_REPLACEMENT_UNSUPPORTED` (the field cannot be dropped);
    5. the probed launcher cannot prove it writes this shape ->
       :data:`STORE_LAUNCHER_CANNOT_WRITE`;
    6. otherwise :data:`STORE_JOIN_OK` — including the v1-store / normal-launch case,
       which acceptance 2 admits via the proven backward-compatible write path.
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
    if version not in observation.writable_store_versions:
        provable = (
            "advertises no writable-store set, so it is credited only with its native "
            f"schema v{observation.advertised_schema_version}"
            if observation.advertised_store_versions is None
            else "advertises writable store shapes "
            f"{sorted(observation.writable_store_versions)}"
        )
        return LauncherCapabilityVerdict(
            False,
            STORE_LAUNCHER_CANNOT_WRITE,
            f"the selected attestation store is v{version}, but the launcher {provable} "
            f"— it cannot be proven to write this store's shape. Its self-attestation "
            f"would be dropped and the pair would boot live but unattested. Either use a "
            f"launcher that writes v{version}, or migrate the store: {_MIGRATE_HINT}",
        )
    return LauncherCapabilityVerdict(
        True,
        STORE_JOIN_OK,
        f"the selected attestation store is v{version} and the launcher can write that "
        f"shape",
    )


__all__ = (
    "ATTEST_CAPABILITY_CONTRACT_PREFIX",
    "ATTEST_CAPABILITY_STORES_PREFIX",
    "LAUNCHER_CAPABILITY_OK",
    "LAUNCHER_SUBCOMMAND_ABSENT",
    "LAUNCHER_SCHEMA_CONTRACT_ABSENT",
    "LAUNCHER_SCHEMA_VERSION_MISMATCH",
    "STORE_JOIN_OK",
    "STORE_LAUNCHER_CANNOT_WRITE",
    "STORE_REPLACEMENT_UNSUPPORTED",
    "STORE_UNREADABLE",
    "STORE_UNSUPPORTED",
    "LauncherCapabilityObservation",
    "LauncherCapabilityVerdict",
    "build_attest_capability_contract_line",
    "build_attest_capability_stores_line",
    "parse_launcher_capability_output",
    "decide_launcher_capability",
    "decide_store_compatibility",
)
