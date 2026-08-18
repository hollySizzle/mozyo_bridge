# Cockpit action gaps (Redmine #15631 prototype, verified in #15642)

The `mozyo-unit-board` herdr plugin (v0.3.0) ships one new operator-invoked
action: `status` (`mozyo-bridge herdr unit-board show`, read-only).

The #15631 prototype also proposed two destructive buttons — `recreate`
(`cockpit rebuild --confirm`) and `rearrange` (`cockpit rebalance --confirm`).
They are NOT shipped. Gap 0 records why; gaps 1 and 2 record the CLI surface
that is missing underneath them. Everything in this file is a recorded gap or
a proposal, not a commitment; each needs its own ticket and review before
implementation.

## Gap 0 — no sanctioned confirmation authority for destructive buttons

**What is blocked:** wiring `cockpit rebuild --confirm` / `cockpit rebalance
--confirm` to one-click manifest actions. The prototype treated the deliberate
button selection as the confirmation. Current policy does not support that
reading:

- The CLI designs `--confirm` as an operator-typed flag that follows a
  detect-only / preview run ("Without `--confirm` every other sub-action is
  detect-only / preview" — `mozyo-bridge cockpit --help`). Hard-coding
  `--confirm` into a manifest command pre-supplies the confirmation at
  manifest-authoring time; the operator's click selects an action but never
  sees a preview.
- `vibes/docs/logics/herdr-unit-board.md` limits plugin commands to
  `mozyo-bridge herdr unit-board` and allows pane-geometry mutation only via
  the preview-first service with explicit apply.
- `vibes/docs/logics/herdr-plugin-presentation-consumer-boundary.md` (consumer
  action contract) requires preview as the default and apply-time
  identity / generation re-verification; direct destructive writes are deny.
- The reviewed plugin registry pins `mozyo.unit-board` as
  `presentation_control`: geometry changes only through a reviewed
  preview-first service. `cockpit rebuild` kills and recreates the whole
  cockpit session, which exceeds that class.
- The static contract test
  (`tests/integration/.../test_herdr_unit_board_plugin.py`) enforces the
  `mozyo-bridge herdr unit-board` command prefix for every hook, including
  `[[actions]]`.

**What is needed:** a design decision (coordinator / owner scope) defining a
sanctioned confirm-capable action surface — most plausibly a preview-first
rail under `herdr unit-board` that previews the rebuild / rebalance plan and
requires a separate explicit apply, mirroring `interact`. That is new
`mozyo-bridge` source surface plus a policy-class review, so it is out of
scope for a manifest-only change.

## Gap 1 — per-project scoped recreate

**What is missing:** `cockpit rebuild` recreates the WHOLE cockpit layout.
There is no way to force-recreate only the panes belonging to a single
project / repo without tearing down every Unit column.

**Current CLI:** `cockpit rebuild --confirm` kills the mozyo-identified
cockpit session and recreates a fresh one for the current workspace. `--repo`
exists on `cockpit` but scopes the *append* target (the workspace repo root to
add), not a scoped rebuild.

**Proposed CLI (needs its own ticket):** a scoped-recreate form that rebuilds
only one project's column(s) while leaving other Units' panes untouched, e.g.

```
mozyo-bridge cockpit rebuild --repo <repo_root> --confirm
mozyo-bridge cockpit rebuild --scope project --repo <repo_root> --confirm
```

Requirements for the implementing sublane:

- Identify the target column(s) by mozyo identity markers (never by name /
  position), consistent with existing `rebuild` safety.
- Kill + recreate only the in-scope panes; other columns must survive.
- Preserve `--confirm` gating and `--dry-run` / `--json` preview.
- Fail closed if the scoped repo is not resolvable to a live cockpit column.

## Gap 2 — stored arrangement preset

**What is missing:** `cockpit rebalance` only restores columns toward an equal
fair-share width. There is no way to save a named arrangement (column order +
widths + codex/claude split ratios) and re-apply it later, so "re-arrange"
cannot restore an operator's preferred custom layout.

**Current CLI:** `cockpit rebalance --confirm` applies the fair-share width
plan with `resize-pane`; `--ratio` sets a codex height percentage at
create / append time but is not a persisted, reusable arrangement.

**Proposed CLI (needs its own ticket):** a preset store + apply, e.g.

```
mozyo-bridge cockpit preset save <name>
mozyo-bridge cockpit preset list
mozyo-bridge cockpit preset apply <name> --confirm
mozyo-bridge cockpit preset delete <name>
```

Requirements for the implementing sublane:

- Persist column order, per-column width, and codex/claude split ratio, keyed
  by mozyo Unit identity (not pane index), so re-apply survives pane churn.
- `apply` is destructive (resize / swap-pane / select-layout), so it must be
  `--confirm`-gated with `--dry-run` / `--json` preview, matching `rebalance` /
  `reconcile`.
- Decide the store location (workspace-local vs. cockpit session) and document
  it; keep secrets / personal home paths out of the stored file.

Once gaps 0–2 land (each via its own reviewed change), the plugin can add the
corresponding buttons through whatever confirm-capable surface gap 0 defines.
