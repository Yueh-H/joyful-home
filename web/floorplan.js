import { API } from './contracts.js';

const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const copy = value => JSON.parse(JSON.stringify(value));
const clamp = (n, min = 0, max = 1) => Math.min(max, Math.max(min, n));
const uid = () => crypto.randomUUID?.() || `map-${Date.now()}-${Math.random().toString(36).slice(2)}`;
const button = (action, label, extra = '') => `<button class="button ghost small" type="button" data-map-action="${action}" ${extra}>${label}</button>`;

export function createFloorplan({ request, getItems, notify, editItem, onItemsChanged }) {
  let root = null, plan = null, base = null, loading = null, loadError = '', edit = false, dirty = false, pending = false;
  let selected = null, query = '', message = '', conflict = false, imageChange, view = null, background = 'reference', epoch = 0;
  let cleanup = () => {}, drag = null, pointers = new Map(), pinch = null;
  const locations = () => plan?.locations || [];
  const location = id => locations().find(value => Number(value.id) === Number(id));
  const spotFor = id => plan?.spots.find(value => Number(value.location_id) === Number(id));
  const itemLocation = item => Number(item.location_id) || Number(locations().find(value => value.name === item.location)?.id) || null;
  const itemsAt = id => getItems().filter(item => itemLocation(item) === Number(id));
  const selectedRoom = () => selected?.type === 'room' ? plan.rooms.find(value => value.id === selected.id) : null;
  const selectedSpot = () => selected?.type === 'spot' ? plan.spots.find(value => value.id === selected.id) : null;
  function markDirty() {
    dirty = true;
    const node = root?.querySelector('[data-map-dirty]');
    if (node) node.textContent = '有尚未保存的地圖變更';
    const save = root?.querySelector('[data-map-action="save"]');
    if (save) save.disabled = pending;
  }
  function fit() { if (plan) view = { x: 0, y: 0, w: plan.canvas.width, h: plan.canvas.height }; }
  function reset() {
    epoch++; cleanup(); root = null; plan = base = loading = null; loadError = ''; edit = dirty = pending = conflict = false;
    selected = null; query = message = ''; imageChange = undefined; view = null; background = 'reference'; pointers.clear(); drag = pinch = null;
  }
  async function refresh({ force = false } = {}) {
    if (loading) return loading;
    if (!force && (dirty || edit)) return plan;
    const generation = epoch;
    loading = (async () => {
      try {
        const next = await request(API.floorplan);
        if (generation !== epoch) return null;
        // A user may have started editing while the request was in flight.
        if (!force && (dirty || edit) && plan) return plan;
        plan = next; base = copy(next); loadError = ''; imageChange = undefined;
        if (!view) fit();
        return plan;
      } catch (error) {
        if (generation === epoch) loadError = error.message;
        return null;
      } finally {
        if (generation === epoch) { loading = null; render(); }
      }
    })();
    return loading;
  }
  async function ensure() { if (plan) return plan; return refresh(); }
  function focusLocation(id, name = '') {
    const spot = spotFor(id);
    if (spot) {
      selected = { type: 'spot', id: spot.id };
      const w = plan.canvas.width * .5, h = plan.canvas.height * .5;
      view = { x: spot.x * plan.canvas.width - w / 2, y: spot.y * plan.canvas.height - h / 2, w, h };
      message = '';
    } else { selected = null; message = `${name || location(id)?.name || '這個物品的位置'}尚未釘在地圖上。可進入編輯地圖，替這個收納位置加上標記。`; }
    render();
  }
  async function focusItem(item) { await ensure(); if (plan) focusLocation(itemLocation(item), item.location || '未設定位置'); }
  function roomCenter(room) { return { x: room.points.reduce((s, p) => s + p.x, 0) / room.points.length, y: room.points.reduce((s, p) => s + p.y, 0) / room.points.length }; }
  function select(type, id) { selected = { type, id }; message = ''; renderSelection(); draw(); }
  function itemRows(items) {
    return items.length ? `<ul class="map-item-list">${items.map(item => `<li><div><strong>${esc(item.name)}</strong><small>${esc(item.quantity)} ${esc(item.unit)} · ${esc(item.location)}</small></div><button class="text-button" data-map-item="${Number(item.id)}">編輯</button></li>`).join('')}</ul>` : '<p class="subtitle map-note">這裡還沒有記錄物品。</p>';
  }
  function selectionHtml() {
    if (!plan) return '';
    const room = selectedRoom(), spot = selectedSpot();
    if (room) {
      const spots = plan.spots.filter(value => value.room_id === room.id);
      return `<h2>${esc(room.name)}</h2>${edit ? `<label class="field"><span>房間名稱</span><input data-room-name value="${esc(room.name)}" maxlength="120"></label><label class="field"><span>房間顏色</span><input data-room-color type="color" value="${esc(room.color)}"></label><p class="subtitle">拖曳房間可移動；拖曳圓點可調整邊界。</p><div class="map-row">${button('vertex-add', '增加頂點')}${button('vertex-remove', '減少頂點', room.points.length <= 3 ? 'disabled' : '')}${button('room-remove', '移除房間')}</div>` : ''}<h3 class="map-section-title">房間裡的收納位置（${spots.length}）</h3>${spots.map(value => `<button class="map-location-row" data-map-spot="${esc(value.id)}"><span>● ${esc(location(value.location_id)?.name || '位置不存在')}</span><small>${itemsAt(value.location_id).length} 樣物品 →</small></button>`).join('') || '<p class="subtitle map-note">還沒有收納位置。編輯地圖後可新增標記。</p>'}${itemRows(spots.flatMap(value => itemsAt(value.location_id)))}`;
    }
    if (spot) {
      const loc = location(spot.location_id);
      const items = itemsAt(spot.location_id);
      return `<h2>${esc(loc?.name || '位置不存在')}</h2><p class="subtitle">${esc(plan.rooms.find(value => value.id === spot.room_id)?.name || '未指定房間')} · ${items.length} 樣物品</p>${edit ? `<label class="field"><span>所屬房間</span><select data-spot-room><option value="">未指定房間</option>${plan.rooms.map(value => `<option value="${esc(value.id)}" ${value.id === spot.room_id ? 'selected' : ''}>${esc(value.name)}</option>`).join('')}</select></label><form data-map-form="rename"><label class="field"><span>收納位置名稱（直接更新家庭庫存）</span><input name="name" value="${esc(loc?.name)}" maxlength="120" required></label><div class="map-row"><button class="button secondary small" ${pending ? 'disabled' : ''}>更新位置名稱</button>${button('spot-remove', '取消釘選')}</div></form>` : ''}${itemRows(items)}<div class="map-row">${button('add-item', '在這裡新增物品')}</div><form data-map-form="assign"><label class="field"><span>把既有物品移到這裡</span><select name="item" required><option value="">選擇物品…</option>${getItems().filter(value => itemLocation(value) !== Number(spot.location_id)).map(value => `<option value="${Number(value.id)}">${esc(value.name)} · ${esc(value.location || '未設定位置')}</option>`).join('')}</select></label><button class="button secondary small" ${pending ? 'disabled' : ''}>更新物品位置</button></form>`;
    }
    return `<h2>${edit ? '安排家的位置' : '點一個房間或位置'}</h2><p class="subtitle map-note">${edit ? '新增房間後調整輪廓，再把真實的收納位置釘上地圖。物品資料仍保存在家庭庫存。' : '看看裡面放了什麼；也可以搜尋物品，直接找到收納位置。'}</p>${plan.spots.filter(value => !value.room_id).length ? '<h3>未指定房間的位置</h3>' + plan.spots.filter(value => !value.room_id).map(value => `<button class="map-location-row" data-map-spot="${esc(value.id)}">${esc(location(value.location_id)?.name)}</button>`).join('') : ''}`;
  }
  function resultsHtml() {
    if (!plan || !query.trim()) return '';
    const term = query.trim().toLocaleLowerCase();
    const matches = [...locations().filter(value => value.name.toLocaleLowerCase().includes(term)).map(value => ({ id: value.id, title: value.name, sub: '收納位置' })), ...getItems().filter(value => [value.name, value.location, value.model].some(text => String(text || '').toLocaleLowerCase().includes(term))).map(value => ({ id: itemLocation(value), title: value.name, sub: value.location || '未設定位置' }))];
    return matches.length ? matches.slice(0, 60).map((value, index) => `<button class="map-result" data-map-result="${index}" data-location-id="${Number(value.id) || ''}" data-location-name="${esc(value.sub === '收納位置' ? value.title : value.sub)}"><strong>${esc(value.title)}</strong><small>${esc(value.sub)} · ${spotFor(value.id) ? '看位置 →' : '尚未釘選'}</small></button>`).join('') : '<p class="subtitle map-note">沒有符合的物品或位置。</p>';
  }
  function render() {
    if (!root?.isConnected) return;
    cleanup();
    if (!plan) {
      root.innerHTML = `<div class="title-row"><div><h1>家的地圖</h1><p class="subtitle">物品放哪裡，一眼就找到。</p></div></div><div class="card" role="status">${esc(loadError || '正在打開家的地圖…')}${loadError ? button('retry', '重新連線') : ''}</div>`;
      root.querySelector('[data-map-action="retry"]')?.addEventListener('click', () => refresh()); return;
    }
    root.innerHTML = `<div class="title-row"><div><h1>家的地圖</h1><p class="subtitle">物品放哪裡，一眼就找到。</p></div>${edit ? '' : button('edit', '編輯地圖')}</div><div class="map-toolbar"><input type="search" data-map-search aria-label="搜尋地圖上的物品或位置" placeholder="找物品、收納位置…" value="${esc(query)}"><div class="map-row">${button('zoom-out', '−', 'aria-label="縮小地圖"')}${button('zoom-in', '＋', 'aria-label="放大地圖"')}${button('fit', '全圖')}${button('background', background === 'reference' ? '原圖' : background === 'original' ? '簡潔' : '參考圖', 'aria-label="切換原圖、簡潔或淡色參考圖"')}</div></div><div class="map-results" aria-live="polite">${resultsHtml()}</div>${message ? `<p class="map-message" role="status">${esc(message)}</p>` : ''}${conflict ? '<div class="banner" role="alert">另一個裝置已更新地圖，你的草稿仍保留。請先保留或檢查變更；重新載入會捨棄這份草稿。<button class="text-button" data-map-action="reload">捨棄草稿並讀取最新版</button></div>' : ''}${loadError ? `<div class="banner" role="alert">${esc(loadError)}</div>` : ''}${edit ? `<section class="map-edit-bar"><div class="map-row"><strong data-map-dirty>${dirty ? '有尚未保存的地圖變更' : '編輯中 · 尚無變更'}</strong><button class="button small" data-map-action="save" ${!dirty || pending ? 'disabled' : ''}>${pending ? '保存中…' : '保存地圖'}</button>${button('cancel', '取消編輯', pending ? 'disabled' : '')}</div><div class="map-row">${button('room-add', '＋ 房間')}<label class="button ghost small map-upload">上傳底圖<input type="file" data-map-image accept="image/png,image/jpeg,image/webp" ${pending ? 'disabled' : ''}></label>${button('image-clear', '清除底圖')}</div><p class="subtitle">房間、標記與底圖在保存後才同步。建立或改名收納位置會直接更新家庭庫存。</p></section>` : ''}<div class="map-layout"><section class="map-stage" aria-label="互動房屋地圖"><svg class="floorplan-svg" xmlns="http://www.w3.org/2000/svg" role="group" aria-label="家的房間與收納位置"><g data-map-layer></g></svg><span class="map-gesture-hint">${edit ? '拖曳房間、頂點或標記 · 空白處拖曳平移' : '點選房間或標記 · 拖曳平移 · 雙指縮放'}</span>${!plan.rooms.length && !plan.image_url && !imageChange ? '<div class="map-empty">從一個房間開始<br><small>進入編輯地圖，可上傳底圖或新增房間</small></div>' : ''}</section><aside class="card map-details" aria-label="選取位置的物品">${selectionHtml()}</aside></div>${edit ? `<section class="card map-pin-editor"><h2>把收納位置釘在地圖上</h2><p class="subtitle">選擇家庭庫存的位置，或建立一個真實的收納位置。</p><form data-map-form="pin"><label class="field"><span>既有收納位置</span><select name="location"><option value="">選擇未釘選的位置…</option>${locations().filter(value => !spotFor(value.id)).map(value => `<option value="${Number(value.id)}">${esc(value.name)}</option>`).join('')}</select></label><label class="field"><span>或建立新位置</span><input name="new_name" maxlength="120" placeholder="例如：書房抽屜"></label><button class="button secondary" ${pending ? 'disabled' : ''}>新增標記</button></form></section>` : ''}`;
    draw(); bind(); bindPointer();
    if (pending) root.querySelectorAll('button,input,select').forEach(node => { node.disabled = true; });
  }
  function draw() {
    const svg = root?.querySelector('.floorplan-svg'); if (!svg || !plan) return;
    const { width: w, height: h } = plan.canvas; if (!view) fit();
    svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`);
    const scale = Math.max(view.w / Math.max(svg.clientWidth, 300), view.h / Math.max(svg.clientHeight, 300)), r = scale * 12, font = scale * 13;
    const image = imageChange === null ? null : imageChange || plan.image_url;
    const allowedImage = image && (image.startsWith('data:image/') || (image === API.floorplanImage || image.startsWith(`${API.floorplanImage}?`)));
    svg.querySelector('[data-map-layer]').innerHTML = `<rect x="0" y="0" width="${w}" height="${h}" fill="#fafbf6" stroke="#d6dfcf"/>${allowedImage && background !== 'clean' ? `<image href="${esc(image)}" x="0" y="0" width="${w}" height="${h}" preserveAspectRatio="none" opacity="${background === 'original' ? 1 : .22}"/>` : ''}${plan.rooms.map(room => { const center = roomCenter(room), active = selected?.id === room.id; return `<g><polygon data-room="${esc(room.id)}" points="${room.points.map(p => `${p.x * w},${p.y * h}`).join(' ')}" fill="${esc(room.color)}" fill-opacity="${background === 'original' ? .12 : .24}" stroke="${active ? '#244b3c' : room.color}" stroke-width="${active ? 3 : 1.7}" vector-effect="non-scaling-stroke" role="button" tabindex="0" aria-label="房間 ${esc(room.name)}"/><text x="${center.x * w}" y="${center.y * h}" text-anchor="middle" font-size="${font}" class="map-room-label">${esc(room.name)}</text>${edit && active ? room.points.map((p, i) => `<circle data-vertex="${i}" data-room-id="${esc(room.id)}" cx="${p.x * w}" cy="${p.y * h}" r="${r * .65}" fill="white" stroke="#244b3c" stroke-width="2" vector-effect="non-scaling-stroke" aria-label="房間頂點 ${i + 1}"/>`).join('') : ''}</g>`; }).join('')}${plan.spots.map(spot => `<g data-spot="${esc(spot.id)}" role="button" tabindex="0" aria-label="收納位置 ${esc(location(spot.location_id)?.name)}" class="map-spot ${selected?.id === spot.id ? 'selected' : ''}"><circle cx="${spot.x * w}" cy="${spot.y * h}" r="${r * (selected?.id === spot.id ? 1.2 : 1)}" fill="${selected?.id === spot.id ? '#b96b36' : '#244b3c'}" stroke="white" stroke-width="2" vector-effect="non-scaling-stroke"/><text x="${spot.x * w}" y="${spot.y * h}" dy=".35em" text-anchor="middle" font-size="${font}" fill="white">●</text><text x="${spot.x * w}" y="${spot.y * h + r * 2.2}" text-anchor="middle" font-size="${font}" class="map-pin-label">${esc(location(spot.location_id)?.name || '位置不存在')}</text></g>`).join('')}`;
  }
  function renderSelection() {
    const panel = root?.querySelector('.map-details'); if (panel) { panel.innerHTML = selectionHtml(); bindPanel(panel); if (pending) panel.querySelectorAll('button,input,select').forEach(node => { node.disabled = true; }); }
  }
  function bindPanel(panel) {
    panel.querySelectorAll('[data-map-spot]').forEach(node => node.addEventListener('click', () => select('spot', node.dataset.mapSpot)));
    panel.querySelectorAll('[data-map-item]').forEach(node => node.addEventListener('click', () => editItem(getItems().find(item => item.id === Number(node.dataset.mapItem)))));
    panel.querySelector('[data-room-name]')?.addEventListener('change', event => { const name = event.target.value.trim(); if (!name) { event.target.value = selectedRoom().name; return; } selectedRoom().name = name; markDirty(); draw(); });
    panel.querySelector('[data-room-color]')?.addEventListener('input', event => { selectedRoom().color = event.target.value; markDirty(); draw(); });
    panel.querySelector('[data-spot-room]')?.addEventListener('change', event => { selectedSpot().room_id = event.target.value || null; markDirty(); });
    bindActions(panel);
    panel.querySelectorAll('[data-map-form]').forEach(form => form.addEventListener('submit', submit));
  }
  function bind() {
    bindActions(root, true); bindPanel(root.querySelector('.map-details'));
    root.querySelector('[data-map-search]').addEventListener('input', event => { query = event.target.value; const results = root.querySelector('.map-results'); results.innerHTML = resultsHtml(); bindResults(results); });
    bindResults(root.querySelector('.map-results'));
    root.querySelector('[data-map-image]')?.addEventListener('change', upload);
    root.querySelector('.map-pin-editor form')?.addEventListener('submit', submit);
  }
  function bindResults(node) { node.querySelectorAll('[data-map-result]').forEach(value => value.addEventListener('click', () => focusLocation(Number(value.dataset.locationId), value.dataset.locationName))); }
  function bindActions(node, skipPanel = false) {
    node.querySelectorAll('[data-map-action]').forEach(value => {
      if (skipPanel && value.closest('.map-details')) return;
      value.addEventListener('click', () => act(value.dataset.mapAction));
    });
  }
  function zoom(factor, point) {
    if (!view) return;
    const { width: w, height: h } = plan.canvas;
    const nextW = clamp(view.w * factor, w * .1, w * 2), ratio = nextW / view.w;
    const center = point || { x: view.x + view.w / 2, y: view.y + view.h / 2 };
    view = { x: center.x - (center.x - view.x) * ratio, y: center.y - (center.y - view.y) * ratio, w: nextW, h: view.h * ratio };
    // Keep very wide/tall plans proportional to their original canvas.
    if (!Number.isFinite(view.h)) view.h = h;
    draw();
  }
  async function act(action) {
    if (pending) return;
    if (action === 'edit') { edit = true; base = copy(plan); render(); }
    if (action === 'zoom-in' || action === 'zoom-out') zoom(action === 'zoom-in' ? .75 : 1 / .75);
    if (action === 'fit') { fit(); draw(); }
    if (action === 'background') { background = background === 'reference' ? 'original' : background === 'original' ? 'clean' : 'reference'; render(); }
    if (action === 'cancel') {
      if (dirty && !window.confirm('捨棄尚未保存的地圖變更？收納位置的建立與改名仍會保留。')) return;
      const liveLocations = locations(); plan = copy(base); plan.locations = liveLocations; dirty = edit = conflict = false; imageChange = undefined; selected = null; fit(); render();
    }
    if (action === 'reload') {
      if (!window.confirm('捨棄這份地圖草稿，讀取另一個裝置保存的最新版？')) return;
      const generation = epoch; pending = true; render(); const next = await refresh({ force: true });
      if (generation !== epoch) return; pending = false;
      if (next) { dirty = edit = conflict = false; imageChange = undefined; selected = null; fit(); }
      render();
    }
    if (action === 'save') await save();
    if (action === 'room-add') {
      if (plan.rooms.length >= 60) return notify('最多可建立 60 個房間。', true);
      const room = { id: uid(), name: `新房間 ${plan.rooms.length + 1}`, color: '#86a77c', points: [{ x: .15, y: .15 }, { x: .45, y: .15 }, { x: .45, y: .4 }, { x: .15, y: .4 }] };
      plan.rooms.push(room); selected = { type: 'room', id: room.id }; markDirty(); render();
    }
    if (action === 'room-remove') { const room = selectedRoom(); if (room && window.confirm(`移除「${room.name}」房間輪廓？位置標記與物品會保留。`)) { plan.rooms = plan.rooms.filter(value => value.id !== room.id); plan.spots.forEach(value => { if (value.room_id === room.id) value.room_id = null; }); selected = null; markDirty(); render(); } }
    if (action === 'vertex-add') { const room = selectedRoom(); if (room && room.points.length < 40) { const a = room.points.at(-1), b = room.points[0]; room.points.push({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }); markDirty(); render(); } }
    if (action === 'vertex-remove') { const room = selectedRoom(); if (room?.points.length > 3) { room.points.pop(); markDirty(); render(); } }
    if (action === 'spot-remove') { const spot = selectedSpot(); if (spot) { plan.spots = plan.spots.filter(value => value.id !== spot.id); selected = null; markDirty(); render(); } }
    if (action === 'image-clear' && edit) { imageChange = null; markDirty(); render(); }
    if (action === 'add-item') { const spot = selectedSpot(); if (spot) editItem(null, location(spot.location_id)?.name || ''); }
  }
  async function save() {
    const generation = epoch;
    pending = true; render();
    const payload = { revision: plan.revision, title: plan.title, canvas: plan.canvas, rooms: plan.rooms, spots: plan.spots };
    if (imageChange !== undefined) payload.image_data_url = imageChange;
    try {
      const result = await request(API.floorplan, payload, 'PUT');
      if (generation !== epoch) return;
      plan = result; base = copy(result); dirty = edit = conflict = false; imageChange = undefined; loadError = ''; notify('家的地圖已保存。');
    } catch (error) { if (generation === epoch) { conflict = error.status === 409; loadError = conflict ? '' : error.message; notify(conflict ? '另一個裝置已更新；你的地圖草稿仍保留。' : error.message, true); } }
    finally { if (generation === epoch) { pending = false; render(); } }
  }
  async function submit(event) {
    event.preventDefault(); if (pending) return;
    const form = event.currentTarget, kind = form.dataset.mapForm, data = new FormData(form), generation = epoch;
    pending = true; root?.querySelectorAll('button,input,select').forEach(node => { node.disabled = true; });
    try {
      if (kind === 'pin') {
        let id = Number(data.get('location')); const name = String(data.get('new_name') || '').trim();
        if (name) { const created = await request(API.locations, { name }); if (generation !== epoch) return; id = Number(created.id); if (!location(id)) plan.locations.push(created); }
        if (!id) throw new Error('請選擇一個位置，或填寫新位置名稱。');
        if (spotFor(id)) throw new Error('這個收納位置已經釘選。');
        if (plan.spots.length >= 300) throw new Error('最多可建立 300 個位置標記。');
        const room = selectedRoom() || plan.rooms.find(value => value.id === selectedSpot()?.room_id);
        const point = room ? roomCenter(room) : { x: clamp((view.x + view.w / 2) / plan.canvas.width), y: clamp((view.y + view.h / 2) / plan.canvas.height) };
        const spot = { id: uid(), location_id: id, room_id: room?.id || null, ...point }; plan.spots.push(spot); selected = { type: 'spot', id: spot.id }; markDirty(); notify('已加上標記，請保存地圖。');
      }
      if (kind === 'rename') {
        const spot = selectedSpot(), name = String(data.get('name') || '').trim(); if (!name) throw new Error('請填寫位置名稱。');
        const updated = await request(API.location(spot.location_id), { name }, 'PATCH'); if (generation !== epoch) return;
        plan.locations = locations().map(value => Number(value.id) === Number(updated.id) ? updated : value); base.locations = copy(plan.locations);
        notify('家庭庫存的位置名稱已更新。'); await onItemsChanged();
      }
      if (kind === 'assign') {
        const id = Number(data.get('item')), spot = selectedSpot();
        if (!id || !spot) throw new Error('請選擇要移入的物品。');
        // Resolve the canonical name now: another device may have renamed this ID.
        // Only refresh location labels; geometry, revision and dirty draft stay intact.
        const current = await request(API.floorplan); if (generation !== epoch) return;
        plan.locations = current.locations; base.locations = copy(current.locations);
        const loc = location(spot.location_id);
        if (!loc) throw new Error('這個收納位置已不存在，物品沒有移動。請重新選擇位置。');
        await request(API.item(id), { location: loc.name }, 'PATCH'); if (generation !== epoch) return;
        notify('物品已移到這個收納位置。'); await onItemsChanged();
      }
    } catch (error) { if (generation === epoch) notify(error.message, true); }
    finally { if (generation === epoch) { pending = false; render(); } }
  }
  async function upload(event) {
    const file = event.target.files?.[0], generation = epoch; if (!file || pending) return;
    pending = true; render();
    try {
      if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) throw new Error('底圖請使用 PNG、JPEG 或 WebP。');
      if (file.size > 10 * 1024 * 1024) throw new Error('底圖不得超過 10 MB。');
      const data = await new Promise((resolve, reject) => { const reader = new FileReader(); reader.onload = () => resolve(reader.result); reader.onerror = () => reject(new Error('無法讀取底圖。')); reader.readAsDataURL(file); });
      const image = await new Promise((resolve, reject) => { const img = new Image(); img.onload = () => resolve(img); img.onerror = () => reject(new Error('無法讀取這張圖片。')); img.src = data; });
      if (generation !== epoch || !edit) return;
      const width = image.naturalWidth, height = image.naturalHeight;
      if (width < 200 || height < 200 || width > 8000 || height > 8000 || width * height > 16000000) throw new Error('底圖每邊需介於 200–8000 像素，總面積最多 1600 萬像素。');
      imageChange = data; plan.canvas = { width, height }; background = 'reference'; markDirty(); fit(); render(); notify('底圖已載入草稿，保存後才會同步。');
    } catch (error) { if (generation === epoch) notify(error.message, true); }
    finally { if (generation === epoch) { pending = false; render(); } }
  }
  function bindPointer() {
    const svg = root.querySelector('.floorplan-svg');
    const point = event => { const p = svg.createSVGPoint(); p.x = event.clientX; p.y = event.clientY; return p.matrixTransform(svg.getScreenCTM().inverse()); };
    const down = event => {
      if (pending || (event.pointerType === 'mouse' && event.button !== 0)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY }); svg.setPointerCapture(event.pointerId);
      if (pointers.size === 2) { const [a, b] = [...pointers.values()]; pinch = { distance: Math.hypot(a.x - b.x, a.y - b.y) }; drag = null; return; }
      const p = point(event), vertex = event.target.closest('[data-vertex]'), spot = event.target.closest('[data-spot]'), room = event.target.closest('[data-room]');
      const type = vertex ? 'vertex' : spot ? 'spot' : room ? 'room' : 'pan';
      const id = vertex?.dataset.roomId || spot?.dataset.spot || room?.dataset.room;
      if (type === 'room' || type === 'spot') { selected = { type, id }; renderSelection(); draw(); }
      drag = { type: edit ? type : 'pan', selectType: type, id, vertex: Number(vertex?.dataset.vertex), start: p, client: { x: event.clientX, y: event.clientY }, view: { ...view }, moved: false, roomSpots: type === 'room' ? plan.spots.filter(value => value.room_id === id).map(copy) : [], original: type === 'room' || type === 'vertex' ? copy(plan.rooms.find(value => value.id === id).points) : type === 'spot' ? copy(plan.spots.find(value => value.id === id)) : null };
      if (type === 'vertex') event.preventDefault();
    };
    const move = event => {
      if (!pointers.has(event.pointerId)) return;
      pointers.set(event.pointerId, { x: event.clientX, y: event.clientY });
      if (pointers.size === 2 && pinch) { const [a, b] = [...pointers.values()], distance = Math.hypot(a.x - b.x, a.y - b.y); if (distance > 0 && pinch.distance > 0) zoom(pinch.distance / distance); pinch.distance = distance; return; }
      if (!drag) return;
      const p = point(event), dx = p.x - drag.start.x, dy = p.y - drag.start.y;
      if (Math.hypot(event.clientX - drag.client.x, event.clientY - drag.client.y) < 4 && !drag.moved) return;
      drag.moved = true;
      if (drag.type === 'pan') { const rect = svg.getBoundingClientRect(), unit = Math.max(drag.view.w / rect.width, drag.view.h / rect.height); view.x = drag.view.x - (event.clientX - drag.client.x) * unit; view.y = drag.view.y - (event.clientY - drag.client.y) * unit; draw(); return; }
      const nx = dx / plan.canvas.width, ny = dy / plan.canvas.height;
      if (drag.type === 'spot') { const spot = plan.spots.find(value => value.id === drag.id); spot.x = clamp(drag.original.x + nx); spot.y = clamp(drag.original.y + ny); }
      if (drag.type === 'vertex') { const room = plan.rooms.find(value => value.id === drag.id); room.points[drag.vertex] = { x: clamp(drag.original[drag.vertex].x + nx), y: clamp(drag.original[drag.vertex].y + ny) }; }
      if (drag.type === 'room') { const bounds = [...drag.original, ...drag.roomSpots]; const dx = clamp(nx, -Math.min(...bounds.map(p => p.x)), 1 - Math.max(...bounds.map(p => p.x))), dy = clamp(ny, -Math.min(...bounds.map(p => p.y)), 1 - Math.max(...bounds.map(p => p.y))); plan.rooms.find(value => value.id === drag.id).points = drag.original.map(p => ({ x: p.x + dx, y: p.y + dy })); drag.roomSpots.forEach(original => { const spot = plan.spots.find(value => value.id === original.id); spot.x = clamp(original.x + dx); spot.y = clamp(original.y + dy); }); }
      markDirty(); draw();
    };
    const up = event => { pointers.delete(event.pointerId); pinch = null; drag = null; if (svg.hasPointerCapture(event.pointerId)) svg.releasePointerCapture(event.pointerId); };
    const wheel = event => { if (event.ctrlKey || event.metaKey) { event.preventDefault(); zoom(event.deltaY < 0 ? .9 : 1.1, point(event)); } };
    const key = event => { if (!['Enter', ' '].includes(event.key)) return; const spot = event.target.closest('[data-spot]'), room = event.target.closest('[data-room]'); if (spot || room) { event.preventDefault(); select(spot ? 'spot' : 'room', spot?.dataset.spot || room.dataset.room); } };
    svg.addEventListener('pointerdown', down); svg.addEventListener('pointermove', move); svg.addEventListener('pointerup', up); svg.addEventListener('pointercancel', up); svg.addEventListener('wheel', wheel, { passive: false }); svg.addEventListener('keydown', key);
    const resize = () => draw(); window.addEventListener('resize', resize);
    cleanup = () => { pointers.clear(); drag = pinch = null; window.removeEventListener('resize', resize); svg.removeEventListener('pointerdown', down); svg.removeEventListener('pointermove', move); svg.removeEventListener('pointerup', up); svg.removeEventListener('pointercancel', up); svg.removeEventListener('wheel', wheel); svg.removeEventListener('keydown', key); cleanup = () => {}; };
  }
  return { mount(node) { cleanup(); root = node; render(); if (!plan) ensure(); }, unmount() { cleanup(); root = null; }, refresh, ensure, reset, focusItem, locations, get dirty() { return dirty; } };
}
