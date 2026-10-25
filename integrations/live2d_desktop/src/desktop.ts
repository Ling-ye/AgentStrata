import {app, BrowserWindow, ipcMain, Menu, nativeImage, screen, session, Tray} from 'electron';
import {readFile, mkdir, writeFile, rename} from 'node:fs/promises';
import {join, basename} from 'node:path';
import {pathToFileURL} from 'node:url';
import {randomUUID} from 'node:crypto';
import {ApplyInput, Connection, Mode, Plugin, Settings, applyInput, connection, identifier, settings, visibleBounds} from './contracts';
import {Backend, WslBackend, distributions} from './wsl';
import {Controller} from './controller';
import {allowed, sameHttpOrigin, WindowGrant, WindowRole} from './security';

export interface DesktopOptions {
  // Injection is only used by the separate, unpackaged smoke harness.
  backend?: (config: Connection) => Backend;
  distributions?: () => Promise<string[]>;
  initial?: Settings;
  settingsFile?: string;
}
export async function startDesktop(options: DesktopOptions = {}): Promise<void> {
  if (!app.requestSingleInstanceLock()) { app.quit(); return; }
  let client: BrowserWindow | undefined, manager: BrowserWindow | undefined, setup: BrowserWindow | undefined;
  let tray: Tray | undefined, quitting = false, forceIgnore = false;
  let prefs: Settings = {mode: 'window', topmost: false};
  let plugins: Plugin[] = [], controller: Controller | undefined;
  let connectionPromise: Promise<void> | undefined;
  let changing = false;
  let lastResult: unknown = null;
  let pending: {nonce: string; resolve: (idle: boolean) => void} | undefined;
  let reloaded: (() => void) | undefined;
  const hovering = new Set<string>();
  const grants = new Map<number, WindowGrant>();
  const configFile = options.settingsFile ?? join(app.getPath('userData'), 'desktop.json');
  const settingsUrl = pathToFileURL(join(__dirname, '../ui/settings.html')).href;
  let saveQueue = Promise.resolve();
  const save = () => {
    const content = JSON.stringify(prefs, null, 2);
    saveQueue = saveQueue.catch(() => undefined).then(async () => {
      await mkdir(join(configFile, '..'), {recursive: true});
      await writeFile(configFile + '.tmp', content);
      await rename(configFile + '.tmp', configFile);
    });
    return saveQueue;
  };
  const send = (channel: string, ...args: unknown[]) => {
    if (client && !client.isDestroyed()) client.webContents.send(channel, ...args);
  };
  const trusted = (event: Electron.IpcMainEvent | Electron.IpcMainInvokeEvent, roles: WindowRole[]) =>
    allowed({senderId: event.sender.id, frameIsMain: event.senderFrame === event.sender.mainFrame,
      url: event.senderFrame?.url ?? ''}, grants.get(event.sender.id), roles);
  function handle(name: string, roles: WindowRole[], fn: (value: unknown) => unknown) {
    ipcMain.handle(name, (event, value) => {
      if (!trusted(event, roles)) throw new Error('此窗口无权调用桌面操作');
      return fn(value);
    });
  }
  function listen(name: string, fn: (...args: any[]) => void) {
    ipcMain.on(name, (event, ...args) => { if (trusted(event, ['client'])) fn(...args); });
  }
  function hide() { send('desktop-visibility', false); client?.hide(); }
  function recover() {
    forceIgnore = false; hovering.clear();
    send('force-ignore-mouse-changed', false);
    client?.setIgnoreMouseEvents(false);
    if (client) { client.show(); client.focus(); send('desktop-visibility', true); }
    else void connect().catch(showError);
  }
  function showError(error: unknown) {
    if (quitting) return;
    lastResult = {ok: false, message: error instanceof Error ? error.message : '后台连接失败'};
    openSettings();
  }
  function mouse() {
    client?.setIgnoreMouseEvents(prefs.mode === 'pet' && (forceIgnore || hovering.size === 0), {forward: true});
  }
  function setMode(mode: Mode) {
    if (!client) return;
    if (prefs.mode === 'window' && mode === 'pet') prefs.bounds = client.getNormalBounds();
    prefs.mode = mode; forceIgnore = false; hovering.clear();
    client.setFullScreen(false);
    if (client.isMaximized()) client.unmaximize();
    const area = screen.getDisplayMatching(prefs.bounds ?? client.getBounds()).workArea;
    client.setResizable(mode === 'window');
    client.setMinimumSize(mode === 'window' ? 640 : 1, mode === 'window' ? 480 : 1);
    client.setBounds(mode === 'pet' ? area : visibleBounds(prefs.bounds, area));
    client.setAlwaysOnTop(mode === 'pet' || prefs.topmost);
    client.setSkipTaskbar(mode === 'pet');
    client.setBackgroundColor(mode === 'pet' ? '#00000000' : '#18202b');
    send('mode-changed', mode); send('force-ignore-mouse-changed', false);
    mouse(); rebuildTray(); void save().catch(showError);
  }
  function requestMode(mode: Mode) { send('pre-mode-changed', mode); }
  function menu() {
    return Menu.buildFromTemplate([
      {label: '显示角色 / 取消穿透', click: recover},
      {label: '窗口模式', type: 'radio', checked: prefs.mode === 'window', click: () => requestMode('window')},
      {label: '透明桌宠', type: 'radio', checked: prefs.mode === 'pet', click: () => requestMode('pet')},
      {label: '窗口置顶', type: 'checkbox', checked: prefs.topmost, click: () => {
        prefs.topmost = !prefs.topmost; client?.setAlwaysOnTop(prefs.mode === 'pet' || prefs.topmost); void save();
      }},
      {label: '鼠标穿透', type: 'checkbox', enabled: prefs.mode === 'pet', checked: forceIgnore, click: () => {
        forceIgnore = !forceIgnore; send('force-ignore-mouse-changed', forceIgnore); mouse(); rebuildTray();
      }},
      {label: '输入框与字幕', click: () => send('toggle-input-subtitle')},
      {label: '停止对话', click: () => send('interrupt')},
      {type: 'separator'},
      {label: '我的角色 / 生成工作台', click: () => void openManager().catch(showError)},
      {label: '连接设置', click: openSettings},
      {label: '隐藏角色', click: hide},
      {label: '退出桌面（后台继续运行）', click: () => { quitting = true; app.quit(); }},
    ]);
  }
  function rebuildTray() { tray?.setContextMenu(menu()); }
  function create(role: WindowRole, url: string): BrowserWindow {
    const win = new BrowserWindow({width: role === 'settings' ? 640 : 1100, height: 780,
      minWidth: 560, minHeight: 480, show: false, autoHideMenuBar: true,
      title: 'AgentStrata Live2D', icon: join(__dirname, '../ui/icon.ico'),
      transparent: role === 'client', frame: role !== 'client', backgroundColor: '#18202b',
      webPreferences: {preload: join(__dirname, 'preload.js'), nodeIntegration: false,
        contextIsolation: true, sandbox: true, webSecurity: true, partition: 'agentstrata-desktop'},
    });
    grants.set(win.webContents.id, {id: win.webContents.id, role, url});
    const samePage = (target: string) => {
      try { return role === 'settings' ? target === url : new URL(target).origin === new URL(url).origin; }
      catch { return false; }
    };
    win.webContents.setWindowOpenHandler(() => ({action: 'deny'}));
    win.webContents.on('will-navigate', (e, target) => { if (!samePage(target)) e.preventDefault(); });
    win.webContents.on('will-redirect', (e, target) => { if (!samePage(target)) e.preventDefault(); });
    win.webContents.on('will-attach-webview', e => e.preventDefault());
    win.webContents.on('render-process-gone', () => { pending?.resolve(false); pending = undefined; });
    const id = win.webContents.id;
    win.on('closed', () => grants.delete(id));
    win.once('ready-to-show', () => win.show());
    void win.loadURL(url).catch(error => {if (!win.isDestroyed()) showError(error);});
    return win;
  }
  function openSettings() {
    if (setup && !setup.isDestroyed()) { setup.show(); setup.focus(); setup.webContents.reload(); return; }
    setup = create('settings', settingsUrl); setup.on('closed', () => { setup = undefined; });
  }
  async function openManager() {
    if (!plugins.length) await connect();
    if (!plugins.length) throw new Error('请先在连接设置中配置 WSL 后台');
    if (manager && !manager.isDestroyed()) { manager.show(); manager.focus(); return; }
    manager = create('manager', plugins.find(p => p.id === 'live2d-generator')!.url);
    manager.on('closed', () => { manager = undefined; });
  }
  async function reachable(plugin: Plugin) {
    // Verify Windows-to-WSL localhost forwarding in addition to the WSL CLI's doctor checks.
    const response = await fetch(plugin.url + (plugin.id === 'live2d-generator' ? '/health' : '/'),
      {signal: AbortSignal.timeout(15_000), redirect: 'error'});
    if (!response.ok) throw new Error(`${plugin.id} 的 Windows 本机连接未就绪`);
    await response.body?.cancel();
  }
  async function connect() {
    if (changing) throw new Error('角色切换尚未完成');
    if (connectionPromise) return connectionPromise;
    if (!prefs.connection) { openSettings(); return; }
    connectionPromise = (async () => {
      const backend = (options.backend ?? (c => new WslBackend(c)))(prefs.connection!);
      const next = new Controller(backend, {
        prepare: async () => {
          if (!client || client.webContents.isLoading()) return false;
          return new Promise<boolean>(resolve => {
            const nonce = randomUUID();
            const timer = setTimeout(() => { if (pending?.nonce === nonce) pending = undefined; resolve(false); }, 5000);
            pending = {nonce, resolve: idle => { clearTimeout(timer); pending = undefined; resolve(idle); }};
            send('desktop-prepare', nonce);
          });
        },
        resume: async reload => {
          if (!client) return;
          if (reload) {
            hovering.clear();
            await new Promise<void>((resolve, reject) => {
              const timer = setTimeout(() => { reloaded = undefined; reject(new Error('交互页面重连超时')); }, 45_000);
              reloaded = () => { clearTimeout(timer); reloaded = undefined; resolve(); };
              client!.webContents.reload();
            });
          }
          else send('desktop-resume');
        },
      }, reachable);
      plugins = await next.connect(); controller = next;
      if (!client || client.isDestroyed()) {
        client = create('client', plugins.find(p => p.id === 'open-llm-vtuber')!.url);
        client.on('close', e => { if (!quitting) { e.preventDefault(); hide(); } });
        client.on('hide', () => send('desktop-visibility', false));
        client.on('show', () => send('desktop-visibility', true));
        client.on('minimize', () => send('desktop-visibility', false));
        client.on('restore', () => send('desktop-visibility', true));
        client.on('maximize', () => send('window-maximized-change', true));
        client.on('unmaximize', () => send('window-maximized-change', false));
        const remember = () => { if (prefs.mode === 'window' && client && !client.isMinimized()) {
          prefs.bounds = client.getNormalBounds(); void save().catch(showError);
        }};
        client.on('resized', remember); client.on('moved', remember);
      }
      lastResult = null; setup?.hide();
    })().finally(() => { connectionPromise = undefined; });
    return connectionPromise;
  }
  handle('settings-read', ['settings'], async () => ({settings: prefs, lastResult,
    distributions: await (options.distributions ?? distributions)().catch(() => [])}));
  handle('settings-save', ['settings'], async value => {
    if (connectionPromise || changing) throw new Error('后台操作尚未完成');
    changing = true;
    try {
      const config = connection(value);
      if (!(await (options.distributions ?? distributions)()).includes(config.distro)) throw new Error('WSL 发行版不存在');
      if (client) {
        // Reconfiguration is explicit and local; never silently interrupt a conversation.
        const idle = await new Promise<boolean>(resolve => {
          const nonce = randomUUID();
          const timer = setTimeout(() => { pending = undefined; resolve(false); }, 5000);
          pending = {nonce, resolve: value => {clearTimeout(timer); pending = undefined; resolve(value);}};
          send('desktop-prepare', nonce);
        });
        if (!idle) {send('desktop-resume'); throw new Error('请先停止当前对话和麦克风，再修改连接');}
        send('desktop-visibility', false); client.destroy(); client = undefined;
        manager?.destroy(); manager = undefined; plugins = []; controller = undefined;
      }
      prefs.connection = config; await save(); return {ok: true};
    } finally { changing = false; }
  });
  handle('settings-connect', ['settings'], async () => { await connect(); return {ok: true}; });
  handle('desktop-status', ['client', 'manager'], () => ({plugins, lastResult, mode: prefs.mode}));
  handle('desktop-manager', ['client', 'manager'], openManager);
  const change = async (value: ApplyInput | {backup: string}) => {
    if (changing || connectionPromise) throw new Error('另一个后台操作正在进行');
    if (!controller) throw new Error('交互后台尚未连接');
    changing = true;
    try {lastResult = await controller.change(value); return lastResult;}
    finally {changing = false;}
  };
  handle('desktop-apply', ['manager'], value => change(applyInput(value)));
  handle('desktop-restore', ['manager'], value => change({backup: identifier(value)}));
  listen('desktop-prepared', value => {
    if (value && value.nonce === pending?.nonce && typeof value.idle === 'boolean') pending?.resolve(value.idle);
  });
  listen('desktop-ready', () => { reloaded?.(); requestMode(prefs.mode); send('desktop-visibility', client?.isVisible() ?? false); });
  listen('desktop-mode', mode => { if (mode === 'pet' || mode === 'window') requestMode(mode); });
  listen('desktop-hover', (id, hoveringNow) => {
    if (!['live2d-model', 'input-subtitle', 'characters'].includes(id) || typeof hoveringNow !== 'boolean') return;
    if (hoveringNow) hovering.add(id); else hovering.delete(id); mouse();
  });
  listen('desktop-force-ignore', () => { forceIgnore = !forceIgnore; mouse(); send('force-ignore-mouse-changed', forceIgnore); rebuildTray(); });
  listen('desktop-menu', () => menu().popup());
  listen('window-command', (command, value) => {
    if (command === 'window-close') hide();
    if (command === 'window-minimize') client?.minimize();
    if (command === 'window-maximize' && prefs.mode === 'window') {
      if (client?.isMaximized()) client.unmaximize(); else client?.maximize();
    }
    if (command === 'window-unfullscreen') client?.setFullScreen(false);
    if (command === 'renderer-ready-for-mode-change' && (value === 'window' || value === 'pet')) setMode(value);
  });
  app.on('second-instance', () => client ? recover() : openSettings());
  app.on('window-all-closed', () => { /* Tray owns the application lifetime. */ });
  app.on('before-quit', () => { quitting = true; send('desktop-visibility', false); });
  await app.whenReady();
  try { prefs = options.initial ?? settings(JSON.parse(await readFile(configFile, 'utf8'))); }
  catch (error: any) { if (error.code !== 'ENOENT') lastResult = {ok: false, message: '桌面设置无法读取，请重新设置连接'}; }
  Menu.setApplicationMenu(null);
  const desktopSession = session.fromPartition('agentstrata-desktop');
  const mediaAllowed = (contents: Electron.WebContents | null, permission: string, origin: string, mediaTypes?: string[]) =>
    !!client && contents === client.webContents && client.isVisible() && !client.isMinimized() &&
    permission === 'media' && sameHttpOrigin(origin, grants.get(client.webContents.id)!.url) &&
    !!mediaTypes?.length && mediaTypes.every(t => t === 'audio');
  desktopSession.setPermissionRequestHandler((contents, permission, callback, details) => {
    callback(details.isMainFrame && mediaAllowed(contents, permission, new URL(details.requestingUrl).origin,
      'mediaTypes' in details ? details.mediaTypes : []));
  });
  desktopSession.setPermissionCheckHandler((contents, permission, origin, details) =>
    mediaAllowed(contents, permission, origin, details.mediaType === 'audio' ? ['audio'] : []));
  desktopSession.setDisplayMediaRequestHandler((_request, callback) => callback({}));
  desktopSession.on('will-download', (event, item, contents) => {
    const grant = grants.get(contents.id);
    const url = item.getURL();
    if (!grant || grant.role !== 'manager' || !(url.startsWith(grant.url + '/') || url.startsWith('blob:' + grant.url + '/'))) {
      event.preventDefault(); return;
    }
    item.setSaveDialogOptions({title: '保存角色资料', defaultPath: basename(item.getFilename())});
  });
  tray = new Tray(nativeImage.createFromPath(join(__dirname, '../ui/icon.ico')));
  tray.setToolTip('AgentStrata Live2D'); tray.on('double-click', recover); rebuildTray();
  screen.on('display-metrics-changed', () => { if (client) setMode(prefs.mode); });
  screen.on('display-removed', () => { if (client) setMode(prefs.mode); });
  await connect().catch(showError);
}

