(() => {
  const form = document.querySelector('#settings-form');
  if (!form) return;
  const $ = selector => document.querySelector(selector);
  const inputs = [...form.querySelectorAll('[data-setting], [data-mapping], #new-app-secret')];
  const tabs = [...document.querySelectorAll('[role=tab]')];
  let saved, busy = false, dirty = false;
  const status = message => { $('#settings-status').textContent = message; };
  function error(message = '') { $('#settings-error').textContent = message; $('#settings-error').hidden = !message; }
  function buttons() { $('#save-config').disabled = busy || !dirty; $('#check-config').disabled = busy || !saved; }
  function tab(active, focus = false) {
    tabs.forEach(node => {
      const selected = node === active;
      node.setAttribute('aria-selected', String(selected)); node.tabIndex = selected ? 0 : -1;
      document.getElementById(node.getAttribute('aria-controls')).hidden = !selected;
    });
    if (focus) active.focus();
  }
  tabs.forEach((node, index) => {
    node.addEventListener('click', () => tab(node));
    node.addEventListener('keydown', event => {
      let next;
      if (['ArrowRight', 'ArrowDown'].includes(event.key)) next = (index + 1) % tabs.length;
      if (['ArrowLeft', 'ArrowUp'].includes(event.key)) next = (index + tabs.length - 1) % tabs.length;
      if (event.key === 'Home') next = 0;
      if (event.key === 'End') next = tabs.length - 1;
      if (next !== undefined) { event.preventDefault(); tab(tabs[next], true); }
    });
  });
  async function api(path, method = 'GET', body) {
    const response = await fetch(path, {method, credentials: 'same-origin', cache: 'no-store',
      headers: {'Content-Type': 'application/json'}, ...(body === undefined ? {} : {body: JSON.stringify(body)})});
    let data;
    try { data = await response.json(); } catch (_) { throw new Error('请求未完成，请刷新后检查当前配置。'); }
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '配置未保存，请检查后重试。');
    return data;
  }
  function fill(data) {
    saved = data;
    for (const node of inputs) {
      if (node.id === 'new-app-secret') { node.value = ''; continue; }
      let value = node.dataset.mapping ? data.fields[node.dataset.mapping] : data.values[node.dataset.setting];
      if (node.dataset.setting === 'allowed_users') value = value.split(',').filter(Boolean).join('\n');
      if (node.type === 'checkbox') node.checked = Boolean(value); else node.value = value ?? '';
    }
    $('#secret-status').textContent = data.secrets.app_secret_configured ? '•••••••• 已配置' : '未配置';
    dirty = false; buttons();
  }
  function payload() {
    const values = {}, fields = {};
    for (const node of inputs) {
      if (node.dataset.mapping) fields[node.dataset.mapping] = node.value.trim();
      else if (node.dataset.setting) values[node.dataset.setting] = node.type === 'checkbox' ? node.checked : node.type === 'number' ? Number(node.value) : node.value.trim();
    }
    const result = {revision: saved.revision, values, fields};
    if ($('#new-app-secret').value) result.app_secret = $('#new-app-secret').value;
    return result;
  }
  for (const node of inputs) node.addEventListener('input', () => { dirty = true; status('有未保存的修改'); error(); buttons(); });
  async function perform(save) {
    if (busy || !saved) return;
    for (const input of inputs) if (!input.checkValidity()) {
      const panel = input.closest('[role=tabpanel]'); if (panel) tab(tabs.find(node => node.getAttribute('aria-controls') === panel.id));
      input.reportValidity(); return;
    }
    busy = true; inputs.forEach(node => { node.disabled = true; }); buttons(); error(); status(save ? '正在验证并保存…' : '正在检查连接…');
    try {
      const result = await api(save ? '/api/admin/config' : '/api/admin/check', save ? 'PUT' : 'POST', payload());
      if (save) { fill(result); status(result.mode === 'demo' ? '已保存 · 演示模式' : '配置已保存并应用');
        $('#settings-help').textContent = '网页立即生效；机器人在当前任务结束后自动重连。';
      } else status(result.message);
    } catch (failure) { error(failure.message); status(save ? '配置未保存' : '连接检查未通过'); }
    finally { busy = false; inputs.forEach(node => { node.disabled = false; }); buttons(); }
  }
  form.addEventListener('submit', event => { event.preventDefault(); perform(true); });
  $('#check-config').addEventListener('click', () => perform(false));
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  api('/api/admin/config').then(data => {
    fill(data); $('#settings-loading').hidden = true; $('#settings-editor').hidden = false;
    if (data.mode === 'demo') status('演示配置 · 不连接飞书');
  }).catch(failure => { $('#settings-loading').textContent = '配置读取失败'; error(failure.message); });
})();
