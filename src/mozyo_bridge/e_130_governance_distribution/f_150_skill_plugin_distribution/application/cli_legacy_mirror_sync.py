"""CLI adapter for the legacy project skill mirror sync (Redmine #14580).

`scripts/sync_legacy_project_skill.sh` resolves the repo root and `src/`, then
execs this module. The wrapper carries no mirror logic — no pinned set, no
audit, no copy, no cleanup — so there is nothing there to drift from the
authority in :mod:`..domain.legacy_mirror_contract` (design consultation
answer, Redmine #14580 j#90402 contract 1 / decision 2).

Deliberately NOT a `mozyo-bridge` subcommand: the answer keeps the public CLI
surface unchanged for this issue. `mozyo-bridge release check drift` keeps
invoking the wrapper as a subprocess.

Argument handling is hand-rolled rather than argparse to preserve the wrapper's
established contract: bare invocation syncs, `--check` verifies, `-h`/`--help`
exits 0, and an unknown argument exits **64** (argparse would exit 2).

The repo root arrives on :data:`REPO_ROOT_ENV`, which the wrapper always sets,
and is **not** an accepted argument. Review j#90418 R6-F2 measured the earlier
`--repo` flag: the wrapper passed its own root first and then appended `"$@"`,
and the parser took the last value — so
`scripts/sync_legacy_project_skill.sh --check --repo <other-tree>` audited, and
in default mode would have written, a completely different checkout. An operator
argument must not be able to redirect where the tool writes; `--repo` is now an
unknown argument like any other.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from pathlib import Path

from .legacy_mirror_sync import LegacyProjectSkillMirrorSync

#: Internal channel for the repo root. The wrapper exports this unconditionally,
#: overwriting anything inherited, so an operator cannot aim the tool elsewhere.
REPO_ROOT_ENV = "MOZYO_LEGACY_MIRROR_REPO_ROOT"

USAGE = """Usage: scripts/sync_legacy_project_skill.sh [--check]

Without --check, replaces the mirrored reference set in
.claude/skills/mozyo-bridge-agent/references/ from canonical
(skills/mozyo-bridge-agent/references/).

With --check, verifies only and exits 1 if the mirror contract is violated.

The mirror's SKILL.md adapter stub is never written or compared.
The target checkout is fixed by the wrapper and cannot be selected here."""

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 64


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)

    check_only = False
    for arg in args:
        if arg == "--check":
            check_only = True
        elif arg in ("-h", "--help"):
            print(USAGE)
            return EXIT_OK
        else:
            print(f"unknown argument: {arg}", file=sys.stderr)
            print(USAGE, file=sys.stderr)
            return EXIT_USAGE

    raw_root = os.environ.get(REPO_ROOT_ENV)
    if not raw_root:
        print(
            f"{REPO_ROOT_ENV} is not set; run scripts/sync_legacy_project_skill.sh "
            "rather than this module directly.",
            file=sys.stderr,
        )
        return EXIT_USAGE
    repo_root = Path(raw_root)

    service = LegacyProjectSkillMirrorSync(repo_root)
    code, out_lines, err_lines = service.check() if check_only else service.sync()
    for line in out_lines:
        print(line)
    for line in err_lines:
        print(line, file=sys.stderr)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
