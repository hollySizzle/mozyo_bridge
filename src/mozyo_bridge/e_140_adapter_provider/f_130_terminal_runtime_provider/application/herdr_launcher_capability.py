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

# --- Target-scoped read capability (Redmine #14258). -----------------------------------
# The three tokens above are all about ONE authority: the attestation store the wrapper
# writes. The launcher must also *read* two authorities it did not write, and neither is
# covered by them:
#
# 1. the TARGET REPO's `.mozyo-bridge/config.yaml` — the wrapper starts with
#    `--cwd <lane worktree>` and a mozyo-bridge CLI parses that config at startup, so a
#    launcher predating a config schema bump exits before `exec`ing the provider (measured:
#    `unknown key 'agents'` / exit 2 against the v2 config, #14258 j#85834);
# 2. the HOME-SCOPED SHARED lane lifecycle authority — a launcher whose reader predates the
#    shared store's component schema zero-starts with `LaneLifecycleReaderUpgradeRequired`
#    (measured on the v7 store vs a v6 reader, #14258 j#85890).
#
# Both were previously invisible to the preflight, so the pair was created and only then
# failed. They are advertised the same way as the store set — declared, wrap-proof tokens —
# so the join is a *declaration* check rather than the incidental exit-code discriminant
# #14231 relies on. That matters beyond honesty: a declaration can be verified BEFORE the
# lane worktree exists, which is what lets `sublane create` refuse without creating one.

#: The repo-local config record versions this launcher can PARSE (its
#: ``SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS``). Underscore-separated for the same wrap-proof
#: reason as the store set.
ATTEST_CAPABILITY_CONFIG_PREFIX = "mozyo_attest_capability_config="

#: The recognized TOP-LEVEL config keys this launcher accepts (its
#: ``REPO_LOCAL_CONFIG_KEYS``). The version set alone is not sufficient: recognized keys
#: have been added *within* a version (``terminal_transport`` / ``lane_placement`` under
#: v1), and an unknown top-level key is exactly what the measured failure reported. Keys are
#: ``.``-separated: a dot is not a hyphen and not whitespace, so the token stays one
#: unbreakable word like its siblings.
ATTEST_CAPABILITY_CONFIG_KEYS_PREFIX = "mozyo_attest_capability_config_keys="

#: The lane lifecycle component schema versions this launcher's READER understands (its
#: ``_RECOGNIZED_SCHEMA_VERSIONS``). Read capability, not write: the launch never migrates
#: the shared authority, so what matters is whether the launcher can read the shape that is
#: already there.
ATTEST_CAPABILITY_LIFECYCLE_PREFIX = "mozyo_attest_capability_lifecycle="

_CONFIG_RE = re.compile(
    r"(?:^|\s)" + re.escape(ATTEST_CAPABILITY_CONFIG_PREFIX) + r"(\d+(?:_\d+)*)(?=\s|$)"
)

#: The key-set grammar: dot-separated lowercase identifiers, bounded on both sides. Bounded
#: for the reason finding 3 gave the sibling tokens — a malformed advertisement is
#: *unprovable*, and crediting its salvageable prefix would admit a launcher that never
#: claimed the keys.
_CONFIG_KEYS_RE = re.compile(
    r"(?:^|\s)"
    + re.escape(ATTEST_CAPABILITY_CONFIG_KEYS_PREFIX)
    + r"([a-z0-9_]+(?:\.[a-z0-9_]+)*)(?=\s|$)"
)

_LIFECYCLE_RE = re.compile(
    r"(?:^|\s)"
    + re.escape(ATTEST_CAPABILITY_LIFECYCLE_PREFIX)
    + r"(\d+(?:_\d+)*)(?=\s|$)"
)

