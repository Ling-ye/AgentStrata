const el = id => document.getElementById(id);
let selected = '', projects = [], busy = false, submitting = false, pending = null;
async function api(path, data) {
  const r = await fetch('/api/' + path, data === undefined ? {} :
    {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const body = await r.json();
  if (!r.ok) throw new Error(body.detail || '请求失败');
  return body;
}
async function act(fn) {try {el('notice').textContent = ''; await fn();} catch(e) {el('notice').textContent = e.message;}}
async function choose(id) {
  selected = id; pending = null; sessionStorage.setItem('project', id);
  el('input').value = ''; el('messages').replaceChildren(); await refresh();
}
async function create(live=false, application_type="desktop") {
  const result = await api('projects',{id:crypto.randomUUID(),live,application_type});
  await choose(result.id); return result.id;
}
function button(text, fn) {const b=document.createElement('button'); b.textContent=text; b.onclick=()=>act(fn); return b;}
const labels={thinking:'正在理解需求',starting:'正在准备开发',developing:'正在开发',verifying:'正在验证',
 packaging:'正在生成安装包',ready:'可以试用',failed:'未完成'};
function turn(row,id) {
  const item=document.createElement('article'); item.className='turn';
  const input=document.createElement('p'); input.className='question';input.textContent=row.text;item.append(input);
  const card=document.createElement('div');if(labels[row.state]){card.className='card';const label=document.createElement('b');label.textContent=labels[row.state];card.append(label);}
  const reply=document.createElement('p');reply.textContent=row.reply;card.append(reply);
  const actions=document.createElement('div');actions.className='actions';
  if(row.state==='new_project') actions.append(button('为它新建聊天',async()=>{await create();el('input').value=row.text;el('input').focus();}));
  if(row.state==='failed'){
    if(row.retryable)actions.append(button('重试此请求',async()=>{await api('projects/'+id+'/messages/'+row.id+'/retry',{});await refresh();}));
    else {const note=document.createElement('p');note.textContent='旧任务及证据已保留，只读查看。请在下方提出新的开发请求。';actions.append(note);}
  }
  if(row.state==='configure') actions.append(button('应用这份配置',async()=>{await api('projects/'+id+'/application/proposals/'+row.id+'/apply',{});window.workbench.reset();await refresh();}));
  if(row.state==='ready'){
    const url='projects/'+id+'/builds/'+row.id;
    actions.append(button('试用桌面版',async()=>{el('notice').textContent=(await api(url+'/try',{})).message;}));
    actions.append(button(row.launched?'本机已运行':'检查桌面是否已启动',async()=>{const r=await api(url+'/observe',{});el('notice').textContent=r.loaded?'已确认本机正在运行这个版本。':'尚未观察到该版本在本机运行。';await refresh();}));
    const a=document.createElement('a');a.href='/api/'+url;a.download='';a.textContent='下载 Windows 安装包';actions.append(a);
  }
  card.append(actions);item.append(card);return item;
}
async function refresh(){
  const id=selected, list=await api('projects');if(id!==selected)return;projects=list;
  el('history').replaceChildren(...list.map(app=>{const b=button(app.name,()=>choose(app.id));b.setAttribute('aria-current',String(app.id===id));return b;}));
  const active=list.find(p=>p.id===id);if(id&&!active){await choose('');return;}el('title').textContent=active?.name||'应用工作台';
  const messages=active?await api('projects/'+id+'/messages'):[];if(id!==selected)return;
  el('welcome').hidden=messages.length>0;el('messages').replaceChildren(...messages.map(row=>turn(row,id)));
  busy=messages.some(r=>['thinking','starting','developing','verifying','packaging'].includes(r.state));el('send').disabled=busy;
  await window.workbench.render(active,api,act);
}
el('compose').onsubmit=event=>{event.preventDefault();void act(async()=>{
  const text=el('input').value.trim();if(!text||busy||submitting)return;
  submitting=true;el('send').disabled=true;
  try {
    if(!selected)await create();
    if(!pending||pending.text!==text||pending.project!==selected)pending={id:crypto.randomUUID(),text,project:selected};
    sessionStorage.setItem('unsent',JSON.stringify(pending));
    await api('projects/'+selected+'/messages',pending);
    sessionStorage.removeItem('unsent');pending=null;el('input').value='';await refresh();
  } finally {submitting=false;el('send').disabled=busy;}
});};
el('input').onkeydown=event=>{if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){event.preventDefault();el('compose').requestSubmit();}};
el('new').onclick=()=>act(()=>choose(''));el('live').onclick=()=>act(()=>create(true));el('qq').onclick=()=>act(()=>create(false,'qq_official'));
async function setup(){
  const value=await api('setup');el('model').value=value.models.application||'';el('worker').value=value.models.worker||'';
  const states={ready:'已登录',missing:'待登录',invalid:'需要重新登录'};
  el('authStatus').textContent=value.auth.map(x=>(x.lane==='main'?'聊天':'开发')+'账号 '+(states[x.state]||x.state)).join('　');
}
el('settings').onclick=()=>act(async()=>{el('config').showModal();await setup();});
el('close').onclick=()=>el('config').close();
async function settings(fn){try{el('settingsNotice').textContent=await fn()||'已保存';}catch(e){el('settingsNotice').textContent=e.message;}}
el('model-form').onsubmit=event=>{event.preventDefault();void settings(async()=>{
 await api('setup',{model:el('model').value,worker:el('worker').value,endpoint:el('endpoint').value,
  key:el('key').value,chatgpt:el('kind').value==='chatgpt'});el('key').value='';
});};
for(const [id,lane] of [['workerLogin','worker'],['chatLogin','main']])el(id).onclick=()=>settings(async()=>(await api('setup/login/'+lane,{})).message);
act(async()=>{
  const token=new URLSearchParams(location.hash.slice(1)).get('login');
  if(token){await api('login',{token});history.replaceState(null,'','/');}
  selected=sessionStorage.getItem('project')||'';await refresh();
  if(!(await api('setup')).configured){el('config').showModal();await setup();}
  const unsent=JSON.parse(sessionStorage.getItem('unsent')||'null');
  if(unsent&&unsent.project===selected){
    const rows=await api('projects/'+selected+'/messages');
    if(rows.some(row=>row.id===unsent.id.replaceAll('-','')))sessionStorage.removeItem('unsent');
    else{pending=unsent;el('input').value=unsent.text;}
  }
  setInterval(()=>{if(!document.hidden)refresh().catch(err=>{el('notice').textContent=err.message;});},1800);
});
