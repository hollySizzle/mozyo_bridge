# VS Code Agent Pane PoC

Experimental workspace for a mozyo-bridge VS Code extension that can host agent panes.

This directory is intentionally inside the mozyo_bridge repository for the PoC phase only. It must not become the permanent release unit. Redmine #11523 tracks promotion to a submodule or independent repository after the PoC proves useful.

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

## Local Smoke

Open an Extension Development Host with the mozyo_bridge repository as the target workspace:

```sh
npm run dev:host
```

This script installs dependencies if needed, compiles the extension, opens VS Code with this directory as the extension development path, opens the mozyo_bridge repo as the workspace, and disables TaskPilot in the development host to avoid unrelated workspace-menu errors.

Then run the command in the Extension Development Host:

```text
mozyo: Open Agent Pane PoC
```

## Expected Promotion

Before closing the PoC feature, decide and execute one of:

- move this PoC to a dedicated repository and add it as a submodule, or
- move it to an independent repository and keep only contract docs here.

Do not leave this directory as an untracked permanent product surface.
