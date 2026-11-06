const el = id => document.getElementById(id);
let revision = '';
async function refresh() {
  const value = await desktop.model(); revision = value.revision;
  el('kind').value = value.kind; el('model').value = value.model; el('endpoint').value = value.base_url;
}
async function run(info, fn) {
  el('status').textContent = info;
  try { await fn(); el('status').textContent = '已完成'; }
  catch (err) { el('status').textContent = err.message; }
}
el('prepare').onclick = () => run('正在准备，首次下载可能需要几分钟…', async () => {await desktop.prepare(); await refresh();});
el('modelForm').onsubmit = event => {event.preventDefault(); void run('正在保存…', async () => {
  const value = {kind:el('kind').value, model:el('model').value, base_url:el('endpoint').value, revision};
  if (el('key').value) value.api_key = el('key').value;
  revision = (await desktop.save(value)).revision; el('key').value = '';
});};
el('login').onclick = () => run('请在系统浏览器中完成登录…', () => desktop.login());
el('start').onclick = () => run('正在启动…', () => desktop.start());
refresh().catch(() => {el('status').textContent = '请先准备运行环境。';});
