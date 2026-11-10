'use strict';
const path = require('node:path');
const fs = require('node:fs');
const {createRequire} = require('node:module');
async function build(tools, job, recipe) {
  const fromTools = createRequire(path.join(tools, 'package.json'));
  const {build, Platform, Arch} = fromTools('electron-builder');
  const ts = fromTools('typescript');
  const descriptor = JSON.parse(fs.readFileSync(path.join(job, 'desktop-build.json'), 'utf8'));
  const host = path.join(job, 'host');
  fs.mkdirSync(path.join(host, 'dist'), {recursive:true});
  const live = path.resolve(recipe, '../live2d_desktop');
  const studio = descriptor.kind === 'studio';
  const presentation = descriptor.kind === 'live2d' ?
    path.join(job,'runtime-payload/source/integrations/live2d_desktop') : live;
  const modules = studio ? [] : descriptor.kind === 'live2d' ?
    fs.readdirSync(path.join(presentation,'src')).filter(n => n.endsWith('.ts')) : ['backend.ts','contracts.ts'];
  for (const name of modules) {
    // Studio owns trial installation. All other pet UI modules come from the accepted source.
    const source = fs.readFileSync(path.join(name === 'backend.ts' ? live : presentation, 'src', name), 'utf8');
    const compiled = ts.transpileModule(source, {compilerOptions:{target:ts.ScriptTarget.ES2022,
      module:ts.ModuleKind.CommonJS, esModuleInterop:true}});
    fs.writeFileSync(path.join(host, 'dist', name.replace(/\.ts$/, '.js')), compiled.outputText);
  }
  if (descriptor.kind === 'live2d') fs.cpSync(path.join(presentation, 'ui'), path.join(host, 'ui'), {recursive:true});
  for (const file of studio ? [] : ['main.cjs','preload.cjs','setup.html','setup.js','desktop.css']) {
    fs.copyFileSync(path.join(recipe, file), path.join(host, file));
  }
  if (studio) fs.copyFileSync(path.resolve(recipe,'../../studio/windows/main.cjs'), path.join(host,'main.cjs'));
  fs.copyFileSync(path.join(job,'desktop-build.json'), path.join(host,'desktop-build.json'));
  const nsisInclude = path.join(job,'app-directory.nsh');
  fs.writeFileSync(nsisInclude, '!define /redef APP_FILENAME "' + descriptor.installId + '"\n');
  const meta = {name:descriptor.installId, productName:descriptor.name, version:'0.1.0',
    description:descriptor.name, author:'AgentStrata', main:'main.cjs', license:'AGPL-3.0-only'};
  fs.writeFileSync(path.join(host,'package.json'), JSON.stringify(meta));
  await build({projectDir:host, targets:Platform.WINDOWS.createTarget('nsis', Arch.x64), config:{
    appId:'org.agentstrata.desktop.' + descriptor.installId, productName:descriptor.name,
    executableName:descriptor.installId, artifactName:'application.exe', electronVersion:fromTools('electron/package.json').version,
    electronDist:path.join(tools,'node_modules/electron/dist'), npmRebuild:false, asar:true,
    directories:{output:path.join(job,'release')}, files:['**/*'],
    extraResources:studio ? ['framework.tar.gz','framework.sha256'].map(n=>({from:path.join(job,n),to:n})).concat(
      ['start.ps1','bootstrap.sh'].map(n=>({from:path.resolve(recipe,'../../studio/windows',n),to:n}))) :
      [{from:path.join(job,'runtime-payload'),to:'runtime-payload'},
       // The reviewed application and the host recipe may come from different source versions.
       {from:recipe,to:'desktop-source/integrations/desktop_app',filter:[
         'main.cjs','preload.cjs','setup.html','setup.js','desktop.css','build.ps1','package.cjs']},
       {from:live,to:'desktop-source/integrations/live2d_desktop',filter:[
         'package.json','package-lock.json','src/backend.ts',...(descriptor.kind === 'application' ? ['src/contracts.ts'] : [])]},
       {from:path.join(job,'desktop-build.json'),to:'desktop-source/desktop-build.json'},
       ...(descriptor.kind === 'live2d' ? [
        {from:path.resolve(live,'scripts/bootstrap-windows.ps1'),to:'desktop-host/bootstrap-windows.ps1'},
        {from:path.resolve(recipe,'../../src/chatcopilot/windows_setup.py'),to:'desktop-host/windows_setup.py'}] : [])],
    win:{signExecutable:false}, nsis:{oneClick:false,perMachine:false,allowToChangeInstallationDirectory:true,createDesktopShortcut:true,
      include:nsisInclude, shortcutName:descriptor.kind==='studio' || descriptor.installId==='desktop-reader' ? descriptor.name :
        descriptor.name + (descriptor.kind==='live2d' ? ' 试用 ' : ' · ') + descriptor.installId.slice(-8)},
    publish:null}});
  if (!studio) {
    // Bind the installed launcher bytes, not merely the installer container.
    const {createHash} = require('node:crypto');
    const directory = path.join(job, 'release', 'win-unpacked');
    const files = {};
    function collect(relative = '') {
      for (const entry of fs.readdirSync(path.join(directory, relative), {withFileTypes:true})) {
        const name = relative ? relative + '/' + entry.name : entry.name;
        if (entry.isDirectory()) collect(name);
        else if (entry.isFile()) files[name] = createHash('sha256').update(fs.readFileSync(path.join(directory,name))).digest('hex');
        else throw new Error('Unsafe desktop launch member');
      }
    }
    collect();
    fs.writeFileSync(path.join(job, 'desktop-launch.json'), JSON.stringify({
      schema:'agentstrata-desktop-launch/v1', install_id:descriptor.installId,
      executable:descriptor.installId + '.exe', files
    }));
  }
}
build(...process.argv.slice(2)).catch(err => {console.error(err); process.exitCode=1;});
