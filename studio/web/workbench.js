/* Standard application controls. Secrets never enter the chat or session storage. */
window.workbench = (() => {
  let current = '', generation = 0, locked = false;
  const container = () => document.getElementById('application');
  const field = (label, id, type='text') => {
    const wrap=document.createElement('label');wrap.textContent=label;
    const input=document.createElement(type==='textarea'?'textarea':'input');
    input.id=id;if(type!=='textarea')input.type=type;input.autocomplete='off';wrap.append(input);return wrap;
  };
  function reset(){current='';generation++;}
  async function render(app,api,act){
    const panel=container();
    if(app?.application_type!=='qq_official'){panel.hidden=true;current='';generation++;return;}
    panel.hidden=false;
    const id=app.id, epoch=generation;
    if(current!==id){
      current=id;
      panel.replaceChildren();
      const heading=document.createElement('b');heading.textContent='AgentStrata Bot · QQ 官方机器人 · 本机运行';panel.append(heading);
      const status=document.createElement('p');status.id='application-status';status.setAttribute('role','status');panel.append(status);
      const checks=document.createElement('p');checks.id='application-checks';checks.setAttribute('role','status');panel.append(checks);
      const form=document.createElement('form');form.id='connection-form';
      form.append(field('应用名称','app-name'),field('用途与回复要求','app-instructions','textarea'),field('QQ AppID','app-id'),field('AppSecret（已保存时留空保留）','app-secret','password'));
      const save=document.createElement('button');save.textContent='保存连接';form.append(save);panel.append(form);
      const note=document.createElement('p');note.textContent='先在 QQ 官方平台创建机器人并设置测试范围。密钥只填写在此处，不发送给聊天助手。本机关闭或休眠时机器人会离线。';panel.append(note);
      const link=document.createElement('a');link.href='https://bot.q.qq.com/';link.target='_blank';link.rel='noopener noreferrer';link.textContent='打开 QQ 官方平台';panel.append(link);
      const actions=document.createElement('div');actions.className='actions';
      async function perform(fn){if(locked)return;locked=true;const buttons=[...panel.querySelectorAll('button')];buttons.forEach(b=>b.disabled=true);try{await fn();}finally{locked=false;if(current===id)buttons.forEach(b=>b.disabled=false);}}
      form.onsubmit=e=>{e.preventDefault();void act(()=>perform(async()=>{
        const value={name:document.getElementById('app-name').value,instructions:document.getElementById('app-instructions').value,app_id:document.getElementById('app-id').value,app_secret:document.getElementById('app-secret').value};
        document.getElementById('app-secret').value='';
        await api('projects/'+id+'/application/configure',value);
        if(current===id)status.textContent='配置已保存。请检查配置，再启动并在 QQ 中试聊。';
      }));};
      for(const [action,label] of [['check','检查配置'],['start','启动'],['stop','停止']]){
        const b=document.createElement('button');b.type='button';b.textContent=label;
        b.onclick=()=>act(()=>perform(async()=>{const result=await api('projects/'+id+'/application/'+action,{});if(current===id)(action==='check'?checks:status).textContent=describe(result);}));actions.append(b);
      }
      panel.append(actions);
      const value=await api('projects/'+id+'/application');
      if(current!==id||generation!==epoch)return;
      document.getElementById('app-name').value=value.name||app.name;
      document.getElementById('app-instructions').value=value.instructions||'';
      document.getElementById('app-id').value=value.app_id||'';
      status.textContent=describe(value);
    } else if(!locked){
      const value=await api('projects/'+id+'/application');
      if(current===id&&generation===epoch)document.getElementById('application-status').textContent=describe(value);
    }
  }
  function describe(value){
    if(Array.isArray(value.checks))return value.checks.map(row=>(row.ok?'✓ ':'待处理：')+row.message).join('；');
    const label={not_created:'先通过对话描述用途，或填写下面的配置。',configured:'配置已保存。',running_unverified:'进程运行中，待实聊验证。',stopped:'已停止。',failed:'启动失败。',runtime_unavailable:'无法读取本机运行状态。',stopping:'正在停止。',starting:'正在启动。'}[value.state]||'状态待确认。';
    return label+(value.message?' '+value.message:'');
  }

  return {render,reset};
})();
