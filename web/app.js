import { API, CATEGORIES } from './contracts.js';
import { createFloorplan } from './floorplan.js';

const $ = selector => document.querySelector(selector);
const app = $('#app');
const dialog = $('#dialog');
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const icon = name => `<svg class="icon" aria-hidden="true"><use href="#i-${esc(name)}"></use></svg>`;
const category = key => CATEGORIES[key] || { label: '未分類', icon: 'grid', hint: '' };
const fmt = value => new Intl.NumberFormat('zh-TW', { maximumFractionDigits: 3 }).format(Number(value) || 0);
const tabs = { today: ['今天', 'home'], inventory: ['家裡有什麼', 'grid'], shopping: ['採買', 'bag'], chat: ['問管家', 'chat'], map: ['家的地圖', 'home'] };
let activeTab = tabs[location.hash.slice(1)] ? location.hash.slice(1) : 'today';
let filter = 'all';
let search = '';
let state = null;
let stateError = '';
let busy = false;
let chatBusy = false;
let messages = [];
let chatBefore = null, chatError = '', chatPersistent = false, chatLoading = false, chatAttempt = null;
let proposal = null;
let sessionGeneration = 0;
let toastTimer;
let returnFocus = null;
let authenticated = false;
let lastUpdated = null;
const AUTO_REFRESH_MS = 30_000;
let lastStateAttempt = null, automaticRefresh = null, automaticRefreshTimer = null;
let chatDraft = '';
let chatComposing = false;
const floorplan = createFloorplan({
  request: async (path, body, method) => body === undefined ? (await api(path)).json() : request(path, body, method),
  onExpired: () => loggedOut('登入已過期，請重新輸入家庭密碼。'),
  getItems: () => state?.items || [], notify, editItem, onItemsChanged: () => loadState({ background: true }),
});

function notify(message, error = false) {
  const toast = $('#toast');
  clearTimeout(toastTimer);
  toast.textContent = message;
  toast.className = `toast${error ? ' error' : ''}`;
  toast.hidden = false;
  toastTimer = setTimeout(() => { toast.hidden = true; }, error ? 7000 : 4000);
}

async function api(path, options = {}) {
  let response;
  try {
    response = await fetch(path, {
      credentials: 'same-origin', cache: 'no-store', ...options,
      headers: { ...(options.body ? { 'Content-Type': 'application/json' } : {}), ...options.headers },
    });
  } catch {
    throw new Error('暫時無法連線，請確認網路後再試。');
  }
  if (!response.ok) {
    let detail = `連線未完成（${response.status}），請稍後再試。`;
    try { const data = await response.json(); if (typeof data.detail === 'string') detail = data.detail; } catch { /* Non-JSON gateway error. */ }
    if (response.status === 401 && path !== API.login && path !== API.session) loggedOut('登入已過期，請重新輸入家庭密碼。');
    const error = new Error(detail); error.status = response.status; throw error;
  }
  return response;
}
const request = async (path, body, method = 'POST', headers = {}) => (await api(path, { method, headers, body: JSON.stringify(body) })).json();
// Keep one key for a retry of the same form payload; edited input is a new operation.
function actionRequester() {
  const keys = new Map();
  return payload => {
    const fingerprint = JSON.stringify(payload);
    if (!keys.has(fingerprint)) keys.set(fingerprint, crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(24)), byte => byte.toString(16).padStart(2, '0')).join(''));
    return request(API.actions, payload, 'POST', { 'Idempotency-Key': keys.get(fingerprint) });
  };
}

function loggedOut(message = '', configured = true) {
  sessionGeneration++;
  floorplan.reset();
  authenticated = false;
  lastUpdated = null;lastStateAttempt=null;automaticRefresh=null;clearTimeout(automaticRefreshTimer);automaticRefreshTimer=null;
  chatDraft = '';
  chatComposing = false;
  state = null;
  messages = [];
  chatBefore = null; chatError = ''; chatPersistent = false; chatLoading = false; chatAttempt = null;
  proposal = null;
  chatBusy = false;
  busy = false;
  stateError = '';
  clearTimeout(toastTimer);$('#toast').hidden=true;$('#toast').textContent='';
  if (dialog.open) dialog.close();
  dialog.replaceChildren();
  renderLogin(message, configured);
}

function renderLogin(message = '', configured = true) {
  app.innerHTML = `<main class="login-page"><section class="login-card"><div class="brand login-brand"><img src="/icon.svg" alt=""><div>愉悅的家庭管家<small>A LITTLE CARE, EVERY DAY</small></div></div><div class="eyebrow">WELCOME HOME</div><h1>回家了，一起慢慢來。</h1><p class="subtitle">把食材、家用品和日常小事，<br>放在彼此都找得到的地方。</p><form id="login-form"><label for="password">家庭密碼</label><input id="password" name="password" type="password" autocomplete="current-password" required placeholder="輸入我們的家庭密碼" ${configured ? '' : 'disabled'}><p class="login-error" role="alert">${esc(configured ? message : '家庭密碼尚未設定，請先完成服務設定。')}</p><button class="button" type="submit" ${configured ? '' : 'disabled'}>進入我們的家 ${icon('arrow')}</button></form><div class="login-footer">一份共享的家的筆記。<br>今天補了什麼、用完什麼，都能一起記得。</div></section></main>`;
  $('#login-form').addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = form.querySelector('button');
    button.disabled = true;
    button.textContent = '正在開門…';
    try {
      await request(API.login, { password: form.elements.password.value });
      authenticated = true;
      form.reset();
      await loadState();
      await loadChat();
    } catch (error) {
      form.querySelector('.login-error').textContent = error.message;
      button.disabled = false;
      button.textContent = '進入我們的家';
    }
  });
}

async function loadState({ background = false } = {}) {
  const generation = sessionGeneration;
  lastStateAttempt=Date.now();
  busy = true;
  if (!background && (state || !$('#login-form'))) render();
  try {
    const next = await (await api(API.state)).json();
    if (generation !== sessionGeneration) return;
    if (!Array.isArray(next.items) || !Array.isArray(next.shopping)) throw new Error('家的資料格式不完整，請重新整理。');
    state = next;
    floorplan.refresh();
    lastUpdated = new Date();
    stateError = next.grocy?.connected ? '' : '目前無法連上家庭庫存。恢復連線後，再讀取最新資料。';
  } catch (error) {
    if (generation !== sessionGeneration) return;
    stateError = error.message || '無法連線，請確認網路後再試。';
  } finally {
    if (generation === sessionGeneration) { lastStateAttempt=Date.now();busy = false; render(); }
  }
}

