// Shared, VS Code-free workspace/session inspection backing for the Agent Pane PoC.
//
// This module is the single source of the read-only mozyo facts consumed by both
// the VS Code extension webview (`extension.ts`) and the experimental MCP server
// (`mcp/server.ts`). It only reads facts through the `mozyo-bridge` CLI, never
// infers workspace identity from editor/tmux state, and never persists anything.
//
// Scope guard (Redmine #11579): read-only inspection only. No session ensure, no
// Claude/Codex launch, no secret/token/chat-log capture or persistence.

import { execFile } from 'child_process';

export interface CommandResult {
  ok: boolean;
  command: string;
  stdout: string;
  stderr: string;
  error?: string;
  json?: unknown;
}

export interface WorkspaceInspection {
  sessionName: CommandResult;
  doctor: CommandResult;
}

/**
 * Injectable command runner. The default shells out to the real `mozyo-bridge`
 * CLI; tests inject a fake so they stay hermetic (no real CLI / tmux access).
 */
export type CommandRunner = (command: string, args: string[]) => Promise<CommandResult>;

export const NO_WORKSPACE = '(no workspace)';

export function defaultRunner(command: string, args: string[]): Promise<CommandResult> {
  const commandLine = [command, ...args].join(' ');

  return new Promise(resolve => {
    execFile(command, args, { timeout: 15000, maxBuffer: 10 * 1024 * 1024 }, (error, stdout, stderr) => {
      // Parse stdout regardless of exit code. Several `mozyo-bridge` JSON
      // surfaces (notably `doctor --json`) exit non-zero when overall health is
      // not green while still emitting the full, valid facts JSON on stdout.
      // Those facts are exactly what the agent needs to decide whether it is
      // safe to open panes, so a non-zero exit must not discard them.
      if (stdout && stdout.trim().length > 0) {
        try {
          const parsed = JSON.parse(stdout);
          resolve({ ok: true, command: commandLine, stdout, stderr, json: parsed });
          return;
        } catch {
          // fall through to the error path below
        }
      }

      const message = error ? error.message : 'No parseable JSON on stdout.';
      resolve({ ok: false, command: commandLine, stdout, stderr, error: message });
    });
  });
}

export async function inspectWorkspace(
  workspacePath: string,
  runner: CommandRunner = defaultRunner
): Promise<WorkspaceInspection> {
  if (!workspacePath || workspacePath === NO_WORKSPACE) {
    const skipped: CommandResult = {
      ok: false,
      command: '(skipped)',
      stdout: '',
      stderr: '',
      error: 'No VS Code workspace folder is open.'
    };

    return { sessionName: skipped, doctor: skipped };
  }

  const sessionName = await runner('mozyo-bridge', ['session', 'name', '--repo', workspacePath, '--json']);
  const doctor = await runner('mozyo-bridge', ['doctor', '--target', workspacePath, '--json']);

  return { sessionName, doctor };
}

/**
 * Curated, read-only fact summary suitable for an agent to decide whether it is
 * safe to open terminal panes for this workspace. Only workspace/session
 * identity and health-section statuses are surfaced — never raw stdout, tokens,
 * or chat logs. This is the payload the MCP resource/tool returns.
 */
export interface WorkspaceFacts {
  workspace_path: string;
  blocked: boolean;
  session: {
    name: string | null;
    repo_root: string | null;
    source: string | null;
    ok: boolean;
    error: string | null;
  };
  doctor: {
    ok: boolean | null;
    sections: {
      cli: string | null;
      rules: string | null;
      tmux: string | null;
    };
    error: string | null;
  };
  // Commands used to derive the facts, so the agent can see provenance. These are
  // command lines only (no secrets); raw stdout/stderr are intentionally omitted.
  sources: string[];
  notes: string;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

export function summarizeInspection(workspacePath: string, inspection: WorkspaceInspection): WorkspaceFacts {
  const sessionFacts = inspection.sessionName.json as
    | { name?: string; repo_root?: string; source?: string }
    | undefined;
  const doctorFacts = inspection.doctor.json as
    | { ok?: boolean; sections?: Record<string, { status?: string }> }
    | undefined;

  const doctorOk = typeof doctorFacts?.ok === 'boolean' ? doctorFacts.ok : null;
  // "blocked" reflects the facts, not the process exit code: the workspace is a
  // safe target only when session identity resolved and doctor reports healthy.
  const blocked = !inspection.sessionName.ok || doctorOk !== true;

  return {
    workspace_path: workspacePath || NO_WORKSPACE,
    blocked,
    session: {
      name: asString(sessionFacts?.name),
      repo_root: asString(sessionFacts?.repo_root),
      source: asString(sessionFacts?.source),
      ok: inspection.sessionName.ok,
      error: inspection.sessionName.error ?? null
    },
    doctor: {
      ok: doctorOk,
      sections: {
        cli: asString(doctorFacts?.sections?.cli?.status),
        rules: asString(doctorFacts?.sections?.rules?.status),
        tmux: asString(doctorFacts?.sections?.tmux?.status)
      },
      error: inspection.doctor.error ?? null
    },
    sources: [inspection.sessionName.command, inspection.doctor.command],
    notes:
      'Read-only workspace/session facts derived from mozyo-bridge CLI. ' +
      'Verify workspace identity before opening agent panes. ' +
      'No session was created and no agent process was launched.'
  };
}

/**
 * Convenience: inspect + summarize in one call. Used by the MCP server.
 */
export async function inspectWorkspaceFacts(
  workspacePath: string,
  runner: CommandRunner = defaultRunner
): Promise<WorkspaceFacts> {
  const inspection = await inspectWorkspace(workspacePath, runner);
  return summarizeInspection(workspacePath, inspection);
}
