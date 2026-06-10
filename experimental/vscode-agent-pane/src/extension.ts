import * as vscode from 'vscode';

export function activate(context: vscode.ExtensionContext): void {
  const disposable = vscode.commands.registerCommand('mozyoAgentPane.open', () => {
    const panel = vscode.window.createWebviewPanel(
      'mozyoAgentPane',
      'mozyo Agent Pane PoC',
      vscode.ViewColumn.Beside,
      { enableScripts: false }
    );

    const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '(no workspace)';
    panel.webview.html = renderPoCPanel(workspacePath);
  });

  context.subscriptions.push(disposable);
}

export function deactivate(): void {
  // No persistent resources yet.
}

function renderPoCPanel(workspacePath: string): string {
  const escapedWorkspace = escapeHtml(workspacePath);

  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>mozyo Agent Pane PoC</title>
  <style>
    body {
      font-family: var(--vscode-font-family);
      color: var(--vscode-foreground);
      background: var(--vscode-editor-background);
      margin: 0;
      padding: 16px;
    }
    main {
      display: grid;
      gap: 12px;
    }
    .workspace {
      border: 1px solid var(--vscode-panel-border);
      padding: 12px;
    }
    .panes {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
      min-height: 260px;
    }
    section {
      border: 1px solid var(--vscode-panel-border);
      padding: 12px;
    }
    h2 {
      font-size: 13px;
      margin: 0 0 8px;
    }
    code {
      word-break: break-all;
    }
  </style>
</head>
<body>
  <main>
    <div class="workspace">
      <h2>Target workspace</h2>
      <code>${escapedWorkspace}</code>
    </div>
    <div class="panes">
      <section>
        <h2>Claude pane placeholder</h2>
        <p>PTY/xterm integration is tracked by Redmine #11521.</p>
      </section>
      <section>
        <h2>Codex pane placeholder</h2>
        <p>CLI contract is tracked by Redmine #11522.</p>
      </section>
    </div>
  </main>
</body>
</html>`;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
