"""Capture a private snapshot and import once into an empty Firestore household."""
import argparse
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys

import httpx
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from server.firestore_inventory import FirestoreInventory


def ordered(rows):
    return sorted(rows, key=lambda row: row["id"])


def capture(source):
    origin = source.rstrip("/")
    settings = dotenv_values(ROOT / ".env")
    with httpx.Client(base_url=origin, headers={"Origin": origin}, timeout=30) as client:
        client.post("/api/login", json={"password": settings["HOME_PASSWORD"]}).raise_for_status()
        try:
            responses = [client.get(path) for path in ("/api/state", "/api/locations", "/api/studio/layout")]
            for response in responses:
                response.raise_for_status()
            state, locations, layout = [response.json() for response in responses]
            return {"state": {key: state[key] for key in ("items", "shopping")},
                    "locations": locations["locations"], "layout": layout}
        finally:
            client.post("/api/logout", json={})


def validate_layout(layout):
    # Reuse the map service's real geometry contract before writing cloud data.
    schema_path = ROOT.parent / "joyful-home-map" / "server.py"
    if not schema_path.is_file():
        raise RuntimeError("Install the companion joyful-home-map folder before migrating.")
    spec = importlib.util.spec_from_file_location("_joyful_studio_schema", schema_path)
    schema = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = schema
    spec.loader.exec_module(schema)
    schema.Layout.model_validate(layout)


def migrate(snapshot, client, household="home"):
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", household):
        raise ValueError("Invalid household ID.")
    validate_layout(snapshot["layout"])
    digest = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    root = client.document(f"households/{household}")
    marker = root.get()
    if marker.exists:
        if (marker.to_dict().get("migration") or {}).get("sha256") != digest:
            raise RuntimeError("Destination has different data; import refused.")
    else:
        for kind in ("items", "locations", "shopping", "maps", "meta"):
            if list(root.collection(kind).limit(1).stream()):
                raise RuntimeError("Destination is nonempty without a matching migration marker; import refused.")
        root.create({"migration": {"sha256": digest, "phase": "prepared"}})
    inventory = FirestoreInventory(client=client, household_id=household)
    counter = root.collection("meta").document("counters").get()
    if not counter.exists:
        inventory.import_snapshot(snapshot["state"], snapshot["locations"])
    actual = inventory.state()
    for key in ("items", "shopping"):
        if ordered(actual[key]) != ordered(snapshot["state"][key]):
            raise RuntimeError("Inventory read-back differs; destination left untouched for inspection.")
    if ordered(inventory.locations()) != ordered(snapshot["locations"]):
        raise RuntimeError("Location read-back differs; destination left untouched for inspection.")
    layout = root.collection("maps").document("layout")
    current = layout.get()
    if not current.exists:
        layout.create(snapshot["layout"])
    if layout.get().to_dict() != snapshot["layout"]:
        raise RuntimeError("Map read-back differs; existing map was not overwritten.")
    root.update({"migration.phase": "complete"})
    return {"items": len(actual["items"]), "shopping": len(actual["shopping"]),
            "locations": len(snapshot["locations"]), "revision": snapshot["layout"]["revision"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="http://127.0.0.1:8765")
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--project", required=True)
    parser.add_argument("--household", default="home")
    parser.add_argument("--credentials", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.snapshot:
        snapshot = json.loads(args.snapshot.read_text())
    else:
        snapshot = capture(args.source)
        runtime = ROOT / ".runtime"
        runtime.mkdir(mode=0o700, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        destination = runtime / f"firebase-migration-{stamp}.json"
        with open(destination, "x", opener=lambda path, flags: os.open(path, flags, 0o600)) as handle:
            json.dump(snapshot, handle, ensure_ascii=False)
        print("Private snapshot:", destination)
    if not args.apply:
        print("Snapshot captured; no cloud writes. Pass --snapshot and --apply to import.")
        return
    os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(args.credentials.resolve())
    from google.cloud import firestore
    client = firestore.Client(project=args.project, database="(default)")
    print("Verified import:", migrate(snapshot, client, args.household))


if __name__ == "__main__":
    main()
