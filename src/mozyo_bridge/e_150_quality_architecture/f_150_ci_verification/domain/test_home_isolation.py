"""Pure core of test-process home isolation (Redmine #14757).

A test process must not read or write the operator's shared mozyo-bridge home.
This module owns the *decisions* — which env keys pin the task-specific root,
which live-lane pins are stripped, and what counts as a change to operator
shared state — with no filesystem, subprocess, or sqlite access. The I/O side
lives in ``application/test_home_fence.py``.

Two mechanisms, and both are needed:

- **pins** (:func:`isolation_env`) redirect ordinary resolution — the home
  contract, temp files, and the XDG roots — into one task-specific temp root,
  and drop the live cockpit-session env so a test can never act on the running
  lane. They are inherited by child processes, which is what extends the
  boundary past the parent.
- **the fence** (:data:`mozyo_bridge.shared.paths.HOME_FENCE_ROOT_ENV`) makes
  the canonical resolver refuse the operator home even when the pins are gone.
  Pins alone are not enough: ``patch.dict(os.environ, {}, clear=True)`` appears
  in dozens of test files, and the cleared-env fallback ``~/.mozyo_bridge``
  resolves onto the operator home through the passwd database even with ``HOME``
  unset. That is what forward-migrated the operator's live store mid-run
  (#14477 j#94521 / j#94527 / j#94528).

``HOME`` is deliberately *not* repurposed (#14757 acceptance 1). Pointing it at
a temp dir hides the interpreter's user site-packages and the operator's git
identity, which the parallel runner had to patch around with ``PYTHONUSERBASE``
and a synthetic committer (#13733). Isolation here comes from the resolver, so
the fenced process keeps a working ``HOME``.

- **the audit hook** (``application/test_home_audit_hook.py``) refuses the write
  *before it happens*, in every Python process of the tree. R1 shipped without it
  and argued from after-the-fact detection; review j#100407 R1-F1 rejected that,
  because acceptance 4 asks for refusal and acceptance 7 for zero changes.

The snapshot is the **coarse backstop** for writers the hook cannot reach (a
non-Python process, an interpreter started with ``PYTHONPATH`` scrubbed). It is
explicitly NOT the evidence for acceptance 7 any more — the refusal ledger is
(:class:`DenyLedger`), because a directory comparison cannot see an in-place row
update or an overwrite of an existing file (j#100408 finding_1). Neither layer is
an OS sandbox: the same code path runs identically on macOS and Linux CI, where
``sandbox-exec`` does not exist (#14757 acceptance 3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - annotation only, no runtime cycle
    from mozyo_bridge.e_150_quality_architecture.f_150_ci_verification.domain.test_disk_pressure import (
        DiskPressureDiagnosis,
    )

#: Live cockpit-session pins removed from a fenced test process, so a test can
#: never attach to or act on the operator's running lane. Shared with the
#: parallel runner (which re-exports it as ``STRIPPED_ENV_KEYS``).
LIVE_LANE_ENV_KEYS = (
    "TMUX",
    "TMUX_PANE",
    "MOZYO_WORKSPACE_ID",
    "MOZYO_LANE_ID",
    "MOZYO_AGENT_ROLE",
)

#: Filename (inside the fenced home) the herdr-binary pin points at. The file is
#: never created: an ABSOLUTE ``MOZYO_HERDR_BINARY`` that does not resolve fails
#: closed with no trusted-PATH fallback (#13496 step 1), so pinning it masks the
#: operator's live herdr from every fenced test — a test that wants a working
#: herdr must inject its own env/runner, exactly what CI (no herdr installed)
#: already forces. Before this pin, tests that reached the ambient binary passed
#: locally and failed in CI (#15531 flip fallout, 6 tests).
HERDR_BINARY_FENCE_NAME = "herdr-fenced-absent"

#: Subdirectories of the task-specific root, by role. Separate directories keep
#: an isolation failure legible: a file's location says which resolver produced
#: it.
FENCE_SUBDIRS = {
    "home": "mozyo-home",
    "tmp": "tmp",
    "xdg_config": "xdg/config",
    "xdg_cache": "xdg/cache",
    "xdg_data": "xdg/data",
    "xdg_state": "xdg/state",
}

#: The operator default home, spelled as the resolver spells it. Denied in every
#: fenced process in addition to whatever ambient home the runner observed.
OPERATOR_DEFAULT_HOME = "~/.mozyo_bridge"

#: Operator-declared base directory for the runner's ``mozyo-tests-home-*``
#: task roots (Redmine #15710): a declarative escape from a quota-pressured
#: /tmp. Read by the *outer* runner only, and stripped from the fenced child
#: env below — a nested guarded run inside the fence must land in the pinned
#: fenced tmp, not escape to the operator's declared base (where the OS write
#: boundary would refuse it anyway).
TESTS_TEMP_BASE_ENV = "MOZYO_TESTS_TMPDIR"


@dataclass(frozen=True)
class IsolationLayout:
    """The task-specific temp root, decomposed by role (pure path algebra)."""

    root: Path

    @property
    def home(self) -> Path:
        return self.root / FENCE_SUBDIRS["home"]

    @property
    def tmp(self) -> Path:
        return self.root / FENCE_SUBDIRS["tmp"]

    @property
    def xdg_config(self) -> Path:
        return self.root / FENCE_SUBDIRS["xdg_config"]

    @property
    def xdg_cache(self) -> Path:
        return self.root / FENCE_SUBDIRS["xdg_cache"]

    @property
    def xdg_data(self) -> Path:
        return self.root / FENCE_SUBDIRS["xdg_data"]

    @property
    def xdg_state(self) -> Path:
        return self.root / FENCE_SUBDIRS["xdg_state"]

    @property
    def directories(self) -> tuple[Path, ...]:
        """Every directory the runner must create before spawning the child."""
        return (
            self.home,
            self.tmp,
            self.xdg_config,
            self.xdg_cache,
            self.xdg_data,
            self.xdg_state,
        )


def isolation_env(
    layout: IsolationLayout,
    *,
    denied_homes: tuple[Path, ...],
    deny_separator: str,
    fence_root_key: str,
    fence_deny_key: str,
) -> dict[str, str]:
    """The env pins for one fenced test process (additive; no ``HOME`` pin).

    Returns only the keys to *set*; :data:`LIVE_LANE_ENV_KEYS` names the keys to
    remove. Keeping the two apart lets a caller apply the same decision to an
    inherited ``os.environ`` copy or to a from-scratch child env.

    ``denied_homes`` is passed explicitly rather than derived here: the runner
    must capture the ambient home from the *real* environment before any pin is
    applied, and a pure function cannot see that moment.
    """
    return {
        "MOZYO_BRIDGE_HOME": str(layout.home),
        # Mask the operator's live herdr (env pin wins over trusted-PATH search,
        # and an absolute non-resolving value never falls back to PATH), so a
        # fenced test can only use herdr it explicitly injects — the same world
        # CI runs in. See HERDR_BINARY_FENCE_NAME.
        "MOZYO_HERDR_BINARY": str(layout.home / HERDR_BINARY_FENCE_NAME),
        "TMPDIR": str(layout.tmp),
        "TMP": str(layout.tmp),
        "TEMP": str(layout.tmp),
        "XDG_CONFIG_HOME": str(layout.xdg_config),
        "XDG_CACHE_HOME": str(layout.xdg_cache),
        "XDG_DATA_HOME": str(layout.xdg_data),
        "XDG_STATE_HOME": str(layout.xdg_state),
        fence_root_key: str(layout.home),
        fence_deny_key: deny_separator.join(
            sorted({str(path) for path in denied_homes})
        ),
    }


def apply_isolation(
    base_env: dict[str, str], pins: dict[str, str]
) -> dict[str, str]:
    """``base_env`` plus ``pins``, minus the live-lane pins (pure)."""
    env = dict(base_env)
    env.update(pins)
    for key in LIVE_LANE_ENV_KEYS:
        env.pop(key, None)
    # See TESTS_TEMP_BASE_ENV: the declared temp base is the outer runner's
    # setting, never the fenced child's.
    env.pop(TESTS_TEMP_BASE_ENV, None)
    return env


# --------------------------------------------------------------------------- #
# Operator-home snapshot: what counts as a change                             #
# --------------------------------------------------------------------------- #

#: Guarded tiers, and why each one is the right granularity.
#:
#: ``entries``  — the direct child names of home. Catches a store, lock, or
#:                credential file appearing that was not there before.
#: ``schema``   — ``PRAGMA user_version`` plus the schema-object set of every
#:                home SQLite. This is the tier the #14477 incident broke: a
#:                test process forward-migrated the shared store v7 -> v8.
#: ``identity`` — per-table row counts of every home SQLite, plus the workspace
#:                *identity set* of the registry. This is the tier the #14741
#:                regression broke: two tests inserted a temp workspace row per
#:                run. Counts and id sets, never row contents, so the operator's
#:                own concurrent activity (``last_seen`` / ``updated_at``
#:                touches on existing rows) is not mistaken for a test write.
#: ``backups``  — the relative path set under ``backups/``. A migration that
#:                took a pre-write backup shows up here even if it then rolled
#:                the schema back.
GUARDED_TIERS = ("entries", "schema", "identity", "backups")

#: Row contents of the operator's high-churn delivery / attestation ledgers are
#: deliberately NOT compared: the operator's own running cockpit writes them
#: continuously, so a content digest would go red for reasons unrelated to the
#: test process and the guard would be turned off within a week. The residual
#: risk — a test that appends a row to one of those ledgers without changing any
#: table's row count, which is impossible for an append — is recorded in
#: ``vibes/docs/logics/test-process-home-isolation.md``.
CHURN_CARVE_OUT = "row contents of home SQLite stores (row counts are compared)"


def digest(parts: tuple[str, ...]) -> str:
    """Stable short digest of an ordered string tuple (value-free evidence).

    Snapshots are recorded in Redmine journals, so they must carry no operator
    path, workspace name, or credential — only a digest and a count. A digest
    difference names *which tier* moved without disclosing the state itself.
    """
    joined = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:16]


@dataclass(frozen=True)
class HomeSnapshot:
    """A value-free logical snapshot of one mozyo-bridge home.

    ``unreadable`` records components that could not be snapshotted
    consistently. It is never empty-and-ignored: a component that cannot be read
    fails the guard, because "I could not look" must not read as "nothing
    changed" (and because falling back to ``immutable=1`` to force a read of a
    live database is prohibited by #14757 acceptance 5).
    """

    home: str
    entry_count: int = 0
    entry_digest: str = ""
    schema_digest: str = ""
    identity_digest: str = ""
    #: Typed rows behind ``identity_digest`` (j#100490 item 2). Counts only, so a
    #: mismatch can name the table that moved without disclosing any row. Typed
    #: rather than tab-delimited text: SQLite identifiers may contain tabs and
    #: newlines, which made distinct tables collide on a re-parse and let control
    #: bytes reach CI output.
    identity_counts: tuple["TableCount", ...] = ()
    backup_count: int = 0
    backup_digest: str = ""
    store_count: int = 0
    unreadable: tuple[str, ...] = ()
    missing: bool = False

    def as_dict(self) -> dict:
        return {
            "home": self.home,
            "missing": self.missing,
            "entry_count": self.entry_count,
            "entry_digest": self.entry_digest,
            "schema_digest": self.schema_digest,
            "identity_digest": self.identity_digest,
            "backup_count": self.backup_count,
            "backup_digest": self.backup_digest,
            "store_count": self.store_count,
            "unreadable": list(self.unreadable),
        }


@dataclass(frozen=True)
class TableCount:
    """One SQLite table's row count, as a typed value rather than a text row."""

    store: str
    table: str
    rows: int

    def as_key(self) -> str:
        """A collision-free key even when identifiers contain separators."""
        return f"{self.store!r}/{self.table!r}"


