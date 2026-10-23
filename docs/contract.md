# Joyful Home v0.1 shared contract

Name: 愉悅的家庭管家. 阿魚 is a family member, never the AI name.
Grocy is the only source of inventory; no duplicate local inventory database. Runtime database for auth/proposals/logs is allowed, ignored by Git.
Application served same-origin by FastAPI on port 8765. Grocy on loopback 9283. `web/` contains buildless vanilla JS/CSS PWA.

## API

All paths defined centrally in `web/contracts.js` for frontend imports. Backend declarations in server/contracts.py. API cookies; login shared household password from env. All mutations require same-origin requests / CSRF protection. No public runtime secrets.

- GET /api/session -> {authenticated:boolean, configured:boolean}
- POST /api/login {password:string} -> {ok:true}; HTTP 401 wrong password.
- POST /api/logout -> {ok:true}
- GET /api/state -> {items:Item[], shopping:Shopping[], activity:Activity[], ai:{enabled:boolean,provider:string}, grocy:{connected:boolean}, today:string}
- POST /api/items ItemInput -> {ok:true,item_id:number}
- PATCH /api/items/{id} ItemInput (partial) -> {ok:true}
- DELETE /api/items/{id} -> {ok:true}; only zero stock with no shopping references, otherwise 409. Manual confirmation only; not an AI tool.
- POST /api/actions {type:'purchase'|'consume'|'maintain'|'shopping_add'|'shopping_complete',item_id:number,amount?:number,shopping_id?:number,best_before?:string|null,opened?:boolean} -> {ok:true,message:string}. shopping_complete requires shopping_id + matching item_id and actual purchased amount. purchase/shopping_complete may set the new batch's best_before/opened. Each new user intent sends a UUID Idempotency-Key; retries reuse that key.
- POST /api/chat {message:string,history?:[{role:'user'|'assistant',content:string}]} -> {reply:string,proposal?:{id:string,summary:string,actions:object[]}|null}
- POST /api/proposals/{id}/apply {} -> {ok:true,message:string}; idempotent/reject stale; do not run model again.
- GET /api/export -> application/json household data export (auth required)
- GET /health -> {ok:boolean,grocy:boolean}

Error body: {detail:string}. Grocy failure must be reported, never silently shown as empty stock. AI missing key should be explicit, manual flows continue.

## Data

Item = {id:number,name:string,category:'food'|'supplies'|'maintenance'|'tools',quantity:number,unit:string,location:string,min_quantity:number,best_before:string|null,opened:boolean,model:string,description:string,interval_days:number|null,last_maintained:string|null,next_due:string|null}. location is the product's primary storage location. best_before/opened refer to the nearest-expiry remaining batch, not all batches.
ItemInput fields same except id,next_due optional; name,category required for create; defaults quantity=0,unit='個',location='未分類',min_quantity=0.
Shopping = {id:number,item_id:number,name:string,amount:number,unit:string}
Activity = {id:string,at:string,message:string}
Dates YYYY-MM-DD in Asia/Taipei. Quantities finite >=0, action amounts >0; reject overconsumption.

## Mapping

Use Grocy products for all categories, product groups for categories, quantity units and locations, stock entries for counts and expiry/opening. Product userfields for category/model/interval_days/last_maintained as needed; map every product including zero-stock to an Item. Equipment/tools are stock=1 plus metadata. Scheduled maintenance uses product metadata dates; store back to Grocy. Schema/bootstrap initialized idempotently. No fake entries in normal production. Demo fixture seeded explicitly by script only and clearly labeled.

## Scope

Real Gemini via google-genai SDK (GEMINI_API_KEY comes from the backend environment; never print/copy into tracked files); model configurable GEMINI_MODEL, default verified working Flash model. Chat grounds on fresh state and date; tools generate validated preview proposals, read-only answers must not mutate. Unknown quantities require clarification. Recipe suggestion never consumes stock. Proposal expiry/stale snapshot prevent applying to outdated quantities. UI is Chinese, responsive 390px/desktop, no remote CDN. Backend user password and secret generated into ignored .env by setup. Docker compose optional standard deployment.

Proposal summary is generated from validated actions and the state snapshot on the server; model-written summary is never the authoritative confirmation. The web confirmation also renders actual names, amounts, units and fields from actions.
