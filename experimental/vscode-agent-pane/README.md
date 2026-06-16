# VS Code Agent Pane PoC

Experimental workspace for a mozyo-bridge VS Code extension that can host agent panes.

This directory is intentionally inside the mozyo_bridge repository for the PoC phase only. It must not become the permanent release unit. Redmine #11523 tracks the promotion; the decided path is `vibes/docs/specs/vscode-agent-pane-promotion-plan.md` (monorepo subproject first, submodule / independent repository when escalation conditions are met).

## Scope

- Provide a VS Code extension scaffold for Agent Pane experiments.
- Keep the integration contract with `mozyo-bridge` explicit.
- Avoid depending on TaskPilot or tmux-integrated behavior.
- Keep generated files out of git.

## Non-Scope

- Marketplace publishing.
- Production UI polish.
- Replacing tmux-integrated directly.
- Shipping this directory inside the Python package.

## Current Contract

The extension must treat the mozyo_bridge CLI as the boundary. The contract source is:

- `vibes/docs/specs/vscode-agent-pane-contract.md`

## Startup Paths

### Isolated dev host (canonical path)

Open an Extension Development Host with the mozyo_bridge repository as the target workspace:

```sh
npm run dev:host
```

This is the canonical startup path for the PoC. Use it for every reproduction and smoke run unless you specifically need the debugger.

The script (`scripts/open-dev-host.sh`):

- installs dependencies if `node_modules` is missing and compiles the extension,
- starts a **separate VS Code app instance** with isolated temporary `--user-data-dir` and `--extensions-dir`, so the PoC host does not inherit the normal VS Code profile, TaskPilot, tmux-integrated, or broken auth state,
- passes `--disable-extensions`, so no installed extension other than the development extension loads,
- opens this directory as the extension development path and the mozyo_bridge repo root as the workspace folder,
- removes `ELECTRON_RUN_AS_NODE` when launching VS Code, which is required when the command is run from a VS Code extension-host environment,
- prints `Opened isolated Extension Development Host state at <tmpdir>` on success.

### F5 / Run Extension (debug UI path)

The launch config `Run mozyo Agent Pane PoC` in `.vscode/launch.json` exists for debugging only. It requires this directory (`experimental/vscode-agent-pane/`) to be the open workspace folder in VS Code, compiles via the `npm: compile` pre-launch task, and attaches the debugger so breakpoints in `src/extension.ts` work.

Differences from `npm run dev:host`:

- F5 launches the dev host from your running VS Code instance and **inherits your normal user profile and installed extensions**. Only TaskPilot is disabled by id (`--disable-extension hollySizzle.taskpilot`); everything else still loads.
- Errors raised by unrelated inherited extensions or stale auth state can therefore appear in an F5 host. They are not PoC failures. Reproduce in the isolated dev host before treating any error as a PoC defect.
- F5 opens `${workspaceFolder}/../..` (the repo root) as the workspace, same as the script, but only when launched from the correct workspace folder.

## Success Criteria

After startup, run this command from the Command Palette in the Extension Development Host window:

```text
mozyo: Open Agent Pane PoC
```

The smoke passes when all of the following are observable (screenshot-equivalent checklist):

- the window title contains `[Extension Development Host]` and the workspace is `mozyo_bridge`
- the `mozyo Agent Pane PoC` webview opens beside the editor
- `Target workspace` shows the mozyo_bridge repo root path, not `(no workspace)`
- `Session identity` status is `ok` and shows `session`, `repo root`, and `source` values
- `Doctor` status is `ok` with `overall` `true` and `cli` / `rules` / `tmux` section statuses
- the `Claude pane placeholder` and `Codex pane placeholder` sections render

## Diagnostics: `mozyo` Command Missing

If `mozyo: Open Agent Pane PoC` does not appear in the Command Palette, check in this order:

1. **Wrong window.** The command only exists in the Extension Development Host window. If the title does not contain `Extension Development Host`, you are in the launcher window. Switch to the new window opened by `npm run dev:host`, or re-run it.
2. **Dev host did not open.** If no new window appeared, re-run `npm run dev:host` in a terminal and read its output. A compile error aborts the script before VS Code launches; fix it and re-run.
3. **Stale build.** The command handler lives in `out/extension.js`. `npm run dev:host` and the F5 pre-launch task recompile automatically; if you launched some other way, run `npm run compile` first.
4. **F5 from the wrong folder.** The launch config is only discovered when `experimental/vscode-agent-pane/` is the open workspace folder. Opening the repo root and pressing F5 will not start this extension host.