async function loadChat({ older = false } = {}) {
  const generation = sessionGeneration;
  chatLoading = true;
  chatError = '';
  try {
    const path = API.chat + (older && chatBefore ? `?before=${encodeURIComponent(chatBefore)}` : '');
    const data = await (await api(path)).json();
    if (generation !== sessionGeneration) return;
    chatPersistent = data.persistent;
    chatBefore = data.before;
    const loaded = data.turns.flatMap(turn => {
      const user = { id: `${turn.request_id}:user`, request_id: turn.request_id, role: 'user', content: turn.message,
        at: turn.created_at, order: turn.order, status: turn.status };
      const reply = turn.reply || (turn.status === 'failed' ? `這次回覆未完成：${turn.error}` :
        turn.status === 'interrupted' ? '訊息已保存，但回覆中斷。請重新提出要求。' : '訊息已保存，回覆尚未完成。稍後按「更新對話」查看；若中斷可重新提出要求。');
      return [user, { id: `${turn.request_id}:assistant`, request_id: turn.request_id, role: 'assistant', content: reply,
        at: turn.finished_at, order: turn.order, status: turn.status, preview: turn.proposal }];
    });
    const merged = new Map(messages.map(message => [message.id, message]));
    loaded.forEach(message => merged.set(message.id, message));
    messages = [...merged.values()].sort((a, b) => (a.order || '~').localeCompare(b.order || '~') || (a.role === 'user' ? -1 : 1));
    if (proposal && loaded.some(message => message.preview?.id === proposal.id && message.preview.status !== 'pending')) proposal = null;
    if (chatAttempt && data.turns.some(turn => turn.request_id === chatAttempt.id && ['failed', 'complete', 'interrupted'].includes(turn.status))) chatAttempt = null;
  } catch (error) {
    if (generation === sessionGeneration) chatError = error.message;
  } finally {
    if (generation === sessionGeneration) { chatLoading = false; if (authenticated) render(); }
  }
}

function renderMessages() {
  const statusLabel = { pending: '舊預覽，請重新提出要求後確認', applied: '已確認並完成', applying: '執行中，請核對庫存',
    failed: '執行未全部完成，請核對庫存', expired: '預覽已過期', dismissed: '已取消' };
  return messages.map(item => `<div class="message ${item.role === 'user' ? 'user' : 'assistant'}"><span class="message-label">${item.role === 'user' ? '你' : '家庭管家'}${item.at ? ` · ${esc(formatTime(item.at))}` : ''}${item.local ? ' · 保存尚未確認' : ''}</span>${esc(item.content)}${item.preview ? `<p class="subtitle">${esc(item.preview.summary)}<br>${esc(item.preview.status === 'pending' && proposal?.id === item.preview.id ? '等待下方確認' : statusLabel[item.preview.status] || '請核對家庭記錄')}。歷史預覽不會自動執行。</p>` : ''}</div>`).join('');
}

function datePlus(date, days) {
  const value = new Date(`${date}T00:00:00Z`);
  value.setUTCDate(value.getUTCDate() + days);
  return value.toISOString().slice(0, 10);
}
function reminders() {
  const today = state.today;
  const result = [];
  for (const item of state.items) {
    if (item.quantity > 0 && item.best_before && item.best_before <= datePlus(today, 7)) result.push({ item, kind: 'expiry', icon: 'clock', label: item.best_before < today ? '效期已過，請確認狀況' : '近期到期，優先安排', detail: `效期 ${item.best_before} · ${fmt(item.quantity)} ${item.unit}`, action: '查看' });
    if (item.quantity < item.min_quantity) result.push({ item, kind: 'low', icon: 'bag', label: '庫存快不夠了', detail: `剩 ${fmt(item.quantity)} ${item.unit} · 希望保有 ${fmt(item.min_quantity)} ${item.unit}`, action: '加入採買' });
    if (item.next_due && item.next_due <= datePlus(today, 7)) result.push({ item, kind: 'maintenance', icon: 'tool', label: item.next_due < today ? '維護時間已過' : '該照顧一下了', detail: `預定維護 ${item.next_due}`, action: '完成維護' });
  }
  return result;
}

function fitChatInput(input = $('#chat-message')) {
  if (!input) return;
  const style = getComputedStyle(input);
  const minimum = parseFloat(style.minHeight) || 48;
  const viewport = window.visualViewport?.height || window.innerHeight;
  const maximum = Math.max(minimum, Math.min(parseFloat(style.maxHeight) || 224, viewport * .35));
  input.style.height = 'auto';
  input.style.height = `${Math.max(minimum, Math.min(input.scrollHeight, maximum))}px`;
  input.style.overflowY = input.scrollHeight > input.clientHeight + 1 ? 'auto' : 'hidden';
}

function syncChatInput(input) {
  chatDraft = input.value;
  fitChatInput(input);
}

function renderChatInPlace(input) {
  // Keep the native editing target connected: moving/replacing it ends IME or dictation sessions.
  const composer = input.closest('form');
  const layout = composer.parentElement;
  const next = document.createElement('div');
  next.innerHTML = renderChat();
  const nextComposer = next.querySelector('#chat-form');
  for (const child of [...layout.children]) if (child !== composer) child.remove();
  for (const child of [...next.firstElementChild.children]) if (child !== nextComposer) composer.before(child);
  input.disabled = nextComposer.querySelector('textarea').disabled;
  composer.querySelector('[type="submit"]').disabled = nextComposer.querySelector('[type="submit"]').disabled;
  composer.querySelector('.chat-footnote').textContent = nextComposer.querySelector('.chat-footnote').textContent;
  const connection = app.querySelector('.connection');
  if (connection) {
    connection.textContent = busy ? '更新中' : stateError ? '家庭庫存未連線' : '家庭庫存已連線';
    connection.classList.toggle('offline', !!stateError);
  }
  app.querySelectorAll('[data-refresh]').forEach(button => { button.disabled = busy; });
  const status = $('#state-status');
  if (status) {
    status.innerHTML = stateStatus();
    status.querySelector('[data-refresh]')?.addEventListener('click', () => { if (!busy) loadState(); });
  }
  const updated = app.querySelector('.last-updated');
  if (lastUpdated && updated) updated.textContent = `更新 ${new Intl.DateTimeFormat('zh-TW', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Taipei', hour12: false }).format(lastUpdated)}`;
  bindPage();
  fitChatInput(input);
}

function stateStatus() {
  return stateError ? `<div class="banner" role="alert">${esc(stateError)} ${state ? '下方是上次讀取的資料。' : ''}<button data-refresh class="text-button">重新連線</button></div>` : '';
}

