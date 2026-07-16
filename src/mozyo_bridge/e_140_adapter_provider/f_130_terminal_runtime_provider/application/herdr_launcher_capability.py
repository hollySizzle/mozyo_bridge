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

Pure: no I/O, no subprocess, no store access. It imports only the sibling capability
marker constant, so the dependency stays within the terminal-runtime application layer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

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
#: stray digit elsewhere in the help can never be misread as the advertised schema.
_CONTRACT_RE = re.compile(re.escape(ATTEST_CAPABILITY_CONTRACT_PREFIX) + r"(\d+)")

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
    """

    subcommand_marker_present: bool
    advertised_schema_version: Optional[int]


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


def parse_launcher_capability_output(text: str) -> LauncherCapabilityObservation:
    """Parse a launcher's capability probe output into observed facts (pure).

    Reads the combined stdout+stderr of ``<launcher> herdr agent-attest --help``. The
    subcommand marker and the advertised schema are looked up independently: a launcher
    can carry the subcommand yet advertise no schema (the v1 installed launcher), which
    the decision fails closed. A malformed / absent contract token leaves the advertised
    schema ``None`` (unprovable → fail closed), never a guessed default.
    """
    haystack = text or ""
    match = _CONTRACT_RE.search(haystack)
    advertised: Optional[int] = int(match.group(1)) if match else None
    return LauncherCapabilityObservation(
        subcommand_marker_present=ATTEST_CAPABILITY_MARKER in haystack,
        advertised_schema_version=advertised,
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


__all__ = (
    "ATTEST_CAPABILITY_CONTRACT_PREFIX",
    "LAUNCHER_CAPABILITY_OK",
    "LAUNCHER_SUBCOMMAND_ABSENT",
    "LAUNCHER_SCHEMA_CONTRACT_ABSENT",
    "LAUNCHER_SCHEMA_VERSION_MISMATCH",
    "LauncherCapabilityObservation",
    "LauncherCapabilityVerdict",
    "build_attest_capability_contract_line",
    "parse_launcher_capability_output",
    "decide_launcher_capability",
)