@dataclass(frozen=True)
class HomeDelta:
    """One guarded tier that moved between two snapshots."""

    tier: str
    before: str
    after: str
    #: Value-free diagnostic granularity (j#100487): which store/table moved and by
    #: how many rows. Counts and names only — never row values, workspace ids or
    #: anything else that could carry operator data into a journal or a CI log.
    detail: str = ""

    def as_dict(self) -> dict:
        payload = {"tier": self.tier, "before": self.before, "after": self.after}
        if self.detail:
            payload["detail"] = self.detail
        return payload

    def __str__(self) -> str:  # pragma: no cover - rendering convenience
        rendered = f"{self.tier}: {self.before} -> {self.after}"
        return f"{rendered} ({self.detail})" if self.detail else rendered


@dataclass(frozen=True)
class HomeGuardVerdict:
    """Fail-closed verdict over a before/after pair of snapshots."""

    home: str
    deltas: tuple[HomeDelta, ...] = ()
    unreadable: tuple[str, ...] = ()
    #: Position in the guarded set, so two homes stay distinguishable once absolute
    #: paths are withheld from default output.
    ordinal: int = 0

    @property
    def label(self) -> str:
        """The journal-safe identifier for this home (j#100490 item 4)."""
        return path_label(self.home, "guarded-home", self.ordinal)

    @property
    def unchanged(self) -> bool:
        """True only when nothing moved *and* every component was readable."""
        return not self.deltas and not self.unreadable

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = [str(delta) for delta in self.deltas]
        reasons += [f"unreadable component: {name}" for name in self.unreadable]
        return tuple(reasons)

    def as_dict(self, *, reveal_paths: bool = False) -> dict:
        # Default output carries a role/ordinal/digest label, never the absolute
        # operator home: these verdicts land in tickets and CI logs, where the path
        # discloses the operator's account name and local layout. `reveal_paths` is
        # the explicit local-debug opt-in.
        return {
            "home": self.home if reveal_paths else self.label,
            "unchanged": self.unchanged,
            "deltas": [delta.as_dict() for delta in self.deltas],
            "unreadable": list(self.unreadable),
        }


