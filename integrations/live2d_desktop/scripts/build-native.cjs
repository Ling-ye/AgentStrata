// Run with the native Windows Node executable, including when invoked from WSL.
const path = require('node:path');
const {spawnSync} = require('node:child_process');
if (process.platform !== 'win32') throw new Error('Windows package build requires native Windows Node');
const nodeDirectory = path.dirname(process.execPath);
const npm = path.join(nodeDirectory, 'node_modules/npm/bin/npm-cli.js');
const entries = Object.entries(process.env).filter(([key]) => /^path$/i.test(key));
const nativePath = [nodeDirectory, ...entries.map(([, value]) => value)].join(';');
for (const [key] of entries) delete process.env[key];
process.env.Path = nativePath;
// PowerShell launched through WSL can supply only .CPL. npm's cmd children need these suffixes.
process.env.PATHEXT = [...new Set([...(process.env.PATHEXT ?? '').split(';').filter(Boolean),
  '.COM', '.EXE', '.BAT', '.CMD'])].join(';');
process.chdir(path.resolve(__dirname, '..'));
for (const args of [['ci', '--no-audit', '--no-fund'], ['test'], ['run', 'smoke'], ['run', 'pack']]) {
  const result = spawnSync(process.execPath, [npm, ...args], {stdio: 'inherit', env: process.env});
  if (result.error) throw result.error;
  if (result.status !== 0) process.exit(result.status ?? 1);
}
