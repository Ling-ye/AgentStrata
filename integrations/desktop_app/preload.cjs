'use strict';
const {contextBridge, ipcRenderer} = require('electron');
const call = name => (...args) => ipcRenderer.invoke('desktop-' + name, ...args);
contextBridge.exposeInMainWorld('desktop', {
  chat: call('chat'), action: call('action'), settings: call('settings'), source: call('source'),
  prepare: call('prepare'), model: call('model'), save: call('save'), login: call('login'), start: call('start')
});
