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


# === Pure state transitions (Redmine #14682) ===============================
#
# Everything below evaluates the machines characterised in
# ``vibes/docs/logics/legacy-mirror-failure-state-characterization.md`` §1.1
# (audit), §1.2 (write) and §1.3 (sync) **without touching a filesystem**. The
# application performs the syscalls and hands the *answers* here; which
# violation an answer means, which rule outranks which, whether the cleanup
# rail runs, and whether a rerun converges are decided in this module — they
# were previously reachable only by provoking a real OS failure.


# --- audit machine (§1.1) --------------------------------------------------


@dataclass(frozen=True)
class TreeObservation:
    """What the walk saw, with no judgement applied yet.

    The observer fills this in as it goes, consulting the ``*_observable`` /
    ``*_suppressed`` properties to decide whether the next observation is worth
    making. :meth:`evaluate` re-reads those same properties, so the
    ``skipped_rules`` it records cannot drift from the suppression that stopped
    the I/O — one predicate, read twice.
    """

    #: Non-empty when the host lacks a primitive the write path is built on.
    missing_capabilities: tuple[str, ...] = ()
    source_topology: tuple[Violation, ...] = ()
    source_missing: bool = False
    source_opened: bool = False
    source_entries: tuple[Violation, ...] = ()
    dest_topology: tuple[Violation, ...] = ()
    dest_missing: bool = False
    dest_opened: bool = False
    dest_entries: tuple[Violation, ...] = ()
    content: tuple[Violation, ...] = ()

    @property
    def platform_unsupported(self) -> bool:
        return bool(self.missing_capabilities)

    @property
    def platform_violations(self) -> tuple[Violation, ...]:
        if not self.missing_capabilities:
            return ()
        return (
            Violation(
                RULE_PLATFORM,
                PLATFORM_UNSUPPORTED,
                "host",
                "missing: " + ", ".join(self.missing_capabilities),
            ),
        )

    @property
    def source_violations(self) -> tuple[Violation, ...]:
        """Rule A/B. An absent canonical body is rule A, not "no entries":
        the recoveries differ, and reporting the pinned names as individually
        missing would point at the wrong one."""
        if self.source_missing:
            return (
                Violation(
                    RULE_SOURCE_TOPOLOGY,
                    PATH_COMPONENT_MISSING,
                    SOURCE_RELATIVE,
                    "canonical body is not present",
                ),
            )
        if self.source_opened:
            return self.source_entries
        return self.source_topology

    @property
    def dest_violations(self) -> tuple[Violation, ...]:
        """Rule C/D/E for the mirror side."""
        if self.dest_opened:
            return self.dest_entries
        return self.dest_topology

    @property
    def source_entries_observable(self) -> bool:
        return not self.source_missing and self.source_opened

    @property
    def dest_entries_observable(self) -> bool:
        return self.dest_opened

    @property
    def content_parity_suppressed(self) -> bool:
        """Whether rule F must not be evaluated at all. Parity against a
        broken or absent source reports a drift the sync cannot resolve, and
        the composite then advertised a resync that its own preflight refuses
        (j#90397 R5-F3)."""
        return bool(
            self.source_violations
            or self.dest_missing
            or not self.source_opened
            or not self.dest_opened
        )

    def evaluate(self) -> MirrorAudit:
        """Compose the A-F observation. Pure."""
        if self.platform_unsupported:
            # Rule P short-circuits: A-F never run, so none is "skipped".
            return MirrorAudit(violations=self.platform_violations)
        suppressed = self.content_parity_suppressed
        return MirrorAudit(
            violations=self.source_violations + self.dest_violations + self.content,
            dest_missing=self.dest_missing,
            skipped_rules=(RULE_CONTENT_PARITY,) if suppressed else (),
        )


