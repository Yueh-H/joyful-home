# Independent Firestore primary-database verification

Date: 2026-10-23 (Asia/Taipei). Status: PASS. Isolated real persistence, restart, production browser and corrected logout cleanup all verified.

## Safety and scope

Independent tests use synthetic documents and temporary session databases. Live writes used only this verifier's unique `qaverify_*` household namespaces in `joyful-home-20261023/(default)`. Neither live script read nor wrote `households/home`. The existing backend-only credential was consumed through the SDK; credential content is not logged or copied into reports. No new cloud project, credential, billing configuration, Docker deployment or public rule was created by this verifier.

## Automated independent evidence

- Original `tests/test_firestore_independent.py`: 10 passed. Strict fake transactions reject reads after writes. Covers concurrent stock consumption (one success/one 409), stock+shopping rollback and replay, stable IDs across adapter restart, household isolation, counter collision, stale quantity patch, confirmation guards, atomic linked-shopping deletion, cloud failure returning 503, migration ID preservation, and native locations/delete routing without Studio recursion.
- Original `tests/test_firestore_migration_independent.py`: 12 passed. Existing items/locations/shopping/maps/meta without a matching migration marker remain completely untouched; invalid map schema creates no marker; prepared matching migration resumes; matching rerun preserves all data; different digest is refused; invalid household names cannot create misplaced markers. Migration ordering tests use an explicit independent validation stub so original-repo CI needs no sibling checkout; a separate test executes the real helper against a missing companion and proves refusal before writes. Actual geometry validation was exercised by the migration and Studio suite.
- Studio `tests/test_firestore_independent.py`: 12 passed. Covers the shared household document, revision compare/increment and two-writer conflict, failed commit preserving existing map, missing/corrupt/unavailable cloud map returning 503 without fallback/SQLite creation, household validation, and authentication preceding private cloud reads.

Independent review initially found migration created a marker before checking whether child collections were nonempty. Root corrected it to check first and validate map/household before writing. The regression cases above cover the correction.

## Actual new Firestore round trip

Verifier started original and Studio from current source on isolated native ports 18765/18780 with a unique QA household and synthetic login/session secret. Both services stopped after the test. `.runtime/verify_firestore_live.py` and `.runtime/qaverify_be432ff8ee03/result.json` retain the reproducible private run artifacts.

| Check | Result |
| --- | --- |
| Authoritative backend | PASS. Native state explicitly reported `inventory.backend=firestore`; isolated household started empty. |
| Item/location write | PASS. Native item creation and canonical location query matched direct Firestore document read-back. |
| Shopping completion | PASS. Native shopping completion increased stock once and removed the linked shopping document. Repeating the same HTTP idempotency key kept the same stock. |
| Shared vector geometry | PASS. Parent same-origin proxy PUT wrote the same `maps/layout` document; revision incremented from 0 to 1. A stale revision was rejected with 409. |
| Restart durability | PASS. Both native processes were terminated and restarted. Stable item ID, quantity and complete map JSON matched the cloud state. |
| Guarded Studio deletion | PASS. Wrong confirmation returned 409 without changing the document. Studio's valid confirmed deletion forwarded to original Firestore handling and removed both item and its linked shopping data without recursion. |
| Cleanup | PASS. Own remaining three documents and root were deleted; all five own collections were empty and own root absent. No `home` namespace access. |

The first script attempt read `id` instead of the API's `item_id`, causing a verifier KeyError after item creation. Both services stopped. Initial cleanup encountered a transient collection-query PermissionDenied; a fresh SDK process then deleted all four own documents plus root from `qaverify_38e46ecc5e71`. The corrected full run passed. gRPC emitted fork file-descriptor notices during subprocess start; no persistence or request failure occurred in the successful run.

## Production browser evidence

A separate single Ego TaskSpace for this Firestore goal, ID 157, reused one page and finished with no retained pages. Tested exact LAN entry `http://192.168.0.232:8765/#map`, desktop 1280×900 and mobile 390×844.

- PASS: production API reported `inventory.backend=firestore`, revision 9, 10 locations, 0 items; same-origin child displayed 23 SVG furniture objects and all five native menus remained.
- PASS: reference image loaded as an allowed child blob URL. Geometry-only editor retained its eight fields. A draft width change survived parent refresh in the identical iframe/child; a fresh cloud-backed layout GET remained unchanged. Cancel/discard restored browse.
- PASS: native new-item dialog opened with all 10 canonical location choices and was closed without submission. Shopping and chat compose opened.
- PASS: no horizontal overflow in parent or child on desktop/mobile. Mobile iframe ended at y=766 and native navigation began at y=772; screenshot visually inspected and retained only at ignored `.runtime/firestore-production-mobile.png`.
- PASS: full layout JSON, items, shopping and locations were identical to the initial production baseline; instrumentation observed zero application mutation attempts.
- PASS: after native parent logout, the retained old child window contained zero furniture nodes, its login form, and an empty dialog. Parent showed login and zero iframes; state, layout and child asset reads each returned 401. This is an actual native browser rerun of the earlier logout race correction.

Ego Lite reported an available update; no upgrade was performed.

## Limits

Synthetic transactions verify failure rollback logic, while live SDK tests verify successful persistence and real process restart. This does not claim a forced network-failure test during an actual committed cloud transaction or physical-phone testing. Formal user data received no test mutation.