#: The grammar a single advertised config key must satisfy to be renderable in the
#: dot-separated key token (see :func:`build_attest_capability_config_keys_line`).
_CONFIG_KEY_TOKEN_RE = re.compile(r"[a-z0-9_]+")

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
    ``advertised_config_versions`` / ``advertised_config_keys`` — the repo-local config
    record versions and top-level keys the launcher declares it can PARSE, and
    ``advertised_lifecycle_versions`` — the shared lane lifecycle component schemas its
    READER understands (all Redmine #14258); each is ``None`` on a build predating that
    token, which is unprovable and therefore fails closed.
    """

    subcommand_marker_present: bool
    advertised_schema_version: Optional[int]
    advertised_store_versions: Optional[frozenset] = None
    advertised_config_versions: Optional[frozenset] = None
    advertised_config_keys: Optional[frozenset] = None
    advertised_lifecycle_versions: Optional[frozenset] = None

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
    return _version_set_token(ATTEST_CAPABILITY_STORES_PREFIX, store_versions)


def build_attest_capability_config_line(config_versions) -> str:
    """The parsable-config-version token the source advertises (pure, Redmine #14258)."""
    return _version_set_token(ATTEST_CAPABILITY_CONFIG_PREFIX, config_versions)


def build_attest_capability_lifecycle_line(lifecycle_versions) -> str:
    """The readable-lane-lifecycle-version token the source advertises (pure, #14258)."""
    return _version_set_token(ATTEST_CAPABILITY_LIFECYCLE_PREFIX, lifecycle_versions)


def build_attest_capability_config_keys_line(config_keys) -> str:
    """The recognized-top-level-config-key token the source advertises (pure, #14258).

    Built from the config module's closed key set at the call site so it cannot drift from
    the keys the parser actually accepts. Dot-separated and sorted for a stable, wrap-proof
    rendering; a key outside the token's grammar (anything but lowercase / digits /
    underscore) is a producer error rather than a silently truncated advertisement.
    """
    keys = sorted(str(key) for key in config_keys)
    for key in keys:
        if not _CONFIG_KEY_TOKEN_RE.fullmatch(key):
            raise ValueError(
                f"config key {key!r} cannot be advertised in the capability contract "
                f"(the token grammar is lowercase letters / digits / underscore)"
            )
    return f"{ATTEST_CAPABILITY_CONFIG_KEYS_PREFIX}{'.'.join(keys)}"


def build_attest_capability_epilog() -> str:
    """The complete ``herdr agent-attest --help`` capability epilog (Redmine #14258).

    The single canonical composer of every advertised capability token, so the CLI parser
    and any harness that must answer the probe render the *same* contract. Adding a token
    used to mean editing every producer, and a producer left behind silently advertised an
    incomplete contract — which fails closed at some *other* call site, looking like a
    launcher defect. Each token is built from the constant of the authority it describes, so
    a schema bump anywhere re-renders here with no edit.

    Imported lazily: the epilog is composed when a CLI parser is built, and this pure module
    must not pull the config / lifecycle schema graphs in at import time.
    """
    from mozyo_bridge.core.state.herdr_identity_attestation import (
        HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
        RECOGNIZED_SCHEMA_VERSIONS,
    )
    from mozyo_bridge.core.state.lane_lifecycle_schema import (
        readable_lane_lifecycle_versions,
    )
    from mozyo_bridge.e_130_governance_distribution.f_140_rules_docs_catalog.domain.repo_local_config import (  # noqa: E501
        REPO_LOCAL_CONFIG_KEYS,
        SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS,
    )

    return (
        "capability contract (Redmine #13847):\n"
        + build_attest_capability_contract_line(
            HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
        )
        + "\nwritable attestation store shapes (Redmine #13882):\n"
        + build_attest_capability_stores_line(RECOGNIZED_SCHEMA_VERSIONS)
        + "\nparsable repo-local config schema (Redmine #14258):\n"
        + build_attest_capability_config_line(SUPPORTED_REPO_LOCAL_CONFIG_VERSIONS)
        + "\n"
        + build_attest_capability_config_keys_line(REPO_LOCAL_CONFIG_KEYS)
        + "\nreadable shared lane lifecycle schema (Redmine #14258):\n"
        + build_attest_capability_lifecycle_line(readable_lane_lifecycle_versions())
    )


def _version_set_token(prefix: str, versions) -> str:
    """Render ``<prefix><v>_<v>…`` — sorted, underscore-separated, wrap-proof (pure)."""
    return f"{prefix}{'_'.join(str(int(v)) for v in sorted(versions))}"


def _parse_version_set(haystack: str, pattern) -> Optional[frozenset]:
    """The one whole-token, conflict-refusing parse every advertised version SET uses.

    Shared by the store / config / lifecycle sets so the hardening review j#80000 finding 3
    established for the first of them cannot drift away from the others: only a **whole
    canonical token** counts, and two *different* advertisements of the same fact are not
    arbitrated — a launcher that said two things has clearly said neither, so the fact stays
    ``None`` and the join fails closed.
    """
    found = {
        frozenset(int(part) for part in match.split("_"))
        for match in pattern.findall(haystack)
    }
    return found.pop() if len(found) == 1 else None


def parse_launcher_capability_output(text: str) -> LauncherCapabilityObservation:
    """Parse a launcher's capability probe output into observed facts (pure).

    Reads the combined stdout+stderr of ``<launcher> herdr agent-attest --help``. Every
    advertised fact is looked up independently: a launcher can carry the subcommand yet
    advertise no schema (the pre-#13847 installed launcher), advertise a schema but no store
    set (a pre-#13882 build), or advertise both but neither the config nor the lane lifecycle
    read capability (any pre-#14258 build). Each unprovable fact stays ``None`` → fail
    closed, never a guessed default.

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
    key_sets = {
        frozenset(match.split(".")) for match in _CONFIG_KEYS_RE.findall(haystack)
    }
    return LauncherCapabilityObservation(
        subcommand_marker_present=ATTEST_CAPABILITY_MARKER in haystack,
        advertised_schema_version=advertised,
        advertised_store_versions=_parse_version_set(haystack, _STORES_RE),
        advertised_config_versions=_parse_version_set(haystack, _CONFIG_RE),
        advertised_config_keys=key_sets.pop() if len(key_sets) == 1 else None,
        advertised_lifecycle_versions=_parse_version_set(haystack, _LIFECYCLE_RE),
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
#: Maintenance holds the store exclusively, so admission fails closed at acquisition
#: (Redmine #13882 j#80190 boundary 1) — before any workspace / tab / agent exists.
STORE_MAINTENANCE_IN_PROGRESS = "attestation_store_maintenance_in_progress"
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


# --- Target-authority join vocabulary (Redmine #14258; fail-closed). ------------------
#: The target repo declares no config at all — there is nothing for the launcher to parse.
TARGET_SCHEMA_ABSENT = "target_schema_absent"
#: The target authority declares a schema this runtime read successfully.
TARGET_SCHEMA_DECLARED = "target_schema_declared"
#: The target authority exists but this runtime could not read its schema at all.
TARGET_SCHEMA_UNREADABLE = "target_schema_unreadable"
#: The target authority declares a schema THIS runtime does not understand.
TARGET_SCHEMA_UNSUPPORTED = "target_schema_unsupported"

#: The target repo's config is parsable by the probed launcher.
CONFIG_JOIN_OK = "target_config_ok"
#: The config file exists but its schema could not be read (malformed / unreadable).
CONFIG_UNREADABLE = "target_config_unreadable"
#: The config declares a record version this runtime itself does not understand.
CONFIG_UNSUPPORTED = "target_config_unsupported"
#: The launcher advertises no config-parse capability (any pre-#14258 build).
LAUNCHER_CONFIG_CONTRACT_ABSENT = "launcher_config_contract_absent"
#: The launcher cannot parse the target config's declared record version.
LAUNCHER_CANNOT_READ_CONFIG_VERSION = "launcher_cannot_read_config_version"
#: The launcher does not recognize a top-level key the target config declares.
LAUNCHER_CANNOT_READ_CONFIG_KEYS = "launcher_cannot_read_config_keys"

#: The shared lane lifecycle authority is readable by the probed launcher.
LIFECYCLE_JOIN_OK = "shared_lane_lifecycle_ok"
#: The shared lifecycle store exists but its schema could not be read.
LIFECYCLE_UNREADABLE = "shared_lane_lifecycle_unreadable"
#: The shared lifecycle store's shape is not one THIS runtime understands.
LIFECYCLE_UNSUPPORTED = "shared_lane_lifecycle_unsupported"
#: The launcher advertises no lane lifecycle read capability (any pre-#14258 build).
LAUNCHER_LIFECYCLE_CONTRACT_ABSENT = "launcher_lane_lifecycle_contract_absent"
#: The launcher's reader cannot read the shared lifecycle store's current shape.
LAUNCHER_CANNOT_READ_LIFECYCLE = "launcher_cannot_read_lane_lifecycle"

_LAUNCHER_HINT = (
    "Either install / release a mozyo-bridge whose CLI carries that capability, or point "
    "an absolute `MOZYO_BRIDGE_LAUNCHER` at a launcher built from a source tree that does"
)


@dataclass(frozen=True)
class TargetSchemaObservation:
    """The schema shape of an authority the LAUNCHER must read (Redmine #14258).

    One value type for both target-scoped authorities — the repo's config record and the
    home-scoped shared lane lifecycle store — because the join is the same shape in both
    cases: what is actually there, versus what the launcher declared it can read.

    ``state`` is one of :data:`TARGET_SCHEMA_ABSENT` / :data:`TARGET_SCHEMA_DECLARED` /
    :data:`TARGET_SCHEMA_UNREADABLE` / :data:`TARGET_SCHEMA_UNSUPPORTED`. ``version`` is the
    declared / recorded schema version when one could be read; ``keys`` carries the config
    record's present top-level keys (config only); ``upgrade_required`` distinguishes "the
    authority is newer than THIS runtime" from "the authority is corrupt", so a refusal names
    the operator's real next action instead of dishonestly suggesting an upgrade.
    """

    state: str
    version: Optional[int] = None
    keys: Optional[frozenset] = None
    upgrade_required: bool = False


def decide_config_schema_compatibility(
    observation: LauncherCapabilityObservation,
    config: TargetSchemaObservation,
) -> LauncherCapabilityVerdict:
    """Can the probed launcher read the TARGET repo's config record? (pure, #14258).

    The gap left by #13748 / #13847 / #13882: all three verify the launcher against the
    *attestation store*, and none of them looks at the config the launcher is about to be
    pointed at. The wrapper runs with ``--cwd <lane worktree>``, a mozyo-bridge CLI parses
    that directory's ``.mozyo-bridge/config.yaml`` at startup, and a launcher that predates a
    schema bump exits there — after the worktree and (before #14258) the pair already existed.

    Fail-closed precedence:

    1. no config at all -> :data:`CONFIG_JOIN_OK`. There is nothing to parse, so a launcher
       that advertises nothing is still admitted: a config-less repo is exactly the case that
       worked before this check existed, and refusing it would break it for no defect;
    2. this runtime could not read the config's schema -> :data:`CONFIG_UNREADABLE`, or read
       a version it does not understand -> :data:`CONFIG_UNSUPPORTED`. Unknowable is not
       compatible;
    3. the launcher advertises no config capability -> :data:`LAUNCHER_CONFIG_CONTRACT_ABSENT`
       (any pre-#14258 build; unprovable fails closed);
    4. the declared record version is outside the launcher's parsable set ->
       :data:`LAUNCHER_CANNOT_READ_CONFIG_VERSION`;
    5. a declared top-level key is outside the launcher's recognized set ->
       :data:`LAUNCHER_CANNOT_READ_CONFIG_KEYS`. The version join alone is not enough:
       recognized keys have been added *within* a version, and an unknown top-level key is
       exactly what the measured failure reported (``unknown key 'agents'``);
    6. otherwise :data:`CONFIG_JOIN_OK`.
    """
    if config.state == TARGET_SCHEMA_ABSENT:
        return LauncherCapabilityVerdict(
            True,
            CONFIG_JOIN_OK,
            "the target repo declares no repo-local config, so the launcher parses none",
        )
    if config.state == TARGET_SCHEMA_UNREADABLE:
        return LauncherCapabilityVerdict(
            False,
            CONFIG_UNREADABLE,
            "the target repo's `.mozyo-bridge/config.yaml` exists but its schema could not "
            "be read (malformed YAML, a non-mapping document, or a non-integer `version`); "
            "an unreadable config is not an absent one, so no launcher can be proven able "
            "to parse it",
        )
    if config.state == TARGET_SCHEMA_UNSUPPORTED:
        return LauncherCapabilityVerdict(
            False,
            CONFIG_UNSUPPORTED,
            f"the target repo's config declares record version {config.version}, which "
            f"THIS runtime does not understand"
            + (
                "; it is newer than this build — use a newer runtime"
                if config.upgrade_required
                else " — the version is not a recognized config schema"
            ),
        )
    if observation.advertised_config_versions is None or (
        observation.advertised_config_keys is None
    ):
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CONFIG_CONTRACT_ABSENT,
            f"the launcher advertises no repo-local config parse capability, so it cannot be "
            f"proven to read the target repo's version {config.version} config; a launcher "
            f"that rejects that config exits before `exec`ing the provider, leaving a "
            f"partial / immediately-vanishing lane. {_LAUNCHER_HINT}",
        )
    version = int(config.version or 0)
    if version not in observation.advertised_config_versions:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CANNOT_READ_CONFIG_VERSION,
            f"the target repo's config declares record version {version}, but the launcher "
            f"advertises parsable config versions "
            f"{sorted(observation.advertised_config_versions)}; it would reject that config "
            f"at startup and exit before the provider starts. {_LAUNCHER_HINT}",
        )
    unknown = sorted((config.keys or frozenset()) - observation.advertised_config_keys)
    if unknown:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CANNOT_READ_CONFIG_KEYS,
            f"the target repo's config declares top-level key(s) {unknown} the launcher "
            f"does not recognize; its config parser fails closed on an unknown key, so it "
            f"would exit before the provider starts. {_LAUNCHER_HINT}",
        )
    return LauncherCapabilityVerdict(
        True,
        CONFIG_JOIN_OK,
        f"the launcher parses config version {version} and recognizes every top-level key "
        f"the target repo declares",
    )


def decide_lifecycle_reader_compatibility(
    observation: LauncherCapabilityObservation,
    lifecycle: TargetSchemaObservation,
) -> LauncherCapabilityVerdict:
    """Can the probed launcher READ the shared lane lifecycle authority? (pure, #14258).

    The second target-scoped authority, and the reason this check cannot be folded into the
    attestation-store join: the lifecycle store is home-scoped and **shared across lanes**,
    it is additively migrated by whichever lane's source CLI is newest, and the launch never
    migrates it. So the question is not "can the launcher write this shape" but "can its
    reader read the shape that is already there" — measured as a v7 store against a v6
    reader, which zero-starts the named lane with ``LaneLifecycleReaderUpgradeRequired``
    (#14258 j#85890).

    Fail-closed precedence mirrors the config join: an absent store is fine (the first write
    creates it at this runtime's version); unreadable / unsupported is unknowable and
    refused; a launcher advertising no read capability is unprovable and refused; a recorded
    version outside the launcher's readable set is :data:`LAUNCHER_CANNOT_READ_LIFECYCLE`.
    """
    if lifecycle.state == TARGET_SCHEMA_ABSENT:
        return LauncherCapabilityVerdict(
            True,
            LIFECYCLE_JOIN_OK,
            "the shared home holds no lane lifecycle authority yet; the first write creates "
            "it at this runtime's schema",
        )
    if lifecycle.state == TARGET_SCHEMA_UNREADABLE:
        return LauncherCapabilityVerdict(
            False,
            LIFECYCLE_UNREADABLE,
            "the shared lane lifecycle authority exists but its schema could not be read "
            "(corrupt, or not a database); an unreadable authority is not an absent one, so "
            "no lane may be launched against it",
        )
    if lifecycle.state == TARGET_SCHEMA_UNSUPPORTED:
        hint = (
            "it is newer than this build; use a newer runtime"
            if lifecycle.upgrade_required
            else "its recorded version and on-disk shape disagree (partial / corrupt / "
            "foreign); repair it before launching a lane"
        )
        return LauncherCapabilityVerdict(
            False,
            LIFECYCLE_UNSUPPORTED,
            f"the shared lane lifecycle authority has a shape THIS runtime does not "
            f"understand — {hint}",
        )
    if observation.advertised_lifecycle_versions is None:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_LIFECYCLE_CONTRACT_ABSENT,
            f"the launcher advertises no lane lifecycle read capability, so it cannot be "
            f"proven to read the shared authority's current schema v{lifecycle.version}; a "
            f"launcher whose reader is older zero-starts the lane with a reader-upgrade "
            f"refusal. {_LAUNCHER_HINT}",
        )
    version = int(lifecycle.version or 0)
    if version not in observation.advertised_lifecycle_versions:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CANNOT_READ_LIFECYCLE,
            f"the shared lane lifecycle authority is at schema v{version}, but the launcher "
            f"advertises readable schemas "
            f"{sorted(observation.advertised_lifecycle_versions)}; its reader would refuse "
            f"the shared authority and the lane could not be resolved. {_LAUNCHER_HINT}",
        )
    return LauncherCapabilityVerdict(
        True,
        LIFECYCLE_JOIN_OK,
        f"the shared lane lifecycle authority is at schema v{version} and the launcher's "
        f"reader understands that shape",
    )


__all__ = (
    "ATTEST_CAPABILITY_CONFIG_KEYS_PREFIX",
    "ATTEST_CAPABILITY_CONFIG_PREFIX",
    "ATTEST_CAPABILITY_CONTRACT_PREFIX",
    "ATTEST_CAPABILITY_LIFECYCLE_PREFIX",
    "ATTEST_CAPABILITY_STORES_PREFIX",
    "CONFIG_JOIN_OK",
    "CONFIG_UNREADABLE",
    "CONFIG_UNSUPPORTED",
    "LAUNCHER_CANNOT_READ_CONFIG_KEYS",
    "LAUNCHER_CANNOT_READ_CONFIG_VERSION",
    "LAUNCHER_CANNOT_READ_LIFECYCLE",
    "LAUNCHER_CONFIG_CONTRACT_ABSENT",
    "LAUNCHER_LIFECYCLE_CONTRACT_ABSENT",
    "LIFECYCLE_JOIN_OK",
    "LIFECYCLE_UNREADABLE",
    "LIFECYCLE_UNSUPPORTED",
    "TARGET_SCHEMA_ABSENT",
    "TARGET_SCHEMA_DECLARED",
    "TARGET_SCHEMA_UNREADABLE",
    "TARGET_SCHEMA_UNSUPPORTED",
    "TargetSchemaObservation",
    "build_attest_capability_config_keys_line",
    "build_attest_capability_config_line",
    "build_attest_capability_epilog",
    "build_attest_capability_lifecycle_line",
    "decide_config_schema_compatibility",
    "decide_lifecycle_reader_compatibility",
    "LAUNCHER_CAPABILITY_OK",
    "LAUNCHER_SUBCOMMAND_ABSENT",
    "LAUNCHER_SCHEMA_CONTRACT_ABSENT",
    "LAUNCHER_SCHEMA_VERSION_MISMATCH",
    "STORE_JOIN_OK",
    "STORE_LAUNCHER_CANNOT_WRITE",
    "STORE_MAINTENANCE_IN_PROGRESS",
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
