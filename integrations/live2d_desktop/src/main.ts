import {app, dialog} from 'electron';
import {startDesktop} from './desktop';
import {join} from 'node:path';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import {connection, launchConnection, settings, Settings} from './contracts';

async function main() {
  const base = join(app.getPath('home'), '.agentstrata', 'desktop-native');
  const selectionFile = join(base, 'setup', 'desktop.json');
  let saved: Settings | undefined;
  try { saved = settings(JSON.parse(readFileSync(selectionFile, 'utf8'))); }
  catch { /* The setup window reports an absent or invalid selection file. */ }
  // Resolve the application before taking Electron's per-userData instance lock.
  const config = launchConnection(process.argv, saved) ?? connection({root: join(app.getPath('home'), '.agentstrata', 'applications', 'live2d-desktop-native')});
  const scope = config ? createHash('sha256').update(JSON.stringify(config)).digest('hex').slice(0,24) : 'setup';
  app.setPath('userData', join(base, scope));
  await startDesktop({connection: config, selectionFile});
}
void main().catch(error => {
  dialog.showErrorBox('AgentStrata Live2D', error instanceof Error ? error.message : '启动失败');
  app.quit();
});
