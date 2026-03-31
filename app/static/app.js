const page = document.body.dataset.page;

function fmtMoney(value) {
  const num = Number(value || 0);
  return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'USD' }).format(num);
}

function fmtNum(value, digits = 4) {
  return Number(value || 0).toLocaleString('pt-BR', { maximumFractionDigits: digits });
}

function toast(targetId, message, isError = false) {
  const el = document.getElementById(targetId);
  if (!el) return;
  el.textContent = message;
  el.classList.remove('hidden');
  el.classList.toggle('alert-error', isError);
}

async function fetchJson(url, options = {}) {
  const response = await fetch(url, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  });
  if (!response.ok) {
    let message = `Erro ${response.status}`;
    try {
      const data = await response.json();
      message = data.error || JSON.stringify(data);
    } catch (_) {}
    throw new Error(message);
  }
  return response.json();
}

async function systemAction(action) {
  try {
    await fetchJson('/api/system/action', {
      method: 'POST',
      body: JSON.stringify({ action }),
    });
    if (page === 'dashboard') await loadDashboard();
    if (page === 'training') await refreshModels();
    if (page === 'traces') await loadTraces();
  } catch (error) {
    alert(error.message);
  }
}

async function loadDashboard() {
  const data = await fetchJson('/api/dashboard');
  document.getElementById('metric-status').textContent = data.runtime.system_status;
  document.getElementById('metric-mode').textContent = data.runtime.mode;
  document.getElementById('metric-portfolio').textContent = fmtMoney(data.portfolio_value);
  document.getElementById('metric-pnl').textContent = fmtMoney(data.pnl_total);

  const positionsBody = document.getElementById('positions-body');
  positionsBody.innerHTML = data.positions.length ? data.positions.map(row => `
    <tr>
      <td>${row.symbol}</td>
      <td>${fmtNum(row.quantity, 6)}</td>
      <td>${fmtMoney(row.avg_price)}</td>
      <td>${fmtMoney(row.current_price)}</td>
      <td class="${row.variation_pct >= 0 ? 'positive' : 'negative'}">${fmtNum(row.variation_pct, 2)}%</td>
      <td class="${row.pnl_usdt >= 0 ? 'positive' : 'negative'}">${fmtMoney(row.pnl_usdt)}</td>
    </tr>
  `).join('') : '<tr><td colspan="6">Sem posições abertas.</td></tr>';

  const ordersBody = document.getElementById('orders-body');
  ordersBody.innerHTML = data.orders.length ? data.orders.map(row => `
    <tr>
      <td>${row.created_at}</td>
      <td>${row.symbol}</td>
      <td>${row.side}</td>
      <td>${fmtNum(row.quantity, 6)}</td>
      <td>${fmtMoney(row.price)}</td>
      <td>${fmtNum(row.fee_amount, 6)} ${row.fee_asset || ''}</td>
      <td>${row.status}</td>
    </tr>
  `).join('') : '<tr><td colspan="7">Sem ordens.</td></tr>';

  const modelsList = document.getElementById('models-list');
  modelsList.innerHTML = data.models.map(model => `
    <div class="model-card">
      <div>
        <strong>${model.name}</strong> ${model.is_active ? '<span class="pill success">ativo</span>' : ''}
        <div class="muted">${model.model_type} • ${model.updated_at}</div>
      </div>
    </div>
  `).join('');

  const runtimeList = document.getElementById('runtime-list');
  const runtime = data.runtime;
  const items = [
    ['Último mercado', runtime.last_market_update],
    ['Último social', runtime.last_social_update],
    ['Último RSS', runtime.last_rss_update],
    ['Últimas features', runtime.last_feature_update],
    ['Última inferência', runtime.last_inference_update],
    ['Última ordem', runtime.last_order_update],
    ['Modelo ativo', runtime.active_model_id],
    ['Símbolos ativos', (runtime.active_symbols || []).join(', ')],
  ];
  runtimeList.innerHTML = items.map(([k, v]) => `<div class="model-card"><strong>${k}</strong><div class="muted">${v || '-'}</div></div>`).join('');
}

