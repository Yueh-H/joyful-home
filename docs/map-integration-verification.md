# Independent map integration verification

Status: PASS after the final native logout correction recheck in the Firestore deployment goal. This document contains synthetic test evidence and aggregate UI observations only. Private screenshots remain under ignored `.runtime/`.

## Scope and safety

- Verify the vector Studio replaces the legacy map at the exact original `http://127.0.0.1:8765/#map`, keeping original navigation and native inventory, new-item, shopping and chat entry points.
- Inspect same-origin proxy allowlists, authenticated assets/API, CSRF/content type/body limits, generic errors, controlled upstream headers, redirect denial, CSP and frame guards.
- Verify desktop 1280px and mobile 390px, geometry-only edit, drag/resize, unsaved draft retention on focus and parent state refresh, dirty tab confirmation, cancellation, inventory location focus and logout cleanup.
- No real layout PUT, inventory mutation, location mutation, shopping mutation or chat submission. Use mock transports for server writes and browser-only synthetic GET fixtures where needed.
- Local `.env` credentials may be read into process memory solely for original household login; never log them or persist them in artifacts.

## Evidence

Independent proxy checks: `tests/test_studio_proxy_independent.py` — 38 passed in 0.63s. Combined proxy producer and independent suites: 83 passed in 0.98s. One existing Starlette/httpx deprecation warning. The first independent run accepted only 404/422 for an unknown PUT suffix; observed 405 is also correct refusal, so the test expectation was corrected. No producer fix was required.

Covered: unauthenticated static/API/read/write rejection; fixed static and dynamic route validation; query URL isolation; controlled upstream Origin/cookie/idempotency headers; Authorization/extra cookie/header suppression; response cookie/CSP/location/private-header suppression; redirect/5xx/wrong MIME generic 502; CSRF/JSON/declared and streamed byte limits; layout-specific 2MiB limit without widening the native API cap; replayed cookie rejection after logout; invalid upstream origin configuration; native empty-body delete versus Studio explicit-confirmation delete routing. Synthetic transport only; no private household writes.

Producer tests are not substituted for independent checks. A blocked or unperformed browser check stays explicitly listed below.

## Final status

Proxy, browser entry points, layout/draft/navigation/focus checks passed. The initially observed child logout cleanup race was corrected and independently rechecked in the real production browser; see the final follow-up below.

## Native browser evidence (2026-10-23)

One Ego TaskSpace, ID 155, one page reused. Desktop 1280×900; mobile 390×844 with Chromium touch emulation and CDP touch input. The space was finished with no retained pages. Screenshots were visually inspected and remain ignored at `.runtime/integration-desktop.png`, `.runtime/integration-mobile-browse.png`, and `.runtime/integration-mobile-properties.png`.

| Check | Result |
| --- | --- |
| Exact original map entry | PASS. `http://127.0.0.1:8765/#map` stayed unchanged while one same-origin child displayed 23 SVG furniture objects. All five original navigation entries remained. |
| Original feature entry points | PASS. Inventory opened; original new-item dialog displayed its 12 fields and was closed without submission. Shopping and chat compose interfaces opened. No chat message or inventory form was submitted. |
| Desktop layout | PASS. No parent/child horizontal overflow. Inner left toolbox is a drawer; the map had 706 CSS px available beside its inspector in the 1006px iframe. Screenshot visually verified. |
| Geometry-only edit | PASS. Only name, kind, room, width, height, rotation, color and notes fields; inventory/binding controls absent. Four handles measured 10px visual and 28px hit regions. |
| Desktop drag and resize | PASS. Real mouse drag changed draft transform and mouse resize changed draft dimensions. The first resize detector compared transform only; field read-back confirmed dimensions changed. |
| Parent refresh and focus | PASS. Explicit parent refresh and window focus retained identical iframe/child identity and the complete draft SVG markup. |
| Dirty navigation | PASS. Clicking native inventory while dirty raised the native confirmation. Dismissing retained map and draft. Cancelling inside Studio and discarding restored browse, then native inventory navigation worked. |
| Mobile fit and menus | PASS. No horizontal overflow in either document. The child frame ended at y=766; original navigation started at y=772 and ended at y=844, leaving menus unobstructed. |
| Mobile touch and properties | PASS. CDP touch tap selected without opening the inspector. Touch drag changed draft transform. Touch resize on a verified handle changed SVG geometry; an earlier trial on another handle did not change geometry, then an element-hit-verified corner succeeded. Explicit properties opened the sheet with the same eight fields; panel bottom y=766 stayed above native navigation. |
| Inventory 看位置 bridge | PASS with browser-only synthetic GET fixtures. A fake item/location and a temporary first-object binding were returned only in browser responses; inventory 看位置 created a new iframe, queued until ready, selected furniture and displayed the synthetic item. No binding or item was written. |
| Fixture removal and source preservation | PASS. Real native GET bypassing fixtures equalled the initial complete layout JSON, inventory items and shopping rows. Revision remained 9, items remained 0. Instrumentation observed zero attempted application mutations. Fixtures were removed by restoring fetch and full reload; both parent and newly loaded child confirmed fixture markers absent. |
| Logout access control | PASS. Parent returned to login, iframe was removed, and native state, Studio layout and child asset requests each returned 401. |
| Logout child memory/DOM | **INITIAL BLOCKING FINDING, RESOLVED IN FINAL RECHECK**. A parent-held reference to the old child window still exposed all 23 furniture nodes after logout. The adapter posts the expiry message asynchronously then immediately removes the iframe, so the child can be detached before its cleanup handler runs. Sent for correction; source and final native-browser reruns below prove resolution. |

The browser script attempted to remove its temporary new-document injection on a later CDP session and received `Script not found`; restoring both fetch wrappers and full reload verified fixture absence. No server write occurred. One subtree snapshot reused a stale ref; its aggregate evaluation had succeeded, and subsequent interactions used current CSS selectors.

## Limits and follow-up

- The deployed logout child cleanup race was fixed and independently rechecked; see the final follow-up below.
- No real layout save or inventory/location/shopping mutation was performed. This run does not claim persistence-write E2E coverage.
- Current real inventory was empty; populated focus used explicitly synthetic browser GET responses.
- Touch input used emulation, not a physical phone.
- Ego Lite reported an available update; no upgrade was performed.

### Logout race correction: independent source-level rerun

Root changed the parent reset to synchronously dispatch the existing expired event into the same-origin child before posting its compatibility message and detaching. Studio handles that event by running the complete `clearPrivate`, including layout/base, items, locations, history, selection, blob URL, dialog and toast state.

Independent execution used the actual adapter and complete actual child app source in separate Node VM contexts with synthetic DOM nodes and private-state sentinels. `.runtime/check-sync-logout.mjs` passed: observed order was `child-clean → post-message → detach`; all private collections/layout/base/blob URL were cleared; child login rendered; dialog and toast were emptied. Thus the source-level race finding is corrected. Native browser recheck of the correction was deferred to the subsequent Firestore QA goal; the old TaskSpace had already been finished before this fix. Do not describe that source-level test as a browser rerun.


### Final native logout recheck after Firestore deployment

PASS. Firestore goal TaskSpace 157 used the exact LAN 8765/#map entry and finished with no retained pages. After parent logout, the old child window reference had zero furniture nodes, a login form and an empty dialog. Parent had zero iframes and showed login; private state/layout/child assets all returned 401. Production full layout, items, shopping and canonical locations remained identical to the initial baseline; no application data mutation was attempted. Thus the earlier blocking race is resolved with actual native-browser evidence. See `firestore-independent-verification.md` for the deployment and isolated cloud persistence evidence.
