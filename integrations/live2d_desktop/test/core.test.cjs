const {test} = require('node:test');
const assert = require('node:assert/strict');
const {connection, identifier, applyInput, localUrl, settings, visibleBounds} = require('../dist/contracts');
const {WslBackend, distributions} = require('../dist/wsl');
const {Controller} = require('../dist/controller');
const {allowed, sameHttpOrigin} = require('../dist/security');
const config = {distro: 'Ubuntu-22.04', command: '/opt/agentstrata/bin/agentstrata', root: '/srv/fixture/plugins'};
const id = 'a'.repeat(32), backup = 'b'.repeat(32);
const installed = [
  {id: 'open-llm-vtuber', status: 'ready', url: 'http://localhost:12493', installed_version: '0.4.0'},
  {id: 'live2d-generator', status: 'ready', url: 'http://127.0.0.1:19594', installed_version: '0.3.0'},
];
test('connection stores only nonsecret fields; commands are absolute WSL paths', () => {
  assert.deepEqual(connection({...config, token: 'never persist'}), config);
  for (const value of [{...config, command: 'bash'}, {...config, distro: '--exec'}, {...config, root: '/tmp\ncommand'}])
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
test('WSL discovery reads UTF-16 and installed backend versions', async () => {
  assert.deepEqual(await distributions(async (args, utf16) => {
    assert.deepEqual(args, ['--list', '--quiet']); assert.equal(utf16, true); return 'Ubuntu-22.04\r\nOther\r\n';
  }), ['Ubuntu-22.04', 'Other']);
  assert.equal((await new WslBackend(config, async () => JSON.stringify(installed)).list()).length, 2);
});
test('native media origins with trailing slashes retain strict scheme, host and port checks', () => {
  assert.equal(sameHttpOrigin('http://localhost:12493/', 'http://localhost:12493'), true);
  for (const origin of ['null', 'file:///page', 'https://localhost:12493', 'http://localhost:12494',
    'http://localhost.example.invalid:12493', 'http://127.0.0.1:12493'])
    assert.equal(sameHttpOrigin(origin, 'http://localhost:12493'), false);
});
test('missing WSL, missing plugins, incompatible versions and malformed results fail explicitly', async () => {
  await assert.rejects(distributions(async () => {throw Error('WSL unavailable');}));
  for (const result of [[], installed.map(x => ({...x, status: 'uninstalled'})),
    installed.map(x => ({...x, installed_version: '0.1.0'})), {}, 'malformed']) {
    await assert.rejects(new WslBackend(config, async () => JSON.stringify(result)).list());
  }
});
test('CLI uses argv without shell expansion and role operations cannot choose executable', async () => {
  const calls = [], backend = new WslBackend({...config, root: '/srv/fixture/a b;literal'}, async args => {
    calls.push(args); return JSON.stringify({ok: true, backup});
  });
  await backend.start('open-llm-vtuber'); await backend.apply(id, 2); await backend.restore(backup);
  assert.deepEqual(calls[0], ['--distribution', config.distro, '--exec', config.command,
    'plugin', 'start', 'open-llm-vtuber', '--root', '/srv/fixture/a b;literal']);
  assert.equal(calls[1][3], '/srv/fixture/a b;literal/live2d-generator/runtime/venv/bin/agentstrata-live2d');
  assert(calls[1].includes(id)); assert(calls[2].includes(backup));
});
test('port conflicts and failed doctor are not accepted as ready', async () => {
  await assert.rejects(new WslBackend(config, async () => '{"ok":false}').start('open-llm-vtuber'));
  await assert.rejects(new WslBackend(config, async () => {throw Error('port occupied');}).start('open-llm-vtuber'));
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
test('startup verifies both services and Windows localhost forwarding', async () => {
  const f = fixture(); await f.controller.connect();
  assert.deepEqual(f.calls, ['list', 'start:open-llm-vtuber', 'reachable', 'start:live2d-generator', 'reachable']);
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