_COMPARED_FIELDS = (
    ("entries", "entry_count", "entry_digest"),
    ("schema", "store_count", "schema_digest"),
    ("identity", None, "identity_digest"),
    ("backups", "backup_count", "backup_digest"),
)


def path_label(path: str, role: str, ordinal: int = 0) -> str:
    """A journal- and CI-safe identifier for a path (j#100490 item 4).

    Default output must not carry absolute operator-home or task-root paths: these
    verdicts are pasted into tickets and CI logs, where an absolute path discloses
    the operator's account name and local layout. A role, an ordinal and a short
    digest identify the same home across a run's before/after pair and across two
    runs, which is all the reader needs. Absolute paths remain available behind an
    explicit local-debug option.
    """
    return f"{role}[{ordinal}] sha256:{digest((path,))[:12]}"


def escape_identifier(name: str) -> str:
    """Render a SQLite identifier safely for a log, a journal or CI output.

    Control bytes in a table name would otherwise be emitted raw. Escaping is a
    rendering concern only — the guard still compares the real names.
    """
    return name.encode("unicode_escape").decode("ascii")


def _identity_detail(before: HomeSnapshot, after: HomeSnapshot) -> str:
    """Name the store/table row counts that moved, and nothing else (j#100487).

    The identity digest answers "did anything move" but not "what", which left a
    red run un-attributable — the state j#100482 ended in. This narrows *the
    report*, never the guard: every table is still compared, and any drift is
    still non-zero. Only counts and names are emitted.
    """

    def counts(snapshot: HomeSnapshot) -> dict[tuple[str, str], int]:
        return {(row.store, row.table): row.rows for row in snapshot.identity_counts}

    was, now = counts(before), counts(after)
    moved = [
        f"{escape_identifier(store)}/{escape_identifier(table)} "
        f"{was.get((store, table), 'absent')}->{now.get((store, table), 'absent')}"
        for store, table in sorted(set(was) | set(now))
        if was.get((store, table)) != now.get((store, table))
    ]
    if not moved:
        # The digest moved but no row count did: the registry's workspace identity
        # set changed (ids added or removed). Say so WITHOUT naming any id.
        return "registry workspace identity set changed"
    return "; ".join(moved)


