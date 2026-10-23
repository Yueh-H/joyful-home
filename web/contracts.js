export const API = Object.freeze({
  mapStudio: '/map-studio/?embedded=1',
  session: '/api/session', login: '/api/login', logout: '/api/logout',
  state: '/api/state', items: '/api/items', actions: '/api/actions',
  chat: '/api/chat', export: '/api/export',
  floorplan: '/api/floorplan', floorplanImage: '/api/floorplan/image', locations: '/api/locations',
  location: id => `/api/locations/${encodeURIComponent(id)}`,
  item: id => `/api/items/${encodeURIComponent(id)}`,
  applyProposal: id => `/api/proposals/${encodeURIComponent(id)}/apply`,
});
export const CATEGORIES = Object.freeze({
  food: { label: '食材', icon: 'leaf', hint: '冰箱、櫥櫃裡的好食材' },
  supplies: { label: '消耗品', icon: 'drop', hint: '每天用得到的小幫手' },
  maintenance: { label: '維護用品', icon: 'spark', hint: '讓家好好運轉' },
  tools: { label: '工具', icon: 'tool', hint: '需要的時候，找得到' },
});

export const MAP_MESSAGES = Object.freeze({ready:'joyful-map:ready',dirty:'joyful-map:dirty',focus:'joyful-map:focus',expired:'joyful-map:expired'});