function render() {
  // Keep the live iframe attached during background inventory/status refreshes.
  if (activeTab === 'map' && state && $('#floorplan-root')) {
    const connection = app.querySelector('.connection');
    if (connection) { connection.textContent = busy ? '更新中' : stateError ? '家庭庫存未連線' : '家庭庫存已連線'; connection.classList.toggle('offline', !!stateError); }
    app.querySelectorAll('[data-refresh]').forEach(button => { button.disabled = busy; });
    return;
  }
  const draftInput = $('#chat-message');
  const restoreChatFocus = draftInput && document.activeElement === draftInput;
  const selection = draftInput ? [draftInput.selectionStart, draftInput.selectionEnd] : null;
  if (draftInput) chatDraft = draftInput.value;
  if (activeTab === 'chat' && state && draftInput) { renderChatInPlace(draftInput); return; }
  const connected = !!state?.grocy?.connected && !stateError;
  const today = state?.today;
  const dateLabel = today ? new Intl.DateTimeFormat('zh-TW', { month: 'long', day: 'numeric', weekday: 'long', timeZone: 'Asia/Taipei' }).format(new Date(`${today}T12:00:00+08:00`)) : '我們的家的筆記';
  floorplan.unmount();
  app.innerHTML = `<div class="shell ${activeTab==='map'?'map-shell':''}"><aside class="sidebar"><div class="brand"><img src="/icon.svg" alt=""><div>愉悅的家庭管家<small>A LITTLE CARE, EVERY DAY</small></div></div><nav class="nav" aria-label="主要導覽">${Object.entries(tabs).map(([key, [label, glyph]]) => `<button data-tab="${key}" class="${key === activeTab ? 'active' : ''}" ${key === activeTab ? 'aria-current="page"' : ''}>${icon(glyph)}<span class="nav-full">${label}</span><span class="nav-short">${key === 'inventory' ? '物品' : key === 'map' ? '地圖' : label}</span></button>`).join('')}</nav><div class="sidebar-note"><b>把家照顧好，也照顧彼此。</b>少一點忘記，多一點餘裕。<div class="sidebar-tools"><button class="text-button" data-export>匯出家的筆記</button><button class="text-button" data-logout>登出</button></div></div></aside><main class="main"><header class="topbar"><div class="eyebrow">${esc(dateLabel)}</div><div class="status-wrap"><span class="connection ${connected ? '' : 'offline'}">${busy ? '更新中' : connected ? '家庭庫存已連線' : '家庭庫存未連線'}</span><button class="refresh" aria-label="重新整理家庭資料" data-refresh ${busy ? 'disabled' : ''}>↻</button></div></header><div id="state-status">${stateStatus()}</div><div id="page">${state ? renderPage() : `<div class="no-data" role="status">${busy ? '正在讀取家的筆記…' : '暫時無法讀取家的筆記。請重新連線。'}</div>`}</div><div class="mobile-tools"><button class="text-button" data-export>匯出家的筆記</button><button class="text-button" data-logout>登出</button></div><footer class="page-footer">一點一滴，把日子過好。</footer></main></div>`;
  bindShell();
  bindPage();
  if (activeTab === 'map' && state) floorplan.mount($('#floorplan-root'));
  const status = app.querySelector('.status-wrap');
  if (lastUpdated && status) {
    const time = new Intl.DateTimeFormat('zh-TW', { hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Taipei', hour12: false }).format(lastUpdated);
    const updated = document.createElement('span');
    updated.className = 'last-updated';
    updated.textContent = `更新 ${time}`;
    status.insertBefore(updated, status.querySelector('.refresh'));
  }
  const currentInput = $('#chat-message');
  if (currentInput) {
    currentInput.value = chatDraft;
    fitChatInput(currentInput);
    if (restoreChatFocus && !currentInput.disabled) { currentInput.focus({ preventScroll: true }); currentInput.setSelectionRange(...selection); }
  }
}

function renderPage() {
  if (activeTab === 'map') return '<div id="floorplan-root"></div>';
  if (activeTab === 'inventory') return renderInventory();
  if (activeTab === 'shopping') return renderShopping();
  if (activeTab === 'chat') return renderChat();
  return renderToday();
}

function empty(glyph, title, text, button = '') {
  return `<div class="empty"><div class="icon-tile">${icon(glyph)}</div><h3>${esc(title)}</h3><p>${esc(text)}</p>${button}</div>`;
}
const addButton = (label = '新增物品') => `<button class="button" data-add>${icon('plus')}${label}</button>`;

function renderToday() {
  const alerts = reminders();
  const expiryCount = alerts.filter(value => value.kind === 'expiry').length;
  return `<div class="title-row"><div><h1>今天，家裡還好嗎？</h1><p class="subtitle">小事有人記得，日子就輕鬆一點。</p></div></div><section class="hero"><div><div class="eyebrow">OUR LITTLE HOME</div><h2>${state.items.length ? '家的日常，一起照顧。' : '我們的家，從一筆開始。'}</h2><p>${state.items.length ? `已記下 ${state.items.length} 樣物品，${alerts.length ? `有 ${alerts.length} 件小事可以留意。` : '今天沒有需要留意的提醒。'}` : '先記下一樣家裡的物品，剩下的慢慢來。'}</p></div><div class="hero-art">${icon('leaf')}</div></section><section class="stats" aria-label="家庭摘要"><button class="stat" data-tab="inventory"><span class="icon-tile">${icon('grid')}</span><span><strong>${state.items.length}</strong><small>家裡的物品</small></span></button><button class="stat" data-tab="shopping"><span class="icon-tile warm">${icon('bag')}</span><span><strong>${state.shopping.length}</strong><small>待採買品項</small></span></button><button class="stat" data-show-reminders><span class="icon-tile warm">${icon('clock')}</span><span><strong>${expiryCount}</strong><small>近期效期提醒</small></span></button><button class="stat" data-show-reminders><span class="icon-tile">${icon('tool')}</span><span><strong>${alerts.filter(value => value.kind === 'maintenance').length}</strong><small>待照顧的維護</small></span></button></section><div class="today-grid"><section class="card" id="reminders"><div class="section-header"><h2>今天的小提醒</h2><span>${alerts.length} 件</span></div>${alerts.length ? alerts.map(value => `<div class="reminder"><span class="icon-tile ${value.kind === 'maintenance' ? '' : 'warm'}">${icon(value.icon)}</span><div class="reminder-body"><h3>${esc(value.item.name)}</h3><p>${esc(value.label)}<br>${esc(value.detail)}</p></div><button data-reminder="${esc(value.kind)}" data-id="${Number(value.item.id)}">${value.action}</button></div>`).join('') : empty('check', state.items.length ? '今天沒有急著處理的小事' : '還沒有提醒', state.items.length ? '物品有了效期、最低庫存或維護週期，管家就會幫忙留意。' : '記下物品的效期或維護週期，重要的小事就不容易忘記。')}</section><section class="card"><div class="section-header"><h2>順手記一下</h2><span>給忙碌的日常</span></div><p class="subtitle">剛買回來、快用完了，或想不起來放哪裡？</p><div class="quick-list"><button class="quick" data-add>${icon('plus')}記下家裡的物品${icon('arrow').replace('class="icon"', 'class="icon end"')}</button><button class="quick" data-tab="shopping">${icon('bag')}看看還需要買什麼${icon('arrow').replace('class="icon"', 'class="icon end"')}</button><button class="quick" data-tab="chat">${icon('chat')}用一句話，問問管家${icon('arrow').replace('class="icon"', 'class="icon end"')}</button></div></section></div><section class="activity"><div class="section-header"><h2>家的近況</h2><span>最近的記錄</span></div>${state.activity?.length ? state.activity.slice(0, 6).map(item => `<div class="activity-row"><time>${esc(formatTime(item.at))}</time><span>${esc(item.message)}</span></div>`).join('') : `<p class="subtitle">還沒有記錄。每一次用心，都會慢慢留下來。</p>`}</section>`;
}

function formatTime(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? '' : new Intl.DateTimeFormat('zh-TW', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZone: 'Asia/Taipei' }).format(date);
}

function renderInventory() {
  return `<div class="title-row"><div><h1>家裡有什麼</h1><p class="subtitle">知道有什麼，也知道放在哪裡。</p></div>${addButton()}</div><div class="toolbar"><div class="search">${icon('search')}<input id="inventory-search" aria-label="搜尋物品、位置或型號" placeholder="找物品、位置或型號…" value="${esc(search)}" type="search"></div></div><div class="filters" role="group" aria-label="物品分類">${[['all', { label: '全部', icon: 'grid' }], ...Object.entries(CATEGORIES)].map(([key, value]) => `<button class="filter ${filter === key ? 'active' : ''}" data-filter="${key}" aria-pressed="${filter === key}">${icon(value.icon)}${value.label}</button>`).join('')}</div><div id="inventory-list" class="inventory-grid">${inventoryList()}</div>`;
}
function inventoryList() {
  const term = search.toLocaleLowerCase();
  const items = state.items.filter(item => (filter === 'all' || item.category === filter) && [item.name, item.location, item.model, item.description].some(value => String(value || '').toLocaleLowerCase().includes(term)));
  if (!items.length) return empty('grid', state.items.length ? '這裡還沒有找到物品' : '家的物品，從這裡開始記', state.items.length ? '換個關鍵字或分類找找，也可以新增一樣物品。' : '不必一次整理完，先記下手邊最常用的一樣就好。', addButton('記下第一樣物品'));
  return items.map(item => {
    const group = category(item.category);
    return `<article class="card item"><div class="item-top"><span class="icon-tile">${icon(group.icon)}</span><div class="item-title"><h3>${esc(item.name)}</h3><p>${esc(group.label)} · ${esc(item.location || '未分類')}</p></div><button class="edit" data-edit="${Number(item.id)}" aria-label="編輯 ${esc(item.name)}">編輯</button></div><div class="item-quantity"><strong>${fmt(item.quantity)}</strong>${esc(item.unit)}<span style="margin-left:auto">最低 ${fmt(item.min_quantity)} ${esc(item.unit)}</span></div><div class="item-meta">${item.quantity < item.min_quantity ? '<span class="tag warn">需要補充</span>' : ''}${item.best_before ? `<span class="tag ${item.best_before < state.today ? 'danger' : item.best_before <= datePlus(state.today, 7) ? 'warn' : ''}">效期 ${esc(item.best_before)}</span>` : ''}${item.opened ? '<span class="tag">已開封</span>' : ''}${item.model ? `<span class="tag">${esc(item.model)}</span>` : ''}${item.next_due ? `<span class="tag ${item.next_due <= state.today ? 'warn' : ''}">下次維護 ${esc(item.next_due)}</span>` : ''}${item.last_maintained ? `<span class="tag">上次維護 ${esc(item.last_maintained)}</span>` : ''}${item.interval_days ? `<span class="tag">每 ${fmt(item.interval_days)} 天維護</span>` : ''}</div>${item.description ? `<p class="item-note">${esc(item.description)}</p>` : ''}<div class="item-actions"><button class="button ghost small" data-look-location="${Number(item.id)}">看位置</button><button class="button secondary small" data-action="purchase" data-id="${Number(item.id)}">購入</button><button class="button ghost small" data-action="consume" data-id="${Number(item.id)}" ${item.quantity <= 0 ? 'disabled' : ''}>使用</button><button class="button ghost small" data-action="shopping_add" data-id="${Number(item.id)}">待買</button>${item.category === 'maintenance' || item.interval_days ? `<button class="button ghost small" data-action="maintain" data-id="${Number(item.id)}">已維護</button>` : ''}</div></article>`;
  }).join('');
}

function renderShopping() {
  return `<div class="title-row"><div><h1>這次，買這些就好。</h1><p class="subtitle">採買清單，兩個人都看得到。</p></div><button class="button" data-shopping-add>${icon('plus')}加到清單</button></div><div class="shopping-note">買回來後，填入實際購買的數量。管家會一起更新家裡的庫存。</div><section class="shopping-list">${state.shopping.length ? state.shopping.map(item => `<article class="card shopping-item"><button class="shop-circle" data-complete="${Number(item.id)}" aria-label="${esc(item.name)} 已買回來">${icon('check')}</button><div class="shopping-info"><h3>${esc(item.name)}</h3><p>預計採買 ${fmt(item.amount)} ${esc(item.unit)}</p></div><button class="button secondary small" data-complete="${Number(item.id)}">買回來了</button></article>`).join('') : empty('bag', '清單上還沒有待買的東西', '想到缺什麼就記下來，下次出門，一眼就知道。', '<button class="button" data-shopping-add>加入採買清單</button>')}</section>`;
}

function renderProposalActions(actions) {
  const labels = { name: '物品名稱', category: '分類', quantity: '庫存數量（設為）', unit: '單位', location: '位置', min_quantity: '最低保有量', best_before: '批次效期', opened: '批次開封狀態', model: '型號／規格', description: '備註', interval_days: '維護週期（天）', last_maintained: '上次維護日期', next_due: '下次維護日期', amount: '操作數量', shopping_id: '採買項目編號' };
  const types = { purchase: '購入物品', consume: '使用物品', maintain: '完成維護', shopping_add: '加入採買清單', shopping_complete: '買回並完成採買', create_item: '新增物品', update_item: '更新物品' };
  const fields = (values, clearing = false) => `<dl class="proposal-fields">${Object.entries(values).map(([key, value]) => {
    let display = value;
    if (value == null) display = clearing ? '清除' : '未設定';
    else if (key === 'opened') display = value ? '已開封' : '未開封';
    else if (key === 'category') display = category(value).label;
    else if (value === '') display = '（空白）';
    else if (typeof value === 'object') display = JSON.stringify(value);
    return `<dt>${esc(labels[key] || key)}</dt><dd>${esc(display)}</dd>`;
  }).join('')}</dl>`;
  return `<ol class="proposal-actions" aria-label="實際將執行的變更">${(Array.isArray(actions) ? actions : []).map(action => {
    const item = state.items.find(entry => Number(entry.id) === Number(action.item_id));
    const name = action.type === 'create_item' ? action.item?.name || '未命名物品' : item?.name || `找不到物品（ID ${action.item_id ?? '未提供'}）`;
    let details;
    if (action.type === 'create_item') details = fields({ quantity: 0, unit: '個', location: '未分類', ...action.item });
    else if (action.type === 'update_item') details = fields(action.patch || {}, true);
    else if (action.type === 'maintain') details = fields({ last_maintained: state.today, ...(item?.interval_days ? { next_due: datePlus(state.today, item.interval_days) } : {}) });
    else {
      const values = { amount: action.amount, unit: item?.unit || '單位未知，請先核對物品' };
      if (['purchase', 'shopping_complete'].includes(action.type)) { values.best_before = action.best_before ?? null; values.opened = action.opened ?? false; }
      if (action.type === 'shopping_complete') values.shopping_id = action.shopping_id;
      details = fields(values);
    }
    return `<li><h4>${esc(types[action.type] || action.type)}：${esc(name)}</h4>${item ? `<div class="proposal-target">物品 #${esc(item.id)} · ${esc(item.location || '未分類')}</div>` : ''}${details}</li>`;
  }).join('')}</ol>`;
}

function renderChat() {
  const enabled = !!state.ai?.enabled;
  return `<div class="chat-layout"><div class="title-row"><div><h1>問問家庭管家</h1><p class="subtitle">找東西、想菜單，或順手記下生活小事。</p></div></div>${!enabled ? '<div class="banner" role="status">AI 尚未設定。食材與物品記錄、採買和維護功能仍可正常使用。</div>' : ''}<section class="chat-welcome"><div class="icon-tile">${icon('chat')}</div><h2>今天有什麼需要幫忙的？</h2><p class="subtitle">自然地說就好。要修改記錄時，<br>管家會先讓你確認，再動手記下來。</p><div class="chips"><button class="chip" data-prompt="查牛奶" ${enabled ? '' : 'disabled'}>查牛奶</button><button class="chip" data-prompt="買兩瓶洗衣精" ${enabled ? '' : 'disabled'}>買兩瓶洗衣精</button><button class="chip" data-prompt="找延長線" ${enabled ? '' : 'disabled'}>找延長線</button></div></section>${chatError ? `<div class="banner" role="alert">對話保存或讀取尚未確認：${esc(chatError)}</div>` : ''}<div class="section-header"><button class="text-button" data-chat-refresh ${chatLoading || chatBusy ? 'disabled' : ''}>${chatLoading ? '正在讀取對話…' : '更新對話'}</button>${chatBefore ? `<button class="text-button" data-chat-older ${chatLoading ? 'disabled' : ''}>載入更早的對話</button>` : ''}</div><div class="messages" aria-live="polite" aria-relevant="additions">${renderMessages()}</div>${chatBusy ? '<div class="busy-text loading" role="status">管家正在翻閱家的筆記…</div>' : ''}${proposal ? `<section class="proposal" aria-label="待確認的變更"><h3>${icon('check')} 先確認，再記錄</h3><p>請核對以下實際將變更的物品與數量：</p>${renderProposalActions(proposal.actions)}<p class="proposal-summary">補充說明：${esc(proposal.summary)}</p><small>共 ${Array.isArray(proposal.actions) ? proposal.actions.length : 0} 項變更。確認後才會更新家的記錄。</small><button class="button" data-apply="${esc(proposal.id)}" ${chatBusy ? 'disabled' : ''}>確認記錄</button> <button class="button ghost" data-dismiss ${chatBusy ? 'disabled' : ''}>先不要</button></section>` : ''}<form id="chat-form" class="chat-compose"><div class="compose-box"><textarea id="chat-message" name="message" rows="1" maxlength="4000" aria-label="給家庭管家的訊息" placeholder="例如：今天買了兩瓶洗衣精…" required ${!enabled || chatBusy ? 'disabled' : ''}></textarea><button class="button" type="submit" aria-label="傳送訊息" ${!enabled || chatBusy ? 'disabled' : ''}>${icon('send')}</button></div><p class="chat-footnote">${chatPersistent ? '對話保存在本機，登入本家庭即可共用與回看。' : '正在確認對話保存狀態。'}庫存與採買以家的實際記錄為準。</p></form></div>`;
}

function bindShell() {
  app.querySelectorAll('[data-tab]').forEach(button => button.addEventListener('click', () => { const nextTab=button.dataset.tab;if(activeTab==='map'&&nextTab!=='map'&&!floorplan.canLeave())return;activeTab = nextTab; history.replaceState(null, '', `#${activeTab}`); render(); if (activeTab === 'chat') loadChat(); window.scrollTo({ top: 0, behavior: 'instant' }); }));
  app.querySelectorAll('[data-refresh]').forEach(button => button.addEventListener('click', () => { if (!busy) loadState(); }));
  app.querySelectorAll('[data-logout]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try { await request(API.logout, {}); loggedOut(); } catch (error) { notify(error.message, true); button.disabled = false; }
  }));
  app.querySelectorAll('[data-export]').forEach(button => button.addEventListener('click', async () => {
    button.disabled = true;
    try {
      const response = await api(API.export);
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement('a');
      link.href = url; link.download = `joyful-home-${state?.today || 'export'}.json`;
      document.body.append(link); link.click(); link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 10000);
      notify('家的筆記已準備下載。');
    } catch (error) { notify(error.message, true); } finally { button.disabled = false; }
  }));
}

