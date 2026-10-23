# Local household chat

Canonical storage is `joyful_home.chat_turns`, separate from the `households` aggregate. `chat_sequences` allocates a journaled, atomic per-household order across workers/restarts. No TTL or history truncation. Household authentication and exact-origin JSON write guards apply. API responses are no-store.

## API contract

- `POST /api/chat`: `{request_id?:string, message?:string, images?:[{mime_type,data}], history?:[...]}`. `request_id` is 16–100 ASCII letters/digits/underscore/hyphen. At least text or an image is required. Legacy text requests remain supported. `history` is ignored in Mongo mode; server text context is the latest eight complete exchanges / 16 messages.
- Images: at most four. `data` is strict base64, never a URL/data-URL. Allowed MIME types: PNG/JPEG/WebP, verified against Pillow's detected format and successful full decoding. Each decoded file and canonical re-encoding must fit 2 MiB; longest edge ≤4096 and total pixels ≤12 million. Animated images are rejected. EXIF/ICC/text metadata is cleared before persistence and provider use. POST body cap is 12 MiB, checked against both declared and actual streamed bytes.
- The client accepts source rasters up to 20 MiB each, normalizes orientation, downsizes to edge 2048 and re-encodes JPEG locally before preview/send. Only prepared bytes are sent. Clipboard text keeps its native paste path; mixed text/image clipboards add images without preventing text insertion.
- User text, normalized image binary and request fingerprints are inserted atomically into one turn document, before AI. A maximum of 8 MiB image bytes per turn keeps the document below MongoDB's 16 MiB document cap. There is no separate upload stage or orphan-image collection. Same ID and same text/image fingerprints returns the existing result without a provider call. Changed text or image bytes conflicts with 409. Uncertain/failed writes never report completion.
- `GET /api/chat?before=<order>&limit=50`: oldest-to-newest page of turns; `before` points to the next older page. Images are metadata and authenticated URLs only, never base64/Binary in history JSON. Pending and failed turns keep readable attachments.
- `GET /api/chat/{request_id}/images/{0..3}`: authenticated, scoped to this configured household, no-store/nosniff, CORP same-origin. Cross-origin Origin or non-same-origin Sec-Fetch-Site requests are rejected. Only server-canonical raster bytes are returned.
- Historical proposal metadata is read-only. SQLite remains proposal status/expiry/session authority; applying a current preview requires explicit user confirmation and fresh inventory validation.

## Context and provider

`MongoInventory.chat_state()` reads one fresh `households` document and projects inventory plus `home_context`: shared version/revision, canvas/coordinate explanation, **all rooms** including empty rooms, all furniture IDs/names/room bindings/coordinates/dimensions/rotation, every storage compartment and binding, and all inventory location IDs/names with item ID membership (including empty/unbound locations). The current inventory items supply names, quantities and units. Missing map data is labelled unavailable/unknown. No GLB, private floorplan photo or map asset is loaded for the provider. This projection is derived, never independently saved.

The configured Gemini key/model stay unchanged. User JSON, names, notes and images are explicitly data rather than instructions; image content never bypasses action confirmation. Current images are prioritized, then images from the same latest eight completed exchanges, until **four images / 8 MiB maximum**. Each image part has a source label (current or previous turn order). Older images remain in local history but are not silently claimed as seen. The text and image bounds apply independently; there is no unbounded base64 history replay.

SDK construction uses `google.genai.types.Part.from_bytes(data=..., mime_type=...)` with typed text parts in the existing `models.generate_content` path, verified against the [official Google Python SDK documentation](https://googleapis.github.io/python-genai/) and the installed SDK signature. No model migration or Files API upload is introduced.

## Backup and testing

Back up the whole MongoDB volume, or both `chat_turns` (including binary images) and `chat_sequences`. Inventory/layout JSON export does not include chat. Existing app-data is still needed for login and proposal state. Never delete or replace production volumes to deploy this feature.

`uv run pytest -q tests/test_chat_images_context.py` uses an isolated real local MongoDB, synthetic raster bytes and fake providers/SDK responses. No real household photo or paid provider request is used. Browser acceptance also uses an isolated fake provider. Messages that existed only in pre-persistence browser memory and disappeared cannot be recovered.
