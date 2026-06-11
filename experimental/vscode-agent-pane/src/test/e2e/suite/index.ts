import * as assert from 'assert';
import * as vscode from 'vscode';

export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension('hollySizzle.mozyo-vscode-agent-pane-poc');
  assert.ok(extension, 'development extension should be present');

  await assert.doesNotReject(async () => {
    await vscode.commands.executeCommand('mozyoAgentPane.open');
  }, 'mozyoAgentPane.open should execute without throwing');

  assert.ok(extension.isActive, 'extension should be active after command execution');
  console.log('mozyo Agent Pane PoC E2E smoke passed');
}
