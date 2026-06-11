import * as path from 'path';
import * as fs from 'fs';
import * as os from 'os';
import { downloadAndUnzipVSCode, runTests } from '@vscode/test-electron';

async function main(): Promise<void> {
  try {
    delete process.env.ELECTRON_RUN_AS_NODE;

    const extensionDevelopmentPath = path.resolve(__dirname, '../../../');
    const extensionTestsPath = path.resolve(__dirname, './suite/index');
    const repoRoot = path.resolve(extensionDevelopmentPath, '../..');
    const profileRoot = fs.mkdtempSync(path.join(os.tmpdir(), 'mozyo-agent-pane-e2e-'));
    const vscodeExecutablePath = await downloadAndUnzipVSCode('1.123.2');

    await runTests({
      vscodeExecutablePath,
      extensionDevelopmentPath,
      extensionTestsPath,
      launchArgs: [
        repoRoot,
        `--user-data-dir=${path.join(profileRoot, 'user-data')}`,
        `--extensions-dir=${path.join(profileRoot, 'extensions')}`,
        '--disable-extensions'
      ]
    });
  } catch (error) {
    console.error('Failed to run VS Code Agent Pane E2E tests:', error);
    process.exit(1);
  }
}

main();