def compare_snapshots(
    before: HomeSnapshot, after: HomeSnapshot, ordinal: int = 0
) -> HomeGuardVerdict:
    """Fail-closed comparison of two snapshots of the same home.

    A home that did not exist before and does now is itself a violation: the
    test process created the operator's home from scratch.
    """
    deltas: list[HomeDelta] = []
    if before.missing != after.missing:
        deltas.append(
            HomeDelta(
                tier="existence",
                before="absent" if before.missing else "present",
                after="absent" if after.missing else "present",
            )
        )
    for tier, count_field, digest_field in _COMPARED_FIELDS:
        before_digest = getattr(before, digest_field)
        after_digest = getattr(after, digest_field)
        if before_digest == after_digest:
            continue
        if count_field is None:
            deltas.append(
                HomeDelta(
                    tier=tier,
                    before=before_digest,
                    after=after_digest,
                    detail=_identity_detail(before, after),
                )
            )
            continue
        deltas.append(
            HomeDelta(
                tier=tier,
                before=f"{getattr(before, count_field)}/{before_digest}",
                after=f"{getattr(after, count_field)}/{after_digest}",
            )
        )
    unreadable = tuple(sorted(set(before.unreadable) | set(after.unreadable)))
    return HomeGuardVerdict(
        home=after.home,
        deltas=tuple(deltas),
        unreadable=unreadable,
        ordinal=ordinal,
    )


