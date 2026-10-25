// Windows-native Electron lifecycle test. No WSL, models, microphones or credentials.
const { _electron: electron } = require('playwright-core');
const http = require('node:http');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const assert = require('node:assert/strict');
const {spawn} = require('node:child_process');
const root = path.resolve(__dirname, '..');
async function main() {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'as-desktop-smoke-'));
  const server = http.createServer((req, res) => {
    res.setHeader('Content-Type', 'text/html');
    res.end(`<!doctype html><title>Controlled desktop fixture</title><p>Local test page</p><script>
      window.bootId=crypto.randomUUID();window.mic=false;window.idle=true;window.mode='window';
      agentstrataDesktop.onVisibility(v=>{if(!v){window.mic=false;window.capture?.getTracks().forEach(t=>t.stop());}});
      agentstrataDesktop.onPrepare(()=>window.idle);
      electron.ipcRenderer.on('pre-mode-changed',(_,mode)=>electron.ipcRenderer.send('renderer-ready-for-mode-change',mode));
      electron.ipcRenderer.on('mode-changed',(_,mode)=>window.mode=mode);
      agentstrataDesktop.ready();</script>`);
  });
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const url = 'http://127.0.0.1:' + server.address().port;
  const env = {...process.env, DESKTOP_SMOKE_DATA: directory, DESKTOP_SMOKE_URL_0: url, DESKTOP_SMOKE_URL_1: url};
  let app;
  try {
    console.log('smoke: launching native Electron');
    app = await electron.launch({args: ['--use-fake-device-for-media-stream', path.join(root, 'test/fixture-main.cjs')], env, timeout: 30000});
    console.log('smoke: waiting for controlled page');
    const client = await app.firstWindow(); await client.waitForFunction(() => window.mode === 'window');
    await app.evaluate(({BrowserWindow}) => {
      const win = BrowserWindow.getAllWindows()[0];
      if (win.isVisible()) return;
      return new Promise(resolve => win.once('show', () => resolve(undefined)));
    });
    const media = await client.evaluate(async () => {
      window.capture = await navigator.mediaDevices.getUserMedia({audio: true});
      let videoDenied = false;
      try {const s = await navigator.mediaDevices.getUserMedia({video: true}); s.getTracks().forEach(t => t.stop());}
      catch (error) {videoDenied = error.name === 'NotAllowedError';}
      return {label: window.capture.getAudioTracks()[0].label, videoDenied};
    });
    assert.match(media.label, /Fake/); assert.equal(media.videoDenied, true);
    const boot = await client.evaluate(() => window.bootId);
    assert.equal(await client.evaluate(() => typeof window.require), 'undefined');
    assert.equal(await client.evaluate(() => typeof window.electron.ipcRenderer.invoke), 'undefined');
    const prefs = await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences());
    console.log('smoke: checking window mode and hide');
    assert.equal(prefs.sandbox, true); assert.equal(prefs.contextIsolation, true); assert.equal(prefs.nodeIntegration, false);
    await client.evaluate(() => window.api.setMode('pet')); await client.waitForFunction(() => window.mode === 'pet');
    assert.equal(await client.evaluate(() => window.bootId), boot);
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isAlwaysOnTop()), true);
    await client.evaluate(() => {window.mic = true; window.electron.ipcRenderer.send('window-close');});
    await client.waitForFunction(() => window.mic === false);
    assert.equal(await client.evaluate(() => window.capture.getTracks().every(t => t.readyState === 'ended')), true);
    assert.equal(await client.evaluate(async () => {
      try {const s = await navigator.mediaDevices.getUserMedia({audio: true}); s.getTracks().forEach(t => t.stop()); return false;}
      catch (error) {return error.name === 'NotAllowedError';}
    }), true);
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()), false);
    // A second process must yield to the first instance, whose tray recovery clears passthrough.
    await new Promise((resolve, reject) => {
      const second = spawn(require('electron'), [path.join(root, 'test/fixture-main.cjs')], {env, stdio: 'ignore'});
      const timer = setTimeout(() => {second.kill(); reject(Error('second instance did not exit'));}, 15000);
      second.on('exit', code => {clearTimeout(timer); code === 0 ? resolve() : reject(Error('second instance exit ' + code));});
    });
    console.log('smoke: checking IPC and role transactions');
    assert.equal((await app.windows()).length, 1);
    assert.equal(await app.evaluate(({BrowserWindow}) => BrowserWindow.getAllWindows()[0].isVisible()), true);
    const forbidden = await client.evaluate(async () => {
      try {await window.desktopSettings.save({distro:'Injected', command:'/evil', root:'/tmp'}); return false;} catch {return true;}
    }); assert.equal(forbidden, true);
    const managementWindow = app.waitForEvent('window');
    await client.evaluate(() => window.agentstrataDesktop.openManager());
    const manager = await managementWindow;
    await manager.waitForLoadState();
    await client.evaluate(() => {window.idle=false;});
    assert.equal((await manager.evaluate(() => window.agentstrataDesktop.apply('a'.repeat(32),2))).ok, false);
    await client.evaluate(() => {window.idle=true;});
    const result = await manager.evaluate(() => window.agentstrataDesktop.apply('a'.repeat(32),2));
    assert.equal(result.ok, true); assert.equal(result.backup, 'b'.repeat(32));
    assert.notEqual(await client.evaluate(() => window.bootId), boot);
    await app.evaluate(() => {global.fixture.failStart=true;});
    const failed = await manager.evaluate(() => window.agentstrataDesktop.apply('a'.repeat(32),1));
    assert.equal(failed.ok, false); assert.equal(failed.recovered, true);
    await client.evaluate(() => {location.href='https://example.invalid/';});
    assert(client.url().startsWith(url));
    const calls = await app.evaluate(() => global.fixture.calls);
    assert(calls.includes('restore')); assert(!calls.includes('stop:live2d-generator'));
    const report = {platform: process.platform, electron: await app.evaluate(() => process.versions.electron),
      passed: ['sandbox', 'no-node', 'ipc-origin-role', 'mode-keeps-page', 'synthetic-audio-capture',
        'video-denied', 'hidden-stops-synthetic-track', 'hidden-denies-capture',
        'single-instance-recovery', 'busy-rejection', 'apply-reload', 'failed-start-rollback', 'navigation-blocked'],
      limitations: ['Controlled pages and backend only; no hardware microphone, real WSL or model inference.']};
    console.log(JSON.stringify(report, null, 2));
    await fs.writeFile(path.join(root, 'release', 'smoke.json'), JSON.stringify(report, null, 2)).catch(async () => {
      await fs.mkdir(path.join(root, 'release'), {recursive:true});
      await fs.writeFile(path.join(root, 'release', 'smoke.json'), JSON.stringify(report, null, 2));
    });
  } finally {
    if (app) {
      // The tray deliberately intercepts window.close; request application quit first.
      await app.evaluate(({app}) => app.quit()).catch(() => undefined);
      await app.close();
    }
    server.close(); await fs.rm(directory, {recursive:true, force:true});
  }
}
main().catch(error => {console.error(error); process.exitCode=1;});
