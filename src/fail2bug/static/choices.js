(() => {
  window.F2BChoices = function(host, config) {
    let value = config.value || '';
    let disabled = false;
    const options = [...new Set(config.options || [])];
    host.replaceChildren();
    if (!config.multiple) {
      const select = document.createElement('select');
      select.setAttribute('aria-label', config.label);
      if (host.id) select.id = host.id + '-control';
      select.append(new Option(config.placeholder || '请选择', ''));
      for (const option of options) select.append(new Option(option, option));
      function set(next) {
        value = next || '';
        if (value && !options.includes(value) && ![...select.options].some(option => option.value === value)) {
          const option = new Option(`${value}（未匹配，请重新选择）`, value);
          option.disabled = true;
          select.append(option);
        }
        select.value = value;
      }
      select.addEventListener('change', () => { value = select.value; config.onChange?.(value); });
      host.append(select); set(value);
      return {getValue: () => value, setValue: set, disable: next => { select.disabled = next; }};
    }
    const details = document.createElement('details'); details.className = 'multi-select';
    const summary = document.createElement('summary'); summary.setAttribute('aria-label', config.label);
    if (host.id) summary.id = host.id + '-control';
    const text = document.createElement('span'); summary.append(text);
    const panel = document.createElement('div'); panel.className = 'multi-panel';
    const search = document.createElement('input'); search.type = 'search'; search.placeholder = '搜索选项'; search.setAttribute('aria-label', `搜索${config.label}`);
    const actions = document.createElement('div'); actions.className = 'multi-actions';
    const count = document.createElement('span');
    const clear = document.createElement('button'); clear.type = 'button'; clear.textContent = '清空选择';
    const list = document.createElement('div'); list.className = 'multi-options';
    actions.append(count, clear); panel.append(search, actions, list); details.append(summary, panel); host.append(details);
    const selected = () => value.split('\n').filter(Boolean);
    function updateSummary() {
      const values = selected(); text.textContent = values.join('、') || config.placeholder || '请选择，可多选';
      text.className = values.length ? '' : 'placeholder';
      summary.title = values.join('、'); count.textContent = `已选 ${values.length} 项`;
    }
    function drawOptions() {
      const picked = selected(); const query = search.value.trim().toLowerCase(); list.replaceChildren();
      const available = [...options, ...picked.filter(option => !options.includes(option))].filter(option => option.toLowerCase().includes(query));
      for (const option of available) {
        const label = document.createElement('label');
        const checkbox = document.createElement('input'); checkbox.type = 'checkbox'; checkbox.checked = picked.includes(option); checkbox.disabled = disabled;
        const name = document.createElement('span'); name.textContent = option + (options.includes(option) ? '' : '（未匹配）');
        checkbox.addEventListener('change', () => {
          const values = selected(); value = (checkbox.checked ? [...new Set([...values, option])] : values.filter(entry => entry !== option)).join('\n');
          updateSummary(); config.onChange?.(value);
        });
        label.append(checkbox, name); list.append(label);
      }
      if (!available.length) { const empty = document.createElement('p'); empty.textContent = '没有匹配的选项'; list.append(empty); }
    }
    summary.addEventListener('click', event => { if (disabled) event.preventDefault(); });
    details.addEventListener('toggle', () => { if (details.open) { search.value = ''; drawOptions(); } });
    search.addEventListener('input', drawOptions);
    clear.addEventListener('click', () => { value = ''; updateSummary(); drawOptions(); config.onChange?.(value); });
    updateSummary(); drawOptions();
    return {
      getValue: () => value,
      setValue: next => { value = next || ''; updateSummary(); drawOptions(); },
      disable: next => { disabled = next; details.classList.toggle('disabled', next); details.setAttribute('aria-disabled', String(next)); search.disabled = clear.disabled = next; list.querySelectorAll('input').forEach(input => { input.disabled = next; }); if (next) details.open = false; },
    };
  };
})();