## Diagnostics: `TaskPilot config error` vs Workspace-less Host

These two failures look similar in a broken host but have different causes. Do not conflate them.

- **`TaskPilot config error` (or other TaskPilot popups)** comes from the unrelated TaskPilot extension inherited through a non-isolated profile, typically in an F5 host or a manually launched host without `--disable-extensions`. It does not indicate an Agent Pane PoC failure. The isolated `npm run dev:host` path cannot show it because TaskPilot is never loaded there. If you see it, switch to the canonical path instead of debugging TaskPilot.
- **Workspace-less host** means the dev host was launched without a workspace folder argument. The webview still opens, but `Target workspace` shows `(no workspace)` and both inspector sections show `blocked` with `No VS Code workspace folder is open.` Fix it by launching through `npm run dev:host` (which always passes the repo root) or by opening the mozyo_bridge repo folder in the dev host window.

Rule of thumb: a TaskPilot popup means the host environment is not isolated; a `(no workspace)` inspector means the host has no target folder. Neither is diagnosed by editing PoC source.

## MCP Server PoC (read-only workspace inspection)

Redmine #11578 decided the first agent-facing surface is a **mozyo MCP server** (not a VS Code Language Model Tool). Redmine #11579 implements exactly one read-only capability on it: `mozyo.workspace.inspect`.

The server is a minimal, dependency-free stdio MCP server. It exposes the same capability two ways so any MCP client can consume it:

- **Resource** `mozyo://workspace/inspect` (primary) — read-only workspace/session facts as `application/json`.
- **Tool** `mozyo_workspace_inspect` (read-only wrapper; underscore name for client compatibility, conceptual id `mozyo.workspace.inspect`) — same facts, optionally for an explicit `workspace_path`. VS Code's agent consumes tools more readily than resources.

Run it:

```sh
npm run compile
node ./out/mcp/main.js --repo <workspace-path>
# or: MOZYO_WORKSPACE=<workspace-path> node ./out/mcp/main.js
```

Workspace resolution order: `--repo <path>`, then `MOZYO_WORKSPACE`, then `process.cwd()`.

The returned facts are curated and CLI-backed (`mozyo-bridge session name --json`, `mozyo-bridge doctor --json`): workspace path, session name / repo root / source, doctor overall plus `cli` / `rules` / `tmux` section statuses, a `blocked` flag, and the command lines used as provenance. `blocked` is `true` unless session identity resolved **and** doctor reports healthy — this is the judgment material an agent uses *before* opening terminal panes. Note that `doctor --json` exits non-zero when health is not green while still emitting valid facts; the server parses those facts rather than discarding them.

Scope and boundary (do not exceed in this PoC):

- Read-only only. No session ensure, no Claude/Codex launch, no terminal/xterm pane (those remain Redmine #11521 / #11527).
- Never persists or returns secrets, tokens, or chat logs; raw stdout/stderr are not surfaced in the facts.
- Experimental surface. The resource/tool names and payload shape are **not** a public, versioned contract; do not depend on them externally. Production should adopt the official `@modelcontextprotocol/sdk` instead of this hand-rolled transport.

Hermetic unit tests (no real CLI or tmux; injected fakes) cover the inspection backing and the MCP protocol handling:

```sh
npm run test:unit
```

## Automated E2E Smoke

```sh
npm run test:e2e
```

This compiles the extension, downloads VS Code `1.123.2` via `@vscode/test-electron`, launches it with a short temporary profile path (`--user-data-dir`, `--extensions-dir`, `--disable-extensions`) and an explicit removal of `ELECTRON_RUN_AS_NODE`, opens the repo root as the workspace, and runs `src/test/e2e/suite/index.ts`.

It verifies that the development extension is present, that `mozyoAgentPane.open` executes without throwing, and that the extension is active afterwards. Success means the process exits `0` and logs `mozyo Agent Pane PoC E2E smoke passed`. It is the automated equivalent of the command-registration part of the manual smoke; it does not assert the webview content, so the Success Criteria checklist above remains the manual acceptance reference.

## Expected Promotion

The management boundary is decided (Redmine #11531): this PoC is promoted to an official monorepo subproject at `packages/vscode-agent-pane/` first; a submodule or independent repository comes later only when the escalation conditions are met. The promotion path, migration steps, and escalation conditions are recorded in:

- `vibes/docs/specs/vscode-agent-pane-promotion-plan.md`

Do not leave this directory as an untracked permanent product surface. Migration execution is tracked by Redmine #11532 and verified by #11533.