function bindPage() {
  bindLocationLinks(app);
  app.querySelectorAll('[data-add]').forEach(button => button.addEventListener('click', () => editItem()));
  app.querySelectorAll('[data-edit]').forEach(button => button.addEventListener('click', () => editItem(state.items.find(item => item.id === Number(button.dataset.edit)))));
  app.querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => actionDialog(button.dataset.action, Number(button.dataset.id))));
  app.querySelectorAll('[data-filter]').forEach(button => button.addEventListener('click', () => { filter = button.dataset.filter; render(); }));
  app.querySelectorAll('[data-show-reminders]').forEach(button => button.addEventListener('click', () => $('#reminders')?.scrollIntoView({ behavior: 'smooth', block: 'center' })));
  app.querySelectorAll('[data-reminder]').forEach(button => button.addEventListener('click', () => {
    const itemId = Number(button.dataset.id);
    if (button.dataset.reminder === 'expiry') editItem(state.items.find(item => item.id === itemId));
    else actionDialog(button.dataset.reminder === 'low' ? 'shopping_add' : 'maintain', itemId);
  }));
  $('#inventory-search')?.addEventListener('input', event => {
    search = event.target.value;
    $('#inventory-list').innerHTML = inventoryList();
    bindInventoryList();
  });
  app.querySelectorAll('[data-shopping-add]').forEach(button => button.addEventListener('click', shoppingAddDialog));
  app.querySelectorAll('[data-complete]').forEach(button => button.addEventListener('click', () => {
    const shopping = state.shopping.find(item => item.id === Number(button.dataset.complete));
    if (shopping) actionDialog('shopping_complete', shopping.item_id, shopping);
  }));
  app.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => {
    const input = $('#chat-message'); input.value = button.dataset.prompt; syncChatInput(input); input.focus();
  }));
  app.querySelector('[data-chat-refresh]')?.addEventListener('click', () => loadChat());
  app.querySelector('[data-chat-older]')?.addEventListener('click', () => loadChat({ older: true }));
  const chatInput = $('#chat-message');
  if (chatInput && !chatInput.dataset.bound) {
    chatInput.dataset.bound = 'true';
    chatComposing = false;
    $('#chat-form').addEventListener('submit', sendChat);
    const sync = () => syncChatInput(chatInput);
    const afterNativeEdit = () => requestAnimationFrame(() => { if (chatInput.isConnected) sync(); });
    chatInput.addEventListener('input', sync);
    chatInput.addEventListener('change', sync);
    // Observe native editing without intercepting paste, beforeinput, or assistive input.
    chatInput.addEventListener('beforeinput', afterNativeEdit);
    chatInput.addEventListener('paste', afterNativeEdit);
    chatInput.addEventListener('compositionstart', () => { chatComposing = true; });
    chatInput.addEventListener('compositionend', () => { chatComposing = false; sync(); afterNativeEdit(); });
    chatInput.addEventListener('keydown', event => {
      if (event.key === 'Enter' && !event.shiftKey && !event.isComposing && !chatComposing && event.keyCode !== 229) {
        event.preventDefault(); $('#chat-form').requestSubmit();
      }
    });
  }
  app.querySelector('[data-dismiss]')?.addEventListener('click', async () => {
    try { await request(API.dismissProposal(proposal.id), {}); proposal = null; await loadChat(); }
    catch (error) { notify(error.message, true); }
  });
  app.querySelector('[data-apply]')?.addEventListener('click', applyProposal);
}