async function saveConfig() {
  try {
    await fetchJson('/api/config/save', {
      method: 'POST',
      body: JSON.stringify({
        config_yaml: document.getElementById('config-yaml').value,
        symbols_yaml: document.getElementById('symbols-yaml').value,
      }),
    });
    toast('config-feedback', 'Configurações salvas com sucesso.');
  } catch (error) {
    toast('config-feedback', error.message, true);
  }
}

async function changePassword() {
  const password = prompt('Digite a nova senha do painel:');
  if (!password) return;
  try {
    await fetchJson('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ new_password: password }),
    });
    toast('config-feedback', 'Senha alterada com sucesso.');
  } catch (error) {
    toast('config-feedback', error.message, true);
  }
}

function renderTraceRows(rows, targetId) {
  const container = document.getElementById(targetId);
  if (!container) return;
  container.innerHTML = rows.map(row => `
    <div class="trace-item">
      <div class="trace-meta">
        <span>${row.timestamp || ''}</span>
        <span>${row.component || ''}</span>
        <span>${row.event_type || ''}</span>
        <span>${row.symbol || '-'}</span>
        <span>${row.level || ''}</span>
      </div>
      <div><strong>${row.message || ''}</strong></div>
      <pre>${JSON.stringify(row.data || {}, null, 2)}</pre>
    </div>
  `).join('');
}

async function loadTraces() {
  try {
    const symbol = document.getElementById('trace-symbol').value;
    const level = document.getElementById('trace-level').value;
    const params = new URLSearchParams({ limit: 200 });
    if (symbol) params.set('symbol', symbol);
    if (level) params.set('level', level);
    const data = await fetchJson(`/api/traces?${params.toString()}`);
    renderTraceRows(data.rows, 'trace-list');
  } catch (error) {
    alert(error.message);
  }
}

function initLiveLogs() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  const socket = new WebSocket(`${protocol}://${window.location.host}/ws/logs`);
  socket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    renderTraceRows(payload.rows || [], 'live-trace-list');
  };
}

async function trainModel() {
  const modelName = document.getElementById('model-name').value || `Modelo ${new Date().toISOString()}`;
  const modelType = document.getElementById('model-type').value;
  try {
    const response = await fetchJson('/api/models/train', {
      method: 'POST',
      body: JSON.stringify({ model_name: modelName, model_type: modelType }),
    });
    toast('training-feedback', `Treinamento concluído: ${response.model.name}`);
    await refreshModels();
  } catch (error) {
    toast('training-feedback', error.message, true);
  }
}

async function refreshModels() {
  const data = await fetchJson('/api/models');
  const container = document.getElementById('training-models');
  if (!container) return;
  container.innerHTML = data.models.map(model => `
    <div class="model-card">
      <div>
        <h4>${model.name} ${model.is_active ? '<span class="pill success">ativo</span>' : ''}</h4>
        <p class="muted">${model.model_type} • ${model.updated_at}</p>
        <pre>${JSON.stringify(model.metrics || {}, null, 2)}</pre>
      </div>
      <div class="actions vertical">
        <button class="btn btn-secondary" onclick="activateModel('${model.id}')">Selecionar como ativo</button>
        <button class="btn btn-danger" onclick="deleteModel('${model.id}')">Excluir modelo</button>
      </div>
    </div>
  `).join('');
}

async function activateModel(modelId) {
  try {
    await fetchJson('/api/models/activate', {
      method: 'POST',
      body: JSON.stringify({ model_id: modelId }),
    });
    await refreshModels();
    toast('training-feedback', `Modelo ${modelId} ativado.`);
  } catch (error) {
    toast('training-feedback', error.message, true);
  }
}

async function deleteModel(modelId) {
  if (!confirm(`Excluir o modelo ${modelId}?`)) return;
  try {
    await fetchJson('/api/models/delete', {
      method: 'POST',
      body: JSON.stringify({ model_id: modelId }),
    });
    await refreshModels();
    toast('training-feedback', `Modelo ${modelId} excluído.`);
  } catch (error) {
    toast('training-feedback', error.message, true);
  }
}

document.addEventListener('DOMContentLoaded', async () => {
  if (page === 'dashboard') {
    await loadDashboard();
    setInterval(loadDashboard, 5000);
  }
  if (page === 'traces') {
    await loadTraces();
    initLiveLogs();
  }
  if (page === 'training') {
    await refreshModels();
  }
});