def path_component_violation(
    rule: str, walked: str, *, unreadable: bool, symlink: bool, directory: bool
) -> Violation:
    """Say *why* a path component could not be opened as a real directory.

    Errno alone cannot tell a symlink from a plain non-directory — on macOS
    both give ENOTDIR under ``O_DIRECTORY | O_NOFOLLOW`` — so the caller
    re-inspects with a no-follow ``lstat`` and reports the facts here.
    """
    if unreadable:
        return Violation(rule, PATH_UNREADABLE, walked, "could not be inspected")
    if symlink:
        return Violation(
            rule,
            PATH_COMPONENT_SYMLINK,
            walked,
            "path components must be real directories, not symlinks",
        )
    if not directory:
        return Violation(rule, PATH_COMPONENT_NOT_DIRECTORY, walked, "exists but is not a directory")
    return Violation(rule, PATH_UNREADABLE, walked, "directory could not be opened")


def repo_root_unreadable(rule: str) -> Violation:
    """The anchor the operator invoked us with is not an accessible directory."""
    return Violation(rule, PATH_UNREADABLE, ".", "repository root is not an accessible directory")


def path_component_uncreatable(rule: str, walked: str) -> Violation:
    """A missing component the sync was allowed to create, and could not."""
    return Violation(rule, PATH_UNREADABLE, walked, "could not be created")


def entry_failure_kind(*, missing: bool, unreadable: bool, symlink: bool, regular: bool) -> str:
    """Classify a leaf open that failed: a TYPE problem or an access one.

    Collapsing every leaf-open ``OSError`` into "unreadable" reported a symlink
    and a socket — the two cases the open exists to reject — as rule F
    unreadable, non-blocking, advising a recovery that does not converge
    (j#90458 R8-F1). Absence is its own answer too: folding it in advised
    restoring access to a file that is simply gone (j#90467 R9-F4).
    """
    if missing:
        return ENTRY_MISSING
    if unreadable:
        return ENTRY_UNREADABLE
    if symlink:
        return ENTRY_SYMLINK
    if not regular:
        return ENTRY_NOT_REGULAR
    return ENTRY_UNREADABLE


#: A read failure on the canonical side keeps its meaning but changes subject.
_SOURCE_FAILURE_KINDS: dict[str, str] = {
    ENTRY_UNREADABLE: SOURCE_UNREADABLE,
    ENTRY_SYMLINK: SOURCE_SYMLINK,
    ENTRY_NOT_REGULAR: SOURCE_NOT_REGULAR,
    ENTRY_MISSING: SOURCE_MISSING,
}


def source_read_failure(failure_kind: str, name: str) -> Violation:
    """Rule B for a pinned canonical name that would not read as a regular file."""
    return Violation(
        RULE_SOURCE_ENTRIES,
        _SOURCE_FAILURE_KINDS.get(failure_kind, SOURCE_UNREADABLE),
        f"{SOURCE_RELATIVE}/{name}",
        "could not be read as a regular file",
    )


def mirror_read_failure(failure_kind: str, subject: str) -> Violation:
    """Rule E or F for a pinned mirror entry that would not read.

    A type failure discovered here is the same defect rule E reports, just
    found a moment later — so it must carry rule E's weight. Filing it under
    rule F left it out of the write-blocking set, and the recovery then said
    "resync" while the sync's own preflight refused the identical tree
    (j#90450 R7-F2).
    """
    rule = (
        RULE_DEST_ENTRY_TYPES
        if failure_kind in (ENTRY_NOT_REGULAR, ENTRY_SYMLINK)
        else RULE_CONTENT_PARITY
    )
    return Violation(rule, failure_kind, subject, "could not be read as a regular file")


def pinned_source_violation(
    name: str, *, missing: bool, unreadable: bool, symlink: bool, regular: bool
) -> Violation | None:
    """Rule B for one pinned canonical name. ``None`` when it is well-formed."""
    subject = f"{SOURCE_RELATIVE}/{name}"
    if missing:
        return Violation(
            RULE_SOURCE_ENTRIES, SOURCE_MISSING, subject, "pinned canonical reference is missing"
        )
    if unreadable:
        return Violation(RULE_SOURCE_ENTRIES, SOURCE_UNREADABLE, subject, "could not be inspected")
    if symlink:
        return Violation(
            RULE_SOURCE_ENTRIES,
            SOURCE_SYMLINK,
            subject,
            "canonical references must be regular files, not symlinks",
        )
    if not regular:
        return Violation(RULE_SOURCE_ENTRIES, SOURCE_NOT_REGULAR, subject, "is not a regular file")
    return None