function bindLocationLinks(node) {
  node.querySelectorAll('[data-look-location]').forEach(button => button.addEventListener('click', async () => {
    const item = state?.items.find(value => value.id === Number(button.dataset.lookLocation));
    if (!item) return;
    activeTab = 'map'; history.replaceState(null, '', '#map'); render();
    await floorplan.focusItem(item);
    window.scrollTo({ top: 0, behavior: 'instant' });
  }));
}

function bindInventoryList() {
  bindLocationLinks($('#inventory-list'));
  $('#inventory-list').querySelectorAll('[data-add]').forEach(button => button.addEventListener('click', () => editItem()));
  $('#inventory-list').querySelectorAll('[data-edit]').forEach(button => button.addEventListener('click', () => editItem(state.items.find(item => item.id === Number(button.dataset.edit)))));
  $('#inventory-list').querySelectorAll('[data-action]').forEach(button => button.addEventListener('click', () => actionDialog(button.dataset.action, Number(button.dataset.id))));
}

function showDialog(title, content) {
  returnFocus = document.activeElement;
  dialog.innerHTML = `<div class="dialog-head"><h2 id="dialog-title">${esc(title)}</h2><button class="close" aria-label="關閉視窗" type="button">×</button></div>${content}`;
  dialog.querySelector('.close').addEventListener('click', () => dialog.close());
  dialog.showModal();
  const first = dialog.querySelector('input,select,textarea');
  first?.focus();
}
dialog.addEventListener('close', () => { if (returnFocus?.isConnected) returnFocus.focus(); });
dialog.addEventListener('cancel', event => { if (dialog.dataset.pending) event.preventDefault(); });
const field = (label, name, type, value = '', extra = '', full = false) => `<label class="field${full ? ' full' : ''}"><span>${esc(label)}</span><input name="${name}" type="${type}" value="${esc(value)}" ${extra}></label>`;
const dialogActions = label => `<p class="form-error" role="alert"></p><div class="dialog-actions"><button class="button ghost" type="button" data-cancel>取消</button><button class="button" type="submit">${label}</button></div>`;
function bindDialogForm(handler) {
  const form = dialog.querySelector('form');
  form.querySelector('[data-cancel]').addEventListener('click', () => dialog.close());
  form.addEventListener('submit', async event => {
    event.preventDefault();
    if (form.dataset.pending) return;
    const generation = sessionGeneration;
    form.dataset.pending = 'true';
    dialog.dataset.pending = 'true';
    dialog.querySelector('.close').disabled = true;
    const controls = [...form.querySelectorAll('button')];
    controls.forEach(button => { button.disabled = true; });
    const errorNode = form.querySelector('.form-error');
    errorNode.textContent = '';
    try {
      const result = await handler(new FormData(form));
      if (generation !== sessionGeneration) return;
      dialog.close();
      notify(result?.message || '已記在家的筆記裡。');
      if (!result?.unchanged) await loadState();
    } catch (error) {
      if (generation === sessionGeneration) errorNode.textContent = error.message || '記錄未完成，請確認連線後再試。';
    } finally {
      delete form.dataset.pending;
      delete dialog.dataset.pending;
      dialog.querySelector('.close').disabled = false;
      controls.forEach(button => { button.disabled = false; });
    }
  });
}

