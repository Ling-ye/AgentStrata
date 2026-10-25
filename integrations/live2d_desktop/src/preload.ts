import {contextBridge, ipcRenderer} from 'electron';

// A small compatibility surface for the pinned renderer, not Electron's ipcRenderer object.
const events = new Set(['pre-mode-changed', 'mode-changed', 'window-maximized-change',
  'window-fullscreen-change', 'mic-toggle', 'interrupt', 'toggle-input-subtitle',
  'force-ignore-mouse-changed']);
const commands = new Set(['window-minimize', 'window-maximize', 'window-close', 'window-unfullscreen',
  'renderer-ready-for-mode-change', 'mode-change-rendered']);
const listeners = new Map<string, Map<Function, (...args: any[]) => void>>();
function on(channel: string, callback: Function): void {
  if (!events.has(channel) || typeof callback !== 'function') return;
  const handler = (_event: unknown, ...args: unknown[]) => callback(undefined, ...args);
  off(channel, callback);
  const group = listeners.get(channel) ?? new Map();
  group.set(callback, handler); listeners.set(channel, group);
  ipcRenderer.on(channel, handler);
}
function off(channel: string, callback: Function): void {
  const handler = listeners.get(channel)?.get(callback);
  if (handler) ipcRenderer.removeListener(channel, handler);
  listeners.get(channel)?.delete(callback);
}
function subscribe(channel: string, callback: Function) {
  const handler = (_event: unknown, ...args: unknown[]) => callback(...args);
  ipcRenderer.on(channel, handler);
  return () => ipcRenderer.removeListener(channel, handler);
}
contextBridge.exposeInMainWorld('electron', {
  process: {platform: process.platform},
  ipcRenderer: {
    send: (channel: string, ...args: unknown[]) => {
      if (commands.has(channel)) ipcRenderer.send('window-command', channel, ...args);
      if (channel === 'update-component-hover') ipcRenderer.send('desktop-hover', ...args);
    },
    on, removeListener: off,
    removeAllListeners: (channel: string) => { for (const callback of listeners.get(channel)?.keys() ?? []) off(channel, callback); },
  },
});
contextBridge.exposeInMainWorld('api', {
  setMode: (mode: unknown) => ipcRenderer.send('desktop-mode', mode),
  toggleForceIgnoreMouse: () => ipcRenderer.send('desktop-force-ignore'),
  updateComponentHover: (id: unknown, hovering: unknown) => ipcRenderer.send('desktop-hover', id, hovering),
  showContextMenu: () => ipcRenderer.send('desktop-menu'),
  onForceIgnoreMouseChanged: (fn: Function) => subscribe('force-ignore-mouse-changed', fn),
  onToggleInputSubtitle: (fn: Function) => subscribe('toggle-input-subtitle', fn),
});
contextBridge.exposeInMainWorld('agentstrataDesktop', {
  version: '0.1.0',
  status: () => ipcRenderer.invoke('desktop-status'),
  openManager: () => ipcRenderer.invoke('desktop-manager'),
  apply: (id: unknown, version: unknown) => ipcRenderer.invoke('desktop-apply', {id, version}),
  restore: (backup: unknown) => ipcRenderer.invoke('desktop-restore', backup),
  onVisibility: (fn: Function) => subscribe('desktop-visibility', fn),
  onResume: (fn: Function) => subscribe('desktop-resume', fn),
  onPrepare: (fn: Function) => subscribe('desktop-prepare', async (nonce: string) => {
    let idle = false;
    try { idle = (await fn()) === true; } finally { ipcRenderer.send('desktop-prepared', {nonce, idle}); }
  }),
  ready: () => ipcRenderer.send('desktop-ready'),
});
// These operations are accepted only from the packaged settings page, never from a backend page.
contextBridge.exposeInMainWorld('desktopSettings', {
  read: () => ipcRenderer.invoke('settings-read'),
  save: (value: unknown) => ipcRenderer.invoke('settings-save', value),
  connect: () => ipcRenderer.invoke('settings-connect'),
});
