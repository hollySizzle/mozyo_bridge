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
through :func:`mozyo_bridge.shared.paths.mozyo_bridge_home`, a frozen dataclass with
``as_payload()``, ISO-second UTC timestamps, and a best-effort write that never raises
into the caller. A pure leaf: it imports only stdlib, shared paths, and the sibling
schema authority, so the dependency never points core -> provider.

**Mixed-runtime compatibility (Redmine #13882).** A shared ``MOZYO_BRIDGE_HOME`` is
written by launchers of different vintages at once, so schema policy lives in
:mod:`.herdr_identity_attestation_schema` and this module never compares versions
itself. Reads are **read-compatible**: an older recognized shape is projected up to
:data:`COLUMNS_V2` inside one pinned read transaction, so a v1 store's rows decode
natively instead of reading as ``absent`` (the #13882 live evidence: 94 real rows that
every downstream verify treated as unattested). Writes are **conservative**: a v1 store
is written v1-shaped rather than migrated, because auto-migrating the shared home would
break every older installed launcher the same way. The one refusal is a **replacement**
launch onto a v1 store — that field cannot be dropped, so the write raises instead of
landing a row a replacement recovery could never match. Forward migration is an
explicit operator command only.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from mozyo_bridge.core.state.herdr_identity_attestation_schema import (
    COLUMNS_V2,
    HERDR_IDENTITY_ATTESTATION_RECOVERY_POLICY,
    HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION,
    RECOGNIZED_SCHEMA_VERSIONS,
    STORE_ABSENT,
    STORE_RECOGNIZED,
    create_schema,
    readonly_compatible_select,
    recorded_version,
    store_status,
    writable_projection,
    write_drops_replacement_action_id,
)
from mozyo_bridge.shared.paths import mozyo_bridge_home

HERDR_IDENTITY_ATTESTATION_FILENAME = "herdr-identity-attestation.sqlite"

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
    #: The replacement transaction ``action_id`` that launched this process (Redmine #13806
    #: R2-F2), or empty on a normal (non-replacement) launch. A token only — never a secret /
    #: env value. A replacement recovery verifies its fresh worker by matching this exactly.
    replacement_action_id: str = ""
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
            "replacement_action_id": self.replacement_action_id,
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


def _connect_rw(path: Path) -> tuple[sqlite3.Connection, int]:
    """Open for writing and resolve the shape to write, without ever migrating.

    Returns ``(conn, version)`` where ``version`` selects the write projection. A fresh
    store is created at the current version; a recognized older store is left **exactly
    as it lies** (Redmine #13882) so older installed launchers sharing this home keep
    reading it. Anything unrecognized raises with the file byte-untouched.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    try:
        conn.execute("PRAGMA busy_timeout = 2000")
        status = store_status(conn)
        if status == STORE_ABSENT:
            with conn:
                create_schema(conn)
            return conn, HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION
        if status != STORE_RECOGNIZED:
            version = recorded_version(conn)
            raise HerdrIdentityAttestationError(
                f"herdr identity attestation store {path} has an unsupported schema "
                f"(recorded version {version}); this build recognizes "
                f"{sorted(RECOGNIZED_SCHEMA_VERSIONS)}. The file is left untouched "
                f"(fail-closed, no silent repair)."
            )
        return conn, int(recorded_version(conn))
    except BaseException:
        conn.close()
        raise


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

        Writes the store's **own** shape (Redmine #13882) rather than migrating it: a v1
        store takes a v1-shaped row, which loses nothing on a normal launch because
        ``replacement_action_id`` is empty. A **replacement** launch onto a v1 store
        raises instead — dropping that field would silently unbind the fresh process from
        the replacement transaction that a recovery matches on exactly.
        """
        observed_at = record.observed_at or _utc_now()
        conn, version = _connect_rw(self.path)
        try:
            if write_drops_replacement_action_id(version, record.replacement_action_id):
                raise HerdrIdentityAttestationError(
                    f"herdr identity attestation store {self.path} is schema v{version}, "
                    f"which has no `replacement_action_id` column, but this is a "
                    f"replacement launch carrying one. Refusing to write a row that "
                    f"silently drops the replacement binding (the store is left "
                    f"untouched). Migrate the store first: "
                    f"`mozyo-bridge herdr attestation-store migrate --write`."
                )
            columns = writable_projection(version)
            assert columns is not None  # store_status() already proved it recognized
            values = {
                "assigned_name": record.assigned_name,
                "workspace_id": record.workspace_id,
                "role": record.role,
                "lane_id": record.lane_id,
                "locator": record.locator,
                "verdict": record.verdict,
                "detail": record.detail,
                "observed_at": observed_at,
                "replacement_action_id": record.replacement_action_id,
            }
            updatable = [c for c in columns if c != "assigned_name"]
            with conn:
                conn.execute(
                    f"INSERT INTO herdr_identity_attestations ({', '.join(columns)}) "
                    f"VALUES ({', '.join('?' for _ in columns)}) "
                    "ON CONFLICT(assigned_name) DO UPDATE SET "
                    + ", ".join(f"{c} = excluded.{c}" for c in updatable),
                    tuple(values[c] for c in columns),
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
            replacement_action_id=record.replacement_action_id,
        )

    def assigned_names(self) -> frozenset:
        """The assigned names carrying a record here (read-only; ``frozenset()`` on any
        unreadable / unsupported store).

        Proves *which* agents actually attested into **this** home (Redmine #13882). herdr
        exposes no surface returning a launched process's environment, so the home a live
        agent was launched against is unobservable from outside — a stored row is the only
        evidence that ties a live agent to this specific store. The maintenance command's
        consumer gate joins this against the live inventory.
        """
        if not self.path.exists():
            return frozenset()
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA busy_timeout = 2000")
                conn.execute("BEGIN")
                if readonly_compatible_select(conn) is None:
                    return frozenset()
                rows = conn.execute(
                    "SELECT assigned_name FROM herdr_identity_attestations"
                ).fetchall()
            finally:
                conn.close()
        except sqlite3.DatabaseError:
            return frozenset()
        return frozenset(row[0] for row in rows)

    def read(self, assigned_name: str) -> Optional[IdentityAttestationRecord]:
        """Return the recorded self-attestation for ``assigned_name``, or ``None``.

        Read-only, fail-open to ``None`` (absent file / unreadable / unsupported shape):
        the caller fails closed on a ``None`` (adopt refuses, doctor non-green), so a
        cache loss never falsely attests a slot.

        **Read-compatible, never migrating** (Redmine #13882): a recognized older shape is
        projected up to the current column vocabulary — a v1 row decodes with an empty
        ``replacement_action_id``, which is that row's true value (its writer had no
        replacement concept), not a fabricated one. The whole read — status, version,
        shape check, projection, and the row ``SELECT`` — runs inside **one explicit read
        transaction** begun before the first schema query, so a peer migration committing
        mid-read cannot yield a torn view (v1 shape + v2 version). ``busy_timeout`` is a
        lock-wait aid only; it is not the snapshot authority (Redmine #13844 R9).
        """
        if not self.path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
            try:
                conn.execute("PRAGMA busy_timeout = 2000")
                conn.execute("BEGIN")
                select = readonly_compatible_select(conn)
                if select is None:
                    return None
                row = conn.execute(
                    f"SELECT {select} FROM herdr_identity_attestations "
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
            replacement_action_id=row[8],
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
    "COLUMNS_V2",
    "HERDR_IDENTITY_ATTESTATION_FILENAME",
    "HERDR_IDENTITY_ATTESTATION_SCHEMA_VERSION",
    "HERDR_IDENTITY_ATTESTATION_RECOVERY_POLICY",
    "RECOGNIZED_SCHEMA_VERSIONS",
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
