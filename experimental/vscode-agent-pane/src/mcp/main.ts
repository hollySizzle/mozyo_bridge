// stdio transport binding for the PoC MCP server (Redmine #11579).
//
// MCP stdio transport: newline-delimited JSON-RPC messages on stdin/stdout,
// with no embedded newlines per message. This binding owns I/O only; all
// protocol behavior lives in `server.ts` and all fact gathering in
// `workspaceInspect.ts`, both of which are unit-tested hermetically.
//
// Workspace resolution order: `--repo <path>` arg, then MOZYO_WORKSPACE env,
// then process.cwd(). Read-only: this process never creates sessions or panes.

import { InspectFn, JsonRpcRequest, handleRequest } from './server';
import { inspectWorkspaceFacts } from '../workspaceInspect';

function resolveDefaultWorkspace(argv: string[]): string {
  const repoFlag = argv.indexOf('--repo');
  if (repoFlag !== -1 && argv[repoFlag + 1]) {
    return argv[repoFlag + 1];
  }
  if (process.env.MOZYO_WORKSPACE) {
    return process.env.MOZYO_WORKSPACE;
  }
  return process.cwd();
}

function makeInspect(defaultWorkspace: string): InspectFn {
  return (workspacePath?: string) => inspectWorkspaceFacts(workspacePath ?? defaultWorkspace);
}

function writeMessage(message: unknown): void {
  process.stdout.write(JSON.stringify(message) + '\n');
}

export function startStdioServer(argv: string[] = process.argv.slice(2)): void {
  const defaultWorkspace = resolveDefaultWorkspace(argv);
  const deps = { inspect: makeInspect(defaultWorkspace) };

  let buffer = '';
  process.stdin.setEncoding('utf8');

  process.stdin.on('data', chunk => {
    buffer += chunk;
    let newlineIndex = buffer.indexOf('\n');
    while (newlineIndex !== -1) {
      const line = buffer.slice(0, newlineIndex).trim();
      buffer = buffer.slice(newlineIndex + 1);
      newlineIndex = buffer.indexOf('\n');
      if (line.length === 0) {
        continue;
      }
      void dispatch(line);
    }
  });

  async function dispatch(line: string): Promise<void> {
    let request: JsonRpcRequest;
    try {
      request = JSON.parse(line) as JsonRpcRequest;
    } catch (error) {
      writeMessage({
        jsonrpc: '2.0',
        id: null,
        error: {
          code: -32700,
          message: `Parse error: ${error instanceof Error ? error.message : String(error)}`
        }
      });
      return;
    }

    const response = await handleRequest(request, deps);
    if (response !== null) {
      writeMessage(response);
    }
  }
}

if (require.main === module) {
  startStdioServer();
}