async function editItem(item, prefilledLocation = '') {
  const generation = sessionGeneration;
  await floorplan.ensure();
  if (generation !== sessionGeneration || !authenticated) return;
  const initial = item || { category: filter === 'all' ? 'food' : filter, quantity: 0, unit: '個', min_quantity: 0, location: prefilledLocation, opened: false };
  showDialog(item ? '編輯家的物品' : '記下一樣物品', `<form><div class="form-grid">${field('物品名稱', 'name', 'text', initial.name, 'required maxlength="120" autocomplete="off"', true)}<label class="field"><span>分類</span><select name="category">${Object.entries(CATEGORIES).map(([key, value]) => `<option value="${key}" ${initial.category === key ? 'selected' : ''}>${value.label}</option>`).join('')}</select></label>${field('放在哪裡', 'location', 'text', initial.location, 'placeholder="例如：廚房上櫃" maxlength="120" list="home-location-options"')}${field('目前數量', 'quantity', 'number', initial.quantity, 'min="0" step="any" required')}${field('計量單位', 'unit', 'text', initial.unit, 'required maxlength="40" placeholder="瓶、包、個…"')}${field('最低保有量', 'min_quantity', 'number', initial.min_quantity, 'min="0" step="any" required')}${field('最近批次效期', 'best_before', 'date', initial.best_before)}${field('型號／規格', 'model', 'text', initial.model, 'maxlength="160"', true)}${field('維護週期（天）', 'interval_days', 'number', initial.interval_days, 'min="1" step="1" placeholder="沒有週期可留白"')}${field('上次維護日期', 'last_maintained', 'date', initial.last_maintained)}<label class="field full"><span>備註</span><textarea name="description" rows="2" maxlength="2000" placeholder="保存方式、使用提醒，或家人想知道的事…">${esc(initial.description)}</textarea></label></div><label class="checkbox-label"><input name="opened" type="checkbox" ${initial.opened ? 'checked' : ''}>最近批次已開封</label>${dialogActions(item ? '儲存變更' : '記好了')}</form>`);
  const choices = document.createElement('datalist');
  choices.id = 'home-location-options';
  choices.innerHTML = floorplan.locations().map(value => `<option value="${esc(value.name)}"></option>`).join('');
  dialog.append(choices);
  bindDialogForm(data => {
    const payload = Object.fromEntries(data);
    ['quantity', 'min_quantity'].forEach(key => { payload[key] = Number(payload[key]); });
    payload.interval_days = payload.interval_days ? Number(payload.interval_days) : null;
    payload.best_before ||= null;
    payload.last_maintained ||= null;
    payload.opened = data.has('opened');
    payload.name = payload.name.trim();
    payload.unit = payload.unit.trim();
    payload.location = payload.location.trim() || '未分類';
    if (!payload.name || !payload.unit) throw new Error('請填寫物品名稱與計量單位。');
    if (!item) return request(API.items, payload);
    const normalized = (key, value) => {
      if (['quantity', 'min_quantity'].includes(key)) return Number(value || 0);
      if (key === 'interval_days') return value === '' || value == null ? null : Number(value);
      if (['best_before', 'last_maintained'].includes(key)) return value || null;
      if (key === 'opened') return Boolean(value);
      if (key === 'location') return String(value ?? '').trim() || '未分類';
      if (['name', 'unit'].includes(key)) return String(value ?? '').trim();
      return String(value ?? '');
    };
    const changes = Object.fromEntries(Object.entries(payload).filter(([key, value]) => normalized(key, value) !== normalized(key, item[key])));
    if (!Object.keys(changes).length) return { unchanged: true, message: '沒有需要更新的欄位' };
    return request(API.item(item.id), changes, 'PATCH');
  });
  if (item) addRemoveItemControl(item);
}