def pinned_mirror_violation(
    name: str, *, missing: bool, unreadable: bool, symlink: bool, regular: bool
) -> Violation | None:
    """Rule E for one pinned mirror entry. ``None`` when it is well-formed.

    An absent entry is *not* rule E: rule F reports it, and that is the class
    the sync can actually repair."""
    if missing:
        return None
    subject = mirror_subject(name)
    if unreadable:
        return Violation(RULE_DEST_ENTRY_TYPES, ENTRY_UNREADABLE, subject, "could not be inspected")
    if symlink:
        return Violation(
            RULE_DEST_ENTRY_TYPES,
            ENTRY_SYMLINK,
            subject,
            "mirror references must be regular files, not symlinks",
        )
    if not regular:
        return Violation(RULE_DEST_ENTRY_TYPES, ENTRY_NOT_REGULAR, subject, "is not a regular file")
    return None


def mirror_subject(name: str) -> str:
    """A mirror entry as it appears in diagnostics, with the name escaped."""
    return f"{MIRROR_RELATIVE}/{describe_name(name)}"


def unpinned_entry_violation(name: str) -> Violation:
    """Rule D: an entry the partial mirror does not pin."""
    return Violation(
        RULE_DEST_ENTRY_SET, UNPINNED_ENTRY, mirror_subject(name), "not in the pinned partial mirror set"
    )


def mirror_listing_failure() -> Violation:
    """Rule D could not be evaluated because the directory would not list."""
    return Violation(
        RULE_DEST_ENTRY_SET, PATH_UNREADABLE, MIRROR_RELATIVE, "directory could not be listed"
    )


def rule_e_subjects(dest_violations: tuple[Violation, ...]) -> frozenset[str]:
    """Mirror entries rule E already condemned, so rule F must not re-report them."""
    return frozenset(v.subject for v in dest_violations if v.rule == RULE_DEST_ENTRY_TYPES)


_CONTENT_NOTES: dict[str, str] = {
    ENTRY_MISSING: "mirrored reference is absent",
    ENTRY_UNREADABLE: "could not be inspected",
    CONTENT_DRIFT: "differs from canonical",
}


def content_violation(kind: str, subject: str) -> Violation:
    """Rule F: absent, unobservable, or drifted — all three the sync repairs."""
    return Violation(RULE_CONTENT_PARITY, kind, subject, _CONTENT_NOTES[kind])


# --- write machine (characterization §1.2 / §1.5 / §1.6) -------------------

#: Answers the ownership proof can give about the staging name. An answer this
#: module does not recognise is treated as :data:`OWNERSHIP_UNPROVEN`, which
#: never unlinks — an unrecognised answer reaching the unlink is exactly the
#: fail-open shape the cleanup rail exists to refuse.
OWNERSHIP_CONFIRMED = "confirmed"
OWNERSHIP_ABSENT = "absent"
OWNERSHIP_FOREIGN = "foreign"
OWNERSHIP_UNREADABLE = "unreadable"
OWNERSHIP_UNPROVEN = "unproven"

#: What the staging name holds once the rail has given up on it. Three values,
#: not two: "could not observe it" is not "it is there" (§1.6). Rounding
#: unknown up demands an operator who may not be needed; rounding it down
#: claims a clean tree nobody saw.
RESIDUE_ABSENT = "absent"
RESIDUE_UNKNOWN = "unknown"
RESIDUE_PRESENT = "present"

#: Whether rerunning the sync converges from that residue.
RETRY_CONVERGES = "converges"
RETRY_UNDETERMINED = "undetermined"
RETRY_NEEDS_OPERATOR = "needs_operator"


#: Ordered by how much they constrain a rerun. An unrecognised cleanup report
#: resolves to :data:`RESIDUE_UNKNOWN`, never to either end.
_RESIDUE_RANK: dict[str, int] = {
    RESIDUE_ABSENT: 0,
    RESIDUE_UNKNOWN: 1,
    RESIDUE_PRESENT: 2,
}


