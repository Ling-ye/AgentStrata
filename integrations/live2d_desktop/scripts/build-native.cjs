// Run with the native Windows Node executable, including when invoked from WSL.
const path = require('node:path');
const {spawnSync} = require('node:child_process');
function build(verifyOnly, invoke) {
  const commands = [['ci', '--no-audit', '--no-fund'], ['test'], ['run', 'smoke']];
  if (!verifyOnly) commands.push(['run', 'pack']);
  for (const args of commands) {
    const result = invoke(args);
    if (result.error) throw result.error;
    if (result.status !== 0) return result.status ?? 1;
  }
  return 0;
}

function main(args = process.argv.slice(2)) {
  if (process.platform !== 'win32') throw new Error('Windows package build requires native Windows Node');
  if (args.some(value => value !== '--verify-only')) throw new Error('Unknown build option');
  const verifyOnly = args.includes('--verify-only');
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
  const status = build(verifyOnly, command => spawnSync(process.execPath, [npm, ...command],
    {stdio: 'inherit', env: process.env}));
  if (status === 0 && verifyOnly) console.log('Verification complete; installer packaging was not requested.');
  return status;
}

module.exports = {build};
if (require.main === module) process.exitCode = main();
