const page = document.body.dataset.page;

let actionInProgress = false;
let feedbackTimer = null;
let dashboardRefreshTimer = null;
let dashboardRequestInFlight = null;
let liveSocket = null;

function fmtMoney(value) {
  const num = Number(value || 0);
  return new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'USD' }).format(num);
}

function fmtNum(value, digits = 4) {
  return Number(value || 0).toLocaleString('pt-BR', { maximumFractionDigits: digits });
}

function getPageFeedbackTarget() {
  const map = {
    dashboard: 'dashboard-feedback',
    training: 'training-feedback',
    traces: 'traces-feedback',
    config: 'config-feedback',
  };
  return map[page] || 'global-feedback';
}

function clearFeedback(targetId) {
  const el = document.getElementById(targetId);
  if (!el) return;
  el.textContent = '';
  el.classList.add('hidden');
  el.classList.remove('alert-error');
}

function showGlobalFeedback(message, isError = false) {
  const el = document.getElementById('global-feedback');
  if (!el) return;

  clearTimeout(feedbackTimer);
  el.textContent = message;
  el.classList.remove('hidden', 'error', 'success');
  el.classList.add(isError ? 'error' : 'success');

  feedbackTimer = window.setTimeout(() => {
    el.classList.add('hidden');
  }, 3600);
}

function toast(targetId, message, isError = false) {
  const el = document.getElementById(targetId);
  if (el) {
    el.textContent = message;
    el.classList.remove('hidden');
    el.classList.toggle('alert-error', isError);
  }
  showGlobalFeedback(message, isError);
}

function setLoadingOverlay(visible, message = 'Processando operação...') {
  const overlay = document.getElementById('app-loader');
  const text = document.getElementById('app-loader-text');
  if (!overlay) return;

  if (text) {
    text.textContent = message;
  }

  overlay.classList.toggle('hidden', !visible);
  overlay.classList.toggle('visible', visible);
  overlay.setAttribute('aria-hidden', visible ? 'false' : 'true');
  document.body.classList.toggle('is-busy', visible);
}

function setActionButtonsDisabled(disabled) {
  document.querySelectorAll('.js-action-btn').forEach((button) => {
    if (button.dataset.loading === '1') {
      return;
    }
    button.disabled = disabled;
    button.classList.toggle('is-disabled', disabled);
  });
}

function setButtonLoading(button, isLoading, loadingText = 'Processando...') {
  if (!button) return;

  if (isLoading) {
    if (!button.dataset.originalText) {
      button.dataset.originalText = button.innerHTML;
    }
    button.dataset.loading = '1';
    button.disabled = true;
    button.classList.add('is-loading');
    button.innerHTML = `<span class="btn-spinner" aria-hidden="true"></span><span>${loadingText}</span>`;
    return;
  }

  if (button.dataset.originalText) {
    button.innerHTML = button.dataset.originalText;
  }
  button.dataset.loading = '0';
  button.disabled = false;
  button.classList.remove('is-loading');
}

async function runAction(options, task) {
  const {
    button = null,
    feedbackId = getPageFeedbackTarget(),
    loadingText = 'Processando...',
    overlayText = 'Aguarde a conclusão da operação...',
    successMessage = '',
  } = options || {};

  if (actionInProgress) {
    toast(feedbackId, 'Já existe uma operação em andamento. Aguarde a conclusão antes de clicar novamente.', true);
    return null;
  }

  actionInProgress = true;
  clearFeedback(feedbackId);
  setActionButtonsDisabled(true);
  setButtonLoading(button, true, loadingText);
  setLoadingOverlay(true, overlayText);

  try {
    const result = await task();
    if (successMessage) {
      toast(feedbackId, successMessage, false);
    }
    return result;
  } catch (error) {
    const message = error?.message || 'Falha ao executar a operação.';
    toast(feedbackId, message, true);
    return null;
  } finally {
    setButtonLoading(button, false);
    setActionButtonsDisabled(false);
    setLoadingOverlay(false);
    actionInProgress = false;
  }
}

