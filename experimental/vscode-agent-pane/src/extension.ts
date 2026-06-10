import * as vscode from 'vscode';
import { execFile } from 'child_process';

export function activate(context: vscode.ExtensionContext): void {
  const disposable = vscode.commands.registerCommand('mozyoAgentPane.open', async () => {
    const panel = vscode.window.createWebviewPanel(
      'mozyoAgentPane',
      'mozyo Agent Pane PoC',
      vscode.ViewColumn.Beside,
      { enableScripts: false }
    );

    const workspacePath = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? '(no workspace)';
    panel.webview.html = renderLoadingPanel(workspacePath);

    const inspection = await inspectWorkspace(workspacePath);
    panel.webview.html = renderPoCPanel(workspacePath, inspection);
  });

  context.subscriptions.push(disposable);
}

export function deactivate(): void {
  // No persistent resources yet.
}

interface CommandResult {
  ok: boolean;
  command: string;
  stdout: string;
  stderr: string;
  error?: string;
  json?: unknown;
}

interface WorkspaceInspection {
  sessionName: CommandResult;
  doctor: CommandResult;
}

async function inspectWorkspace(workspacePath: string): Promise<WorkspaceInspection> {
  if (workspacePath === '(no workspace)') {
    const skipped: CommandResult = {
      ok: false,
      command: '(skipped)',
      stdout: '',
      stderr: '',
      error: 'No VS Code workspace folder is open.'
    };

    return {
      sessionName: skipped,
      doctor: skipped
    };
  }

  const sessionName = await runJsonCommand('mozyo-bridge', [
    'session',
    'name',
    '--repo',
    workspacePath,
    '--json'
  ]);
  const doctor = await runJsonCommand('mozyo-bridge', [
    'doctor',
    '--target',
    workspacePath,
    '--json'
  ]);

  return { sessionName, doctor };
}

function runJsonCommand(command: string, args: string[]): Promise<CommandResult> {
  const commandLine = [command, ...args].join(' ');

  return new Promise(resolve => {
    execFile(command, args, { timeout: 15000, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
      if (error) {
        resolve({
          ok: false,
          command: commandLine,
          stdout,
          stderr,
          error: error.message
        });
        return;
      }

      let parsed: unknown;
      try {
        parsed = JSON.parse(stdout);
      } catch (parseError) {
        resolve({
          ok: false,
          command: commandLine,
          stdout,
          stderr,
          error: parseError instanceof Error ? parseError.message : String(parseError)
        });
        return;
      }

      resolve({
        ok: true,
        command: commandLine,
        stdout,
        stderr,
        json: parsed
      });
    });
  });
}

function renderLoadingPanel(workspacePath: string): string {
  return renderDocument(`
    <main>
      <div class="workspace">
        <h2>Target workspace</h2>
        <code>${escapeHtml(workspacePath)}</code>
      </div>
      <div class="workspace">
        <h2>Inspector</h2>
        <p>Loading mozyo workspace/session facts...</p>
      </div>
      ${renderPanePlaceholders()}
    </main>
  `);
}

function renderPoCPanel(workspacePath: string, inspection: WorkspaceInspection): string {
  const escapedWorkspace = escapeHtml(workspacePath);
  const sessionFacts = inspection.sessionName.json as { name?: string; repo_root?: string; source?: string } | undefined;
  const doctorFacts = inspection.doctor.json as { ok?: boolean; sections?: Record<string, { status?: string }> } | undefined;

  return renderDocument(`
    <main>
      <div class="workspace">
        <h2>Target workspace</h2>
        <code>${escapedWorkspace}</code>
      </div>
      <div class="inspector">
        <section>
          <h2>Session identity</h2>
          ${renderCommandStatus(inspection.sessionName)}
          <dl>
            <dt>session</dt>
            <dd><code>${escapeHtml(sessionFacts?.name ?? '(unknown)')}</code></dd>
            <dt>repo root</dt>
            <dd><code>${escapeHtml(sessionFacts?.repo_root ?? '(unknown)')}</code></dd>
            <dt>source</dt>
            <dd><code>${escapeHtml(sessionFacts?.source ?? '(unknown)')}</code></dd>
          </dl>
        </section>
        <section>
          <h2>Doctor</h2>
          ${renderCommandStatus(inspection.doctor)}
          <dl>
            <dt>overall</dt>
            <dd><code>${escapeHtml(String(doctorFacts?.ok ?? false))}</code></dd>
            <dt>cli</dt>
            <dd><code>${escapeHtml(doctorFacts?.sections?.cli?.status ?? '(unknown)')}</code></dd>
            <dt>rules</dt>
            <dd><code>${escapeHtml(doctorFacts?.sections?.rules?.status ?? '(unknown)')}</code></dd>
            <dt>tmux</dt>
            <dd><code>${escapeHtml(doctorFacts?.sections?.tmux?.status ?? '(unknown)')}</code></dd>
          </dl>
        </section>
      </div>
      ${renderPanePlaceholders()}
    </main>
  `);
}

function renderCommandStatus(result: CommandResult): string {
  const status = result.ok ? 'ok' : 'blocked';
  const error = result.error ? `<p class="error">${escapeHtml(result.error)}</p>` : '';
  const stderr = result.stderr ? `<pre>${escapeHtml(result.stderr)}</pre>` : '';

  return `
    <p class="status ${status}">${status}</p>
    <p><code>${escapeHtml(result.command)}</code></p>
    ${error}
    ${stderr}
  `;
}

function renderPanePlaceholders(): string {
  return `
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
  `;
}

function renderDocument(body: string): string {
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
    .workspace,
    .inspector section {
      border: 1px solid var(--vscode-panel-border);
      padding: 12px;
    }
    .inspector {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 12px;
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
    dl {
      display: grid;
      grid-template-columns: max-content minmax(0, 1fr);
      gap: 6px 12px;
      margin: 0;
    }
    dt {
      color: var(--vscode-descriptionForeground);
    }
    dd {
      margin: 0;
      min-width: 0;
    }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      max-height: 120px;
      overflow: auto;
      border: 1px solid var(--vscode-panel-border);
      padding: 8px;
    }
    .status {
      display: inline-block;
      margin: 0 0 8px;
      padding: 2px 6px;
      border: 1px solid var(--vscode-panel-border);
    }
    .status.ok {
      color: var(--vscode-testing-iconPassed);
    }
    .status.blocked,
    .error {
      color: var(--vscode-testing-iconFailed);
    }
  </style>
</head>
<body>
  ${body}
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