@dataclass(frozen=True)
class DenyLedger:
    """Refused write attempts recorded by the injected audit hook (R2).

    This is the *primary* evidence for acceptance 7. R1 argued "zero changes from
    this process tree" from a before/after snapshot, and review j#100408 finding_1
    showed that argument does not hold: the snapshot's tiers miss an in-place row
    update, a write inside an existing directory, and an overwrite of an existing
    file. Attribution is the sound argument instead — the hook refuses each attempt
    *and names it*, so an empty ledger says the tree attempted nothing, which no
    amount of directory comparison can say.

    ``missing`` is a failure, not an absence: the runner writes the ledger empty
    before the run, so a ledger that is gone means the fence did not run.
    """

    attempts: tuple[str, ...] = ()
    missing: bool = False

    @property
    def clean(self) -> bool:
        return not self.attempts and not self.missing

    @property
    def reasons(self) -> tuple[str, ...]:
        if self.missing:
            return (
                "the deny-attempt ledger is missing; the audit hook cannot be shown "
                "to have run, so isolation is unproven",
            )
        return tuple(
            f"refused write attempt to the operator's shared home: {attempt}"
            for attempt in self.attempts
        )

    def as_dict(self) -> dict:
        return {
            "clean": self.clean,
            "missing": self.missing,
            "attempt_count": len(self.attempts),
            "attempts": list(self.attempts),
        }


@dataclass(frozen=True)
class IsolatedRunOutcome:
    """The fail-closed verdict of one isolated run: suite AND refusals AND guards.

    Kept as one value object because no part alone is sufficient. A green suite
    that mutated the operator's home is not a pass (the #14477 shape: green tests,
    migrated store). A green suite whose audit hook refused a write is not a pass
    either — the write was prevented, but a test that tried is a defect that must
    surface. And a red suite with everything else clean is still red.

    ``guards`` is a tuple, not a single verdict: every root the fence denies is
    also snapshotted. R1 denied both the operator default and an explicit
    ``MOZYO_BRIDGE_HOME`` while snapshotting only one of them, so a child that lost
    the fence env could write the other and still be reported green
    (review j#100408 finding_2).
    """

    suite_success: bool
    guards: tuple[HomeGuardVerdict, ...] = ()
    ledger: DenyLedger = field(default_factory=DenyLedger)
    returncode: int | None = None
    detail: str | None = None
    fence_root: str = ""
    #: Which OS-level write boundary enforced the run, and whether that backend has
    #: actually been measured in this repository. An unmeasured backend still runs,
    #: but nothing may report it as proven (Redmine #14757 j#100419).
    os_backend: str = ""
    os_backend_verified: bool = False
    reasons: tuple[str, ...] = field(default_factory=tuple)
    #: Environmental annotation (Redmine #15710), never part of the verdict:
    #: a run that saw EDQUOT/ENOSPC markers is still red, but its reasons say
    #: the failure looks environmental instead of letting dozens of quota
    #: errors read as regressions of the change under test.
    disk_pressure: "DiskPressureDiagnosis | None" = None

    @property
    def homes_unchanged(self) -> bool:
        return all(guard.unchanged for guard in self.guards)

    @property
    def success(self) -> bool:
        return self.suite_success and self.ledger.clean and self.homes_unchanged

    @property
    def all_reasons(self) -> tuple[str, ...]:
        reasons = list(self.reasons)
        if not self.suite_success:
            reasons.append(
                self.detail or f"test suite failed (returncode={self.returncode})"
            )
        reasons += list(self.ledger.reasons)
        for guard in self.guards:
            reasons += [
                f"operator shared home changed during the run -- {reason}"
                for reason in guard.reasons
            ]
        if self.disk_pressure is not None and self.disk_pressure.suspected:
            reasons += list(self.disk_pressure.reasons)
        return tuple(reasons)

    def as_dict(self, *, reveal_paths: bool = False) -> dict:
        return {
            "success": self.success,
            "suite_success": self.suite_success,
            "returncode": self.returncode,
            "fence_root": (
                self.fence_root
                if reveal_paths
                else path_label(self.fence_root, "fence-root")
            ),
            "os_backend": self.os_backend,
            "os_backend_verified": self.os_backend_verified,
            "deny_ledger": self.ledger.as_dict(),
            "home_guards": [
                guard.as_dict(reveal_paths=reveal_paths) for guard in self.guards
            ],
            "disk_pressure": (
                None
                if self.disk_pressure is None
                else self.disk_pressure.as_dict(reveal_paths=reveal_paths)
            ),
            "reasons": list(self.all_reasons),
        }


__all__ = (
    "CHURN_CARVE_OUT",
    "DenyLedger",
    "FENCE_SUBDIRS",
    "GUARDED_TIERS",
    "LIVE_LANE_ENV_KEYS",
    "OPERATOR_DEFAULT_HOME",
    "TESTS_TEMP_BASE_ENV",
    "HomeDelta",
    "HomeGuardVerdict",
    "HomeSnapshot",
    "IsolatedRunOutcome",
    "IsolationLayout",
    "apply_isolation",
    "compare_snapshots",
    "digest",
    "isolation_env",
)