async function fetchJson(url, options = {}) {
  const {
    timeoutMs = 20000,
    headers = {},
    ...fetchOptions
  } = options;

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);

  try {
    const response = await fetch(url, {
      headers: { 'Content-Type': 'application/json', ...headers },
      signal: controller.signal,
      ...fetchOptions,
    });

    if (!response.ok) {
      let message = `Erro ${response.status}`;
      try {
        const data = await response.json();
        message = data.error || JSON.stringify(data);
      } catch (_) {}
      throw new Error(message);
    }

    if (response.status === 204) {
      return {};
    }

    return response.json();
  } catch (error) {
    if (error?.name === 'AbortError') {
      throw new Error('A operação demorou além do esperado. Tente novamente.');
    }
    throw error;
  } finally {
    window.clearTimeout(timeout);
  }
}

function actionLabels(action) {
  return {
    start: 'Inicialização do bot',
    stop: 'Parada do bot',
    start_simulated: 'Inicialização em modo simulado',
    start_real: 'Inicialização em modo real',
    reset_wallet: 'Reset da carteira simulada',
    reload_config: 'Recarga da configuração',
    refresh_market: 'Atualização de mercado',
    refresh_social: 'Atualização social',
  }[action] || 'Operação';
}

async function systemAction(action, button = null) {
  const label = actionLabels(action);
  const feedbackId = getPageFeedbackTarget();

  await runAction({
    button,
    feedbackId,
    loadingText: `${label}...`,
    overlayText: `${label} em andamento. Aguarde...`,
    successMessage: `${label} realizada com sucesso.`,
  }, async () => {
    await fetchJson('/api/system/action', {
      method: 'POST',
      body: JSON.stringify({ action }),
      timeoutMs: 30000,
    });

    if (page === 'dashboard') {
      await loadDashboard({ silent: true, force: true });
    }
    if (page === 'training') await refreshModels();
    if (page === 'traces') await loadTraces();
  });
}

