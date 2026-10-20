'use strict';
(() => {
  const byId = id => document.getElementById(id);
  let state, original, draft, category = 'all', dirty = false, busy = false;
  let refreshTimer;
  const modeValue = policy => policy.mode === 'node' ? policy.node : policy.mode;
  const policyFor = id => draft[id] || {mode: 'default'};
  const element = (tag, className, text) => {
    const item = document.createElement(tag);
    if (className) item.className = className;
    if (text !== undefined) item.textContent = text;
    return item;
  };
  function message(text, error = false) {
    const box = byId('route-message'); box.hidden = false;
    box.className = 'route-message' + (error ? ' is-error' : ''); box.textContent = text;
  }
  async function request(payload) {
    const response = await fetch('act/serviceRoutes.php', payload ? {
      method: 'POST', credentials: 'same-origin', headers: {'Content-Type': 'application/json', 'X-GWD-CSRF': state.csrf},
      body: JSON.stringify(payload)
    } : {credentials: 'same-origin', cache: 'no-store'});
    let body;
    try { body = await response.json(); } catch (_) { throw new Error('暂时无法读取配置，请确认服务在线后重试。'); }
    if (!response.ok) throw new Error(body.error || '操作未完成，请重试。');
    return body;
  }
  function options(select, value) {
    select.replaceChildren();
    [{id: 'default', name: '跟随默认节点'}, {id: 'direct', name: '直接连接'}, ...state.nodes].forEach(node => {
      const option = element('option', '', node.name); option.value = node.id; select.append(option);
    });
    if (!Array.from(select.options).some(option => option.value === value)) {
      const option = element('option', '', '所选节点已移除，请重新选择'); option.value = value; select.append(option);
    }
    select.value = value;
  }
  function visible() {
    const query = byId('route-search').value.trim().toLowerCase();
    return state.services.filter(service => (category === 'all' || category === service.category)
      && (service.name + ' ' + service.id).toLowerCase().includes(query));
  }
  function changed() {
    dirty = JSON.stringify(draft) !== JSON.stringify(original);
    byId('route-save').disabled = busy || !dirty;
    byId('route-discard').disabled = busy || !dirty;
    byId('route-save-state').textContent = busy ? '正在校验并应用…' : dirty ? '有未保存的更改' : '配置已保存';
    byId('route-active-count').textContent = Object.values(draft).filter(policy => policy.mode !== 'default' || policy.devices?.length).length;
  }
  function setMode(id, value) {
    const old = policyFor(id);
    draft[id] = Object.assign({}, old, value.startsWith('node-') ? {mode: 'node', node: value} : {mode: value});
    if (draft[id].mode !== 'node') delete draft[id].node;
    changed();
  }
  function render() {
    const grid = byId('route-grid'), services = visible(); grid.replaceChildren();
    services.forEach(service => {
      const policy = policyFor(service.id), card = element('article', 'service-card');
      card.dataset.service = service.id;
      const heading = element('div', 'service-heading');
      heading.append(element('span', 'service-icon ' + service.category, service.name.slice(0, 1)));
      const title = element('div'); title.append(element('h2', '', service.name), element('span', 'service-category',
        state.categories.find(item => item.id === service.category)?.name || ''));
      heading.append(title); card.append(heading);
      const label = element('label', 'service-label', '出口策略');
      const select = element('select', 'service-select'); select.id = 'policy-' + service.id;
      label.htmlFor = select.id; options(select, modeValue(policy)); select.disabled = busy;
      select.addEventListener('change', () => setMode(service.id, select.value));
      card.append(label, select);
      if (service.devices) {
        const deviceLabel = element('label', 'service-label', '主机固定 IP（可选，每行一个）');
        const devices = element('textarea', 'service-devices'); devices.id = 'devices-' + service.id;
        devices.rows = 2; devices.placeholder = '例如 10.0.0.50'; devices.value = (policy.devices || []).join('\n');
        devices.disabled = busy; deviceLabel.htmlFor = devices.id;
        devices.addEventListener('input', () => {
          const values = Array.from(new Set(devices.value.split(/[\s,，]+/).filter(Boolean)));
          draft[service.id] = Object.assign({}, policyFor(service.id));
          if (values.length) draft[service.id].devices = values; else delete draft[service.id].devices;
          changed();
        });
        card.append(deviceLabel, devices);
      }
      card.append(element('p', 'service-source', service.geosite.length ? '社区域名库自动更新 · 专用域名补充' : '服务专用域名规则'));
      grid.append(card);
    });
    grid.setAttribute('aria-busy', 'false'); byId('route-empty').hidden = services.length !== 0;
    byId('route-visible-count').textContent = '显示 ' + services.length + ' 项服务';
  }
  async function load() {
    state = await request();
    draft = {};
    state.services.forEach(service => { draft[service.id] = state.policies[service.id] || {mode: 'default'}; });
    original = structuredClone(draft);
    byId('route-node-count').textContent = state.nodes.length;
    byId('route-udp').textContent = state.udpEnabled ? '已开启' : '未开启';
    showRules();
    byId('route-applied-time').textContent = state.lastApplied ? '上次应用 ' + new Date(state.lastApplied).toLocaleString('zh-CN') : '跟随当前默认节点';
    const tabs = byId('route-tabs'); tabs.replaceChildren();
    [{id:'all', name:'全部服务'}, ...state.categories].forEach(item => {
      const button = element('button', 'route-tab' + (category === item.id ? ' active' : ''), item.name);
      button.type = 'button'; button.setAttribute('aria-pressed', String(category === item.id));
      button.addEventListener('click', () => {
        category = item.id;
        tabs.querySelectorAll('button').forEach(tab => { tab.classList.remove('active'); tab.setAttribute('aria-pressed', 'false'); });
        button.classList.add('active'); button.setAttribute('aria-pressed', 'true'); render();
      }); tabs.append(button);
    });
    options(byId('route-bulk-node'), 'default'); changed(); render();
  }
  function showRules() {
    const stamp = state.rules.updated_at_utc;
    const progress = state.ruleUpdate || {};
    byId('route-rules-time').textContent = stamp ? new Date(stamp).toLocaleString('zh-CN', {month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit'}) : '等待首次更新';
    byId('refresh-rules').disabled = progress.state === 'running';
    byId('refresh-rules').textContent = progress.state === 'running' ? '正在更新…' : '立即更新 ↻';
    if (progress.state === 'failed') message(progress.message || '规则更新未完成，继续使用上次验证的规则。', true);
    if (progress.state === 'running' && !refreshTimer) refreshTimer = setTimeout(pollRules, 5000);
  }
  async function pollRules() {
    refreshTimer = null;
    try {
      const latest = await request();
      const previous = state.ruleUpdate?.state;
      state.rules = latest.rules; state.ruleUpdate = latest.ruleUpdate;
      showRules();
      if (previous === 'running' && latest.ruleUpdate?.state === 'success') message('规则库更新完成，已重新应用当前分流策略。');
    } catch (error) {
      byId('refresh-rules').disabled = false; message(error.message, true);
    }
  }
  byId('route-search').addEventListener('input', () => { if (state) render(); });
  byId('route-bulk-apply').addEventListener('click', () => {
    if (!state || busy) return;
    visible().forEach(service => setMode(service.id, byId('route-bulk-node').value)); render();
  });
  byId('route-discard').addEventListener('click', () => { draft = structuredClone(original); changed(); render(); });
  byId('route-save').addEventListener('click', async () => {
    busy = true; changed(); render();
    try { await request({action: 'save', policies: draft}); await load(); message('分流已应用。后续新建连接使用新策略，现有连接可能继续使用原出口。'); }
    catch (error) { message(error.message, true); }
    finally { busy = false; changed(); render(); }
  });
  byId('refresh-rules').addEventListener('click', async () => {
    if (!state) return;
    byId('refresh-rules').disabled = true;
    try {
      const result = await request({action:'refreshRules'}); message(result.message);
      state.ruleUpdate = {state:'running'}; showRules();
    }
    catch (error) { message(error.message, true); }
    finally { if (state.ruleUpdate?.state !== 'running') byId('refresh-rules').disabled = false; }
  });
  window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
  load().catch(error => message(error.message, true));
})();
