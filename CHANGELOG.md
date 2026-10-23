# Changelog

## Unreleased

Add Firestore as the explicitly selected authoritative store for inventory, stock batches, locations, shopping and vector layout. Quantity changes and shopping completion use transactions; layout revisions reject stale saves. One-time migration verifies read-back and refuses a nonempty destination without its matching import marker. Credentials stay backend-only and browser database rules deny access.

Replace the legacy `/#map` editor with the vector furniture map while retaining the household navigation, inventory, shopping and chat. A fixed same-origin bridge reuses the existing Studio layout and Grocy inventory; private data is not copied. Parent refresh preserves the map draft, and navigation protects unsaved edits.

## 0.2.0 — 2026-10-23

Add a private editable household map: room polygons, draggable storage markers linked to Grocy locations, inventory search and position lookup, and authenticated background images. Map saves use revision checks to preserve concurrent drafts. Private floorplans remain runtime data and are excluded from AI requests and public source.

See `docs/floorplan-verification.md` for verified behavior and remaining limits.

## 0.1.0 — 2026-10-23

Initial implementation of 愉悅的家庭管家: mobile-first household inventory, food expiry, shared shopping, maintenance dates, and natural-language interaction backed by Grocy.

See `docs/verification.md` for the actual verification scope and remaining limits.