def retry_admissibility(residue: str) -> str:
    """Whether a rerun converges from ``residue`` (§1.6).

    Keyed on the residue, never on whether a :data:`CLEANUP_FAILED` was
    emitted. Those disagree in **both** directions: a foreign entry left at the
    staging name emits none yet blocks the next run as an unpinned entry, and
    an unreadable one emits one while saying its presence is unknown.
    """
    if residue == RESIDUE_ABSENT:
        return RETRY_CONVERGES
    if residue == RESIDUE_UNKNOWN:
        # The next run repeats the same `lstat`; if the condition cleared it
        # observes an absent name and converges. This run cannot say.
        return RETRY_UNDETERMINED
    return RETRY_NEEDS_OPERATOR


@dataclass(frozen=True)
class SwapDecision:
    """Whether the staging entry may be renamed into place (§1.2 W6-W9)."""

    #: The rename may run.
    proceed: bool
    #: Route the failure through the cleanup rail.
    release: bool
    #: Still ours. ``False`` means *never touch it* — not "already tidy".
    owned: bool
    violations: tuple[Violation, ...] = ()
    #: Set only when the rail stops without releasing; otherwise the residue is
    #: whatever the release (or the rename) leaves behind.
    residue: str | None = None


def swap_decision(resolved: str, staging_subject: str) -> SwapDecision:
    """React to the ownership answer taken immediately before the rename.

    ``os.replace`` renames whatever the NAME refers to, and it does not follow
    symlinks — so a name re-bound between create and swap gets the foreign
    entry installed as a pinned reference (j#90418 R6-F1 case 4). Only
    :data:`OWNERSHIP_CONFIRMED` may proceed. Not observing the entry is not
    evidence that it is foreign: those answers still route through the cleanup
    rail, which re-proves ownership and leaves anything that is not ours
    untouched — safer than leaving guaranteed residue (j#90472 R10-F2).
    """
    if resolved == OWNERSHIP_CONFIRMED:
        return SwapDecision(proceed=True, release=False, owned=True)
    if resolved == OWNERSHIP_FOREIGN:
        # Not ours: never unlink it, never claim we tidied up. It stays at the
        # staging name and blocks the next run as rule D.
        return SwapDecision(
            proceed=False,
            release=False,
            owned=False,
            violations=(
                Violation(
                    RULE_WRITE,
                    WRITE_FAILED,
                    staging_subject,
                    "staging entry was rebound while the sync held it",
                ),
            ),
            residue=RESIDUE_PRESENT,
        )
    if resolved == OWNERSHIP_ABSENT:
        detail = "staging entry was gone before it could be installed"
    elif resolved == OWNERSHIP_UNREADABLE:
        detail = "staging entry could not be re-validated"
    else:
        detail = "staging entry's ownership could not be proved"
    return SwapDecision(
        proceed=False,
        release=True,
        owned=True,
        violations=(Violation(RULE_WRITE, WRITE_FAILED, staging_subject, detail),),
    )


@dataclass(frozen=True)
class ReleaseDecision:
    """What the cleanup rail may do, and what it leaves behind (§1.5)."""

    unlink: bool
    violations: tuple[Violation, ...] = ()
    #: ``None`` only while ``unlink`` is true: the answer is then the unlink's,
    #: read back through :func:`unlink_outcome`.
    residue: str | None = None


#: Cleanup diagnostics, named so that the residue table below and the
#: violations themselves are built from one string each. A caller reading a
#: cleanup report back into a residue must not re-spell these.
_CLEANUP_UNINSPECTABLE = "staging file could not be inspected and may still be present"
_CLEANUP_FOREIGN = "the staging name now refers to another entry, which was left untouched"
_CLEANUP_UNPROVEN = "the staging entry's ownership could not be proved, so it was left in place"
_CLEANUP_NOT_REMOVED = "staging file could not be removed and is still present"

_CLEANUP_RESIDUE: dict[str, str] = {
    # "may still be present" is not "is present": the `lstat` failed, so the
    # entry's existence was never observed.
    _CLEANUP_UNINSPECTABLE: RESIDUE_UNKNOWN,
    _CLEANUP_FOREIGN: RESIDUE_PRESENT,
    _CLEANUP_UNPROVEN: RESIDUE_PRESENT,
    _CLEANUP_NOT_REMOVED: RESIDUE_PRESENT,
}


