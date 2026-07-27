"""Legacy project Claude skill partial-mirror contract (Redmine #14580).

The repo ships a grace-period-deprecated legacy project skill at
`.claude/skills/mozyo-bridge-agent/`. Its `references/` directory is a
deliberately *partial* byte mirror of the canonical body at
`skills/mozyo-bridge-agent/references/`: only :data:`MIRRORED_REFERENCES` is
shipped, and `SKILL.md` is an intentional Claude Code adapter stub that this
contract never reads or writes. See
``vibes/docs/logics/skill-distribution.md`` -> ``Mirror Contract``.

This module is the **single authority** for that contract's vocabulary. It is
pure: no filesystem access, no I/O. The application layer observes the tree and
reports what it saw; everything about *what counts as a violation* and *which
recovery converges* is decided here, so both modes and the tests reason from
one definition.

Why a typed result rather than a pile of flags
----------------------------------------------
Six review rounds found the same fail-open — reporting success in the presence
of a state the sync cannot resolve — reached through a different axis each
time: the mode, the entry type, the path topology, the filename domain, the
canonical *source* side, and finally the composition of two classes
(j#90322 F1, j#90342 R2-F1, j#90360 R3-F1, j#90378 R4-F1/F2/F3, j#90397
R5-F1/F2/F3). The last round is the reason recovery is *derived from the
complete audit* instead of being emitted per class as each check runs: a
`source_bad` tree also reports content drift, and the per-class emission then
offered a resync that the sync itself refuses.

Diagnostics escape their filenames (:func:`describe_name`). A mirror entry
named with an embedded newline could otherwise forge additional report lines.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: The tracked partial-mirror reference set — the one definition. The shell
#: wrapper carries no copy of it (Redmine #14580 j#90402 contract 1), so there
#: is nothing to cross-check and nothing to drift.
MIRRORED_REFERENCES: tuple[str, ...] = (
    "project-map.md",
    "release.md",
    "safety.md",
    "workflow.md",
)

#: Repo-relative canonical body and mirror, as reported in diagnostics.
SOURCE_RELATIVE = "skills/mozyo-bridge-agent/references"
MIRROR_RELATIVE = ".claude/skills/mozyo-bridge-agent/references"

# --- rules -----------------------------------------------------------------
#
# A/B guard the canonical source, C-F the mirror. Ordered so that a violated
# prerequisite suppresses the checks that would misreport because of it.

RULE_SOURCE_TOPOLOGY = "A"  # every source path component is a real directory
RULE_SOURCE_ENTRIES = "B"  # every pinned source name is a regular file
RULE_DEST_TOPOLOGY = "C"  # every existing mirror path component is a real dir
RULE_DEST_ENTRY_SET = "D"  # every direct mirror entry is a pinned name
RULE_DEST_ENTRY_TYPES = "E"  # every pinned mirror entry is a regular file
RULE_CONTENT_PARITY = "F"  # every pinned mirror entry matches its source
#: Not a tree rule: the host itself cannot support the contract's primitives.
RULE_PLATFORM = "P"
#: Not a tree rule either: the write itself failed for a reason that is not a
#: statement about the tree's shape — permission, free space, a read-only
#: mount, an I/O error. Collapsing these into "the destination is no longer a
#: regular file" reported a fact that was not true and pointed at the wrong
#: recovery (j#90458 R8-F3).
RULE_WRITE = "W"

#: The rules the sync cannot repair. A breach of any of them means write zero.
#: Rule F is absent on purpose — repairing content drift IS the sync.
WRITE_BLOCKING_RULES: frozenset[str] = frozenset(
    {
        RULE_SOURCE_TOPOLOGY,
        RULE_SOURCE_ENTRIES,
        RULE_DEST_TOPOLOGY,
        RULE_DEST_ENTRY_SET,
        RULE_DEST_ENTRY_TYPES,
        RULE_PLATFORM,
        RULE_WRITE,
    }
)

#: Violation kinds, machine-readable so tests and callers never match prose.
PATH_COMPONENT_SYMLINK = "path_component_symlink"
PATH_COMPONENT_MISSING = "path_component_missing"
PATH_COMPONENT_NOT_DIRECTORY = "path_component_not_directory"
SOURCE_SYMLINK = "source_symlink"
SOURCE_MISSING = "source_missing"
SOURCE_NOT_REGULAR = "source_not_regular"
UNPINNED_ENTRY = "unpinned_entry"
ENTRY_SYMLINK = "entry_symlink"
ENTRY_NOT_REGULAR = "entry_not_regular"
ENTRY_MISSING = "entry_missing"
CONTENT_DRIFT = "content_drift"
MIRROR_MISSING = "mirror_missing"
SOURCE_SWAPPED_DURING_SYNC = "source_swapped_during_sync"
#: The tree could not be observed at all. An unreadable path is not "clean" and
#: not a crash: it is its own class with its own recovery (j#90418 R6-F3, where
#: a mode-000 canonical file escaped the typed result as a traceback).
PATH_UNREADABLE = "path_unreadable"
SOURCE_UNREADABLE = "source_unreadable"
ENTRY_UNREADABLE = "entry_unreadable"
#: The host cannot provide the no-follow / directory-fd primitives the write
#: path is built on. Fail closed rather than silently degrade to path-based I/O
#: (j#90418 R6-F1 correction condition 4).
PLATFORM_UNSUPPORTED = "platform_unsupported"
#: The write could not be completed, for a reason unrelated to entry type.
WRITE_FAILED = "write_failed"
#: The staging file could not be removed after a failed write, so it remains.
CLEANUP_FAILED = "cleanup_failed"

#: Kinds that mean "could not observe", as opposed to "observed something bad".
UNREADABLE_KINDS: frozenset[str] = frozenset(
    {PATH_UNREADABLE, SOURCE_UNREADABLE, ENTRY_UNREADABLE}
)

#: Recovery actions, most specific first. Precedence matters: a tree whose
#: source is broken must not be told to rerun the sync.
RECOVERY_PLATFORM_UNSUPPORTED = "platform_unsupported"
RECOVERY_RESTORE_SOURCE = "restore_source"
RECOVERY_RESTORE_MIRROR_PATH = "restore_mirror_path"
RECOVERY_RESTORE_ACCESS = "restore_access"
RECOVERY_DISPOSITION_UNPINNED = "disposition_unpinned"
RECOVERY_REPLACE_ENTRY = "replace_entry"
RECOVERY_WRITE_FAILED = "write_failed"
RECOVERY_CLEAR_RESIDUE = "clear_residue"
RECOVERY_RESYNC = "resync"

_RECOVERY_TEXT: dict[str, tuple[str, ...]] = {
    RECOVERY_PLATFORM_UNSUPPORTED: (
        "This host does not provide the no-follow / directory-descriptor primitives the",
        "mirror sync is built on, so it cannot guarantee that it writes inside the mirror.",
        "It refuses rather than fall back to path-based I/O, which is what allowed an",
        "aliased path to be written through. Run the sync on a POSIX host.",
    ),
    RECOVERY_WRITE_FAILED: (
        "The mirror could not be written. This is not a statement about the entry's type:",
        "check write permission on the mirror directory, free space, and whether the",
        "filesystem is mounted read-only, then rerun. Nothing outside the mirror was",
        "modified.",
    ),
    RECOVERY_CLEAR_RESIDUE: (
        "A staging file this sync created could not be removed and is still present. It",
        "will block the next run as an unpinned entry. It is this tool's own residue, so",
        "deleting it is safe once the underlying permission or filesystem problem is fixed.",
    ),
    RECOVERY_RESTORE_ACCESS: (
        "Part of the tree could not be read. Restore read access (and, for the canonical",
        "body, the tracked permissions) and rerun. An unreadable path is reported rather",
        "than skipped: skipping it would let an unobservable tree pass as clean.",
    ),
    RECOVERY_RESTORE_SOURCE: (
        f"The canonical body at {SOURCE_RELATIVE} must be real directories and regular files.",
        "Restore the tracked canonical path (git restore / checkout); do NOT point it at an",
        "external location. This refuses rather than mirror bytes from an aliased source,",
        "which would publish content the repo does not track.",
    ),
    RECOVERY_RESTORE_MIRROR_PATH: (
        f"The mirror path {MIRROR_RELATIVE} must be real directories all the way down.",
        "Replace the offending component with a real directory (or restore it from git).",
        "Rerunning the sync cannot fix the path it has to write into, and syncing through",
        "an alias would write outside the mirror.",
    ),
    RECOVERY_DISPOSITION_UNPINNED: (
        "Unpinned entries need a reviewed disposition: either delete them, or add them to",
        "MIRRORED_REFERENCES in",
        "src/mozyo_bridge/e_130_governance_distribution/f_150_skill_plugin_distribution/"
        "domain/legacy_mirror_contract.py.",
        "The sync never deletes them for you — including residue left by an interrupted",
        "run, which it cannot distinguish from a file someone meant to keep.",
    ),
    RECOVERY_REPLACE_ENTRY: (
        "Mirror references must be regular files — not symlinks, directories, FIFOs,",
        "sockets or devices. Replace the offending entry with a regular file (or delete",
        "it), then rerun the sync. Writing over a symlink or a hardlink would write",
        "through it into the link target.",
    ),
    RECOVERY_RESYNC: (
        "Rerun 'scripts/sync_legacy_project_skill.sh' (no --check, from the repo root)"
        " to resync the mirror.",
    ),
}


def describe_name(name: str) -> str:
    """Render a filename safely for a single-line diagnostic.

    ``repr`` escapes newlines, tabs and other control characters, so an entry
    named ``"a\\nlegacy project skill mirror is up to date"`` cannot forge a
    second report line (Redmine #14580 j#90402 contract 2).
    """
    return repr(name)


@dataclass(frozen=True)
class Violation:
    """One rule breach, with a machine-readable kind and a safe description."""

    rule: str
    kind: str
    #: Already-escaped subject (a filename via :func:`describe_name`, or a
    #: repo-relative path). Never interpolate a raw filename into this.
    subject: str
    note: str = ""

    def message(self) -> str:
        base = f"[{self.rule}/{self.kind}] {self.subject}"
        return f"{base}: {self.note}" if self.note else base


@dataclass(frozen=True)
class MirrorAudit:
    """The complete A-F observation of one tree.

    ``recovery_actions`` is derived from the whole result on purpose. Emitting
    a recovery as each class is discovered is what produced j#90397 R5-F3: a
    missing canonical source printed both "restore the source" and "rerun the
    sync", and the sync then refused at its source preflight.
    """

    violations: tuple[Violation, ...] = ()
    #: The mirror directory does not exist yet. Not a violation on its own —
    #: the sync creates it — but it must not be confused with an existing
    #: non-directory component, whose recovery is completely different
    #: (j#90378 R4-F3).
    dest_missing: bool = False
    #: Rules whose evaluation was suppressed by a failed prerequisite.
    skipped_rules: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.violations and not self.dest_missing

    def rules_violated(self) -> frozenset[str]:
        return frozenset(v.rule for v in self.violations)

    def kinds(self) -> frozenset[str]:
        return frozenset(v.kind for v in self.violations)

    def has_rule(self, rule: str) -> bool:
        return any(v.rule == rule for v in self.violations)

    @property
    def source_invalid(self) -> bool:
        return self.has_rule(RULE_SOURCE_TOPOLOGY) or self.has_rule(RULE_SOURCE_ENTRIES)

    @property
    def blocks_write(self) -> bool:
        """Whether the sync must write zero.

        Rules A-E are preconditions the sync cannot resolve, so any one of them
        blocks it entirely — never partially (Redmine #14580 j#90402 contract
        4). Rule F is deliberately excluded: content drift is precisely what the
        sync exists to repair, and ``dest_missing`` is the directory it is
        meant to create. Treating either as a blocker would make the command
        refuse its own job.
        """
        return any(v.rule in WRITE_BLOCKING_RULES for v in self.violations)

    def recovery_actions(self) -> tuple[str, ...]:
        """The actions that actually converge for this exact state.

        Only classes a rerun clears get :data:`RECOVERY_RESYNC`, and only when
        nothing upstream would make that rerun refuse.
        """
        actions: list[str] = []
        kinds = self.kinds()
        if self.has_rule(RULE_PLATFORM):
            # Keyed on the RULE, not the kind: keying it on the kind left any
            # other rule-P violation falling through to the resync line, which
            # the exhaustive blocking-rule test caught.
            return (RECOVERY_PLATFORM_UNSUPPORTED,)
        if kinds & UNREADABLE_KINDS:
            actions.append(RECOVERY_RESTORE_ACCESS)
        if self.source_invalid:
            actions.append(RECOVERY_RESTORE_SOURCE)
        if self.has_rule(RULE_DEST_TOPOLOGY):
            actions.append(RECOVERY_RESTORE_MIRROR_PATH)
        if self.has_rule(RULE_DEST_ENTRY_SET):
            actions.append(RECOVERY_DISPOSITION_UNPINNED)
        if self.has_rule(RULE_DEST_ENTRY_TYPES):
            actions.append(RECOVERY_REPLACE_ENTRY)
        if self.has_rule(RULE_WRITE):
            actions.append(RECOVERY_WRITE_FAILED)
        if CLEANUP_FAILED in kinds:
            actions.append(RECOVERY_CLEAR_RESIDUE)

        resync_clears = self.has_rule(RULE_CONTENT_PARITY) or self.dest_missing
        blocked_upstream = bool(actions)
        if resync_clears and not blocked_upstream:
            actions.append(RECOVERY_RESYNC)
        return tuple(actions)

    def recovery_lines(self) -> tuple[str, ...]:
        lines: list[str] = []
        for action in self.recovery_actions():
            lines.extend(_RECOVERY_TEXT[action])
        return tuple(lines)

    def report_lines(self) -> tuple[str, ...]:
        """Violation messages followed by the derived recovery, blank-separated."""
        lines = [v.message() for v in self.violations]
        if self.dest_missing:
            lines.append(
                Violation(
                    rule=RULE_DEST_TOPOLOGY,
                    kind=MIRROR_MISSING,
                    subject=MIRROR_RELATIVE,
                    note="mirror directory does not exist",
                ).message()
            )
        recovery = self.recovery_lines()
        if lines and recovery:
            lines.append("")
            lines.extend(recovery)
        return tuple(lines)

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "dest_missing": self.dest_missing,
            "skipped_rules": list(self.skipped_rules),
            "violations": [
                {
                    "rule": v.rule,
                    "kind": v.kind,
                    "subject": v.subject,
                    "note": v.note,
                }
                for v in self.violations
            ],
            "recovery_actions": list(self.recovery_actions()),
        }
