const $ = id => document.getElementById(id);
let revision = '';
const controls = ['save','prepare','connect','save-model','login'];
async function run(action, message) {
  for (const id of controls) $(id).disabled = true;
  $('status').textContent = message;
  try {await action();}
  catch (error) {$('status').textContent = error.message;}
  finally {for (const id of controls) $(id).disabled = false;}
}
function fields() {
  const chatgpt = $('kind').value === 'chatgpt';
  $('api-fields').hidden = chatgpt;
  $('login').hidden = !chatgpt;
}
async function readModel() {
  const value = await window.desktopSettings.readModel();
  if (!value) return;
  revision = value.revision;
  for (const key of ['kind','model','base_url']) $(key).value = value[key];
  fields();
}
$('kind').onchange = fields;
$('settings').onsubmit = event => {
  event.preventDefault();
  void run(async () => {
    const result = await window.desktopSettings.save({root:$('root').value.trim()});
    if (!result.relaunch) $('status').textContent = '目录已保存，可以准备本机应用。';
  }, '正在保存本机安装目录…');
};
$('prepare').onclick = () => run(async () => {
  await window.desktopSettings.prepare();
  await readModel();
  $('status').textContent = '运行环境已准备，请配置模型连接。';
}, '正在下载并准备原生运行环境。首次准备可能需要几分钟，请保持窗口打开。');
$('model-settings').onsubmit = event => {
  event.preventDefault();
  void run(async () => {
    const value = {kind:$('kind').value, model:$('model').value.trim(), base_url:$('base_url').value.trim(), revision};
    if ($('api_key').value) value.api_key = $('api_key').value;
    const saved = await window.desktopSettings.saveModel(value);
    revision = saved.revision;
    $('api_key').value = '';
    $('status').textContent = value.kind === 'chatgpt' ? '模型已保存，请完成 ChatGPT 登录后启动。' : '模型已保存，可以启动桌宠。';
  }, '正在保存本应用的模型连接…');
};
$('login').onclick = () => run(async () => {
  await window.desktopSettings.login();
  $('status').textContent = 'ChatGPT 登录已完成，可以启动桌宠。';
}, '请在系统浏览器完成 ChatGPT 登录。凭据将保存到本应用。');
$('connect').onclick = () => run(async () => {
  await window.desktopSettings.connect();
  $('status').textContent = '后台已就绪，正在打开角色窗口。';
}, '正在启动并检查本机后台…');
window.desktopSettings.read().then(async result => {
  const config = result.settings.connection;
  if (config) $('root').value = config.root;
  $('status').textContent = result.lastResult?.message || '先准备本机应用，再配置模型并启动桌宠。';
  try {await readModel();} catch { /* An unprepared installation is expected on first launch. */ }
}).catch(error => {$('status').textContent = error.message;});
