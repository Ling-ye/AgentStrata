'use strict';
const path = require('node:path');
const {createRequire} = require('node:module');
const {execFileSync} = require('node:child_process');

// Use the same locked compiler distribution as the native desktop builder on Linux/WSL.
const tools = createRequire(path.resolve(__dirname, '../live2d_desktop/package.json'));
async function compile(cwd = process.cwd(), script = "setup.nsi") {
  const {getMakeNsisPath} = tools('app-builder-lib/out/toolsets/windows');
  const compiler = await getMakeNsisPath();
  execFileSync(compiler.path, ['-V2', script], {
    stdio: 'inherit', cwd, env: {...process.env, ...compiler.env},
  });
}
module.exports = {compile};
if (require.main === module) compile().catch(error => { console.error(error.message); process.exitCode = 1; });
