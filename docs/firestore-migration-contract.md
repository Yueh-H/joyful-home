# Firestore primary data contract

User explicitly chose Firestore as the formal database for vector map, items, locations and shopping (not a backup). New isolated project `joyful-home-20261023`, Standard `(default)` database, `asia-east1`. Keep the existing local WiFi URL, parent navigation/session/CSRF/AI previews. No public site deployment, billing linkage, unrelated projects or public read/write rules.

## Storage and boundary

- All formal household data under `households/home/`: `items/{positive numeric id}`, `locations/{positive numeric id}`, `shopping/{positive numeric id}`, `maps/layout`, and `meta/counters`. Private original reference image may remain a legacy local read-only reference; canonical vector geometry is Firestore. Existing sessions, rate limits, AI proposal/idempotency bookkeeping may remain local operational data.
- Items retain the existing API fields. Numeric IDs stay stable. Use Firestore transactions for quantity changes, consume guards, shopping completion (stock + remove shopping together), and counter allocation. Per-item stock batches may be retained where needed; no silent inventory duplication/fallback to Grocy when cloud is unavailable.
- `INVENTORY_BACKEND=firestore`, `FIREBASE_PROJECT_ID=joyful-home-20261023`, `FIRESTORE_DATABASE_ID=(default)`, `FIRESTORE_HOUSEHOLD_ID=home`. Backend-only least-privilege credential via `GOOGLE_APPLICATION_CREDENTIALS`; never browser credentials/private keys.
- Firestore security rules deny all browser read/write. Existing authenticated app backend accesses via project-scoped service account (datastore.user). No anonymous/public Firebase access.
- Original app gets FirestoreInventory with the existing adapter surface so frontend API stays stable. Existing tests can still inject Grocy mock; legacy Grocy optional mode is not active in this deployment.
- Original DELETE `/api/items/{id}` with explicit `{confirm_name,expected_location_id}` in Firestore mode validates/deletes directly using the Firestore adapter (no Studio forwarding recursion). Empty `{}` retains existing zero-stock/no-shopping guard. Guarded full removal cleans linked shopping atomically.
- Studio Firestore Store reads/saves the SAME `households/home/maps/layout` with revision compare-and-increment transaction; no SQLite fallback in Firestore mode. Studio inventory DELETE forwards explicit confirmation to original app; original handles it directly. Studio clear validates stored layout + latest contents before those guarded deletes.

## Migration and verification

Root takes private local backups, imports current real items/locations/shopping/current vector layout once with stable IDs, compares read-back before switching runtime. Never overwrite an existing cloud dataset or delete old local volumes. Import script must refuse nonempty destination unless an explicit migration marker matches a verified resumable import.

Producer inventory agent owns original `server/firestore_inventory.py` (new), `server/app.py` minimal adapter selection/guarded delete, `pyproject.toml`/`uv.lock`, and `tests/test_firestore_inventory.py` (new). Studio agent owns `server.py`, `pyproject.toml`/`uv.lock`, `compose.yaml`, `scripts/start.py` and its own new tests. Root owns original compose, private credentials/project/IAM/rules, migration scripts/docs/deployment. Fresh independent verifier must test actual new database writes/read-back/restart while protecting user records; synthetic isolated QA namespace only with explicit cleanup of its own data.