function addRemoveItemControl(item) {
  const reason = Number(item.quantity) !== 0 ? '尚有庫存，不能移除這個物品。' : state.shopping.some(entry => Number(entry.item_id) === Number(item.id)) ? '仍在採買清單，不能移除這個物品。' : '';
  const section = document.createElement('div');
  section.className = 'remove-zone';
  section.innerHTML = `<button type="button" class="text-button danger-text" ${reason ? 'disabled aria-describedby="remove-reason"' : ''}>移除物品</button><p id="remove-reason">${esc(reason || '只可移除沒有庫存，且未列入採買清單的物品。')}</p>`;
  dialog.append(section);
  section.querySelector('button').addEventListener('click', async () => {
    const form = dialog.querySelector('form');
    if (reason || form.dataset.pending || !window.confirm(`移除這個沒有庫存的物品？\n\n${item.name}\n\n確認後，將從家的物品清單中移除。`)) return;
    const generation = sessionGeneration;
    const controls = [...dialog.querySelectorAll('button')].map(button => [button, button.disabled]);
    form.dataset.pending = 'true';
    dialog.dataset.pending = 'true';
    controls.forEach(([button]) => { button.disabled = true; });
    const errorNode = form.querySelector('.form-error');
    errorNode.textContent = '';
    try {
      const result = await request(API.item(item.id), {}, 'DELETE');
      if (generation !== sessionGeneration) return;
      dialog.close();
      notify(result.message || '已移除這個沒有庫存的物品。');
      await loadState();
    } catch (error) {
      if (generation === sessionGeneration) errorNode.textContent = error.message || '移除未完成，請稍後再試。';
    } finally {
      delete form.dataset.pending;
      delete dialog.dataset.pending;
      controls.forEach(([button, disabled]) => { button.disabled = disabled; });
    }
  });
}