def cleanup_residue(violations: tuple[Violation, ...]) -> str:
    """Read a cleanup report back into what the staging name now holds.

    Silence means the name is empty — the rail reports every answer that leaves
    something behind. Where several are reported the most constraining one
    wins, so a rerun is never advertised on a quieter sibling's strength.
    """
    residue = RESIDUE_ABSENT
    for violation in violations:
        if violation.kind != CLEANUP_FAILED:
            continue
        answer = _CLEANUP_RESIDUE.get(violation.note, RESIDUE_UNKNOWN)
        if _RESIDUE_RANK[answer] > _RESIDUE_RANK[residue]:
            residue = answer
    return residue


def release_decision(resolved: str, display: str) -> ReleaseDecision:
    """Decide whether this run's staging name may be removed.

    **Only :data:`OWNERSHIP_CONFIRMED` unlinks.** Falling through on an answer
    this module does not recognise is fail-open, and this is the value the
    release consults *before* deleting anything (j#90467 R9-F2, where unlinking
    by name alone deleted a foreign entry substituted at that name).
    """
    if resolved == OWNERSHIP_ABSENT:
        # Nothing to remove, and nothing to report: claiming residue the caller
        # had already cleared is its own defect (j#90467 R9-F3).
        return ReleaseDecision(unlink=False, residue=RESIDUE_ABSENT)
    if resolved == OWNERSHIP_CONFIRMED:
        return ReleaseDecision(unlink=True)
    if resolved == OWNERSHIP_UNREADABLE:
        # The `lstat` itself failed, so the entry's very existence is unknown.
        # The text says so, and the residue must agree with the text.
        detail = _CLEANUP_UNINSPECTABLE
    elif resolved == OWNERSHIP_FOREIGN:
        detail = _CLEANUP_FOREIGN
    else:
        detail = _CLEANUP_UNPROVEN
    return ReleaseDecision(
        unlink=False,
        violations=(Violation(RULE_WRITE, CLEANUP_FAILED, display, detail),),
        residue=_CLEANUP_RESIDUE[detail],
    )


def unlink_outcome(*, removed: bool, display: str) -> ReleaseDecision:
    """Read back the unlink :func:`release_decision` authorised. ``removed``
    covers the entry having gone in the meantime: the name is empty either
    way."""
    if removed:
        return ReleaseDecision(unlink=False, residue=RESIDUE_ABSENT)
    return ReleaseDecision(
        unlink=False,
        violations=(Violation(RULE_WRITE, CLEANUP_FAILED, display, _CLEANUP_NOT_REMOVED),),
        residue=_CLEANUP_RESIDUE[_CLEANUP_NOT_REMOVED],
    )


def source_swapped_during_sync(name: str) -> Violation:
    """W0: the canonical entry stopped reading as a regular file at write time."""
    return Violation(
        RULE_SOURCE_ENTRIES,
        SOURCE_SWAPPED_DURING_SYNC,
        f"{SOURCE_RELATIVE}/{name}",
        "could not be read as a regular file at write time",
    )


def staging_creation_failure() -> Violation:
    """W1: the exclusive create never produced a staging file."""
    return Violation(RULE_WRITE, WRITE_FAILED, MIRROR_RELATIVE, "staging file could not be created")


def staging_write_failure(subject: str, *, flushing: bool) -> Violation:
    """W2-W5: the staging file could not be produced.

    ``flushing`` separates the deferred write error the ``fsync`` reports from
    the write itself. The close used to sit where the ``fsync`` now does; it
    had to move last so the ownership proof stays sound (#14652).
    """
    return Violation(
        RULE_WRITE,
        WRITE_FAILED,
        subject,
        "staging file could not be flushed to disk" if flushing else "staging file could not be written",
    )


def replace_failure(failure_kind: str, subject: str) -> Violation:
    """W10/W11: say what actually stopped the rename.

    Reporting every failure as "it is no longer a regular file" stated a fact
    that was often untrue — an injected ``PermissionError`` produced exactly
    that against a destination that was still a regular file — and it pointed
    at the wrong recovery (j#90458 R8-F3).
    """
    if failure_kind in (ENTRY_SYMLINK, ENTRY_NOT_REGULAR):
        return Violation(
            RULE_DEST_ENTRY_TYPES,
            failure_kind,
            subject,
            "could not be replaced; it is no longer a regular file",
        )
    return Violation(RULE_WRITE, WRITE_FAILED, subject, "could not be replaced")