async function loadDashboard(options = {}) {
  const { silent = false, force = false } = options;

  if (dashboardRequestInFlight && !force) {
    return dashboardRequestInFlight;
  }

  const promise = (async () => {
    const data = await fetchJson('/api/dashboard', {
      timeoutMs: 15000,
      headers: { 'Cache-Control': 'no-cache' },
    });

    const statusEl = document.getElementById('metric-status');
    const modeEl = document.getElementById('metric-mode');
    const portfolioEl = document.getElementById('metric-portfolio');
    const pnlEl = document.getElementById('metric-pnl');
    const positionsBody = document.getElementById('positions-body');
    const ordersBody = document.getElementById('orders-body');
    const modelsList = document.getElementById('models-list');
    const runtimeList = document.getElementById('runtime-list');

    if (!statusEl || !modeEl || !portfolioEl || !pnlEl || !positionsBody || !ordersBody || !modelsList || !runtimeList) {
      return data;
    }

    statusEl.textContent = data.runtime.system_status;
    modeEl.textContent = data.runtime.mode;
    portfolioEl.textContent = fmtMoney(data.portfolio_value);
    pnlEl.textContent = fmtMoney(data.pnl_total);

    positionsBody.innerHTML = data.positions.length ? data.positions.map((row) => `
      <tr>
        <td>${row.symbol}</td>
        <td>${fmtNum(row.quantity, 6)}</td>
        <td>${fmtMoney(row.avg_price)}</td>
        <td>${fmtMoney(row.current_price)}</td>
        <td class="${row.variation_pct >= 0 ? 'positive' : 'negative'}">${fmtNum(row.variation_pct, 2)}%</td>
        <td class="${row.pnl_usdt >= 0 ? 'positive' : 'negative'}">${fmtMoney(row.pnl_usdt)}</td>
      </tr>
    `).join('') : '<tr><td colspan="6">Sem posições abertas.</td></tr>';

    ordersBody.innerHTML = data.orders.length ? data.orders.map((row) => `
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

    modelsList.innerHTML = data.models.map((model) => `
      <div class="model-card">
        <div>
          <strong>${model.name}</strong> ${model.is_active ? '<span class="pill success">ativo</span>' : ''}
          <div class="muted">${model.model_type} • ${model.updated_at}</div>
        </div>
      </div>
    `).join('');

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
    runtimeList.innerHTML = items.map(([key, value]) => `<div class="model-card"><strong>${key}</strong><div class="muted">${value || '-'}</div></div>`).join('');

    return data;
  })();

  dashboardRequestInFlight = promise;

  try {
    return await promise;
  } catch (error) {
    if (!silent) {
      toast('dashboard-feedback', error?.message || 'Falha ao atualizar o dashboard.', true);
    }
    throw error;
  } finally {
    if (dashboardRequestInFlight === promise) {
      dashboardRequestInFlight = null;
    }
  }
}

function scheduleDashboardRefresh() {
  if (page !== 'dashboard') return;
  if (dashboardRefreshTimer) {
    window.clearTimeout(dashboardRefreshTimer);
  }

  dashboardRefreshTimer = window.setTimeout(async () => {
    try {
      if (!actionInProgress) {
        await loadDashboard({ silent: true });
      }
    } catch (_) {
      // feedback silencioso no auto refresh
    } finally {
      scheduleDashboardRefresh();
    }
  }, 5000);
}

async function saveConfig(button = null) {
  await runAction({
    button,
    feedbackId: 'config-feedback',
    loadingText: 'Salvando...',
    overlayText: 'Salvando as alterações de configuração...',
    successMessage: 'Configurações salvas com sucesso.',
  }, async () => {
    await fetchJson('/api/config/save', {
      method: 'POST',
      body: JSON.stringify({
        config_yaml: document.getElementById('config-yaml').value,
        symbols_yaml: document.getElementById('symbols-yaml').value,
      }),
      timeoutMs: 25000,
    });
  });
}

async function changePassword(button = null) {
  const password = prompt('Digite a nova senha do painel:');
  if (!password) return;

  await runAction({
    button,
    feedbackId: 'config-feedback',
    loadingText: 'Alterando senha...',
    overlayText: 'Atualizando a senha do painel...',
    successMessage: 'Senha alterada com sucesso.',
  }, async () => {
    await fetchJson('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify({ new_password: password }),
    });
  });
}

function renderTraceRows(rows, targetId) {
  const container = document.getElementById(targetId);
  if (!container) return;

  container.innerHTML = rows.map((row) => `
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

async function loadTraces(button = null) {
  const fetchTraces = async () => {
    const symbol = document.getElementById('trace-symbol').value;
    const level = document.getElementById('trace-level').value;
    const params = new URLSearchParams({ limit: 200 });
    if (symbol) params.set('symbol', symbol);
    if (level) params.set('level', level);

    const data = await fetchJson(`/api/traces?${params.toString()}`);
    renderTraceRows(data.rows, 'trace-list');
  };

  if (!button) {
    await fetchTraces();
    return;
  }

  await runAction({
    button,
    feedbackId: 'traces-feedback',
    loadingText: 'Buscando...',
    overlayText: 'Atualizando os traces filtrados...',
    successMessage: 'Consulta atualizada com sucesso.',
  }, fetchTraces);
}

function initLiveLogs() {
  const protocol = window.location.protocol === 'https:' ? 'wss' : 'ws';
  liveSocket = new WebSocket(`${protocol}://${window.location.host}/ws/logs`);
  liveSocket.onmessage = (event) => {
    const payload = JSON.parse(event.data);
    renderTraceRows(payload.rows || [], 'live-trace-list');
  };
}

async function trainModel(button = null) {
  const modelName = document.getElementById('model-name').value || `Modelo ${new Date().toISOString()}`;
  const modelType = document.getElementById('model-type').value;

  await runAction({
    button,
    feedbackId: 'training-feedback',
    loadingText: 'Treinando...',
    overlayText: 'Treinamento em andamento. Isso pode levar alguns instantes...',
    successMessage: '',
  }, async () => {
    const response = await fetchJson('/api/models/train', {
      method: 'POST',
      body: JSON.stringify({ model_name: modelName, model_type: modelType }),
      timeoutMs: 90000,
    });

    toast('training-feedback', `Treinamento concluído: ${response.model.name}`, false);
    await refreshModels();
  });
}

async function refreshModels() {
  const data = await fetchJson('/api/models');
  const container = document.getElementById('training-models');
  if (!container) return;

  container.innerHTML = data.models.map((model) => `
    <div class="model-card">
      <div>
        <h4>${model.name} ${model.is_active ? '<span class="pill success">ativo</span>' : ''}</h4>
        <p class="muted">${model.model_type} • ${model.updated_at}</p>
        <pre>${JSON.stringify(model.metrics || {}, null, 2)}</pre>
      </div>
      <div class="actions vertical">
        <button class="btn btn-secondary js-action-btn" onclick="activateModel('${model.id}', this)">Selecionar como ativo</button>
        <button class="btn btn-danger js-action-btn" onclick="deleteModel('${model.id}', this)">Excluir modelo</button>
      </div>
    </div>
  `).join('');
}

async function activateModel(modelId, button = null) {
  await runAction({
    button,
    feedbackId: 'training-feedback',
    loadingText: 'Ativando...',
    overlayText: 'Definindo o modelo como ativo...',
    successMessage: `Modelo ${modelId} ativado com sucesso.`,
  }, async () => {
    await fetchJson('/api/models/activate', {
      method: 'POST',
      body: JSON.stringify({ model_id: modelId }),
    });
    await refreshModels();
  });
}

async function deleteModel(modelId, button = null) {
  if (!confirm(`Excluir o modelo ${modelId}?`)) return;

  await runAction({
    button,
    feedbackId: 'training-feedback',
    loadingText: 'Excluindo...',
    overlayText: 'Removendo o modelo selecionado...',
    successMessage: `Modelo ${modelId} excluído com sucesso.`,
  }, async () => {
    await fetchJson('/api/models/delete', {
      method: 'POST',
      body: JSON.stringify({ model_id: modelId }),
    });
    await refreshModels();
  });
}

function triggerTraceExport(format, button = null) {
  if (actionInProgress) {
    toast('traces-feedback', 'Aguarde a conclusão da operação atual antes de exportar.', true);
    return;
  }

  setButtonLoading(button, true, 'Abrindo...');
  window.open(`/api/traces/export?format=${format}`, '_blank');
  toast('traces-feedback', `Exportação ${format.toUpperCase()} iniciada em nova guia.`, false);
  window.setTimeout(() => setButtonLoading(button, false), 900);
}

document.addEventListener('visibilitychange', () => {
  if (document.hidden && dashboardRefreshTimer) {
    window.clearTimeout(dashboardRefreshTimer);
    dashboardRefreshTimer = null;
  } else if (!document.hidden && page === 'dashboard') {
    scheduleDashboardRefresh();
  }
});

window.addEventListener('beforeunload', () => {
  if (dashboardRefreshTimer) {
    window.clearTimeout(dashboardRefreshTimer);
    dashboardRefreshTimer = null;
  }
  if (liveSocket) {
    liveSocket.close();
  }
});

document.addEventListener('DOMContentLoaded', async () => {
  if (page === 'dashboard') {
    try {
      await loadDashboard({ silent: true });
    } catch (error) {
      toast('dashboard-feedback', error?.message || 'Falha ao carregar o dashboard.', true);
    } finally {
      scheduleDashboardRefresh();
    }
  }

  if (page === 'traces') {
    await loadTraces();
    initLiveLogs();
  }

  if (page === 'training') {
    await refreshModels();
  }
});
