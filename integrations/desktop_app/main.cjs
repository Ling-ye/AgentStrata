'use strict';
const {app, BrowserWindow, ipcMain, dialog, shell, protocol} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
protocol.registerSchemesAsPrivileged([{scheme:'studio-app', privileges:{standard:true, secure:true}}]);
const {randomUUID} = require('node:crypto');
const {NativeBackend, runNative} = require('./dist/backend');

const build = require('./desktop-build.json');
const payload = path.join(process.resourcesPath, 'runtime-payload');
const userData = path.join(process.env.LOCALAPPDATA || app.getPath('appData'), 'AgentStrata', build.installId);
app.setPath('userData', userData);
const root = path.join(userData, 'application');
const backend = new NativeBackend({root}, payload, undefined, undefined,
  build.kind === 'live2d' ? path.join(process.resourcesPath, 'desktop-host') : undefined);
const setupPath = path.join(__dirname, 'setup.html');
const pageRoot = path.join(root, 'integrations', build.botId);
const pageURL = 'studio-app://product/index.html';
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
let win, preparing, quitting = false;

function handler(name, file, fn) {
  const url = file === pageURL ? pageURL : pathToFileURL(file).href;
  ipcMain.handle('desktop-' + name, (event, ...args) => {
    if (!win || event.sender !== win.webContents || event.senderFrame !== win.webContents.mainFrame ||
        event.senderFrame.url !== url) throw new Error('该操作只允许来自本应用');
    return fn(...args);
  });
}
async function prepare() {
  if (!preparing) preparing = backend.prepare().finally(() => { preparing = undefined; });
  return preparing;
}
function operation(mode, value) {
  return runNative(python, [path.join(root, 'integrations/desktop_app/bridge.py'), mode, root],
    JSON.stringify(value)).then(JSON.parse);
}
async function openProduct() {
  if (!(await backend.modelSettings()).configured) throw new Error('请先填写模型连接');
  await backend.startApplication();
  await win.loadURL(pageURL);
}
async function start() {
  if (build.kind === 'live2d') {
    const {startDesktop} = require('./dist/desktop');
    return startDesktop({connection: {root}, backend: config => {
      if (path.resolve(config.root) !== root) throw new Error('试用版只使用自己的安装目录');
      return backend;
    }});
  }
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  await app.whenReady();
  protocol.handle('studio-app', async request => {
    const url = new URL(request.url);
    if (url.host !== 'product' || request.method !== 'GET') return new Response(null,{status:403});
    const parts = decodeURIComponent(url.pathname.slice(1)).split('/');
    if (parts.some(p => !p || p === '.' || p === '..' || /[\\:\x00-\x1f]/.test(p))) return new Response(null,{status:403});
    const file = path.join(pageRoot,...parts);
    const types = {'.html':'text/html; charset=utf-8','.js':'text/javascript','.css':'text/css','.png':'image/png','.jpg':'image/jpeg','.svg':'image/svg+xml'};
    const type = types[path.extname(file).toLowerCase()];
    if (!type || !fs.existsSync(file) || fs.realpathSync(file).toLowerCase() !== file.toLowerCase()) return new Response(null,{status:404});
    return new Response(await fs.promises.readFile(file),{headers:{'Content-Type':type,'X-Content-Type-Options':'nosniff',
      'Content-Security-Policy':"default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; base-uri 'none'; form-action 'none'"}});
  });
  win = new BrowserWindow({width: 920, height: 710, title: build.name,
    webPreferences: {preload: path.join(__dirname, 'preload.cjs'), sandbox: true,
      contextIsolation: true, nodeIntegration: false, webSecurity: true}});
  win.webContents.setWindowOpenHandler(() => ({action:'deny'}));
  win.webContents.on('will-navigate', event => event.preventDefault());
  win.webContents.on('will-attach-webview', event => event.preventDefault());
  win.webContents.session.setPermissionRequestHandler((_contents, _permission, answer) => answer(false));
  handler('prepare', setupPath, prepare);
  handler('model', setupPath, () => backend.modelSettings());
  handler('save', setupPath, value => backend.modelSettings(value));
  handler('login', setupPath, () => backend.login());
  handler('start', setupPath, openProduct);
  handler('settings', pageURL, async () => {await backend.stop(); await win.loadFile(setupPath);});
  handler('chat', pageURL, text => operation('chat', {text, request: randomUUID()}));
  handler('action', pageURL, (name, args) => operation('action', {name, args}));
  handler('source', pageURL, () => shell.openPath(process.resourcesPath));
  try {
    if (fs.existsSync(python)) { await openProduct(); return; }
  } catch (error) { console.error(error.message); }
  await win.loadFile(setupPath);
}
app.on('window-all-closed', () => app.quit());
app.on('before-quit', event => {
  if (build.kind === 'live2d' || quitting) return;
  event.preventDefault(); quitting = true;
  backend.stop().then(() => app.quit()).catch(error => {quitting = false; dialog.showErrorBox('停止应用', error.message);});
});
start().catch(error => {dialog.showErrorBox(build.name, error.message); app.quit();});
