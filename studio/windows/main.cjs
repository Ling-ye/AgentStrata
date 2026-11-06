const {app,dialog} = require('electron');
const {spawn} = require('node:child_process');
const path = require('node:path');
app.whenReady().then(() => {
  const child = spawn(path.join(process.env.SystemRoot,'System32/WindowsPowerShell/v1.0/powershell.exe'),
    ['-NoProfile','-WindowStyle','Hidden','-ExecutionPolicy','Bypass','-File',path.join(process.resourcesPath,'start.ps1'),
     '-Launcher',process.execPath], {stdio:'ignore'});
  child.once('error',err => {dialog.showErrorBox('AgentStrata',err.message); app.quit();});
  child.once('exit',() => app.quit());
});
