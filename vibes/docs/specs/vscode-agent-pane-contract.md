# VS Code Agent Pane Contract

## Purpose

This document defines the boundary between mozyo_bridge and the experimental VS Code Agent Pane extension.

The extension is a UI client. It must not infer workspace identity from VS Code terminal names, TaskPilot menu state, tmux-integrated sanitized session names, or current shell basename. It must call mozyo_bridge CLI surfaces that return explicit workspace and session facts.

## Repository Layout

During PoC, the extension lives at:

- `experimental/vscode-agent-pane/`

This location is temporary. Redmine #11523 tracks promotion to a submodule or independent repository after PoC validation. The Python package must not include this directory.

## Required CLI Surfaces

### Ensure Session

Command:

```sh
mozyo --repo <workspace-path> --no-attach --json
```

Required behavior:

- Create or reuse the workspace-scoped tmux session.
- Ensure `claude` and `codex` windows exist.
- Return machine-readable JSON.
- Never attach when `--json` is supplied.

The extension must treat this command as the session bootstrap entrypoint.

### Resolve Session Name

Command:

```sh
mozyo-bridge session name --repo <workspace-path> --json
```

Required behavior:

- Return the collision-safe tmux session name.
- Include the derivation source when JSON is requested.
- Prefer workspace defaults Redmine identifier when available.
- Avoid basename-only inference for non-ASCII or duplicate paths.

### Diagnose Workspace

Command:

```sh
mozyo-bridge doctor --target <workspace-path> --json
```

Required behavior:

- Return structured diagnostics for CLI, rules, skills, scaffold, and tmux readiness.
- Be read-only.
- Provide enough information for the UI to show blocking conditions before launching panes.

### Discover Agents

Command:

```sh
mozyo-bridge agents --json
```

Required behavior:

- Return visible tmux panes with session, window, pane id, process, cwd, inferred repo root, and agent kind.
- Allow the UI to verify that a visible pane belongs to the target workspace before sending input or presenting it as active.

## Workspace Identity Rules

The extension must carry both forms when available:

- `workspace_path`: the path supplied by VS Code or the user.
- `resolved_workspace_path`: the canonical path reported or confirmed by mozyo_bridge.

When the two differ because of symlinks, cloud drive aliases, or Unicode normalization, the UI must display enough information to prevent accidental cross-workspace operation.

The extension must not use these as identity by themselves:

- VS Code terminal title.
- tmux-integrated sanitized session name.
- basename of the workspace path.
- TaskPilot `cwd` or menu source.

## Pane Identity Rules

The UI may label panes as Claude and Codex only after verifying:

- expected tmux session name,
- expected window name,
- pane id,
- pane cwd or inferred repo root,
- process classification from mozyo_bridge where available.

If verification fails, the UI must show an explicit blocked state instead of falling back to a shell pane.

## Error Handling

The extension must surface the failing command, exit code, and stderr summary without logging secrets.

The extension must stop before launching panes when:

- `mozyo-bridge` is not found,
- `doctor --json` reports missing central rules,
- workspace identity is ambiguous,
- target session collides with a different workspace,
- expected Claude or Codex process cannot be verified.

## Non-Goals For PoC

- Marketplace publishing.
- Production-grade persistence.
- Replacing TaskPilot global menu behavior.
- Modifying tmux-integrated.
- Sending handoff messages directly from the UI.

## Follow-Up Candidates

Create separate Redmine UserStories if PoC requires new CLI support:

- stable `workspace inspect --json`,
- stable pane attach metadata for VS Code clients,
- explicit health result schema versioning,
- restart/adopt commands designed for extension clients.
