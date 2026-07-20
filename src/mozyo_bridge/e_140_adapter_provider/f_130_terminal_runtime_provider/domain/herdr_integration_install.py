"""Opt-in herdr integration-hook installer — pure model (Redmine #13249).

The #13175 PoC (E2 / E10) established the two facts this installer is built on:

- ``herdr integration install claude|codex`` writes a **wholly local** session hook
  into the agent's config dir (``~/.claude`` / ``~/.codex``) — a self-contained
  ``sh`` + inline ``python3`` that talks only to herdr's Unix socket, no URL / curl
  (E2). herdr *refuses* the install when the target config dir does not exist.
- session-resume across a herdr restart needs that hook installed (E10): without it
  ``resume_agents_on_restore`` has no session ref to restore.

So the hook is genuinely useful, but installing it **mutates operator home**, and the
governing rule for this US is that home is only ever touched on **explicit opt-in** —
never as a side effect of a default command, a normal install, or onboarding (issue
#13249 Scope: "global hook mutation を side effect にしない"). This module is the pure
core of the opt-in installer: the agent vocabulary, the fail-closed reason set, the
directory-snapshot / diff / rollback *data model*, and the path-safety predicate. It
performs **no IO** — resolving home, hashing files, invoking herdr, and writing/restoring
live under the application ops layer (:mod:`...application.herdr_integration_install_ops`),
so this module can be reasoned about and tested as pure data.

The installer never authors the hook itself: the hook is herdr's artifact, so mozyo
orchestrates ``herdr integration install`` through an injected runner and only *brackets*
it — a read-only **plan** (default, zero mutation), an explicit **apply** (``--apply``),
a pre/post content snapshot that yields the exact **diff** herdr made, and a snapshot
**rollback** so a partial multi-agent failure leaves home as it was found. Duplicating
herdr's hook bytes would drift and would put mozyo-authored code into ``~/.claude`` — the
opposite of this supply-chain US's intent.

Boundary (kept enforced in code):

- **Pure / fail-closed.** Unknown agent, a missing / unsafe config dir, an unpinned herdr
  posture, a herdr error, and a partial failure each resolve to a closed
  :data:`INSTALL_FAILURE_REASONS` reason — never a silent success.
- **Never reads credentials.** The snapshot the ops layer builds excludes
  credential-shaped files (:data:`CREDENTIAL_SHAPED_PARTS`); this module defines the
  denylist so the exclusion rule lives with the model, and no credential ever enters a
  snapshot, a diff, a backup, or a rollback.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# --- agent vocabulary (core-owned) -------------------------------------------
#: The agents herdr can install a session hook for (PoC E2). Closed: an unknown
#: agent fails closed rather than being forwarded to herdr.
AGENT_CLAUDE = "claude"
AGENT_CODEX = "codex"

INTEGRATION_AGENTS: frozenset[str] = frozenset({AGENT_CLAUDE, AGENT_CODEX})

#: The home-relative config dir each agent keeps, resolved under the operator's
#: home by the ops layer. A fixed map so a caller can never point the installer at
#: an arbitrary directory by naming a different "agent".
AGENT_CONFIG_DIRNAME: "dict[str, str]" = {
    AGENT_CLAUDE: ".claude",
    AGENT_CODEX: ".codex",
}

# --- fail-closed reason vocabulary (core-owned) ------------------------------
#: A requested agent is not one herdr installs a hook for.
REASON_UNKNOWN_AGENT = "unknown_agent"
#: The agent's config dir does not exist. herdr itself refuses this (E2); the
#: installer refuses it *first* so it never asks herdr to touch a dir that is not
#: there, and never silently creates one.
REASON_CONFIG_DIR_MISSING = "config_dir_missing"
#: The resolved config path is not a real directory safely under home — a symlink
#: escaping home, a traversal (``..``) component, or a non-directory. The install
#: is refused before any snapshot / mutation.
REASON_UNSAFE_CONFIG_PATH = "unsafe_config_path"
#: The herdr supply-chain posture is not pinned (see :mod:`...domain.herdr_pin_posture`).
#: Installing a hook onto an unpinned herdr would leave unattended egress enabled,
#: so the installer refuses until the posture is pinned.
REASON_UNPINNED_REMOTE = "unpinned_remote"
#: herdr was invoked but reported a non-zero exit / spawn failure for an agent.
REASON_HERDR_ERROR = "herdr_error"
#: The trusted herdr binary could not be resolved (env / trusted PATH). Because a
#: plan promises that an apply *could* run, an unresolvable binary gates the plan
#: closed — a plan can never report ``ok`` for a target no apply could touch
#: (Redmine #13249 review j#83613 finding 2).
REASON_HERDR_UNRESOLVED = "herdr_unresolved"
#: A rollback did not fully restore the dir: a remove/restore raised, or the
#: post-rollback snapshot still differs from the pre-apply snapshot (residue
#: remains). The installer must never claim ``home left as found`` / ``rolled_back``
#: when restoration could not be *proven* (Redmine #13249 review j#83613 finding 1).
REASON_ROLLBACK_INCOMPLETE = "rollback_incomplete"
#: A target config dir holds a non-credential file the installer cannot read, so a
#: rollback of that dir could never be byte-verified. The apply is refused *before*
#: any mutation — an un-provable rollback must never be started, and a pair of
#: unreadable files must never read as "restored" (Redmine #13249 review j#83674
#: finding 1: `unreadable == unreadable` is not restoration proof).
REASON_CONFIG_DIR_UNREADABLE = "config_dir_unreadable"
#: Apply was requested for several agents and at least one failed after another had
#: already been mutated — the whole operation is reported failed and rolled back.
REASON_PARTIAL_FAILURE = "partial_failure"
#: Apply was requested but the explicit opt-in was not given (defence in depth: the
#: ops layer only mutates when the caller passes the apply intent *and* nothing is
#: gated). Never reached on the read-only plan path.
REASON_NOT_OPTED_IN = "not_opted_in"

INSTALL_FAILURE_REASONS: frozenset[str] = frozenset(
    {
        REASON_UNKNOWN_AGENT,
        REASON_CONFIG_DIR_MISSING,
        REASON_CONFIG_DIR_UNREADABLE,
        REASON_UNSAFE_CONFIG_PATH,
        REASON_UNPINNED_REMOTE,
        REASON_HERDR_ERROR,
        REASON_HERDR_UNRESOLVED,
        REASON_ROLLBACK_INCOMPLETE,
        REASON_PARTIAL_FAILURE,
        REASON_NOT_OPTED_IN,
    }
)

#: Lowercased substrings that mark a file as credential-shaped. A file whose name
#: matches is **never** hashed, snapshotted, diffed, backed up, or restored — the
#: installer must not read or copy operator credentials (the hook herdr installs is
#: credential-free, PoC E2, so excluding these can never drop a hook artifact).
CREDENTIAL_SHAPED_PARTS: tuple[str, ...] = (
    "credential",
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "auth",
    ".key",
    ".pem",
    "id_rsa",
)


class HerdrIntegrationInstallError(ValueError):
    """A programming-level misuse of the installer model (fail-closed).

    Distinct from an *install verdict*: a verdict carries a runtime
    :data:`INSTALL_FAILURE_REASONS` reason and is data the caller inspects, whereas
    this exception guards the record invariants (an outcome that claims success with
    a reason, a snapshot with a non-string hash, …). Inherits :class:`ValueError` to
    match the sibling adapter-boundary errors.
    """


def is_credential_shaped(name: str) -> bool:
    """True iff ``name`` looks like a credential file the installer must not read."""
    if not isinstance(name, str):
        return True  # unreadable name → treat as sensitive (fail-safe)
    lowered = name.lower()
    return any(part in lowered for part in CREDENTIAL_SHAPED_PARTS)


def normalize_agents(requested: "Optional[list[str]]") -> "tuple[str, ...]":
    """Return the ordered, de-duplicated agents to operate on (fail-closed).

    ``None`` / empty means *both* agents in a stable order (claude, codex). A
    requested list is validated against :data:`INTEGRATION_AGENTS`; an unknown or
    non-string entry raises :class:`HerdrIntegrationInstallError` (the CLI turns
    this into an ``unknown_agent`` verdict) rather than being silently dropped, and
    duplicates collapse order-preserving.
    """
    if not requested:
        return (AGENT_CLAUDE, AGENT_CODEX)
    ordered: list[str] = []
    for agent in requested:
        if not isinstance(agent, str) or agent not in INTEGRATION_AGENTS:
            raise HerdrIntegrationInstallError(
                f"unknown integration agent {agent!r}; known agents: "
                f"{sorted(INTEGRATION_AGENTS)}"
            )
        if agent not in ordered:
            ordered.append(agent)
    return tuple(ordered)


@dataclass(frozen=True)
class DirSnapshot:
    """An immutable content manifest of a config dir: relpath -> sha256 hex.

    Credential-shaped files are excluded by the ops layer *before* a snapshot is
    built (:func:`is_credential_shaped`), so a snapshot only ever names hook-class
    files. Two snapshots (pre / post an apply) feed :func:`diff_snapshots` to yield
    the exact set of files herdr added / changed / removed — the installer's
    ``diff`` display and the input to a rollback.
    """

    entries: "tuple[tuple[str, str], ...]" = ()

    def __post_init__(self) -> None:
        seen: set[str] = set()
        for item in self.entries:
            if (
                not isinstance(item, tuple)
                or len(item) != 2
                or not isinstance(item[0], str)
                or not isinstance(item[1], str)
            ):
                raise HerdrIntegrationInstallError(
                    f"snapshot entry must be a (relpath, sha256) pair, got {item!r}"
                )
            if item[0] in seen:
                raise HerdrIntegrationInstallError(
                    f"snapshot has a duplicate path {item[0]!r}"
                )
            seen.add(item[0])

    @classmethod
    def of(cls, mapping: "dict[str, str]") -> "DirSnapshot":
        """Build a snapshot from a ``{relpath: sha256}`` mapping (sorted, stable)."""
        return cls(entries=tuple(sorted(mapping.items())))

    def as_dict(self) -> "dict[str, str]":
        return dict(self.entries)

    @property
    def paths(self) -> "frozenset[str]":
        return frozenset(path for path, _hash in self.entries)


@dataclass(frozen=True)
class SnapshotDiff:
    """The change between a pre- and post-apply snapshot (added / removed / changed)."""

    added: "tuple[str, ...]" = ()
    removed: "tuple[str, ...]" = ()
    changed: "tuple[str, ...]" = ()

    @property
    def is_empty(self) -> bool:
        return not (self.added or self.removed or self.changed)

    @property
    def touched(self) -> "tuple[str, ...]":
        """Every path the apply touched, sorted — the rollback work list."""
        return tuple(sorted({*self.added, *self.removed, *self.changed}))


def diff_snapshots(before: DirSnapshot, after: DirSnapshot) -> SnapshotDiff:
    """Compute the added / removed / changed paths between two snapshots (pure)."""
    before_map = before.as_dict()
    after_map = after.as_dict()
    added = tuple(sorted(after_map.keys() - before_map.keys()))
    removed = tuple(sorted(before_map.keys() - after_map.keys()))
    changed = tuple(
        sorted(
            path
            for path in before_map.keys() & after_map.keys()
            if before_map[path] != after_map[path]
        )
    )
    return SnapshotDiff(added=added, removed=removed, changed=changed)


def is_safe_config_dir(*, resolved: str, home_resolved: str) -> bool:
    """True iff ``resolved`` (a realpath) is safely contained in ``home_resolved``.

    The path-safety guard for symlink / traversal escapes: the config dir's realpath
    must be ``home_resolved`` itself or a descendant of it. A symlink whose target
    leaves home, or a ``..`` traversal, resolves outside and is rejected. Both
    arguments are already-``os.path.realpath``-ed by the ops layer; this predicate is
    pure string containment on normalized absolute paths so it is trivially testable.
    """
    if not resolved or not home_resolved:
        return False
    if resolved == home_resolved:
        return True
    prefix = home_resolved.rstrip("/") + "/"
    return resolved.startswith(prefix)


@dataclass(frozen=True)
class AgentInstallPlan:
    """The read-only plan for one agent: what would happen, or why it is gated.

    ``ready`` means every gate passed and an apply *would* invoke herdr for this
    agent. When ``ready`` is ``False``, ``reason`` is one of
    :data:`INSTALL_FAILURE_REASONS` and ``detail`` explains it. ``config_dir`` is the
    resolved (display) config path; ``herdr_argv`` is the exact argv an apply would
    run (surfaced in the plan so a dry-run shows the command, PoC E2 shape
    ``integration install <agent>``).
    """

    agent: str
    config_dir: str
    ready: bool
    reason: Optional[str] = None
    detail: str = ""
    herdr_argv: "tuple[str, ...]" = ()

    def __post_init__(self) -> None:
        if self.ready and self.reason is not None:
            raise HerdrIntegrationInstallError(
                "a ready agent plan may not carry a failure reason"
            )
        if not self.ready and self.reason not in INSTALL_FAILURE_REASONS:
            raise HerdrIntegrationInstallError(
                f"a gated agent plan must carry a reason from "
                f"{sorted(INSTALL_FAILURE_REASONS)}, got {self.reason!r}"
            )


@dataclass(frozen=True)
class AgentInstallOutcome:
    """The outcome of applying (or attempting) one agent's hook install.

    ``ok`` is the sole success authority. On success ``diff`` is the exact change
    herdr made to that agent's config dir; on failure ``reason`` is a closed reason.
    ``rolled_back`` records whether this agent's mutation was reverted (because a
    later agent failed the transaction).
    """

    agent: str
    config_dir: str
    ok: bool
    reason: Optional[str] = None
    detail: str = ""
    diff: Optional[SnapshotDiff] = None
    rolled_back: bool = False

    def __post_init__(self) -> None:
        if self.ok and self.reason is not None:
            raise HerdrIntegrationInstallError(
                "a successful agent outcome may not carry a failure reason"
            )
        if not self.ok and self.reason not in INSTALL_FAILURE_REASONS:
            raise HerdrIntegrationInstallError(
                f"a failed agent outcome must carry a reason from "
                f"{sorted(INSTALL_FAILURE_REASONS)}, got {self.reason!r}"
            )


@dataclass(frozen=True)
class InstallReport:
    """The whole installer verdict: overall ``ok`` + the per-agent plans/outcomes.

    ``applied`` distinguishes a read-only plan (``False``) from an executed apply
    (``True``). ``ok`` on a plan means every agent is ``ready``; on an apply it means
    every agent outcome is ``ok`` (and nothing was rolled back). A single failing
    agent makes the whole report not-``ok`` — partial success is never reported as
    success (issue #13249: "部分失敗は成功扱いしない").
    """

    applied: bool
    ok: bool
    plans: "tuple[AgentInstallPlan, ...]" = ()
    outcomes: "tuple[AgentInstallOutcome, ...]" = ()
    detail: str = ""
    pin_mode: Optional[str] = None


__all__ = (
    "AGENT_CLAUDE",
    "AGENT_CODEX",
    "AGENT_CONFIG_DIRNAME",
    "CREDENTIAL_SHAPED_PARTS",
    "INSTALL_FAILURE_REASONS",
    "INTEGRATION_AGENTS",
    "REASON_CONFIG_DIR_MISSING",
    "REASON_CONFIG_DIR_UNREADABLE",
    "REASON_HERDR_ERROR",
    "REASON_HERDR_UNRESOLVED",
    "REASON_NOT_OPTED_IN",
    "REASON_PARTIAL_FAILURE",
    "REASON_ROLLBACK_INCOMPLETE",
    "REASON_UNKNOWN_AGENT",
    "REASON_UNPINNED_REMOTE",
    "REASON_UNSAFE_CONFIG_PATH",
    "AgentInstallOutcome",
    "AgentInstallPlan",
    "DirSnapshot",
    "HerdrIntegrationInstallError",
    "InstallReport",
    "SnapshotDiff",
    "diff_snapshots",
    "is_credential_shaped",
    "is_safe_config_dir",
    "normalize_agents",
)
