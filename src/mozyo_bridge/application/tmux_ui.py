"""Host-side tmux UI wiring (install / uninstall / status).

Adds a managed source-file block to a host tmux config (default
``~/.tmux.conf``) that conditionally sources the repo-local snippet at
``<repo>/.mozyo-bridge/tmux/agent-ui.conf``.

Design contract:

- The host config is never overwritten wholesale. Only the literal block
  between the begin / end markers is created, replaced, or removed.
- The block uses ``if-shell`` so a missing repo path does not break tmux
  startup on the host (relevant when ``~/.tmux.conf`` is shared between
  machines or when the repo is moved).
- ``install`` is idempotent on the same repo path; re-running on the
  same target makes no change. Drift (existing block points at a
  different path) requires ``--force`` to overwrite, which surfaces the
  intent in the operator's command history.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from time import strftime
from typing import Any

TMUX_UI_RELATIVE_PATH = Path(".mozyo-bridge/tmux/agent-ui.conf")
MANAGED_BLOCK_BEGIN = "# >>> mozyo-bridge tmux-ui >>>"
MANAGED_BLOCK_END = "# <<< mozyo-bridge tmux-ui <<<"
# Canonical source-path metadata line. The path on this line is the
# byte-literal absolute path (no escapes); the `if-shell` directive
# below it carries the same path but with shell + tmux quoting applied.
# Status parsing reads this comment, not the `if-shell` line, so escape
# sequences in the directive never round-trip through the comparator.
SOURCE_COMMENT_PREFIX = "# mozyo-bridge tmux-ui source: "
DEFAULT_HOST_TMUX_CONF = "~/.tmux.conf"

STATE_NOT_INSTALLED = "not-installed"
STATE_INSTALLED = "installed"
STATE_DRIFT = "drift"


def default_host_tmux_conf() -> Path:
    """Return the default host tmux config path."""
    return Path(DEFAULT_HOST_TMUX_CONF).expanduser()


def resolve_host_tmux_conf(path: str | Path | None) -> Path:
    if path is None:
        return default_host_tmux_conf()
    return Path(path).expanduser()


def snippet_path_for_repo(repo_root: Path) -> Path:
    return (Path(repo_root).expanduser().resolve() / TMUX_UI_RELATIVE_PATH)


def _validate_quote_safety(absolute: str) -> None:
    """Reject paths that the managed block cannot represent safely.

    The ``source-file`` directive in the rendered block uses tmux
    single quotes around the path (literal inside ``if-shell``'s
    re-parsed command argv). tmux single quotes have no escape
    mechanism whatsoever — a single quote inside a tmux SQ region
    closes it, so we can never round-trip a literal ``'`` through
    this representation. Newlines / carriage returns would also break
    the single-line tmux config form.
    """
    if "'" in absolute:
        raise TmuxUiError(
            f"snippet path {absolute!r} contains a single quote; tmux config "
            "strings cannot embed a single quote safely. Move the repository "
            "to a path without a single quote."
        )
    if "\n" in absolute or "\r" in absolute:
        raise TmuxUiError(
            f"snippet path {absolute!r} contains a newline; tmux config "
            "strings cannot embed a newline. Move the repository to a path "
            "without a newline."
        )


def _shell_dquote_escape(value: str) -> str:
    """Escape ``value`` for inclusion inside SHELL double quotes.

    POSIX shell double-quoted strings treat ``\\``, ``"``, ``$``, and
    backtick as escapable specials; every other byte is literal.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
        .replace("`", "\\`")
    )


def _tmux_dquote_escape(value: str) -> str:
    """Escape ``value`` for inclusion inside a TMUX double-quoted argument.

    tmux double-quoted command-line arguments recognise three
    backslash escapes — ``\\\\``, ``\\"``, ``\\$`` — and otherwise
    pass content through to the next stage (command re-parse for an
    ``if-shell`` body, or shell + format-expand for an ``if-shell``
    shell-command). Other bytes (including ``#`` and ``'``) are
    literal.
    """
    return (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("$", "\\$")
    )


def _format_string_escape(value: str) -> str:
    """Double-up ``#`` so it survives a tmux format-string expansion pass.

    ``if-shell``'s *shell-command* argument is passed through tmux's
    format expansion before being handed to ``/bin/sh -c``. The
    expansion turns ``#h`` / ``#H`` / ``#{...}`` / ``#(...)`` into
    substitutions; ``##`` is the documented literal-``#`` escape. The
    second argument (the tmux command body) is *not* format-expanded,
    so this transform is intentionally one-way for the shell side
    only.
    """
    return value.replace("#", "##")


