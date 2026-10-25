const $ = id => document.getElementById(id);
async function run(action) {
  $('save').disabled = $('connect').disabled = true;
  $('status').textContent = '正在启动并检查后台，请稍候…';
  try { await action(); $('status').textContent = '后台已就绪，正在打开角色窗口。'; }
  catch (error) { $('status').textContent = error.message; }
  finally { $('save').disabled = $('connect').disabled = false; }
}
$('settings').onsubmit = event => {
  event.preventDefault();
  void run(async () => {
    await window.desktopSettings.save({distro: $('distro').value, command: $('command').value.trim(), root: $('root').value.trim()});
    await window.desktopSettings.connect();
  });
};
$('connect').onclick = () => run(() => window.desktopSettings.connect());
window.desktopSettings.read().then(result => {
  for (const name of result.distributions) $('distro').add(new Option(name, name));
  const config = result.settings.connection;
  if (config) for (const key of ['distro', 'command', 'root']) $(key).value = config[key];
  $('status').textContent = result.lastResult?.message || (result.distributions.length ?
    '选择发行版并填写已安装后台的位置。' : '未检测到 WSL 发行版。请在 Windows 安装 WSL 和 Ubuntu 后重试。');
}).catch(error => { $('status').textContent = error.message; });