function actionDialog(type, itemId, shopping = null) {
  const performAction = actionRequester();
  const item = state.items.find(value => value.id === itemId);
  if (!item) { notify('找不到這樣物品，請重新整理後再試。', true); return; }
  const labels = { purchase: '買回來了', consume: '記下一次使用', maintain: '完成維護', shopping_add: '加入採買清單', shopping_complete: '買回來，放進家裡' };
  showDialog(labels[type], `<form><p class="action-summary"><strong>${esc(item.name)}</strong><br>${type === 'maintain' ? `將記錄今天（${esc(state.today)}）已完成維護，並依週期更新下一次提醒。` : `家裡目前有 ${fmt(item.quantity)} ${esc(item.unit)}。${type === 'shopping_complete' ? `清單預計買 ${fmt(shopping.amount)} ${esc(item.unit)}。` : ''}`}</p>${type !== 'maintain' ? field(type === 'shopping_add' ? `預計採買數量（${item.unit}）` : type === 'consume' ? `使用數量（${item.unit}）` : `實際購入數量（${item.unit}）`, 'amount', 'number', '', `required min="0.000001" step="any" placeholder="請填寫數量" ${type === 'consume' ? `max="${Number(item.quantity)}"` : ''}`, true) : ''}${['purchase', 'shopping_complete'].includes(type) ? `<div style="margin-top:16px">${field('這次購入批次的效期（可留白）', 'best_before', 'date')}</div><label class="checkbox-label"><input type="checkbox" name="opened">這次購入批次已開封</label>` : ''}${dialogActions(type === 'maintain' ? '確認完成維護' : '確認記錄')}</form>`);
  bindDialogForm(data => {
    const payload = { type, item_id: itemId };
    if (type !== 'maintain') {
      payload.amount = Number(data.get('amount'));
      if (!Number.isFinite(payload.amount) || payload.amount <= 0) throw new Error('請填寫大於 0 的實際數量。');
    }
    if (shopping) payload.shopping_id = shopping.id;
    if (['purchase', 'shopping_complete'].includes(type)) {
      payload.best_before = data.get('best_before') || null;
      payload.opened = data.has('opened');
    }
    return performAction(payload);
  });
}

function shoppingAddDialog() {
  const performAction = actionRequester();
  if (!state.items.length) { editItem(); return; }
  showDialog('加到採買清單', `<form><label class="field full"><span>想買什麼？</span><select name="item_id" required>${state.items.map(item => `<option value="${Number(item.id)}">${esc(item.name)}（${esc(item.unit)}）</option>`).join('')}</select><small>還沒記過的新物品，先到「家裡有什麼」新增。</small></label><div style="margin-top:16px">${field('預計採買數量', 'amount', 'number', '', 'required min="0.000001" step="any" placeholder="請填寫數量"', true)}</div>${dialogActions('加入清單')}</form>`);
  bindDialogForm(data => performAction({ type: 'shopping_add', item_id: Number(data.get('item_id')), amount: Number(data.get('amount')) }));
}

async function sendChat(event) {
  event.preventDefault();
  const input = $('#chat-message');
  const text = input.value.trim();
  if (!text || chatBusy || chatComposing || !state.ai?.enabled) return;
  const generation = sessionGeneration;
  if (!chatAttempt || chatAttempt.text !== text) chatAttempt = { text, id: crypto.randomUUID?.() || Array.from(crypto.getRandomValues(new Uint8Array(24)), byte => byte.toString(16).padStart(2, '0')).join('') };
  const requestId = chatAttempt.id;
  if (!messages.some(message => message.id === `${requestId}:user`)) messages.push({ id: `${requestId}:user`, request_id: requestId, role: 'user', content: text, local: true });
  input.value = '';
  chatDraft = '';
  proposal = null;
  chatBusy = true;
  render();
  try {
    const response = await request(API.chat, { message: text, request_id: requestId });
    if (generation !== sessionGeneration) return;
    if (response.status === 'complete') {
      messages = messages.map(message => message.id === `${requestId}:user` ? { ...message, local: false } : message);
      proposal = response.proposal || null;
      chatAttempt = null;
    }
    await loadChat();
  } catch (error) {
    if (generation !== sessionGeneration) return;
    // A lost response does not prove a failed write. Read back before offering a retry.
    await loadChat();
    if (generation !== sessionGeneration) return;
    chatError = error.message;
    if (!messages.some(message => message.id === `${requestId}:user` && !message.local)) {
      chatDraft = text;
      const currentInput = $('#chat-message');
      if (currentInput && !currentInput.value) { currentInput.value = text; fitChatInput(currentInput); }
    }
  } finally {
    if (generation === sessionGeneration) {
      chatBusy = false;
      render();
      if (activeTab === 'chat') { $('#chat-message')?.focus(); $('.chat-compose')?.scrollIntoView({ behavior: 'smooth', block: 'end' }); }
    }
  }
}

async function applyProposal() {
  if (!proposal || chatBusy) return;
  const generation = sessionGeneration;
  const proposalId = proposal.id;
  chatBusy = true;
  render();
  try {
    const result = await request(API.applyProposal(proposalId), {});
    if (generation !== sessionGeneration) return;
    proposal = null;
    notify(result.message || '已確認並更新家的記錄。');
    await loadChat();
    await loadState();
  } catch (error) {
    if (generation !== sessionGeneration) return;
    notify(error.message, true);
  } finally {
    if (generation === sessionGeneration) { chatBusy = false; render(); }
  }
}

async function start() {
  try {
    const session = await (await api(API.session)).json();
    if (!session.authenticated) renderLogin('', session.configured);
    else { authenticated = true; await loadState(); await loadChat(); }
  } catch (error) {
    app.innerHTML = `<main class="login-page"><section class="login-card"><h1>暫時還沒連上家</h1><p class="subtitle">${esc(error.message || '請確認網路後再試。')}</p><button class="button" id="retry" style="margin-top:20px">再連一次</button></section></main>`;
    $('#retry').addEventListener('click', start);
  }
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { /* The app still works without installation. */ });
}
// Returning/focus events may come in bursts; manual and post-write loadState calls
// deliberately bypass this gate, including after a failed automatic read.
function refreshWhenReturning(debounce = true) {
  clearTimeout(automaticRefreshTimer);automaticRefreshTimer=null;
  if (document.visibilityState !== 'visible' || !authenticated || busy || chatBusy || dialog.open) return;
  if (automaticRefresh) return automaticRefresh;
  if ([lastUpdated?.getTime(),lastStateAttempt].some(at=>at!=null&&Date.now()-at<AUTO_REFRESH_MS)) return;
  if (debounce) { automaticRefreshTimer=setTimeout(()=>refreshWhenReturning(false),180);return; }
  const request=loadState({background:true}).finally(()=>{if(automaticRefresh===request)automaticRefresh=null;});
  automaticRefresh=request;return request;
}
window.addEventListener('resize', () => fitChatInput());
window.visualViewport?.addEventListener('resize', () => fitChatInput());
document.addEventListener('visibilitychange', refreshWhenReturning);
window.addEventListener('focus', refreshWhenReturning);
window.addEventListener('hashchange', () => { if(!authenticated)return;const nextTab=tabs[location.hash.slice(1)]?location.hash.slice(1):'today';if(activeTab==='map'&&nextTab!=='map'&&!floorplan.canLeave()){history.replaceState(null,'','#map');return;}if(nextTab!==activeTab){activeTab=nextTab;render();if(activeTab==='chat')loadChat();} });
window.addEventListener('beforeunload', event => { if (floorplan.dirty) { event.preventDefault(); event.returnValue = ''; } });
start();
