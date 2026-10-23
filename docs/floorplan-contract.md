# Floorplan feature contract (v0.2)

The inventory source of truth remains Grocy. Layout geometry and the private uploaded image belong to the existing runtime SQLite DB and /app/data volume. No home image or actual household layout is committed to Git, bundled in web assets, or included in AI model context. User's attached plan is imported locally after implementation.

## Data

Point = {x:number,y:number}, normalized finite coordinates 0..1.
Room = {id:string,name:string,color:string,points:Point[]} (3..40 points). User can rename, move the whole polygon and drag vertices; new rooms start as editable rectangles. Regions have no fabricated inventory or cabinet locations.
Spot = {id:string,location_id:positive int,room_id:string|null,x:number,y:number}; each Grocy location may be pinned once. Removing a spot only unpins it; it never deletes Grocy locations or inventory. Removing a room retains its spots with room_id=null.
Plan = {revision:int,title:string,canvas:{width:int,height:int},rooms:Room[],spots:Spot[],image_url:string|null,locations:[{id:int,name:string}]}. Canvas 200..8000 per side; <=16M pixels.
SavePlan = {revision:int,title:string,canvas:{width:int,height:int},rooms:Room[],spots:Spot[],image_data_url?:string|null}. Omitted image_data_url retains image, null explicitly clears the background, data URL imports PNG/JPEG/WebP. Client cannot supply an image filename or arbitrary remote URL.

## API

Central constants in server/contracts.py and web/contracts.js:
- GET /api/floorplan -> Plan, authenticated, no-store. Empty default revision=0, canvas=1000x800, rooms=[],spots=[],image_url=null; locations always read live from Grocy.
- PUT /api/floorplan SavePlan -> Plan. Optimistic revision conflict=409 (never overwrite another device); all location_ids must exist; spots reference existing rooms or null; duplicate IDs and duplicate location pins rejected. Limit max60 rooms/max300 spots. Same-origin JSON protection applies; this route permits a bounded image-bearing body <=16MiB. Image <=10MiB, valid raster only, decoded safely, auth-only serving at image route. Fresh image updates canvas to actual dimensions with normalized geometry preserved.
- GET /api/floorplan/image -> current private raster, auth/no-store; 404 if absent.
- POST /api/locations {name:string} -> {id:int,name:string}, create Grocy location or return exact existing name, auth/same-origin.
- PATCH /api/locations/{location_id} {name:string} -> {id:int,name:string}; rename canonical Grocy location (duplicate name=409), no inventory duplication. UI labels derive from live location name, not saved copies.

Add location_id to normalized Item responses. Existing item PATCH location:string remains valid; map assignment uses the canonical location name resolved from ID and existing authenticated item PATCH. No location or inventory deletion from map.

## Frontend

Add fifth tab `家的地圖`; on 390px retain accessible bottom navigation (short label `地圖` if needed). Map is SVG overlay + optional private background. Default uploaded image available as faint reference; toggle original plan vs clean editable regions. View: tap room to see its spots/items, tap spot to list inventory there. Search item/location, select result to highlight exact pin, unmatched location clearly shown as not pinned. Selected spot permits moving an existing item there and adding a new item with location prefilled.

Edit: explicit mode, drag rooms/pins and room vertices using pointer events (mouse/touch), add/rename/remove rooms, add a pin for an existing or newly-created Grocy location, rename canonical storage location, remove pin, replace/clear background. Pan/zoom/fit and auto-reveal search result. Save/cancel, dirty indicator, revision conflicts preserve draft. Do not silently discard edits on tab/focus refresh; auth/logout must clear private state.

Inventory location input offers actual location options. Inventory card `看位置` opens map and highlights its pin, or explains unpinned status. Existing controls, chat and manual actions remain usable.

## Verification

Meaningful tests for auth/CSRF, image bounds/type/auth, geometry validation, revision conflict, canonical location rename and no inventory deletion, and data surviving restart. Independent browser checks at 390px and desktop: import plan, select room/spot, move/resize polygon, pin location, save/reload, search test item, assign inventory, correct Grocy read-back. Test only explicitly-created QA items and shapes; preserve user's imported home layout. Never publish screenshots of the private floorplan; use synthetic room geometry for public evidence.
