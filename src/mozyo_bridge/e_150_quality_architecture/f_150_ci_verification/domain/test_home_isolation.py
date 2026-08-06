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

The snapshot half is the fail-closed backstop for everything the pins and the
fence cannot reach in-process — a grandchild launched with a scrubbed env, a
``multiprocessing`` worker, an installed console script from a throwaway venv.
It is deliberately *not* an OS sandbox: the same code path runs identically on
macOS and Linux CI, where ``sandbox-exec`` does not exist (#14757 acceptance 3).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

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
class HomeDelta:
    """One guarded tier that moved between two snapshots."""

    tier: str
    before: str
    after: str

    def as_dict(self) -> dict:
        return {"tier": self.tier, "before": self.before, "after": self.after}

    def __str__(self) -> str:  # pragma: no cover - rendering convenience
        return f"{self.tier}: {self.before} -> {self.after}"


@dataclass(frozen=True)
class HomeGuardVerdict:
    """Fail-closed verdict over a before/after pair of snapshots."""

    home: str
    deltas: tuple[HomeDelta, ...] = ()
    unreadable: tuple[str, ...] = ()

    @property
    def unchanged(self) -> bool:
        """True only when nothing moved *and* every component was readable."""
        return not self.deltas and not self.unreadable

    @property
    def reasons(self) -> tuple[str, ...]:
        reasons = [str(delta) for delta in self.deltas]
        reasons += [f"unreadable component: {name}" for name in self.unreadable]
        return tuple(reasons)

    def as_dict(self) -> dict:
        return {
            "home": self.home,
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


def compare_snapshots(before: HomeSnapshot, after: HomeSnapshot) -> HomeGuardVerdict:
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
                HomeDelta(tier=tier, before=before_digest, after=after_digest)
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
        home=after.home, deltas=tuple(deltas), unreadable=unreadable
    )


@dataclass(frozen=True)
class IsolatedRunOutcome:
    """The fail-closed verdict of one isolated run: suite AND home guard.

    Kept as one value object because either half alone is misleading. A green
    suite that mutated the operator's home is not a pass (that is exactly the
    #14477 shape: the tests were green and the shared store was migrated), and a
    red suite with an untouched home is still red.
    """

    suite_success: bool
    guard: HomeGuardVerdict
    returncode: int | None = None
    detail: str | None = None
    fence_root: str = ""
    reasons: tuple[str, ...] = field(default_factory=tuple)

    @property
    def success(self) -> bool:
        return self.suite_success and self.guard.unchanged

    @property
    def all_reasons(self) -> tuple[str, ...]:
        reasons = list(self.reasons)
        if not self.suite_success:
            reasons.append(
                self.detail or f"test suite failed (returncode={self.returncode})"
            )
        reasons += [
            f"operator shared home changed during the run -- {reason}"
            for reason in self.guard.reasons
        ]
        return tuple(reasons)

    def as_dict(self) -> dict:
        return {
            "success": self.success,
            "suite_success": self.suite_success,
            "returncode": self.returncode,
            "fence_root": self.fence_root,
            "home_guard": self.guard.as_dict(),
            "reasons": list(self.all_reasons),
        }


__all__ = (
    "CHURN_CARVE_OUT",
    "FENCE_SUBDIRS",
    "GUARDED_TIERS",
    "LIVE_LANE_ENV_KEYS",
    "OPERATOR_DEFAULT_HOME",
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
