# VS Code Agent Pane Contract

## Purpose

This document defines the boundary between mozyo_bridge and the experimental VS Code Agent Pane extension.

The extension is a UI client. It must not infer workspace identity from VS Code terminal names, TaskPilot menu state, tmux-integrated sanitized session names, or current shell basename. It must call mozyo_bridge CLI surfaces that return explicit workspace and session facts.

## Repository Layout

During PoC, the extension lives at:

- `experimental/vscode-agent-pane/`

This location is temporary. Redmine #11523 tracks promotion to a submodule or independent repository after PoC validation. The Python package must not include this directory.

Startup procedure, success criteria, and failure diagnostics for the PoC (isolated dev host vs F5 debug launch, missing `mozyo` command, `TaskPilot config error` vs workspace-less host, E2E smoke) are documented in `experimental/vscode-agent-pane/README.md`. This contract document stays limited to the CLI boundary.

## Required CLI Surfaces

These CLI surfaces are still required even if the VS Code integration later uses MCP or Agent APIs. They are the local source for workspace/session facts and the fallback path when the VS Code agent surface cannot host a full terminal UI.

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
- Replacing VS Code Agent Window, MCP servers, MCP Apps, or Language Model Tools.

## Prior Art

The PoC is not exploring an unknown UI pattern. Similar terminal-hosting implementations already exist:

- Claude Code Sidebar: VS Code sidebar extension that uses a webview, xterm.js, and node-pty to run a shell and send the `claude` command. Reference: https://marketplace.visualstudio.com/items?itemName=diruuu.claude-code-sidebar
- Claude Code Crew: web UI for managing multiple Claude Code sessions across worktrees, using xterm.js and node-pty for terminal emulation and PTY management. Reference: https://github.com/to-na/claude-code-crew
- xterm.js: mature terminal rendering library used by VS Code integrated terminal and many browser terminal tools. References: https://xtermjs.org/ and https://github.com/xtermjs/xterm.js/

These examples support the feasibility of a terminal-pane PoC. They do not solve mozyo_bridge-specific requirements:

- explicit workspace identity,
- symlink / cloud drive path ambiguity,
- Redmine-governed session routing,
- Claude and Codex dual-pane coordination,
- avoiding TaskPilot and tmux-integrated hidden state.

## TaskPilot And tmux-integrated Lessons

The dedicated Agent Pane must avoid repeating the failure mode seen in the TaskPilot / tmux-integrated workflow:

- A user-level `taskPilot.configPath` can hide the workspace-local `.vscode/task-menu.yaml`.
- A global or stale TaskPilot menu can execute commands for a different workspace.
- tmux-integrated can display or attach a fallback shell that is not the intended Claude / Codex pane.
- Sanitized or basename-derived tmux session names are unsafe for non-ASCII paths and duplicate workspace names.

For the PoC, the UI must therefore show the target workspace, resolved workspace, expected tmux session, and each pane identity before it treats a pane as active.

TaskPilot remains a useful reference for VS Code extension structure, command registration, packaging, and settings UI patterns. It must not be a runtime dependency for Agent Pane.

## VS Code AI Surface Positioning

As of 2026-06-11, VS Code has first-class AI extension surfaces that the PoC must account for:

- MCP servers can provide tools, resources, prompts, authentication, sampling, workspace roots, and MCP Apps.
- MCP Apps can return interactive UI components inside chat for forms, visualizations, and multi-step workflows.
- Language Model Tools can be contributed by VS Code extensions and can use VS Code APIs from the extension host.
- Chat Participants can own a specialized chat flow.
- The Agents Window and remote agents are emerging as VS Code's native surface for monitoring and controlling agent sessions.

The mozyo integration should not compete with these surfaces by default. Its long-term value is the governed workspace/session layer:

- Redmine gate visibility,
- workspace and resolved path verification,
- tmux session and pane identity,
- safe handoff / review / close routing,
- wrong-workspace prevention.

Therefore the PoC has two tracks:

1. Agent ecosystem integration: expose mozyo facts and actions to VS Code agents through MCP and/or Language Model Tool surfaces.
2. Terminal fallback UI: prove that an embedded xterm.js/node-pty pane can run local agent CLIs when VS Code's native Agent surfaces are insufficient.

Do not treat terminal fallback UI as the primary product until the Agent ecosystem integration path has been evaluated.

## Candidate Architecture

Use a layered design:

- `mozyo-bridge` CLI: authoritative workspace/session/gate facts.
- Optional mozyo MCP server: reusable tools/resources/prompts for VS Code and other MCP clients.
- VS Code extension: workspace inspector, command registration, MCP registration if needed, and fallback terminal UI.
- MCP Apps or webviews: interactive confirmation, Redmine gate summaries, workspace selection, and session verification.
- xterm.js/node-pty: only for terminal-like agent interaction that cannot be represented through VS Code Agent APIs.

Initial MCP/tool candidates:

- `mozyo.workspace.inspect`: return workspace path, resolved path, Redmine defaults, session name, and health.
- `mozyo.session.ensure`: ensure `claude` / `codex` windows exist and return machine-readable session facts.
- `mozyo.agents.list`: return discovered panes and classifications.
- `mozyo.gate.status`: summarize active Redmine issue, latest gates, and missing approvals.

These names are conceptual. Do not implement them as public contract until a schema and versioning decision exists.

## Next Implementation Order

Proceed in this order:

1. Confirm the scaffold opens in VS Code and the placeholder command works.
2. Add a workspace/session inspector view that runs read-only mozyo CLI commands and displays target workspace, resolved workspace, session name, and diagnostics.
3. Decide whether the first agent-facing integration should be MCP server, VS Code Language Model Tool, or both. Record the decision in Redmine before implementation.
4. Prototype one read-only tool/resource for workspace inspection before implementing terminal panes.
5. Add webview-side xterm.js rendering without starting Claude or Codex.
6. Add extension-host PTY creation for a simple shell command such as `pwd`.
7. Wire resize, input, paste, and cleanup between xterm.js and the PTY.
8. Replace the simple shell command with explicit `mozyo --repo <workspace> --no-attach --json` bootstrap and display the returned session facts.
9. Start Claude and Codex panes only after workspace/session identity is displayed and verified.
10. Run the smoke in Redmine #11528: Japanese input, paste, resize, scrollback, 30-minute stability, and wrong-workspace prevention.

Do not start by launching `claude` and `codex` directly. The first implementation risk to retire is workspace/session visibility in the VS Code surface. The second is whether MCP or Language Model Tools can carry the main workflow. The PTY/webview bridge is a fallback UI risk, not the center of the product.

## Follow-Up Candidates

Create separate Redmine UserStories if PoC requires new CLI support:

- stable `workspace inspect --json`,
- stable pane attach metadata for VS Code clients,
- explicit health result schema versioning,
- restart/adopt commands designed for extension clients.
- mozyo MCP server package / command surface,
- VS Code Language Model Tool contribution,
- MCP Apps UI for Redmine gate approval and workspace/session verification.
