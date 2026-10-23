# Replace the legacy map at /#map

User explicitly wants the new vector furniture map to replace the old map inside the original household UI at port 8765/#map. Keep original navigation, inventory/new-item/shopping/chat. Do not redirect the whole page to 8780 or duplicate map/inventory data. The separate joyful-home-map folder stays the implementation/service for the vector map and its current geometry store.

## Same-origin bridge

Original backend serves a fixed allowlist proxy to trusted MAP_STUDIO_URL (native default http://127.0.0.1:8780; Docker http://joyful-home-map-studio-1:8780):
- GET /map-studio/ and /map-studio/{app.js,contracts.js,styles.css} -> corresponding Studio static assets. New index uses relative asset links. No arbitrary path, remote URL or redirects.
- GET/PUT /api/studio/layout; GET /api/studio/reference; GET /api/studio/export; POST /api/studio/objects/{safe_id}/clear -> same upstream routes.
- GET /api/locations (the original only has POST) -> Studio GET /api/locations.
- DELETE /api/items/{positive_id} with an explicit Studio confirmation body -> Studio guarded delete. Empty `{}` retains the original zero-stock/no-shopping-reference guard. Original POST/PATCH items, auth, state, actions and location creation remain native.

Authenticate every proxy request with original session guard; retain original same-Origin/JSON/stream-byte CSRF checks. Layout PUT gets 2MiB limit (original legacy image PUT keeps its existing limit). Only forward validated household cookie, controlled upstream Origin, content-type and necessary idempotency key; never relay user-controlled URLs/origins or secrets. Studio Docker hosts/origins permit its fixed internal hostname. Proxy errors must be generic and private APIs no-store; strip upstream frame/CSP/cookie headers. Embedded static HTML gets original SAMEORIGIN and frame-ancestors 'self'; top-level original page retains DENY/frame-ancestors none and adds frame-src 'self'. No external framing permission.

## Frontend bridge

Original web/floorplan.js becomes an adapter using iframe src /map-studio/?embedded=1. Retain API locations/ensure/refresh methods required by original item editor; read canonical locations, not old map geometry. Inventory '看位置' sends the selected location ID to the child; child selects/highlights linked furniture or explains it is not linked. Message constants are shared literally between the two apps' contracts.js.

Parent/child messages validate exact same-origin and event.source (child=parent, parent=the iframe contentWindow). Messages: joyful-map:ready, joyful-map:dirty {dirty:boolean}, joyful-map:focus {location_id:int}, joyful-map:expired. Queue focus until child ready. Child reports dirty on changes/render/reset. Parent warns before leaving a dirty map via navigation; background state/focus refresh must retain the same iframe/context and draft. Reset/logout removes child and private state. Original canLeave support guards tab changes. Parent native beforeunload reads adapter dirty.

Embed mode hides redundant Studio brand/inventory-return/logout navigation but retains configuration editing and contents controls. Original navigation remains outside iframe. Compact the inner toolbox to avoid double sidebar crowding. Desktop and 390px mobile must fit and preserve edit-only geometry mode, explicit mobile properties, 10px visual/28px hit resize handles. Original map page uses available height with no outer footer consuming map space. Standalone 8780 continues working.

## Ownership / verification

Backend producer owns original server/studio_proxy.py (new), server/app.py/contracts.py, compose.yaml and tests/test_studio_proxy.py. Frontend producer owns original web/floorplan.js/app.js/contracts.js/styles.css/sw.js and new Studio dist/*.js/index.html/styles.css. Root owns documentation, Studio compose env, deployment/commit. No private data in source, no backend real inventory mutation during QA.
Independent QA: exact 8765/#map shows vector furniture, original menus usable, same shared layout source, new-item dialog reachable, desktop/mobile edit drag/resize and browse behavior, focus/parent refresh preserves draft, dirty tab switch requires confirmation, 看位置 bridge tested with synthetic browser data if no real items, auth/logout/image guards and proxy allowlist/CSRF. Do not save changes to real home geometry or inventory just to test; mock-only dirty save when needed. Retain existing private layout/inventory untouched.