def staging_close_failure(subject: str) -> Violation:
    """W14: the staging descriptor did not close cleanly.

    Discarding a close result folded a real deferred write error into a
    `synced` banner and exit 0 (j#90467 R9-F1). Nothing is written between the
    ``fsync`` and the close, so this is a backstop — but a backstop that
    returns silence is not one.
    """
    return Violation(RULE_WRITE, WRITE_FAILED, subject, "staging file could not be closed cleanly")


@dataclass(frozen=True)
class WriteOutcome:
    """One pinned reference's trip through the write rail (§1.2).

    ``violations`` is what the operator sees; ``residue`` is what the *next*
    run finds at the staging name. Separate because they disagree — see
    :func:`retry_admissibility`.
    """

    violations: tuple[Violation, ...] = ()
    residue: str = RESIDUE_ABSENT

    @property
    def failed(self) -> bool:
        return bool(self.violations)

    @property
    def retry(self) -> str:
        return retry_admissibility(self.residue)


# --- sync machine (characterization §1.3) ----------------------------------

SYNC_REFUSED = "refused"
SYNC_ABORTED = "aborted"
SYNC_DIVERGED = "diverged"
SYNC_SYNCED = "synced"
CHECK_CLEAN = "clean"
CHECK_VIOLATED = "violated"


@dataclass(frozen=True)
class CommandOutcome:
    """A terminal state of :meth:`check` / :meth:`sync`, with its report."""

    state: str
    exit_code: int
    stdout: tuple[str, ...] = ()
    stderr: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[int, tuple[str, ...], tuple[str, ...]]:
        return self.exit_code, self.stdout, self.stderr


def check_outcome(audit: MirrorAudit, source_dir: str, mirror_dir: str) -> CommandOutcome:
    """Read-only: the tree is either clean or it is reported."""
    if audit.ok:
        return CommandOutcome(
            CHECK_CLEAN,
            0,
            stdout=(
                "legacy project skill mirror is up to date",
                f"  source: {source_dir}",
                f"  destination: {mirror_dir}",
            ),
        )
    return CommandOutcome(CHECK_VIOLATED, 1, stderr=audit.report_lines())


def sync_refused(audit: MirrorAudit) -> CommandOutcome:
    """Preflight said write zero, so nothing was written — say exactly that."""
    return CommandOutcome(
        SYNC_REFUSED,
        1,
        stderr=(
            "refusing to sync the legacy project skill mirror; nothing was written.",
            *audit.report_lines(),
        ),
    )


def sync_aborted(violations: tuple[Violation, ...]) -> CommandOutcome:
    """A write rail failed part-way through the pinned set.

    This partiality is not the one :attr:`MirrorAudit.blocks_write` refuses:
    that is a preflight claim, and by here earlier names may already be
    installed. The closing advice is written for that."""
    return CommandOutcome(
        SYNC_ABORTED,
        1,
        stderr=(
            "aborted the legacy project skill mirror sync.",
            *MirrorAudit(violations=violations).report_lines(),
            "",
            "The tree changed underneath the sync, or the write could not",
            "complete. Nothing outside the mirror was modified. Re-run once the",
            "tracked paths are stable.",
        ),
    )


def sync_diverged(audit: MirrorAudit) -> CommandOutcome:
    """Never announce success on an unverified tree: the re-audit disagreed."""
    return CommandOutcome(
        SYNC_DIVERGED,
        1,
        stderr=(
            "the legacy project skill mirror did not converge after syncing.",
            *audit.report_lines(),
        ),
    )


def sync_succeeded(source_dir: str, mirror_dir: str) -> CommandOutcome:
    return CommandOutcome(
        SYNC_SYNCED,
        0,
        stdout=(
            "synced legacy project skill mirror",
            f"  source: {source_dir}",
            f"  destination: {mirror_dir}",
            f"  references: {' '.join(MIRRORED_REFERENCES)}",
            "  SKILL.md adapter stub left untouched (intentional divergence)",
        ),
    )
