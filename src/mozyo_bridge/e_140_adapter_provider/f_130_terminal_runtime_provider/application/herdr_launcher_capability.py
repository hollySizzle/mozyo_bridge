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

from mozyo_bridge.e_140_adapter_provider.f_130_terminal_runtime_provider.application.herdr_launch_epoch import (  # noqa: E501
    MIGRATE_HINT as _MIGRATE_HINT,
    epoch_store_admission,
)
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

#: The stable prefix advertising the launcher's **generation-protocol** wire version
#: (Redmine #14203 review j#87479 F1). The #13847 tokens above prove the wrapper writes an
#: attestation of a given schema; they say NOTHING about whether it emits the
#: ``attestation_write_succeeded`` startup execution event the parent's launch-generation
#: finalize requires. A launcher carrying ``agent-attest`` + a matching attestation schema
#: but predating that event would pass the attestation preflight, let the parent reserve a
#: pending generation, actuate, and only then be discovered un-finalizable — so the
#: generation is advertised and decided as its OWN capability, independent of the store
#: schema. Whitespace-/hyphen-free for the same wrap-proof reason as its siblings.
GENERATION_PROTOCOL_CAPABILITY_PREFIX = "mozyo_generation_protocol_capability="

#: Matches the advertised generation-protocol version, bounded on both sides so only a
#: whole canonical token is credited (review j#80000 finding 3 discipline: a malformed
#: advertisement is unprovable and must not be salvaged into a capability).
_GENERATION_RE = re.compile(
    r"(?:^|\s)" + re.escape(GENERATION_PROTOCOL_CAPABILITY_PREFIX) + r"(\d+)(?=\s|$)"
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
# failed. They are verified by DIFFERENT means, and the difference is load-bearing:
#
# - the lane lifecycle is a *declaration* join — the launcher advertises the reader schemas it
#   understands, and that set is joined against the store's recorded shape;
# - the config is a *direct measurement* — the launcher's own parser is run against the exact
#   target bytes. Every summary of the grammar was measured insufficient (#14258 j#87752 R4),
#   so what a launcher advertises about config is only that it can be ASKED.
#
# What they share is the property that made them worth adopting: both can be evaluated BEFORE
# the lane worktree exists, which is what lets `sublane create` refuse without creating one —
# unlike the incidental exit-code discriminant #14231 relies on.

#: The version of the read-only config-parse contract this launcher provides — i.e. that it
#: registers ``config check-parse --file <path>`` and answers with the documented exit codes
#: (Redmine #14258, review j#87752 R4).
#:
#: This token deliberately replaced an earlier pair that advertised the *supported config
#: versions* and the *recognized top-level keys*. Both were summaries of the grammar, and a
#: summary cannot answer the question: commit ``d28e59e2`` added the nested
#: ``lane_placement.by_lane_kind`` key without changing either, so a launcher predating it
#: advertised an identical contract and still rejected the config (measured against real
#: pre-``d28e59e2`` source). Rather than enumerate a third summary, the contract now says
#: only "I can be ASKED", and the answer comes from the launcher's own parser running on the
#: exact target bytes — which covers version, top-level, nested, and any axis added later.
ATTEST_CAPABILITY_CONFIG_PARSE_PREFIX = "mozyo_attest_capability_config_parse="

#: The config-parse contract version this build both advertises and requires. Bump only if
#: the probe's argv or exit-code meaning changes (never for a config *grammar* change — the
#: whole point is that grammar changes need no token bump).
CONFIG_PARSE_CONTRACT_VERSION = 1

#: The lane lifecycle component schema versions this launcher's READER understands (its
#: ``_RECOGNIZED_SCHEMA_VERSIONS``). Read capability, not write: the launch never migrates
#: the shared authority, so what matters is whether the launcher can read the shape that is
#: already there.
ATTEST_CAPABILITY_LIFECYCLE_PREFIX = "mozyo_attest_capability_lifecycle="

_CONFIG_PARSE_RE = re.compile(
    r"(?:^|\s)"
    + re.escape(ATTEST_CAPABILITY_CONFIG_PARSE_PREFIX)
    + r"(\d+)(?=\s|$)"
)

_LIFECYCLE_RE = re.compile(
    r"(?:^|\s)"
    + re.escape(ATTEST_CAPABILITY_LIFECYCLE_PREFIX)
    + r"(\d+(?:_\d+)*)(?=\s|$)"
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
    ``advertised_config_parse_contract`` — the version of the read-only config-parse
    contract the launcher provides (so the preflight can ASK it to parse a document rather
    than summarize its grammar), and ``advertised_lifecycle_versions`` — the shared lane
    lifecycle component schemas its READER understands (both Redmine #14258); each is
    ``None`` on a build predating that token, which is unprovable and therefore fails closed.
    ``advertised_generation_protocol_version`` — the launch-generation wire protocol the
    launcher declares (Redmine #14203), decided independently of every token above because
    writing an attestation of a given schema does not imply emitting the
    ``attestation_write_succeeded`` startup event the parent's finalize joins on.
    """

    subcommand_marker_present: bool
    advertised_schema_version: Optional[int]
    advertised_store_versions: Optional[frozenset] = None
    advertised_config_parse_contract: Optional[int] = None
    advertised_lifecycle_versions: Optional[frozenset] = None
    #: The generation-protocol wire version the launcher declares (Redmine #14203 F1), or
    #: ``None`` when it advertises none (a build predating the launch-generation event
    #: protocol). Independent of the attestation schema above.
    advertised_generation_protocol_version: Optional[int] = None

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


def build_attest_capability_config_parse_line(contract_version: int) -> str:
    """The config-parse contract token the source advertises (pure, Redmine #14258 R4)."""
    return f"{ATTEST_CAPABILITY_CONFIG_PARSE_PREFIX}{int(contract_version)}"


def build_attest_capability_lifecycle_line(lifecycle_versions) -> str:
    """The readable-lane-lifecycle-version token the source advertises (pure, #14258)."""
    return _version_set_token(ATTEST_CAPABILITY_LIFECYCLE_PREFIX, lifecycle_versions)


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
    from mozyo_bridge.core.state.herdr_launch_generation import (
        HERDR_LAUNCH_GENERATION_PROTOCOL_VERSION,
    )
    from mozyo_bridge.core.state.lane_lifecycle_schema import (
        readable_lane_lifecycle_versions,
    )

    return (
        "capability contract (Redmine #13847):\n"
        + build_attest_capability_contract_line(
            HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
        )
        + "\nwritable attestation store shapes (Redmine #13882):\n"
        + build_attest_capability_stores_line(RECOGNIZED_SCHEMA_VERSIONS)
        + "\nrepo-local config parse contract (Redmine #14258):\n"
        + build_attest_capability_config_parse_line(CONFIG_PARSE_CONTRACT_VERSION)
        + "\nreadable shared lane lifecycle schema (Redmine #14258):\n"
        + build_attest_capability_lifecycle_line(readable_lane_lifecycle_versions())
        + "\ngeneration protocol (Redmine #14203):\n"
        + build_generation_protocol_capability_line(
            HERDR_LAUNCH_GENERATION_PROTOCOL_VERSION
        )
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


def build_generation_protocol_capability_line(protocol_version: int) -> str:
    """The generation-protocol capability token the source ``agent-attest --help``
    advertises (pure, Redmine #14203 F1).

    Built from the generation-protocol version constant at the call site so the advertised
    number can never drift from the protocol the wrapper actually implements. Whitespace-free
    so ``--help`` wrapping cannot split it.
    """
    return f"{GENERATION_PROTOCOL_CAPABILITY_PREFIX}{int(protocol_version)}"


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
    parse_contracts = {int(m) for m in _CONFIG_PARSE_RE.findall(haystack)}
    generation_values = {int(m) for m in _GENERATION_RE.findall(haystack)}
    return LauncherCapabilityObservation(
        subcommand_marker_present=ATTEST_CAPABILITY_MARKER in haystack,
        advertised_schema_version=advertised,
        advertised_store_versions=_parse_version_set(haystack, _STORES_RE),
        advertised_config_parse_contract=(
            parse_contracts.pop() if len(parse_contracts) == 1 else None
        ),
        advertised_lifecycle_versions=_parse_version_set(haystack, _LIFECYCLE_RE),
        advertised_generation_protocol_version=(
            generation_values.pop() if len(generation_values) == 1 else None
        ),
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


# --- Generation-protocol verdict vocabulary (Redmine #14203 F1; fail-closed). ---------
#: The launcher advertises exactly the required generation-protocol version.
GENERATION_PROTOCOL_OK = "generation_protocol_ok"
#: The launcher advertises NO generation-protocol contract — it predates the launch-
#: generation event protocol (it emits no ``attestation_write_succeeded`` event). Unprovable
#: compatibility fails closed BEFORE the generation is reserved.
GENERATION_PROTOCOL_CONTRACT_ABSENT = "generation_protocol_contract_absent"
#: The launcher advertises a generation-protocol version that is not the exact one this
#: runtime finalizes against — an incompatible wire protocol, refused fail-closed.
GENERATION_PROTOCOL_VERSION_MISMATCH = "generation_protocol_version_mismatch"


def decide_generation_protocol_capability(
    observation: LauncherCapabilityObservation,
    *,
    required_version: int,
) -> LauncherCapabilityVerdict:
    """Decide whether a probed launcher speaks this runtime's generation protocol (pure).

    Independent of the attestation schema decision (Redmine #14203 review j#87479 F1): a
    launcher can carry ``agent-attest`` and a matching attestation schema yet predate the
    ``attestation_write_succeeded`` startup execution event the parent's launch-generation
    finalize requires. Without this check that skew is invisible until AFTER the first Herdr
    side effect — the generation is reserved, the pair actuates, and the finalize then never
    lands, stranding the row ``pending``. Deciding it here lets the caller refuse before any
    actuation.

    Fail-closed precedence:

    1. no advertised generation-protocol contract -> :data:`GENERATION_PROTOCOL_CONTRACT_ABSENT`
       (a build predating the event protocol; unprovable fails closed);
    2. advertised version != the required version -> :data:`GENERATION_PROTOCOL_VERSION_MISMATCH`
       (an incompatible wire protocol; only the current marker admits);
    3. otherwise :data:`GENERATION_PROTOCOL_OK`.

    The subcommand-marker absence is left to :func:`decide_launcher_capability` (reported
    first there); a caller runs both decisions on the same observation.
    """
    required = int(required_version)
    advertised = observation.advertised_generation_protocol_version
    if advertised is None:
        return LauncherCapabilityVerdict(
            False,
            GENERATION_PROTOCOL_CONTRACT_ABSENT,
            "the launcher carries the `herdr agent-attest` subcommand but advertises no "
            "generation-protocol capability contract; this runtime requires generation "
            f"protocol v{required} (the wrapper must emit the launch-generation "
            "`attestation_write_succeeded` event), and a launcher that cannot prove it "
            "would let a generation be reserved and the pair actuate before the finalize "
            "is discovered impossible — stranding the generation and blocking recovery",
        )
    if advertised != required:
        return LauncherCapabilityVerdict(
            False,
            GENERATION_PROTOCOL_VERSION_MISMATCH,
            f"the launcher advertises generation protocol v{advertised} but this runtime "
            f"finalizes against exactly v{required}; an incompatible generation protocol "
            "would strand every reserved generation, so the launch is refused before any "
            "side effect",
        )
    return LauncherCapabilityVerdict(
        True,
        GENERATION_PROTOCOL_OK,
        f"launcher advertises the required generation protocol v{required}",
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


def decide_store_compatibility(
    observation: LauncherCapabilityObservation,
    store: StoreSchemaObservation,
    *,
    required_schema_version: int,
    replacement_launch: bool,
    epoch_launch: bool = False,
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
    4b. epoch-bearing launch onto a pre-``lane_epoch`` shape ->
       ``STORE_EPOCH_UNSUPPORTED`` (Redmine #14756; the same "field that cannot be
       dropped" shape, on the axis the resume generation proof depends on);
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
    epoch_refusal = epoch_store_admission(
        epoch_launch=epoch_launch, store_version=version, migrate_hint=_MIGRATE_HINT
    )
    if epoch_refusal is not None:
        return LauncherCapabilityVerdict(False, *epoch_refusal)
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
#: The target config does not parse under THIS runtime either. Not a launcher problem — the
#: config itself is broken — and reported as such so an operator is never told to upgrade a
#: launcher over their own malformed config (review j#87752's explicit requirement).
TARGET_CONFIG_INVALID = "target_config_invalid"
#: The launcher advertises no read-only config-parse contract (any pre-#14258 build), so it
#: cannot be asked whether it can read the target. Unprovable fails closed.
LAUNCHER_CONFIG_VALIDATOR_ABSENT = "launcher_config_validator_absent"
#: The bytes the lane will actually receive could not be established, so no parser — this
#: runtime's or the launcher's — could be asked the real question. Distinct from
#: :data:`TARGET_CONFIG_INVALID` because the config may be perfectly valid: what is unknown is
#: whether a checkout transforms it (consultation j#87807 — collapsing the two told the
#: operator their config was broken and that changing the launcher would not help, when
#: neither was true and the real action was neither).
TARGET_CONFIG_UNVERIFIABLE = "target_config_unverifiable"
#: The launcher's own parser REJECTED the exact target bytes — the direct measurement.
LAUNCHER_CANNOT_PARSE_TARGET_CONFIG = "launcher_cannot_parse_target_config"
#: The launcher advertises the contract but its validator could not be run / answered with
#: an exit code outside the contract. Unknowable, so fail closed.
LAUNCHER_CONFIG_VALIDATOR_UNUSABLE = "launcher_config_validator_unusable"

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

#: The operator-facing recovery for every launcher-side refusal. Both supported actions are
#: named in full (review j#87746 R3: the earlier wording trailed off after "that does", which
#: is not an actionable instruction), and the sentence is terminated so a caller can append
#: nothing and still emit a complete message.
_LAUNCHER_HINT = (
    "Recovery: either install / release a mozyo-bridge whose CLI advertises the required "
    "capability, or set `MOZYO_BRIDGE_LAUNCHER` to the absolute path of a launcher built "
    "from a source tree that advertises it."
)


#: The target repo declares no config at all — nothing for any launcher to parse.
CONFIG_PARSE_TARGET_ABSENT = "config_parse_target_absent"
#: Both this runtime and the launcher parsed the exact target bytes.
CONFIG_PARSE_BOTH_OK = "config_parse_both_ok"
#: This runtime itself rejected the target bytes.
CONFIG_PARSE_SELF_REJECTED = "config_parse_self_rejected"
#: This runtime parsed them; the launcher's own parser rejected them.
CONFIG_PARSE_LAUNCHER_REJECTED = "config_parse_launcher_rejected"
#: The launcher's validator could not be run, or answered outside the contract.
CONFIG_PARSE_LAUNCHER_UNUSABLE = "config_parse_launcher_unusable"
#: The document the lane will receive could not be established at all — not because it is
#: broken, but because a checkout may transform it (Redmine #14258, consultation j#87807).
CONFIG_PARSE_TARGET_UNVERIFIABLE = "config_parse_target_unverifiable"


@dataclass(frozen=True)
class ConfigParseObservation:
    """The measured outcome of making BOTH parsers read the exact target bytes (#14258 R4).

    Deliberately not a summary of the grammar. The earlier design advertised the supported
    config versions and the recognized top-level keys and joined those — a *proxy*, and a
    measured-insufficient one: commit ``d28e59e2`` added the nested
    ``lane_placement.by_lane_kind`` key without changing either, so a launcher predating it
    advertised an identical contract and still rejected the config. What a preflight needs is
    the answer, not a description of the grammar that might produce it, so the probe hands
    the same bytes to this runtime's loader and to the candidate launcher's own
    ``config check-parse`` and records what each said.

    ``launcher_detail`` carries a bounded excerpt of the launcher's own error so a refusal
    names the real reason (``unknown key 'by_lane_kind'``) instead of a generic mismatch.
    """

    state: str
    launcher_detail: str = ""


def decide_config_parse_compatibility(
    observation: LauncherCapabilityObservation,
    config: ConfigParseObservation,
    *,
    required_contract_version: int,
) -> LauncherCapabilityVerdict:
    """Can the probed launcher parse the TARGET repo's config? (pure, #14258 R4).

    The verdict is a product of two *measurements*, not of any advertised summary:

    1. no config at all -> :data:`CONFIG_JOIN_OK`. Nothing to parse; a config-less repo is
       exactly the case that worked before this check existed;
    2. THIS runtime rejected the bytes -> :data:`TARGET_CONFIG_INVALID`. The config is broken
       on its own terms, so the refusal must say that rather than blame the launcher — the
       distinction review j#87752 required be preserved;
    3. the launcher advertises no parse contract -> :data:`LAUNCHER_CONFIG_VALIDATOR_ABSENT`
       (any pre-#14258 build: it cannot be asked, so it cannot be proven);
    4. it advertises a contract this build does not speak ->
       :data:`LAUNCHER_CONFIG_VALIDATOR_ABSENT` as well (the probe's argv / exit-code meaning
       is what the version pins, so a different contract is not a contract we can invoke);
    5. its validator could not be run / answered outside the contract ->
       :data:`LAUNCHER_CONFIG_VALIDATOR_UNUSABLE`;
    6. its parser rejected the bytes -> :data:`LAUNCHER_CANNOT_PARSE_TARGET_CONFIG`, quoting
       the launcher's own error;
    7. both parsed -> :data:`CONFIG_JOIN_OK`.
    """
    required = int(required_contract_version)
    if config.state == CONFIG_PARSE_TARGET_ABSENT:
        return LauncherCapabilityVerdict(
            True,
            CONFIG_JOIN_OK,
            "the target repo declares no repo-local config, so the launcher parses none",
        )
    if config.state == CONFIG_PARSE_TARGET_UNVERIFIABLE:
        return LauncherCapabilityVerdict(
            False,
            TARGET_CONFIG_UNVERIFIABLE,
            f"the repo-local config the lane would receive could not be established, so "
            f"nothing was created rather than verifying a document the lane may never see: "
            f"{config.launcher_detail or 'the materialized bytes are not knowable here'}",
        )
    if config.state == CONFIG_PARSE_SELF_REJECTED:
        return LauncherCapabilityVerdict(
            False,
            TARGET_CONFIG_INVALID,
            f"the target repo's `.mozyo-bridge/config.yaml` does not parse under THIS "
            f"runtime either, so this is a config defect rather than a launcher skew: "
            f"{config.launcher_detail or 'see the config error above'}. Fix the config; "
            f"changing the launcher will not help",
        )
    advertised = observation.advertised_config_parse_contract
    if advertised is None or advertised != required:
        said = (
            "advertises no read-only config-parse contract"
            if advertised is None
            else f"advertises config-parse contract v{advertised}, not the v{required} "
            f"this runtime invokes"
        )
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CONFIG_VALIDATOR_ABSENT,
            f"the launcher {said}, so it cannot be asked whether it can read the target "
            f"repo's config. A launcher that rejects that config exits before `exec`ing the "
            f"provider, leaving a partial / immediately-vanishing lane. {_LAUNCHER_HINT}",
        )
    if config.state == CONFIG_PARSE_LAUNCHER_UNUSABLE:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CONFIG_VALIDATOR_UNUSABLE,
            f"the launcher advertises the config-parse contract but its validator could not "
            f"be run, or answered outside the contract: "
            f"{config.launcher_detail or 'no usable answer'}. An unanswerable probe proves "
            f"nothing, so the launch fails closed. {_LAUNCHER_HINT}",
        )
    if config.state == CONFIG_PARSE_LAUNCHER_REJECTED:
        return LauncherCapabilityVerdict(
            False,
            LAUNCHER_CANNOT_PARSE_TARGET_CONFIG,
            f"this runtime parses the target repo's config, but the selected launcher's own "
            f"parser rejects it: {config.launcher_detail or 'no detail reported'}. It would "
            f"exit before the provider starts. {_LAUNCHER_HINT}",
        )
    return LauncherCapabilityVerdict(
        True,
        CONFIG_JOIN_OK,
        "the selected launcher parsed the target repo's exact config with its own grammar",
    )


@dataclass(frozen=True)
class TargetSchemaObservation:
    """The schema shape of an authority the LAUNCHER must read (Redmine #14258).

    Used by the lane lifecycle join only. It once served the config axis too, back when that
    axis compared advertised summaries; the config axis now measures the exact bytes directly
    (#14258 j#87752 R4), so this type describes a *declared* authority shape and nothing else.

    ``state`` is one of :data:`TARGET_SCHEMA_ABSENT` / :data:`TARGET_SCHEMA_DECLARED` /
    :data:`TARGET_SCHEMA_UNREADABLE` / :data:`TARGET_SCHEMA_UNSUPPORTED`. ``version`` is the
    recorded schema version when one could be read; ``upgrade_required`` distinguishes "the
    authority is newer than THIS runtime" from "the authority is corrupt", so a refusal names
    the operator's real next action instead of dishonestly suggesting an upgrade.
    """

    state: str
    version: Optional[int] = None
    upgrade_required: bool = False


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
    "ATTEST_CAPABILITY_CONFIG_PARSE_PREFIX",
    "ATTEST_CAPABILITY_CONTRACT_PREFIX",
    "ATTEST_CAPABILITY_LIFECYCLE_PREFIX",
    "ATTEST_CAPABILITY_STORES_PREFIX",
    "CONFIG_JOIN_OK",
    "CONFIG_PARSE_BOTH_OK",
    "CONFIG_PARSE_CONTRACT_VERSION",
    "CONFIG_PARSE_LAUNCHER_REJECTED",
    "CONFIG_PARSE_LAUNCHER_UNUSABLE",
    "CONFIG_PARSE_SELF_REJECTED",
    "CONFIG_PARSE_TARGET_UNVERIFIABLE",
    "CONFIG_PARSE_TARGET_ABSENT",
    "ConfigParseObservation",
    "LAUNCHER_CANNOT_PARSE_TARGET_CONFIG",
    "LAUNCHER_CANNOT_READ_LIFECYCLE",
    "LAUNCHER_CONFIG_VALIDATOR_ABSENT",
    "LAUNCHER_CONFIG_VALIDATOR_UNUSABLE",
    "LAUNCHER_LIFECYCLE_CONTRACT_ABSENT",
    "LIFECYCLE_JOIN_OK",
    "LIFECYCLE_UNREADABLE",
    "LIFECYCLE_UNSUPPORTED",
    "TARGET_CONFIG_INVALID",
    "TARGET_CONFIG_UNVERIFIABLE",
    "TARGET_SCHEMA_ABSENT",
    "TARGET_SCHEMA_DECLARED",
    "TARGET_SCHEMA_UNREADABLE",
    "TARGET_SCHEMA_UNSUPPORTED",
    "TargetSchemaObservation",
    "build_attest_capability_config_parse_line",
    "build_attest_capability_epilog",
    "build_attest_capability_lifecycle_line",
    "decide_config_parse_compatibility",
    "decide_lifecycle_reader_compatibility",
    "GENERATION_PROTOCOL_CAPABILITY_PREFIX",
    "GENERATION_PROTOCOL_OK",
    "GENERATION_PROTOCOL_CONTRACT_ABSENT",
    "GENERATION_PROTOCOL_VERSION_MISMATCH",
    "LAUNCHER_CAPABILITY_OK",
    "LAUNCHER_SUBCOMMAND_ABSENT",
    "LAUNCHER_SCHEMA_CONTRACT_ABSENT",
    "LAUNCHER_SCHEMA_VERSION_MISMATCH",
    "build_generation_protocol_capability_line",
    "decide_generation_protocol_capability",
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
