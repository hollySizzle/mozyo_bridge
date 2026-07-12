"""herdr startup self-attestation store — generation-bound env observation (Redmine #13637).

A herdr-managed agent's self-identity triplet (``MOZYO_WORKSPACE_ID`` /
``MOZYO_AGENT_ROLE`` / ``MOZYO_LANE_ID``) is injected at ``herdr agent start``
via ``--env`` (spec ``vibes/docs/specs/herdr-native-identity.md`` §2/§5). Two
facts make a launcher / doctor unable to *verify* that injection after the fact
(Redmine #13637 Design Consultation j#76456, Answer j#76462):

- herdr exposes ``agent get/list/pane/read/target`` only — **no surface returns a
  launched process's environment**, so nothing outside the agent can read its env;
- a running process's environment is immutable to any other process (POSIX), so an
  env-less agent can never be repaired in place — only relaunched.

The only place the triplet is truthfully readable is **inside the agent's own
process**. This module is the durable landing spot for that self-observation: the
managed launch wraps the provider in a bounded self-check use-case
(:mod:`...application.herdr_agent_attest`) that inspects *its own* ``os.environ``
against the identity the launcher expected, records the verdict here, then ``exec``s
the provider. The adopt path (:func:`...herdr_session_start.prepare_session`) and the
doctor herdr section read this record to fail closed / go non-green on a live slot
whose startup self-attestation is absent, stale, missing, or conflicting.

Boundary (``vibes/docs/logics/managed-state-model.md`` ``### 正本境界`` /
``### state kind ownership / recovery matrix``): this is a
**``last_observed_projection``** — a home-scoped runtime observation, recovery
policy ``rebuildable_cache``. It is **not** authority for workspace identity
(registry), pane liveness (live herdr inventory), or workflow truth (Redmine). Its
loss degrades *safely to fail-closed* (an absent record makes adopt refuse and
doctor go non-green); it never promotes to a permission / liveness / identity
verdict, and a stale record is never re-used as a live process's attestation — the
generation is pinned by the **live locator** captured at write time (the only
externally observable discriminant), and a read whose live locator no longer matches
the recorded one is ``stale`` (Design Answer j#76462 refinement 2).

**Privacy (refinement 3):** the record stores only the verdict token, the expected
identity (workspace / role / lane / assigned name), the live locator, a
detail token naming *which variable* was missing/conflicting (never a value), and a
timestamp / schema version. Env **values**, credentials, and ambient env are never
stored — the whitelist dataclass simply has no field for them.

Conventions mirror the sibling home-scoped stores (``herdr_delivery_ledger`` /
``workspace_registry``): a ``*_FILENAME`` constant, a ``*_path(home=None)`` helper
through :func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a ``PRAGMA
user_version`` guard, a frozen dataclass with ``as_payload()``, ISO-second UTC
timestamps, and a best-effort write that never raises into the caller. A pure leaf:
it imports only stdlib + shared paths, so the dependency never points core ->
provider.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.shared.paths import mozyo_bridge_home

HERDR_IDENTITY_ATTESTATION_FILENAME = "herdr-identity-attestation.sqlite"
HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION = 1

#: Recovery policy (managed-state-model.md ``### recovery policy vocabulary``): a
#: rebuildable projection. Losing the file degrades to fail-closed (adopt refuses,
#: doctor non-green) and is re-derived by the next launch's self-attestation; it is
#: never authoritative for identity / liveness / workflow truth.
HERDR_IDENTITY_ATTESTATION_RECOVERY_POLICY = "rebuildable_cache"

# --- Env self-observation verdict vocabulary (what the AGENT observed of its env). --
#: All three identity vars present and equal to the expected identity.
VERDICT_PRESENT = "present"
#: One or more identity vars absent / empty in the agent's own process env.
VERDICT_MISSING = "missing"
#: An identity var is present but disagrees with the launcher-expected identity.
VERDICT_CONFLICT = "conflict"

# --- Read-side join states (adopt / doctor: recorded observation vs live slot). ----
#: A ``present`` record whose recorded generation (locator) + identity match the
#: live slot — the only state that authorizes adopt / keeps doctor green.
ATTEST_OK = "attested"
#: No self-attestation record for this assigned name (legacy / pre-feature launch,
#: or the self-check never wrote one).
ATTEST_ABSENT = "absent"
#: A record exists but its recorded live locator is empty or no longer matches the
#: live inventory locator — a DIFFERENT process generation; never re-used.
ATTEST_STALE = "stale"
#: The recorded verdict was ``missing`` (the agent booted without its triplet).
ATTEST_MISSING = "missing"
#: The recorded verdict was ``conflict``, or the recorded identity drifted from the
#: expected slot identity.
ATTEST_CONFLICT = "conflict"

# Identity-variable names (kept in sync with the terminal-runtime domain constants;
# duplicated as literals so this core leaf imports no provider module). Only NAMES
# ever reach a stored ``detail`` token — never values.
_WORKSPACE_VAR = "MOZYO_WORKSPACE_ID"
_ROLE_VAR = "MOZYO_AGENT_ROLE"
_LANE_VAR = "MOZYO_LANE_ID"

#: The lane a launch injects when the requested lane is empty (spec §2: empty ->
#: ``default``). Mirrors ``herdr_identity.DEFAULT_LANE`` as a literal (leaf purity).
_DEFAULT_LANE = "default"


def herdr_identity_attestation_path(home: Path | None = None) -> Path:
    return (home or mozyo_bridge_home()) / HERDR_IDENTITY_ATTESTATION_FILENAME


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _norm(value: object) -> str:
    """Trimmed string form of a value (``""`` for ``None`` / blank)."""
    if value is None:
        return ""
    return str(value).strip()


@dataclass(frozen=True)
class IdentityAttestationRecord:
    """One agent's startup self-attestation (a generation-bound env observation).

    Every field is a token / identity segment / locator / timestamp — **never** an
    env value or secret. ``verdict`` is what the agent observed of *its own*
    process env (:data:`VERDICT_PRESENT` / :data:`VERDICT_MISSING` /
    :data:`VERDICT_CONFLICT`). ``locator`` is the live herdr locator the agent
    resolved for itself at write time — the generation pin a later read compares
    against the live inventory. ``detail`` names which variable was missing /
    conflicting (a variable NAME, never a value), for operator diagnosis.
    """

    assigned_name: str
    workspace_id: str
    role: str
    lane_id: str
    locator: str
    verdict: str
    detail: str = ""
    observed_at: Optional[str] = None
    schema_version: int = HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION

    def as_payload(self) -> dict:
        return {
            "assigned_name": self.assigned_name,
            "workspace_id": self.workspace_id,
            "role": self.role,
            "lane_id": self.lane_id,
            "locator": self.locator,
            "verdict": self.verdict,
            "detail": self.detail,
            "observed_at": self.observed_at,
            "schema_version": self.schema_version,
        }


@dataclass(frozen=True)
class AttestationJoin:
    """The read-side verdict of joining a stored record with the live slot.

    ``ok`` is True only for :data:`ATTEST_OK`. ``state`` is the join state and
    ``reason`` an operator-facing, value-free explanation worded as *self-attestation*
    (never "read the live env", which herdr cannot do).
    """

    ok: bool
    state: str
    reason: str


class HerdrIdentityAttestationError(RuntimeError):
    """A user-actionable store error (schema mismatch / corruption)."""


def classify_identity_env(
    *,
    expected_workspace_id: str,
    expected_role: str,
    expected_lane: str,
    env: Mapping[str, str],
) -> tuple[str, str]:
    """Classify an agent's OWN process env against the launcher-expected identity.

    Pure: reads the passed ``env`` mapping only (the self-check use-case passes its
    own ``os.environ``). Returns ``(verdict, detail)`` where ``detail`` names the
    first offending variable (a NAME, never a value) so no secret is surfaced. An
    empty expected lane is normalised to ``default`` to match the launch injection
    (spec §2). Precedence: any missing var -> ``missing`` (reported before conflict)
    so an env-less boot is never masked by also mismatching.
    """
    expected = (
        (_WORKSPACE_VAR, _norm(expected_workspace_id)),
        (_ROLE_VAR, _norm(expected_role)),
        (_LANE_VAR, _norm(expected_lane) or _DEFAULT_LANE),
    )
    actual = {
        _WORKSPACE_VAR: _norm(env.get(_WORKSPACE_VAR)),
        _ROLE_VAR: _norm(env.get(_ROLE_VAR)),
        _LANE_VAR: _norm(env.get(_LANE_VAR)) or _DEFAULT_LANE,
    }
    missing = [name for name, _ in expected if not actual[name]]
    if missing:
        return VERDICT_MISSING, ",".join(missing)
    conflicts = [name for name, want in expected if actual[name] != want]
    if conflicts:
        return VERDICT_CONFLICT, ",".join(conflicts)
    return VERDICT_PRESENT, ""


def evaluate_attestation(
    record: Optional[IdentityAttestationRecord],
    *,
    live_locator: str,
    expected_workspace_id: str,
    expected_role: str,
    expected_lane: str,
) -> AttestationJoin:
    """Join a stored self-attestation with the live slot (pure; adopt + doctor share).

    The single read-side policy both the adopt classifier and the doctor section
    use, so they can never drift. Fail-closed precedence:

    1. no record -> :data:`ATTEST_ABSENT` (legacy / pre-feature slot);
    2. recorded identity (workspace / role / lane) != the expected slot ->
       :data:`ATTEST_CONFLICT` (a foreign record, never trusted for this slot);
    3. recorded ``locator`` empty or != ``live_locator`` -> :data:`ATTEST_STALE`
       (a different process generation; the whole point of the generation pin —
       a stale ``present`` record is never re-used as this process's attestation);
    4. recorded verdict ``missing`` / ``conflict`` -> the matching non-green state;
    5. otherwise :data:`ATTEST_OK`.

    ``reason`` is value-free and phrased as startup self-attestation, never as a
    live-env read (herdr has no such surface).
    """
    if record is None:
        return AttestationJoin(
            False,
            ATTEST_ABSENT,
            "no startup self-attestation record for this slot (a legacy / pre-feature "
            "launch, or the self-check never ran); the slot's identity env is unverified",
        )
    expected_lane_norm = _norm(expected_lane) or _DEFAULT_LANE
    if (
        _norm(record.workspace_id) != _norm(expected_workspace_id)
        or _norm(record.role) != _norm(expected_role)
        or (_norm(record.lane_id) or _DEFAULT_LANE) != expected_lane_norm
    ):
        return AttestationJoin(
            False,
            ATTEST_CONFLICT,
            "the startup self-attestation record's identity does not match this slot; "
            "refusing to trust a foreign record",
        )
    if not _norm(record.locator) or _norm(record.locator) != _norm(live_locator):
        return AttestationJoin(
            False,
            ATTEST_STALE,
            "the startup self-attestation was written by a different process generation "
            "(its recorded locator no longer matches the live slot); a stale record is "
            "never re-used as this process's attestation",
        )
    if record.verdict == VERDICT_MISSING:
        return AttestationJoin(
            False,
            ATTEST_MISSING,
            "the agent booted without its identity triplet in its own process env "
            "(startup self-attestation = missing); handoff sends will fail closed",
        )
    if record.verdict == VERDICT_CONFLICT:
        return AttestationJoin(
            False,
            ATTEST_CONFLICT,
            "the agent's identity env disagreed with its expected slot identity at boot "
            "(startup self-attestation = conflict)",
        )
    if record.verdict != VERDICT_PRESENT:
        return AttestationJoin(
            False,
            ATTEST_CONFLICT,
            f"unrecognised startup self-attestation verdict {record.verdict!r}",
        )
    return AttestationJoin(
        True,
        ATTEST_OK,
        "startup self-attestation present and generation-matched for this live slot",
    )


_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS herdr_identity_attestations (
    assigned_name TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    role TEXT NOT NULL,
    lane_id TEXT NOT NULL,
    locator TEXT NOT NULL,
    verdict TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    observed_at TEXT NOT NULL
)
"""

_COLUMNS = "assigned_name, workspace_id, role, lane_id, locator, verdict, detail, observed_at"


def _connect_rw(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA busy_timeout = 2000")
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(_TABLE_SQL)
        conn.execute(
            f"PRAGMA user_version = {HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}"
        )
        conn.commit()
    elif version != HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION:
        conn.close()
        raise HerdrIdentityAttestationError(
            f"herdr identity attestation store {path} has schema version {version}; "
            f"this build supports {HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION}. The file "
            f"is left untouched (downgrade-safe)."
        )
    return conn


class HerdrIdentityAttestationStore:
    """Snapshot-per-slot durable self-attestation store (one row per assigned name).

    Snapshot-replace, not append-only (managed-state-model.md ``### writer rules``
    for a projection): each launch's self-check upserts the latest generation for
    its slot. Reads are the join input for adopt / doctor.
    """

    def __init__(self, path: Path | None = None, *, home: Path | None = None):
        self.path = path or herdr_identity_attestation_path(home)

    def upsert(self, record: IdentityAttestationRecord) -> IdentityAttestationRecord:
        """Write (replacing any prior generation for the same assigned name).

        Stamps ``observed_at`` when absent. Returns the persisted record. The
        best-effort :func:`record_identity_attestation` wraps this so a store failure
        never blocks an agent boot.
        """
        observed_at = record.observed_at or _utc_now()
        conn = _connect_rw(self.path)
        try:
            with conn:
                conn.execute(
                    f"INSERT INTO herdr_identity_attestations ({_COLUMNS}) "
                    f"VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(assigned_name) DO UPDATE SET "
                    "workspace_id = excluded.workspace_id, role = excluded.role, "
                    "lane_id = excluded.lane_id, locator = excluded.locator, "
                    "verdict = excluded.verdict, detail = excluded.detail, "
                    "observed_at = excluded.observed_at",
                    (
                        record.assigned_name,
                        record.workspace_id,
                        record.role,
                        record.lane_id,
                        record.locator,
                        record.verdict,
                        record.detail,
                        observed_at,
                    ),
                )
        finally:
            conn.close()
        return IdentityAttestationRecord(
            assigned_name=record.assigned_name,
            workspace_id=record.workspace_id,
            role=record.role,
            lane_id=record.lane_id,
            locator=record.locator,
            verdict=record.verdict,
            detail=record.detail,
            observed_at=observed_at,
        )

    def read(self, assigned_name: str) -> Optional[IdentityAttestationRecord]:
        """Return the recorded self-attestation for ``assigned_name``, or ``None``.

        Read-only, fail-open to ``None`` (absent file / unreadable / schema drift):
        the caller fails closed on a ``None`` (adopt refuses, doctor non-green), so a
        cache loss never falsely attests a slot.
        """
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                version = conn.execute("PRAGMA user_version").fetchone()[0]
                if version != HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION:
                    return None
                row = conn.execute(
                    f"SELECT {_COLUMNS} FROM herdr_identity_attestations "
                    "WHERE assigned_name = ?",
                    (assigned_name,),
                ).fetchone()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return None
        if row is None:
            return None
        return IdentityAttestationRecord(
            assigned_name=row[0],
            workspace_id=row[1],
            role=row[2],
            lane_id=row[3],
            locator=row[4],
            verdict=row[5],
            detail=row[6],
            observed_at=row[7],
        )


def record_identity_attestation(
    record: IdentityAttestationRecord, *, home: Path | None = None
) -> Optional[IdentityAttestationRecord]:
    """Best-effort self-attestation write. Never raises into the caller.

    The self-check use-case calls this just before ``exec``ing the provider: a store
    failure must never stop the agent from booting (it degrades to an absent record,
    which the adopt / doctor read side treats as fail-closed / non-green). Returns
    the persisted record, or ``None`` on any failure.
    """
    try:
        return HerdrIdentityAttestationStore(home=home).upsert(record)
    except (HerdrIdentityAttestationError, sqlite3.DatabaseError, OSError):
        return None


__all__ = (
    "HERDR_IDENTITY_ATTESTATION_FILENAME",
    "HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION",
    "HERDR_IDENTITY_ATTESTATION_RECOVERY_POLICY",
    "VERDICT_PRESENT",
    "VERDICT_MISSING",
    "VERDICT_CONFLICT",
    "ATTEST_OK",
    "ATTEST_ABSENT",
    "ATTEST_STALE",
    "ATTEST_MISSING",
    "ATTEST_CONFLICT",
    "IdentityAttestationRecord",
    "AttestationJoin",
    "HerdrIdentityAttestationError",
    "HerdrIdentityAttestationStore",
    "classify_identity_env",
    "evaluate_attestation",
    "herdr_identity_attestation_path",
    "record_identity_attestation",
)
