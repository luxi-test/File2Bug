(() => {
  const $ = selector => document.querySelector(selector);
  let toastTimer;
  function toast(message, error = false) {
    const node = $('#toast'); node.textContent = message; node.className = 'toast' + (error ? ' error' : ''); node.hidden = false;
    clearTimeout(toastTimer); toastTimer = setTimeout(() => { node.hidden = true; }, error ? 8000 : 3500);
  }
  async function api(path, method = 'GET', body) {
    const multipart = body instanceof FormData;
    const response = await fetch(path, {method, headers: multipart ? {} : {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: multipart ? body : JSON.stringify(body)})});
    let data; try { data = await response.json(); } catch (_) { throw new Error('请求未完成，请稍后检查处理结果。'); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '输入内容不符合要求，请检查后重试。');
    return data;
  }
  async function withButton(button, label, callback) {
    const original = button.textContent; button.disabled = true; button.textContent = label;
    try { await callback(); } catch (error) { toast(error.message, true); }
    finally { button.disabled = false; button.textContent = original; }
  }
  $('#login-form')?.addEventListener('submit', event => {
    event.preventDefault(); withButton(event.target.querySelector('button'), '正在进入…', async () => { await api('/api/login', 'POST', {access_key: $('#access-key').value}); location.reload(); });
  });
  $('#import-form')?.addEventListener('submit', event => {
    event.preventDefault(); withButton(event.target.querySelector('button'), '正在读取…', async () => { const batch = await api('/api/batches', 'POST', {url: $('#sheet-url').value.trim()}); location.href = `/batches/${batch.id}`; });
  });
  $('#demo-button')?.addEventListener('click', event => {
    withButton(event.currentTarget, '正在准备…', async () => { const batch = await api('/api/batches/demo', 'POST'); location.href = `/batches/${batch.id}`; });
  });
  $('#local-import-form')?.addEventListener('submit',event=>{
    event.preventDefault();withButton(event.target.querySelector('button'),'正在导入…',async()=>{
      const form=new FormData();form.append('file',$('#local-file').files[0]);form.append('sheet',$('#local-sheet').value);
      const batch=await api('/api/batches/file','POST',form);location.href=`/batches/${batch.id}`;
    });
  });
  if (!$('#batch-data')) return;
  let batch = JSON.parse($('#batch-data').textContent), dirty = false, busy = false, editingId = null;
  const coreFields = ['title', 'priority', 'preconditions', 'steps', 'expected', 'actual', 'evidence'];
  const metadata = {module: '模块', submodule: '子模块', problem_type: '问题类型', frequency: '发生概率'};
  const fields = [...coreFields, 'test_data', ...Object.keys(metadata)];
  const defaultChoices = {}, itemChoices = {};
  const escape = value => String(value ?? '').replace(/[&<>"']/g, character => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[character]));
  const display = value => String(value || '').replace(/\n/g, '、');
  const effective = (item, key) => item[key] || '';
  function reproParts(item) {
    let steps=item.steps||'', premise=item.preconditions||'', data=item.test_data||'';
    if (/^(前置条件|测试数据|操作步骤)\n/.test(steps)) {
      const parts=steps.split(/(?:^|\n\n)(前置条件|测试数据|操作步骤)\n/), found={};
      for(let index=1;index<parts.length-1;index+=2)found[parts[index]]=parts[index+1].trim();
      premise=premise||found['前置条件']||'';data=data||found['测试数据']||'';steps=found['操作步骤']||steps;
    }
    if(data)premise+=(premise?'\n':'')+'测试数据：'+data;
    return {premise:premise.trim(),steps:steps.trim()};
  }
  function formatRepro(item) {
    const parts=reproParts(item);
    return [['前提',parts.premise],['操作',parts.steps],['预期结果',(item.expected||'').trim()],['实际结果',(item.actual||'').trim()]].map(([label,value])=>`【${label}】：\n${value}`).join('\n\n');
  }
  function missing(item) {
    if (item.status === 'unknown') return [];
    const values = [];
    if (!item.record_id) for (const [key, label] of Object.entries({title:'标题',steps:'复现步骤',expected:'预期结果',actual:'实际结果'})) if (!String(item[key] || '').trim()) values.push(label);
    for (const [key, label] of Object.entries(metadata)) if (batch.default_hints?.[key]?.enabled && !effective(item, key).trim()) values.push(label);
    return values;
  }
  function metadataDirty(item) {
    if (!item.record_id) return false;
    const baseline = item.metadata_baseline || {};
    const snapshot = Object.fromEntries(Object.keys(baseline).map(key => [key, key === 'attachments' ? (item.attachments || []).map(entry => entry.id) : key === 'description' ? formatRepro(item) : effective(item, key)]));
    return JSON.stringify(snapshot) !== JSON.stringify(baseline);
  }
  const eligible = () => batch.items.filter(item => item.status !== 'succeeded' || metadataDirty(item));
  const selected = () => eligible().filter(item => item.selected);
  function status(item) {
    if (item.status === 'succeeded' && metadataDirty(item)) return ['incomplete', '已提单 · 待补充'];
    const map = {succeeded:['succeeded','已写入'],submitting:['submitting','写入中'],failed:['failed',item.record_id ? '补充失败' : '提交失败'],unknown:['unknown','结果待确认']};
    return map[item.status] || (missing(item).length ? ['incomplete','待补充'] : ['ready','内容已齐全']);
  }
  function showMessage(id, message = '') {
    const node = $(id);
    node.textContent = message;
    node.hidden = !message;
  }
  function markDirty() {
    dirty = true;
    showMessage('#save-status', '有未保存的修改');
    showMessage('#submit-error');
  }
  function render() {
    const rows = $('#draft-rows');
    if (rows) rows.innerHTML = batch.items.map(item => {
      const [kind, label] = status(item), absent = missing(item);
      const note = item.error || (absent.length ? '待补充：' + absent.join('、') : '');
      const canSelect = item.status !== 'succeeded' || metadataDirty(item);
      const attachmentCount = (item.attachments || []).length;
      const ref = [item.case_id || '缺少用例 ID', display(item.module)].filter(Boolean).join(' · ');
      return `<tr data-id="${escape(item.id)}">
        <td class="check-column"><input type="checkbox" class="row-select" aria-label="选择 ${escape(item.case_id)}" ${item.selected && canSelect ? 'checked' : ''} ${!canSelect || busy || item.status === 'submitting' ? 'disabled' : ''}></td>
        <td class="title-cell"><div class="bug-title">${escape(item.title || '缺少标题')}</div><div class="bug-ref">${escape(ref)}</div>
        ${note ? `<div class="row-note">${escape(note)}</div>` : ''}${item.record_url ? `<a class="record-link" href="${escape(item.record_url)}" target="_blank" rel="noopener noreferrer">查看飞书缺陷 ↗</a>` : ''}</td>
        <td class="priority-cell" data-label="优先级"><span class="priority ${escape(item.priority.toLowerCase())}">${escape(item.priority)}</span></td>
        <td class="evidence-cell" data-label="附件">${attachmentCount ? `${attachmentCount} 个文件` : item.evidence ? '仅备注' : '<span class="muted-text">无</span>'}</td>
        <td class="state-cell"><span class="status ${kind}">${label}</span></td>
        <td class="action-cell"><button class="row-button" aria-label="${item.status === 'unknown' ? '查看' : '检查'} ${escape(item.case_id)}" ${busy ? 'disabled' : ''}>${item.status === 'unknown' ? '查看' : '检查'}</button></td>
      </tr>`;
    }).join('');
    const picked = selected(), updates = picked.filter(item => item.record_id).length;
    const incomplete = picked.filter(item => missing(item).length).length;
    const actions = [picked.length > updates && `新建 ${picked.length - updates} 条`, updates && `更新 ${updates} 条`, incomplete && `${incomplete} 条待补充`].filter(Boolean);
    $('#submit-summary').textContent = actions.join(' · ') || '请选择要写入的缺陷';
    const moduleHint = batch.default_hints?.module;
    const newModules = batch.mode === 'live' && moduleHint?.type === 4
      ? [...new Set(picked.filter(item => item.status !== 'unknown').flatMap(item => effective(item, 'module').split('\n').map(value => value.trim()).filter(value => value && !moduleHint.options.includes(value))))]
      : [];
    showMessage('#new-module-note', newModules.length ? `写入时将新增模块选项：${newModules.join('、')}` : '');
    $('#submit-button').disabled = busy || !picked.length || batch.items.some(item => item.status === 'submitting');
    if (!busy) $('#submit-button').textContent = batch.mode === 'demo' ? '模拟写入' : updates && updates === picked.length ? '确认更新' : '确认写入';
    $('#save-button').disabled = busy || !dirty || batch.items.some(item => item.status === 'submitting');
    $('#select-all').checked = eligible().length > 0 && eligible().every(item => item.selected);
    $('#select-all').indeterminate = picked.length > 0 && picked.length < eligible().length;
    $('#select-all').disabled = busy || !eligible().length;
    document.querySelectorAll('[data-default]').forEach(input => { input.disabled = busy; });
    Object.values(defaultChoices).forEach(control => control.disable(busy));
  }
  document.querySelectorAll('[data-choice-default]').forEach(host => {
    const key = host.dataset.choiceDefault, hint = batch.default_hints[key];
    defaultChoices[key] = window.F2BChoices(host, {label:host.dataset.label, options:hint.options, multiple:hint.type===4, value:batch.defaults[key] || '', onChange:value=>{batch.defaults[key]=value;markDirty();render();}});
  });
  document.querySelectorAll('[data-default]').forEach(input=>input.addEventListener('input',()=>{batch.defaults[input.dataset.default]=input.value;markDirty();render();}));
  $('#draft-rows')?.addEventListener('change', event => {
    if (!event.target.matches('.row-select')) return;
    const row = event.target.closest('tr');
    const index = row.sectionRowIndex;
    batch.items.find(item => item.id === row.dataset.id).selected = event.target.checked;
    markDirty();
    render();
    $('#draft-rows').rows[index]?.querySelector('.row-select')?.focus();
  });
  $('#select-all').addEventListener('change',event=>{eligible().forEach(item=>{if(item.status!=='submitting')item.selected=event.target.checked;});markDirty();render();});
  function attachmentList() {
    const item = batch.items.find(item=>item.id===editingId); if(!item)return;
    const locked = ['unknown','submitting'].includes(item.status);
    $('#attachment-input').disabled = busy || locked;
    $('#attachment-list').innerHTML = (item.attachments || []).map(entry=>{
      const url=`/api/batches/${batch.id}/items/${item.id}/attachments/${entry.id}`;
      return `<div class="attachment-entry">${entry.content_type.startsWith('image/') ? `<img src="${url}" alt="${escape(entry.name)}" loading="lazy">` : `<span class="video-icon">${entry.content_type.startsWith('video/') ? '▶' : '▤'}</span>`}<div><a href="${url}" target="_blank" rel="noopener">${escape(entry.name)} ↗</a><small>${(entry.size/1024/1024).toFixed(2)} MB · ${entry.linked ? '已写入缺陷' : '待确认写入'}</small></div>${!entry.linked && !locked ? `<button type="button" data-remove-file="${entry.id}" ${busy?'disabled':''}>移除</button>` : ''}</div>`;
    }).join('');
  }
  function readDialog() {
    const item=batch.items.find(item=>item.id===editingId); if(!item || ['unknown','submitting'].includes(item.status))return;
    coreFields.forEach(key=>{if(!item.record_id || ['preconditions','steps','expected','actual'].includes(key))item[key]=$('#edit-form').elements.namedItem(key).value.trim();});
    item.test_data='';
    Object.entries(itemChoices).forEach(([key,control])=>{item[key]=control.getValue();});
    markDirty();
  }
  function openItem(id) {
    const item=batch.items.find(item=>item.id===id); editingId=id;
    $('#edit-case').textContent=`${item.case_id} · 源表第 ${item.row_no} 行`;
    $('#edit-heading').textContent=item.record_id ? '补充已提缺陷字段' : '检查缺陷内容';
    $('.evidence-details').open = false;
    const frozen=['unknown','submitting'].includes(item.status), coreLocked=frozen || Boolean(item.record_id), parts=reproParts(item);
    coreFields.forEach(key=>{const input=$('#edit-form').elements.namedItem(key);input.value=key==='preconditions'?parts.premise:key==='steps'?parts.steps:item[key]||'';if(input.tagName==='SELECT')input.disabled=coreLocked;else input.readOnly=frozen || (Boolean(item.record_id) && ['title','evidence'].includes(key));});
    for(const key of Object.keys(itemChoices))delete itemChoices[key];
    document.querySelectorAll('[data-choice-item]').forEach(host=>{
      const key=host.dataset.choiceItem,hint=batch.default_hints[key];
      const options=[...hint.options];
      const newModule = key === 'module' && hint.type === 4 && (item.module || '').split('\n').some(value => value && !options.includes(value));
      if (newModule) options.push(...item.module.split('\n').filter(Boolean));
      if(key==='submodule' && item.submodule===display(item.module) && !options.includes(item.submodule))options.push(item.submodule);
      const value = item[key] || '';
      if (hint.type === 1) {
        const input = document.createElement('input');
        input.setAttribute('aria-label', host.dataset.label);
        input.value = value;
        input.readOnly = frozen;
        host.replaceChildren(input);
        itemChoices[key] = {getValue: () => input.value.trim()};
      } else {
        itemChoices[key] = window.F2BChoices(host, {label:host.dataset.label, options, multiple:hint.type===4, value, placeholder:'请选择'});
        itemChoices[key].disable(frozen);
      }
      const message = newModule && batch.mode === 'live' ? '保留源表模块名，确认写入后加入缺陷表。'
        : key === 'frequency' && item.inference?.frequency?.source === 'default' ? '缺少复现依据，已填默认值。' : '';
      if (message) {
        const note = document.createElement('span');
        note.className = 'helper';
        note.textContent = message;
        host.append(note);
      }
    });
    $('#apply-edit').hidden=frozen;
    $('#apply-edit').textContent=item.record_id?'完成补充':'完成修改';
    showMessage('#edit-note', frozen ? '请先重试原操作，确认写入结果。' : '');
    attachmentList();$('#edit-dialog').showModal();
  }
  $('#draft-rows')?.addEventListener('click',event=>{if(event.target.closest('.row-button'))openItem(event.target.closest('tr').dataset.id);});
  $('#close-dialog').addEventListener('click',()=>$('#edit-dialog').close());
  $('#edit-form').addEventListener('submit',async event=>{event.preventDefault();readDialog();busy=true;render();try{await save();$('#edit-dialog').close();}catch(error){showMessage('#edit-note', error.message);}finally{busy=false;render();}});
  async function save() {
    const payload={revision:batch.revision,defaults:batch.defaults,items:batch.items.map(item=>({id:item.id,selected:item.selected,...Object.fromEntries(fields.map(key=>[key,item[key]||'']))}))};
    batch=await api(`/api/batches/${batch.id}`,'PUT',payload);dirty=false;showMessage('#save-status');
  }
  $('#save-button').addEventListener('click',async()=>{busy=true;render();try{await save();toast('修改已保存');}catch(error){toast(error.message,true);}finally{busy=false;render();}});
  $('#attachment-input').addEventListener('change',async event=>{
    const files=[...event.target.files];if(!files.length)return;
    const id=editingId;readDialog();busy=true;render();attachmentList();
    try {
      await save();
      for(const file of files){const form=new FormData();form.append('file',file);form.append('revision',String(batch.revision));batch=await api(`/api/batches/${batch.id}/items/${id}/attachments`,'POST',form);}
      toast('文件已加入草稿，确认写入后会上传并绑定到缺陷。');
    }catch(error){toast(error.message,true);}finally{busy=false;event.target.value='';render();attachmentList();}
  });
  $('#attachment-list').addEventListener('click',async event=>{
    const button=event.target.closest('[data-remove-file]');if(!button)return;
    const id=editingId;readDialog();busy=true;render();
    try{await save();batch=await api(`/api/batches/${batch.id}/items/${id}/attachments/${button.dataset.removeFile}`,'DELETE',{revision:batch.revision});}catch(error){toast(error.message,true);}finally{busy=false;render();attachmentList();}
  });
  $('#submit-button').addEventListener('click',async()=>{
    const absent=selected().find(item=>missing(item).length);if(absent){openItem(absent.id);showMessage('#edit-note', `请补充：${missing(absent).join('、')}`);return;}
    if(selected().some(item=>!item.record_id && item.status!=='unknown')){
      const labels={reporter:'提交人',version:'测试版本',device:'测试设备',environment:'测试环境'};
      const key=Object.keys(labels).find(key=>!String(batch.defaults[key]||'').trim());
      if(key){toast(`请补充${labels[key]}。`,true);return;}
    }
    showMessage('#submit-error');busy=true;render();$('#submit-button').textContent='正在写入…';
    try {
      await save();
      batch=await api(`/api/batches/${batch.id}/submit`,'POST',{revision:batch.revision});
      if (batch.items.some(item => ['failed','unknown'].includes(item.status))) showMessage('#submit-error','请查看清单中的写入失败或待确认项。');
      else toast('写入完成');
    } catch(error) {
      showMessage('#submit-error',error.message);
      if(!dirty){try{batch=await api(`/api/batches/${batch.id}`);}catch(_){}}
    }
    finally{busy=false;render();if(batch.items.some(item=>item.status==='submitting'))poll();}
  });
  async function poll(){setTimeout(async()=>{try{batch=await api(`/api/batches/${batch.id}`);render();if(batch.items.some(item=>item.status==='submitting'))poll();}catch(error){toast('暂时无法读取结果，请恢复连接后刷新。',true);}},1500);}
  window.addEventListener('beforeunload',event=>{if(dirty){event.preventDefault();event.returnValue='';}});
  render();if(batch.items.some(item=>item.status==='submitting'))poll();
})();
