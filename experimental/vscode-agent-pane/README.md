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
It starts a separate VS Code app instance with isolated temporary `--user-data-dir` and `--extensions-dir`, so the PoC host does not inherit the normal VS Code profile, TaskPilot, tmux-integrated, or broken auth state. The script also removes `ELECTRON_RUN_AS_NODE` when launching VS Code, which is required when the command is run from a VS Code extension-host environment.

Then run the command in the Extension Development Host:

```text
mozyo: Open Agent Pane PoC
```

Expected smoke result:

- the window title contains `Extension Development Host`
- the workspace is `mozyo_bridge`
- the `mozyo Agent Pane PoC` webview opens
- `Session identity` is `ok`
- `Doctor` is `ok`

If `mozyo` does not appear in the Command Palette, the active window is not the isolated Extension Development Host. Re-run `npm run dev:host` and switch to the new window it opens.

## Expected Promotion

Before closing the PoC feature, decide and execute one of:

- move this PoC to a dedicated repository and add it as a submodule, or
- move it to an independent repository and keep only contract docs here.

Do not leave this directory as an untracked permanent product surface.