def render_managed_block(snippet_path: Path) -> str:
    """Render the managed block as a newline-terminated string.

    The block uses tmux's ``if-shell`` so a missing snippet path
    silently no-ops instead of aborting tmux config load on operators
    who share ``~/.tmux.conf`` across machines or who move the repo.

    The two ``if-shell`` arguments are quoted differently because tmux
    treats them differently at runtime:

    - The first argument (the shell-command) is format-expanded by
      tmux before being passed to ``/bin/sh -c``. Any ``#`` in this
      argument is doubled to ``##`` so it survives format expansion
      as a literal ``#``. The path then sits inside a SHELL
      double-quoted string with ``\\`` / ``\\"`` / ``\\$`` / `\\``
      escapes. The whole shell command is wrapped in a TMUX
      double-quoted argument with its own ``\\`` / ``\\"`` / ``\\$``
      escapes.
    - The second argument (the tmux command body, ``source-file
      '<path>'``) is *not* format-expanded — tmux only re-parses it
      as a command line when the if-shell condition is truthy. The
      path is wrapped in tmux single quotes (literal) inside a tmux
      double-quoted outer with ``\\`` / ``\\"`` / ``\\$`` escapes.

    Paths containing a single quote or a newline are rejected up-front
    by ``_validate_quote_safety`` because the tmux SQ region around
    the path in the second argument cannot round-trip those bytes.

    A canonical ``# mozyo-bridge tmux-ui source: <path>`` comment
    carries the byte-literal absolute path so status parsing has a
    handle that does not depend on escape sequences; the ``if-shell``
    directive is the operational mechanism, the comment is the
    source-of-truth for comparators.
    """
    absolute = str(Path(snippet_path).expanduser().resolve())
    _validate_quote_safety(absolute)

    # arg1 (shell test): path lives inside shell-DQ inside tmux-DQ,
    # and the whole arg goes through tmux format-expand, so we
    # ##-escape `#` and then layer the tmux-DQ escapes.
    shell_cmd = f'test -f "{_shell_dquote_escape(absolute)}"'
    shell_cmd_format_safe = _format_string_escape(shell_cmd)
    arg1 = f'"{_tmux_dquote_escape(shell_cmd_format_safe)}"'

    # arg2 (tmux source-file): path lives inside tmux-SQ (literal)
    # inside tmux-DQ. No format-expand applies here, so `#` stays
    # literal; only the outer tmux-DQ escapes are needed and the
    # inner SQ keeps `#` / `$` / `"` from being touched again.
    tmux_cmd = f"source-file '{absolute}'"
    arg2 = f'"{_tmux_dquote_escape(tmux_cmd)}"'

    lines = [
        MANAGED_BLOCK_BEGIN,
        "# Managed by `mozyo-bridge tmux-ui install`. Do not edit by hand.",
        "# Re-run install to update; remove with `mozyo-bridge tmux-ui uninstall`.",
        f"{SOURCE_COMMENT_PREFIX}{absolute}",
        f"if-shell {arg1} {arg2}",
        MANAGED_BLOCK_END,
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class _BlockSpan:
    begin_line: int
    end_line: int
    text: str
    source_path: str | None


def _find_block(text: str) -> _BlockSpan | None:
    lines = text.splitlines(keepends=True)
    begin_idx: int | None = None
    end_idx: int | None = None
    for idx, line in enumerate(lines):
        stripped = line.rstrip("\n")
        if begin_idx is None and stripped == MANAGED_BLOCK_BEGIN:
            begin_idx = idx
        elif begin_idx is not None and stripped == MANAGED_BLOCK_END:
            end_idx = idx
            break
    if begin_idx is None or end_idx is None:
        return None
    block_lines = lines[begin_idx : end_idx + 1]
    block_text = "".join(block_lines)
    return _BlockSpan(
        begin_line=begin_idx,
        end_line=end_idx,
        text=block_text,
        source_path=_extract_source_path(block_text),
    )


def _extract_source_path(block_text: str) -> str | None:
    """Return the canonical source path recorded in the block.

    Reads the dedicated ``# mozyo-bridge tmux-ui source: <path>``
    comment line, which carries the byte-literal absolute path. The
    ``if-shell`` directive on the next line carries the same value but
    after shell + tmux quoting; using the comment as the parse target
    keeps comparators escape-sequence-agnostic.

    Returns ``None`` when the block lacks the comment (older block
    shape or hand-edited corruption); callers then surface that as
    drift.
    """
    for raw in block_text.splitlines():
        if raw.startswith(SOURCE_COMMENT_PREFIX):
            return raw[len(SOURCE_COMMENT_PREFIX):]
    return None


def _strip_block(text: str, span: _BlockSpan) -> str:
    """Remove the literal block span and exactly one trailing newline.

    The block already ends with ``\n``; if the very next character is
    another ``\n`` (caused by the blank-line spacing install added
    above it) the resulting file would carry an extra blank line. Drop
    one such adjacent newline so install / uninstall is byte-stable.
    """
    lines = text.splitlines(keepends=True)
    before = "".join(lines[: span.begin_line])
    after = "".join(lines[span.end_line + 1 :])

    # If install added a leading blank-line separator above the block
    # (which it does when appending to a non-empty existing file), strip
    # that one separator so uninstall reverts byte-for-byte.
    if before.endswith("\n\n"):
        before = before[:-1]
    return before + after


def _append_block(text: str, block: str) -> str:
    """Append the block to ``text``, ensuring a blank-line separator.

    Empty file → block only. Otherwise ensure the file ends with a
    trailing newline plus exactly one blank-line separator before the
    block. Always end with the block's own trailing newline.
    """
    if not text:
        return block
    if not text.endswith("\n"):
        text = text + "\n"
    # Single blank line of separation between existing content and the
    # managed block; don't pile up newlines if the file already ends
    # with one or more blank lines.
    if not text.endswith("\n\n"):
        text = text + "\n"
    return text + block


def _backup_path(path: Path) -> Path:
    return path.with_name(f"{path.name}.bak.{strftime('%Y%m%d%H%M%S')}")


def compute_status(repo_root: Path, tmux_conf: Path) -> dict[str, Any]:
    """Return a status payload describing host wiring against ``repo_root``.

    Pure computation: no file writes. The payload includes the resolved
    host config path, the resolved expected snippet path, presence
    flags, and an aggregate ``state`` (``not-installed`` / ``installed``
    / ``drift``).
    """
    expected_snippet = snippet_path_for_repo(repo_root)
    expected_str = str(expected_snippet)
    snippet_exists = expected_snippet.exists()

    if not tmux_conf.exists():
        return {
            "state": STATE_NOT_INSTALLED,
            "tmux_conf": str(tmux_conf),
            "tmux_conf_exists": False,
            "expected_snippet": expected_str,
            "snippet_exists": snippet_exists,
            "current_source_path": None,
            "drift_reason": None,
        }

    try:
        text = tmux_conf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {
            "state": STATE_NOT_INSTALLED,
            "tmux_conf": str(tmux_conf),
            "tmux_conf_exists": True,
            "tmux_conf_unreadable": True,
            "error": str(exc),
            "expected_snippet": expected_str,
            "snippet_exists": snippet_exists,
            "current_source_path": None,
            "drift_reason": None,
        }

    span = _find_block(text)
    if span is None:
        return {
            "state": STATE_NOT_INSTALLED,
            "tmux_conf": str(tmux_conf),
            "tmux_conf_exists": True,
            "expected_snippet": expected_str,
            "snippet_exists": snippet_exists,
            "current_source_path": None,
            "drift_reason": None,
        }

    current = span.source_path
    drift_reason: str | None = None
    if current is None:
        drift_reason = "managed block present but no source-file path could be parsed"
    elif current != expected_str:
        drift_reason = (
            f"managed block points to {current} but the resolved repo "
            f"snippet is {expected_str}"
        )
    elif not snippet_exists:
        drift_reason = f"managed block points to {expected_str} but the snippet file is missing"

    state = STATE_INSTALLED if drift_reason is None else STATE_DRIFT
    return {
        "state": state,
        "tmux_conf": str(tmux_conf),
        "tmux_conf_exists": True,
        "expected_snippet": expected_str,
        "snippet_exists": snippet_exists,
        "current_source_path": current,
        "drift_reason": drift_reason,
    }


@dataclass
class InstallResult:
    changed: bool
    action: str  # "created" | "appended" | "replaced" | "noop"
    state_before: str
    state_after: str
    tmux_conf: Path
    expected_snippet: Path
    previous_source_path: str | None
    backup_path: Path | None
    dry_run: bool


@dataclass
class UninstallResult:
    changed: bool
    action: str  # "removed" | "noop"
    state_before: str
    state_after: str
    tmux_conf: Path
    backup_path: Path | None
    dry_run: bool


class TmuxUiError(RuntimeError):
    """User-actionable error during install / uninstall."""


def apply_install(
    *,
    repo_root: Path,
    tmux_conf: Path,
    force: bool = False,
    dry_run: bool = False,
    backup: bool = False,
) -> InstallResult:
    expected_snippet = snippet_path_for_repo(repo_root)
    if not expected_snippet.exists():
        raise TmuxUiError(
            f"snippet {expected_snippet} is missing. Run "
            f"`mozyo-bridge scaffold apply <preset> --target {repo_root}` first "
            "(or omit --skip-tmux-ui)."
        )

    block = render_managed_block(expected_snippet)
    expected_str = str(expected_snippet)

    tmux_conf_existed = tmux_conf.exists()
    original_text = ""
    if tmux_conf_existed:
        try:
            original_text = tmux_conf.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise TmuxUiError(
                f"cannot read existing tmux config {tmux_conf}: {exc}"
            ) from exc

    existing = _find_block(original_text)
    state_before = compute_status(repo_root, tmux_conf)["state"]

    if existing is None:
        if not tmux_conf_existed:
            new_text = block
            action = "created"
        else:
            new_text = _append_block(original_text, block)
            action = "appended"
    else:
        current_source = existing.source_path
        if current_source == expected_str and existing.text == block:
            return InstallResult(
                changed=False,
                action="noop",
                state_before=state_before,
                state_after=state_before,
                tmux_conf=tmux_conf,
                expected_snippet=expected_snippet,
                previous_source_path=current_source,
                backup_path=None,
                dry_run=dry_run,
            )
        if current_source != expected_str and not force:
            raise TmuxUiError(
                f"managed block already points to {current_source}; "
                f"expected {expected_str}. Re-run with --force to replace it, "
                "or `mozyo-bridge tmux-ui uninstall` to remove it first."
            )
        # Replace in place: keep surrounding bytes untouched.
        lines = original_text.splitlines(keepends=True)
        new_text = (
            "".join(lines[: existing.begin_line])
            + block
            + "".join(lines[existing.end_line + 1 :])
        )
        action = "replaced"

    backup_target: Path | None = None
    if not dry_run:
        if backup and tmux_conf_existed and original_text != "":
            backup_target = _backup_path(tmux_conf)
            backup_target.write_text(original_text, encoding="utf-8")
        tmux_conf.parent.mkdir(parents=True, exist_ok=True)
        tmux_conf.write_text(new_text, encoding="utf-8")
        state_after = compute_status(repo_root, tmux_conf)["state"]
    else:
        state_after = STATE_INSTALLED

    return InstallResult(
        changed=True,
        action=action,
        state_before=state_before,
        state_after=state_after,
        tmux_conf=tmux_conf,
        expected_snippet=expected_snippet,
        previous_source_path=existing.source_path if existing else None,
        backup_path=backup_target,
        dry_run=dry_run,
    )


def apply_uninstall(
    *,
    tmux_conf: Path,
    dry_run: bool = False,
    backup: bool = False,
) -> UninstallResult:
    if not tmux_conf.exists():
        return UninstallResult(
            changed=False,
            action="noop",
            state_before=STATE_NOT_INSTALLED,
            state_after=STATE_NOT_INSTALLED,
            tmux_conf=tmux_conf,
            backup_path=None,
            dry_run=dry_run,
        )

    try:
        original_text = tmux_conf.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise TmuxUiError(
            f"cannot read tmux config {tmux_conf}: {exc}"
        ) from exc

    existing = _find_block(original_text)
    if existing is None:
        return UninstallResult(
            changed=False,
            action="noop",
            state_before=STATE_NOT_INSTALLED,
            state_after=STATE_NOT_INSTALLED,
            tmux_conf=tmux_conf,
            backup_path=None,
            dry_run=dry_run,
        )

    new_text = _strip_block(original_text, existing)
    backup_target: Path | None = None
    if not dry_run:
        if backup:
            backup_target = _backup_path(tmux_conf)
            backup_target.write_text(original_text, encoding="utf-8")
        tmux_conf.write_text(new_text, encoding="utf-8")

    return UninstallResult(
        changed=True,
        action="removed",
        state_before=STATE_INSTALLED if existing.source_path else STATE_DRIFT,
        state_after=STATE_NOT_INSTALLED,
        tmux_conf=tmux_conf,
        backup_path=backup_target,
        dry_run=dry_run,
    )
