// Hermetic unit tests for the PoC MCP server and workspace inspection backing.
// No real `mozyo-bridge` CLI or tmux access: the command runner and inspect
// callback are injected fakes (Redmine #11579).

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  CommandResult,
  CommandRunner,
  NO_WORKSPACE,
  defaultRunner,
  inspectWorkspace,
  inspectWorkspaceFacts,
  summarizeInspection
} from '../../workspaceInspect';
import {
  RESOURCE_URI,
  TOOL_NAME,
  PROTOCOL_VERSION,
  handleRequest,
  JsonRpcRequest
} from '../../mcp/server';

function jsonResult(command: string, json: unknown): CommandResult {
  return { ok: true, command, stdout: JSON.stringify(json), stderr: '', json };
}

function fakeRunner(): CommandRunner {
  return async (command, args) => {
    const line = [command, ...args].join(' ');
    if (args[0] === 'session') {
      return jsonResult(line, {
        name: 'mozyo-giken-3800-mozyo-bridge',
        repo_root: '/workspace/project-alpha',
        source: 'workspace-defaults'
      });
    }
    if (args[0] === 'doctor') {
      return jsonResult(line, {
        ok: true,
        sections: { cli: { status: 'ok' }, rules: { status: 'ok' }, tmux: { status: 'ok' } }
      });
    }
    return { ok: false, command: line, stdout: '', stderr: '', error: 'unexpected command' };
  };
}

const fakeInspect = (workspacePath?: string) =>
  inspectWorkspaceFacts(workspacePath ?? '/workspace/project-alpha', fakeRunner());

test('inspectWorkspace shells the expected read-only CLI surfaces', async () => {
  const seen: string[][] = [];
  const runner: CommandRunner = async (command, args) => {
    seen.push([command, ...args]);
    return jsonResult([command, ...args].join(' '), {});
  };
  await inspectWorkspace('/workspace/project-alpha', runner);
  assert.deepEqual(seen[0], ['mozyo-bridge', 'session', 'name', '--repo', '/workspace/project-alpha', '--json']);
  assert.deepEqual(seen[1], ['mozyo-bridge', 'doctor', '--target', '/workspace/project-alpha', '--json']);
});

test('inspectWorkspace short-circuits when no workspace is open', async () => {
  let called = false;
  const runner: CommandRunner = async (command, args) => {
    called = true;
    return jsonResult([command, ...args].join(' '), {});
  };
  const inspection = await inspectWorkspace(NO_WORKSPACE, runner);
  assert.equal(called, false);
  assert.equal(inspection.sessionName.ok, false);
  assert.equal(inspection.doctor.ok, false);
});

test('summarizeInspection surfaces curated facts and no secret-shaped keys', async () => {
  const facts = await inspectWorkspaceFacts('/workspace/project-alpha', fakeRunner());
  assert.equal(facts.workspace_path, '/workspace/project-alpha');
  assert.equal(facts.blocked, false);
  assert.equal(facts.session.name, 'mozyo-giken-3800-mozyo-bridge');
  assert.equal(facts.session.repo_root, '/workspace/project-alpha');
  assert.equal(facts.doctor.ok, true);
  assert.equal(facts.doctor.sections.tmux, 'ok');

  const serialized = JSON.stringify(facts).toLowerCase();
  for (const banned of ['token', 'secret', 'password', 'api_key', 'apikey', 'chat']) {
    assert.equal(serialized.includes(banned), false, `facts must not expose "${banned}"`);
  }
  // Raw stdout must not leak into the curated payload.
  assert.equal('stdout' in facts.session, false);
});

test('summarizeInspection surfaces doctor facts and blocks when doctor is unhealthy', async () => {
  // doctor --json exits non-zero when overall health is not green but still
  // emits valid sections JSON; those facts must survive and drive `blocked`.
  const runner: CommandRunner = async (command, args) => {
    const line = [command, ...args].join(' ');
    if (args[0] === 'session') {
      return jsonResult(line, { name: 's', repo_root: '/workspace/project-alpha', source: 'home-registry' });
    }
    return jsonResult(line, {
      ok: false,
      sections: { cli: { status: 'ok' }, rules: { status: 'ok' }, tmux: { status: 'missing' } }
    });
  };
  const facts = await inspectWorkspaceFacts('/workspace/project-alpha', runner);
  assert.equal(facts.session.ok, true);
  assert.equal(facts.doctor.ok, false);
  assert.equal(facts.doctor.sections.tmux, 'missing');
  assert.equal(facts.blocked, true);
});

test('defaultRunner parses stdout JSON even on a non-zero exit code', async () => {
  // Real subprocess (no tmux/network): emit JSON then exit 1, mirroring doctor.
  const result = await defaultRunner(process.execPath, [
    '-e',
    'process.stdout.write(JSON.stringify({ok:false,sections:{}}));process.exit(1)'
  ]);
  assert.equal(result.ok, true);
  assert.deepEqual(result.json, { ok: false, sections: {} });
});

