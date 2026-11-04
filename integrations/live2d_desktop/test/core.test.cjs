const {test} = require('node:test');
const assert = require('node:assert/strict');
const {connection, launchConnection, identifier, applyInput, localUrl, settings, visibleBounds} = require('../dist/contracts');
const {NativeBackend} = require('../dist/backend');
const {EventEmitter} = require('node:events');
const {PassThrough} = require('node:stream');
function processFixture(file,args,options) {
  const child = new EventEmitter(); child.stdin=new PassThrough(); child.stdout=new PassThrough(); child.stderr=new PassThrough(); child.exitCode=null;
  child.stdin.once('finish',()=>{child.exitCode=0;child.emit('exit',0);});
  process.nextTick(()=>child.stdout.write(JSON.stringify({ok:true})+'\n'));
  return child;
}
const {Controller} = require('../dist/controller');
const {allowed, sameHttpOrigin} = require('../dist/security');
const config = {root: 'C:\\fixture\\application'};
const id = 'a'.repeat(32), backup = 'b'.repeat(32);
const installed = [
  {id: 'open-llm-vtuber', status: 'ready', url: 'http://localhost:12493', installed_version: '0.4.0'},
  {id: 'live2d-generator', status: 'ready', url: 'http://127.0.0.1:19594', installed_version: '0.3.0'},
];
test('a shortcut and explicit launch resolve the same installed application before locking', () => {
  assert.deepEqual(launchConnection(['desktop.exe'], {connection: config, mode: 'pet', topmost: false}), config);
  assert.deepEqual(launchConnection(['desktop.exe', '--application-root', config.root]), config);
  assert.equal(launchConnection(['desktop.exe']), undefined);
});
test('an explicit target overrides the remembered app; incomplete targets fail', () => {
  const other = {...config, root: 'C:\\fixture\\other'};
  assert.deepEqual(launchConnection(['desktop.exe', '--application-root', other.root],
    {connection: config, mode: 'pet', topmost: false}), other);
  assert.throws(() => launchConnection(['desktop.exe', '--application-root'],
    {connection: config, mode: 'pet', topmost: false}));
});
test('connection stores only nonsecret fields; commands use native installation paths', () => {
  assert.deepEqual(connection({...config, token: 'never persist'}), config);
  for (const value of [{...config, root: 'relative'}, {...config, root: '\\\\server\\share'}, {...config, root: '/tmp\ncommand'}])
    assert.throws(() => connection(value));
  assert.deepEqual(settings({connection: {...config, token: 'x'}, mode: 'pet', topmost: true, token: 'x'}),
    {connection: config, mode: 'pet', topmost: true});
});
test('character inputs reject traversal and invalid revisions', () => {
  assert.deepEqual(applyInput({id, version: 2}), {id, version: 2});
  for (const value of ['../backup', '', 'a'.repeat(31), '--help']) assert.throws(() => identifier(value));
  for (const version of [0, -1, '2', 1.5, NaN, Infinity]) assert.throws(() => applyInput({id, version}));
});
test('only HTTP loopback installation origins accepted', () => {
  assert.equal(localUrl('http://localhost:12493/'), 'http://localhost:12493');
  const credentialUrl = new URL('http://localhost/'); credentialUrl.username = 'fixture'; credentialUrl.password = 'fixture';
  const queryUrl = new URL('http://localhost/'); queryUrl.searchParams.set('token', 'fixture');
  for (const url of ['https://remote.example', 'http://localhost.example.invalid:80', 'file:///tmp/page',
    credentialUrl.href, 'http://127.0.0.1/foo', queryUrl.href]) assert.throws(() => localUrl(url));
});
test('restored bounds remain on a display after DPI or monitor removal', () => {
  assert.deepEqual(visibleBounds({x: 3000, y: -3000, width: 2000, height: 1000}, {x: -1280, y: 0, width: 1280, height: 720}),
    {x: -1280, y: 0, width: 1280, height: 720});
});
test('IPC requires exact granted main frame and role; origin checks do not accept prefix spoofing', () => {
  const grant = {id: 2, url: 'http://localhost:19594', role: 'manager'};
  const caller = {senderId: 2, frameIsMain: true, url: 'http://localhost:19594/'};
  assert.equal(allowed(caller, grant, ['manager']), true);
  for (const changed of [{senderId: 3}, {frameIsMain: false}, {url: 'http://localhost:19594.evil/'},
    {url: 'http://localhost:12493'}, {url: 'file:///settings.html'}])
    assert.equal(allowed({...caller, ...changed}, grant, ['manager']), false);
  assert.equal(allowed(caller, grant, ['settings']), false);
});
test('the native backend reads application-scoped prepared components', async () => {
  assert.equal((await new NativeBackend(config, 'payload', async () => JSON.stringify({bot_id:'fixture',plugins:installed})).list()).length, 1);
});
test('native media origins with trailing slashes retain strict scheme, host and port checks', () => {
  assert.equal(sameHttpOrigin('http://localhost:12493/', 'http://localhost:12493'), true);
  for (const origin of ['null', 'file:///page', 'https://localhost:12493', 'http://localhost:12494',
    'http://localhost.example.invalid:12493', 'http://127.0.0.1:12493'])
    assert.equal(sameHttpOrigin(origin, 'http://localhost:12493'), false);
});
test('missing components and malformed status are not accepted', async () => {
  for (const value of [{}, {bot_id:'fixture',plugins:[]}, {bot_id:'fixture',plugins:installed.map(x=>({...x,status:'missing'}))}])
    await assert.rejects(new NativeBackend(config,'payload',async()=>JSON.stringify(value)).list());
});
test('native CLI receives separated argv and secrets only over stdin', async () => {
  const calls=[];
  const backend=new NativeBackend(config,'payload',async(file,args,input)=>{
    calls.push({file,args,input});return JSON.stringify({ok:true});
  },processFixture);
  await backend.modelSettings({model:'fixture-model',api_key:'fixture-secret'});
  assert.equal(calls[0].file,config.root+'\\.venv\\Scripts\\python.exe');
  assert.deepEqual(calls[0].args,['-m','chatcopilot','app','model-settings','--root',config.root,'--save']);
  assert(!calls[0].args.join(' ').includes('fixture-secret'));
  assert.equal(JSON.parse(calls[0].input).api_key,'fixture-secret');
  await backend.start('open-llm-vtuber');
  await backend.stop();
  assert.equal(calls.length,1,'desktop shutdown must not signal another launch through app stop');
  await assert.rejects(backend.apply(id,2));
  await assert.rejects(backend.start('live2d-generator'));
});
test('a failed desktop launch never stops another supervisor for the installation', async () => {
  const calls=[];
  const backend=new NativeBackend(config,'payload',async(...args)=>{calls.push(args);return '{}';},()=>{
    const child=new EventEmitter();child.stdin=new PassThrough();child.stdout=new PassThrough();child.stderr=new PassThrough();child.exitCode=null;
    process.nextTick(()=>{child.exitCode=1;child.emit('exit',1);});
    return child;
  });
  await assert.rejects(backend.start('open-llm-vtuber'));
  await backend.stop();
  assert.deepEqual(calls,[]);
});
function fixture(fail = {}) {
  const calls = [], counts = {};
  function call(name) { calls.push(name); counts[name] = (counts[name] || 0) + 1;
    if (fail[name] === true || fail[name] === counts[name]) throw Error(name + ' failed'); }
  const backend = {list: async () => {call('list'); return installed;},
    start: async plugin => call('start:' + plugin), stop: async () => call('stop'),
    apply: async () => {call('apply'); return {backup};}, restore: async () => call('restore')};
  const renderer = {prepare: async () => {call('prepare'); return !fail.busy;}, resume: async reload => call('resume:' + reload)};
  return {calls, backend, renderer, controller: new Controller(backend, renderer, async () => call('reachable'))};
}
test('startup verifies the application and Windows localhost forwarding without requiring generator', async () => {
  const f = fixture(); await f.controller.connect();
  assert.deepEqual(f.calls, ['list', 'start:open-llm-vtuber', 'reachable']);
  await assert.rejects(fixture({reachable: true}).controller.connect());
});
test('busy dialogue prevents backend mutations and releases renderer lock', async () => {
  const f = fixture({busy: true}); assert.equal((await f.controller.change({id, version: 2})).ok, false);
  assert.deepEqual(f.calls, ['prepare', 'resume:false']);
});
test('apply waits for reload; generator never stopped', async () => {
  const f = fixture(); assert.deepEqual(await f.controller.change({id, version: 2}), {ok: true, backup});
  assert.deepEqual(f.calls, ['prepare', 'stop', 'apply', 'start:open-llm-vtuber', 'list', 'reachable', 'resume:true']);
});
test('failed new-role startup restores backup and restarts original; result remains failure', async () => {
  const f = fixture({'start:open-llm-vtuber': 1}); const result = await f.controller.change({id, version: 2});
  assert.equal(result.ok, false); assert.equal(result.recovered, true); assert.equal(result.backup, backup);
  assert.deepEqual(f.calls, ['prepare', 'stop', 'apply', 'start:open-llm-vtuber', 'stop', 'restore',
    'start:open-llm-vtuber', 'list', 'reachable', 'resume:true']);
});
test('rollback failure retains the backup and never claims success', async () => {
  const f = fixture({'start:open-llm-vtuber': true, restore: true}); const result = await f.controller.change({id, version: 2});
  assert.equal(result.ok, false); assert.equal(result.recovered, false); assert.equal(result.backup, backup);
  assert.match(result.message, /恢复失败/); assert.equal(f.calls.at(-1), 'resume:false');
});
test('a timed-out apply without receipt does not claim known rollback', async () => {
  const result = await fixture({apply: true}).controller.change({id, version: 2});
  assert.equal(result.ok, false); assert.equal(result.recovered, false); assert.match(result.message, /核对/);
});
test('explicit restore restarts service; failed restore startup retains honest status', async () => {
  assert.deepEqual(await fixture().controller.change({backup}), {ok: true, restored: backup});
  const result = await fixture({'start:open-llm-vtuber': true}).controller.change({backup});
  assert.equal(result.ok, false); assert.equal(result.recovered, false); assert.equal(result.backup, backup);
});
test('concurrent apply or startup cannot stop the client twice', async () => {
  const f = fixture(); let release;
  f.renderer.prepare = () => new Promise(resolve => {release = resolve;});
  const first = f.controller.change({id, version: 2});
  assert.equal((await f.controller.change({id, version: 1})).ok, false);
  await assert.rejects(f.controller.connect()); release(true); await first;
  assert.equal(f.calls.filter(x => x === 'stop').length, 1);
});

const {Perception}=require('../dist/perception');
test('generator is optional for the independent desktop application',async()=>{
  const client=installed.slice(0,1);
  const backend=new NativeBackend(config,'payload',async()=>JSON.stringify({bot_id:'fixture',plugins:client}));
  assert.equal((await backend.list()).length,1);
  assert.equal(settings({}).mode,'pet');
});
test('perception never captures before opt-in or while hidden; delayed capture cannot re-enable after stop',async()=>{
  const published=[];let captureCount=0,finish;
  const sampler=new Perception(async()=>{captureCount++;return new Promise(resolve=>finish=resolve);},async(...args)=>published.push(args),error=>{throw error;});
  await sampler.tick();assert.equal(captureCount,0);
  await sampler.setEnabled(true);await sampler.tick();assert.equal(captureCount,0);
  sampler.visible=true;const pending=sampler.tick();assert.equal(captureCount,1);
  await sampler.setEnabled(false);finish('a'.repeat(64));await pending;
  assert(published.every(([enabled])=>enabled===false));
  await sampler.close();
});
