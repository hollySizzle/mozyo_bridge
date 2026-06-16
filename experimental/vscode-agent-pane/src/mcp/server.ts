// Minimal, dependency-free MCP server for the Agent Pane PoC (Redmine #11579).
//
// Decision anchor: Redmine #11578 chose "mozyo MCP server" as the first
// agent-facing surface. This PoC implements exactly ONE read-only capability,
// `mozyo.workspace.inspect`, exposed both as an MCP resource (primary) and a
// read-only tool wrapper (so VS Code's agent, which consumes tools more
// readily than resources, can fetch the same facts).
//
// This is an experimental surface. The names/shapes here are NOT a public,
// versioned contract; do not depend on them externally. The protocol handling
// is intentionally hand-rolled (no @modelcontextprotocol/sdk) to keep the PoC
// self-contained and offline-testable; production should adopt the official SDK.
//
// Read-only guard: the server exposes no method that creates sessions, launches
// agents, or mutates state. It never persists secrets, tokens, or chat logs.

import { WorkspaceFacts } from '../workspaceInspect';

export const PROTOCOL_VERSION = '2024-11-05';
export const SERVER_NAME = 'mozyo-workspace-inspect-poc';
export const SERVER_VERSION = '0.0.1';

// Dotted form is the conceptual capability id (#11578/#11579). The tool `name`
// uses the underscore form because some MCP clients restrict tool names to
// [A-Za-z0-9_-]; the resource uri/name carries the dotted form.
export const CAPABILITY_ID = 'mozyo.workspace.inspect';
export const RESOURCE_URI = 'mozyo://workspace/inspect';
export const TOOL_NAME = 'mozyo_workspace_inspect';

export interface JsonRpcRequest {
  jsonrpc: '2.0';
  id?: string | number | null;
  method: string;
  params?: Record<string, unknown>;
}

export interface JsonRpcResponse {
  jsonrpc: '2.0';
  id: string | number | null;
  result?: unknown;
  error?: { code: number; message: string; data?: unknown };
}

/**
 * Inspect callback. `workspacePath` is optional; when omitted the binding layer
 * supplies the server's default workspace. Injected so the protocol layer can
 * be tested hermetically.
 */
export type InspectFn = (workspacePath?: string) => Promise<WorkspaceFacts>;

export interface ServerDeps {
  inspect: InspectFn;
}

const ERR_METHOD_NOT_FOUND = -32601;
const ERR_INVALID_PARAMS = -32602;
const ERR_INTERNAL = -32603;

function resourceDescriptor() {
  return {
    uri: RESOURCE_URI,
    name: CAPABILITY_ID,
    title: 'mozyo workspace inspect',
    description:
      'Read-only mozyo workspace/session facts (CLI-backed) for verifying ' +
      'workspace identity before opening agent panes. No session is created ' +
      'and no agent process is launched.',
    mimeType: 'application/json'
  };
}

function toolDescriptor() {
  return {
    name: TOOL_NAME,
    title: CAPABILITY_ID,
    description:
      'Return read-only mozyo workspace/session facts for the given workspace ' +
      '(or the server default). Use to verify workspace identity and health ' +
      'before opening Claude/Codex terminal panes. Read-only: creates nothing.',
    inputSchema: {
      type: 'object',
      properties: {
        workspace_path: {
          type: 'string',
          description: 'Absolute workspace path to inspect. Defaults to the server workspace.'
        }
      },
      additionalProperties: false
    },
    annotations: {
      title: CAPABILITY_ID,
      readOnlyHint: true,
      destructiveHint: false,
      openWorldHint: false
    }
  };
}

function ok(id: JsonRpcResponse['id'], result: unknown): JsonRpcResponse {
  return { jsonrpc: '2.0', id, result };
}

function fail(id: JsonRpcResponse['id'], code: number, message: string): JsonRpcResponse {
  return { jsonrpc: '2.0', id, error: { code, message } };
}

/**
 * Handle a single JSON-RPC request. Returns a response, or `null` for
 * notifications (requests without an id), which must not be answered.
 */
export async function handleRequest(
  request: JsonRpcRequest,
  deps: ServerDeps
): Promise<JsonRpcResponse | null> {
  const id = request.id ?? null;
  const isNotification = request.id === undefined || request.id === null;

  switch (request.method) {
    case 'initialize':
      return ok(id, {
        protocolVersion: PROTOCOL_VERSION,
        capabilities: {
          resources: {},
          tools: {}
        },
        serverInfo: { name: SERVER_NAME, version: SERVER_VERSION }
      });

    case 'notifications/initialized':
    case 'notifications/cancelled':
      // Client lifecycle notifications: acknowledge by producing no response.
      return null;

    case 'ping':
      return ok(id, {});

    case 'resources/list':
      return ok(id, { resources: [resourceDescriptor()] });

    case 'resources/read': {
      const uri = request.params?.uri;
      if (uri !== RESOURCE_URI) {
        return fail(id, ERR_INVALID_PARAMS, `Unknown resource uri: ${String(uri)}`);
      }
      try {
        const facts = await deps.inspect();
        return ok(id, {
          contents: [
            {
              uri: RESOURCE_URI,
              mimeType: 'application/json',
              text: JSON.stringify(facts, null, 2)
            }
          ]
        });
      } catch (error) {
        return fail(id, ERR_INTERNAL, error instanceof Error ? error.message : String(error));
      }
    }

    case 'tools/list':
      return ok(id, { tools: [toolDescriptor()] });

    case 'tools/call': {
      const name = request.params?.name;
      if (name !== TOOL_NAME) {
        return fail(id, ERR_INVALID_PARAMS, `Unknown tool: ${String(name)}`);
      }
      const args = (request.params?.arguments ?? {}) as { workspace_path?: unknown };
      const workspacePath =
        typeof args.workspace_path === 'string' ? args.workspace_path : undefined;
      try {
        const facts = await deps.inspect(workspacePath);
        return ok(id, {
          content: [{ type: 'text', text: JSON.stringify(facts, null, 2) }],
          structuredContent: facts,
          isError: false
        });
      } catch (error) {
        return ok(id, {
          content: [
            {
              type: 'text',
              text: `workspace inspection failed: ${error instanceof Error ? error.message : String(error)}`
            }
          ],
          isError: true
        });
      }
    }

    default:
      if (isNotification) {
        // Unknown notification: ignore silently per JSON-RPC.
        return null;
      }
      return fail(id, ERR_METHOD_NOT_FOUND, `Method not found: ${request.method}`);
  }
}