test('defaultRunner reports failure when there is no parseable stdout', async () => {
  const result = await defaultRunner(process.execPath, ['-e', 'process.exit(2)']);
  assert.equal(result.ok, false);
  assert.ok(result.error);
});

test('summarizeInspection marks blocked when a CLI surface fails', () => {
  const facts = summarizeInspection('/workspace/project-alpha', {
    sessionName: { ok: false, command: 'mozyo-bridge session name ...', stdout: '', stderr: '', error: 'not found' },
    doctor: { ok: false, command: 'mozyo-bridge doctor ...', stdout: '', stderr: '', error: 'not found' }
  });
  assert.equal(facts.blocked, true);
  assert.equal(facts.session.ok, false);
  assert.equal(facts.session.error, 'not found');
  assert.equal(facts.session.name, null);
});

test('initialize advertises resources and tools capabilities', async () => {
  const res = await handleRequest({ jsonrpc: '2.0', id: 1, method: 'initialize' }, { inspect: fakeInspect });
  assert.ok(res);
  const result = res!.result as Record<string, any>;
  assert.equal(result.protocolVersion, PROTOCOL_VERSION);
  assert.ok(result.capabilities.resources);
  assert.ok(result.capabilities.tools);
});

test('notifications/initialized produces no response', async () => {
  const res = await handleRequest(
    { jsonrpc: '2.0', method: 'notifications/initialized' } as JsonRpcRequest,
    { inspect: fakeInspect }
  );
  assert.equal(res, null);
});

test('resources/list and resources/read return the single inspect capability', async () => {
  const list = await handleRequest({ jsonrpc: '2.0', id: 2, method: 'resources/list' }, { inspect: fakeInspect });
  const resources = (list!.result as any).resources;
  assert.equal(resources.length, 1);
  assert.equal(resources[0].uri, RESOURCE_URI);

  const read = await handleRequest(
    { jsonrpc: '2.0', id: 3, method: 'resources/read', params: { uri: RESOURCE_URI } },
    { inspect: fakeInspect }
  );
  const contents = (read!.result as any).contents;
  assert.equal(contents[0].mimeType, 'application/json');
  const facts = JSON.parse(contents[0].text);
  assert.equal(facts.session.name, 'mozyo-giken-3800-mozyo-bridge');
});

test('resources/read rejects an unknown uri', async () => {
  const res = await handleRequest(
    { jsonrpc: '2.0', id: 4, method: 'resources/read', params: { uri: 'mozyo://nope' } },
    { inspect: fakeInspect }
  );
  assert.ok(res!.error);
});

test('tools/list exposes a read-only tool', async () => {
  const res = await handleRequest({ jsonrpc: '2.0', id: 5, method: 'tools/list' }, { inspect: fakeInspect });
  const tools = (res!.result as any).tools;
  assert.equal(tools.length, 1);
  assert.equal(tools[0].name, TOOL_NAME);
  assert.equal(tools[0].annotations.readOnlyHint, true);
});

test('tools/call returns structured workspace facts', async () => {
  const res = await handleRequest(
    { jsonrpc: '2.0', id: 6, method: 'tools/call', params: { name: TOOL_NAME, arguments: {} } },
    { inspect: fakeInspect }
  );
  const result = res!.result as any;
  assert.equal(result.isError, false);
  assert.equal(result.structuredContent.session.repo_root, '/workspace/project-alpha');
  assert.ok(result.content[0].text.includes('mozyo-giken-3800-mozyo-bridge'));
});

test('tools/call honors an explicit workspace_path argument', async () => {
  const seen: string[] = [];
  const inspect = async (workspacePath?: string) => {
    seen.push(workspacePath ?? '(default)');
    return inspectWorkspaceFacts(workspacePath ?? '/workspace/project-alpha', fakeRunner());
  };
  await handleRequest(
    {
      jsonrpc: '2.0',
      id: 7,
      method: 'tools/call',
      params: { name: TOOL_NAME, arguments: { workspace_path: '/workspace/project-beta' } }
    },
    { inspect }
  );
  assert.equal(seen[0], '/workspace/project-beta');
});

test('unknown tool and unknown method are rejected', async () => {
  const badTool = await handleRequest(
    { jsonrpc: '2.0', id: 8, method: 'tools/call', params: { name: 'evil' } },
    { inspect: fakeInspect }
  );
  assert.ok(badTool!.error);

  const badMethod = await handleRequest({ jsonrpc: '2.0', id: 9, method: 'does/notExist' }, { inspect: fakeInspect });
  assert.equal(badMethod!.error!.code, -32601);
});
